"""Full-text search over the catalogue.

FTS5 does the work. Two details make it usable for a mixed-script catalogue:

* every row also stores a transliterated copy of its text in the ``alt``
  column, and Cyrillic queries are transliterated before matching, so
  ``кино``/``kino`` find each other in both directions;
* user input is never passed to FTS5 as a query expression. It is tokenised
  and re-quoted, because a stray ``"`` or ``NEAR`` would otherwise raise
  ``sqlite3.OperationalError`` in the user's face.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from . import metadata
from .db import Database

#: bm25 column weights, in declaration order:
#: title, artist, album, tags, note, alt.
WEIGHTS = (8.0, 6.0, 1.5, 3.0, 1.0, 4.0)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def index_track(
    db: Database,
    track_id: int,
    title: str,
    artist: str,
    album: str = "",
    tags: Sequence[str] = (),
    note: str = "",
) -> None:
    """Insert or replace one row in the search index."""
    tag_text = " ".join(tags)
    original = " ".join([title, artist, album, tag_text])
    alt = metadata.translit(original)
    if metadata.fold(alt) == metadata.fold(original):
        alt = ""
    with db.transaction() as conn:
        conn.execute("DELETE FROM search WHERE rowid=?", (track_id,))
        conn.execute(
            "INSERT INTO search(rowid, title, artist, album, tags, note, alt) "
            "VALUES(?,?,?,?,?,?,?)",
            (track_id, title, artist, album or "", tag_text, note or "", alt),
        )


def remove_track(db: Database, track_id: int) -> None:
    db.execute("DELETE FROM search WHERE rowid=?", (track_id,))


def rebuild(db: Database) -> int:
    """Re-index every approved track. Used by ``minimalma doctor --reindex``."""
    rows = db.query(
        "SELECT t.id, t.title, a.name AS artist, t.album, t.note "
        "FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "WHERE t.status='approved'"
    )
    db.execute("DELETE FROM search")
    count = 0
    for row in rows:
        tags = [
            r["name"]
            for r in db.query(
                "SELECT g.name FROM track_tags tt JOIN tags g ON g.id = tt.tag_id "
                "WHERE tt.track_id=?",
                (row["id"],),
            )
        ]
        index_track(
            db, row["id"], row["title"], row["artist"], row["album"] or "", tags, row["note"] or ""
        )
        count += 1
    return count


def _expression(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Tokens are ANDed; the final token gets a prefix wildcard so that typing
    continues to narrow results as the user types.
    """
    tokens = _TOKEN.findall(text)[:12]
    if not tokens:
        return ""
    parts: list[str] = []
    for index, token in enumerate(tokens):
        quoted = '"' + token.replace('"', '""') + '"'
        if index == len(tokens) - 1 and len(token) >= 2:
            quoted += " OR " + quoted + "*"
            parts.append("(" + quoted + ")")
        else:
            parts.append(quoted)
    return " AND ".join(parts)


def search(db: Database, text: str, limit: int = 20, offset: int = 0) -> list[tuple[int, float]]:
    """Return ``(track_id, relevance)`` best first. Relevance is higher-is-better."""
    text = metadata.clean_text(text)
    if not text:
        return []
    expressions = [_expression(text)]
    if metadata.has_cyrillic(text):
        alternative = _expression(metadata.translit(text))
        if alternative and alternative not in expressions:
            expressions.append(alternative)

    seen: dict = {}
    for expression in expressions:
        if not expression:
            continue
        try:
            rows = db.query(
                "SELECT rowid AS id, bm25(search, ?, ?, ?, ?, ?, ?) AS rank "
                "FROM search WHERE search MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                (*WEIGHTS, expression, limit + offset, 0),
            )
        except sqlite3.OperationalError:
            # A tokenizer or syntax surprise must never surface to the user.
            rows = []
        for row in rows:
            # bm25() is negative and smaller is better; flip it once here.
            score = -float(row["rank"])
            if row["id"] not in seen or score > seen[row["id"]]:
                seen[row["id"]] = score

    ranked = sorted(seen.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        ranked = _fuzzy(db, text, limit + offset)
    return ranked[offset : offset + limit]


def _fuzzy(db: Database, text: str, limit: int) -> list[tuple[int, float]]:
    """Last-resort similarity scan for typos and partial words.

    Bounded to the newest 2000 approved tracks: beyond that the linear scan
    stops being worth the latency, and by then FTS5 will have matched anyway.
    """
    rows = db.query(
        "SELECT t.id, t.title, a.name AS artist FROM tracks t "
        "JOIN artists a ON a.id = t.artist_id "
        "WHERE t.status='approved' ORDER BY t.published_at DESC LIMIT 2000"
    )
    scored: list[tuple[int, float]] = []
    for row in rows:
        best = max(
            metadata.similarity(text, row["title"]),
            metadata.similarity(text, row["artist"]),
            metadata.similarity(text, f"{row['artist']} {row['title']}"),
        )
        if best >= 0.62:
            scored.append((row["id"], best))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def search_artists(db: Database, text: str, limit: int = 10) -> list[int]:
    """Artist ids matching a query, by name and by transliteration."""
    text = metadata.clean_text(text)
    if not text:
        return []
    key = metadata.key_of(text)
    rows = db.query(
        "SELECT DISTINCT a.id, a.name, a.key FROM artists a "
        "JOIN tracks t ON t.artist_id = a.id AND t.status='approved' "
        "WHERE a.key LIKE ? LIMIT 200",
        (f"%{key}%",),
    )
    if rows:
        return [row["id"] for row in rows[:limit]]
    rows = db.query(
        "SELECT DISTINCT a.id, a.name FROM artists a "
        "JOIN tracks t ON t.artist_id = a.id AND t.status='approved' LIMIT 2000"
    )
    scored = [(row["id"], metadata.similarity(text, row["name"])) for row in rows]
    scored = [item for item in scored if item[1] >= 0.62]
    scored.sort(key=lambda item: item[1], reverse=True)
    return [track_id for track_id, _ in scored[:limit]]


def suggest_tags(db: Database, prefix: str = "", limit: int = 24) -> list[str]:
    """Tags in use, most common first — the vocabulary curators actually use."""
    prefix = metadata.normalise_tag(prefix)
    args: Sequence = ()
    clause = ""
    if prefix:
        clause = "WHERE g.name LIKE ?"
        args = (f"{prefix}%",)
    rows = db.query(
        "SELECT g.name, COUNT(*) AS n FROM tags g "
        "JOIN track_tags tt ON tt.tag_id = g.id "
        "JOIN tracks t ON t.id = tt.track_id AND t.status='approved' "
        f"{clause} GROUP BY g.id ORDER BY n DESC, g.name LIMIT ?",
        (*args, limit),
    )
    return [row["name"] for row in rows]


def tracks_by_tag(db: Database, tag: str, limit: int = 50) -> list[int]:
    tag = metadata.normalise_tag(tag)
    if not tag:
        return []
    rows = db.query(
        "SELECT t.id FROM tracks t "
        "JOIN track_tags tt ON tt.track_id = t.id "
        "JOIN tags g ON g.id = tt.tag_id "
        "WHERE g.name = ? AND t.status='approved' "
        "ORDER BY t.published_at DESC LIMIT ?",
        (tag, limit),
    )
    return [row["id"] for row in rows]


def optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
