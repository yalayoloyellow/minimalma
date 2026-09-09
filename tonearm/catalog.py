"""Intake, moderation and the catalogue itself.

Intake is the only place in the service that trusts nothing. A submission
arrives as a Telegram ``Audio`` object plus, when it is small enough to fetch,
the file's own bytes. Four sources of metadata are reconciled here, in
descending order of trust:

1. tags embedded in the file (:mod:`tonearm.metadata`),
2. what ``ffprobe`` says about the stream (:mod:`tonearm.audio`),
3. the ``performer``/``title`` Telegram itself parsed,
4. the filename.

Nothing is published without a human. ``auto_approve`` exists in the config
because operators asked for it; it is off, and the README says plainly why
turning it on makes this a different product.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from . import audio as audio_mod
from . import metadata, search
from .config import Config
from .db import Database, now
from .telegram import Api

log = logging.getLogger("tonearm.catalog")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_HIDDEN = "hidden"

#: Two submissions are the same track when the names are this close and the
#: durations agree to within :data:`DUPLICATE_SECONDS`.
DUPLICATE_RATIO = 0.90
DUPLICATE_SECONDS = 4


@dataclass
class Intake:
    """The outcome of processing one uploaded file."""

    track_id: int | None = None
    release_id: int | None = None
    release_title: str = ""
    track_no: int | None = None
    needs_cover: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    year: int | None = None
    duration: int = 0
    cover_file_id: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    duplicate_of: int | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.track_id is not None and not self.error


# --------------------------------------------------------------------------
# Users and artists
# --------------------------------------------------------------------------


def ensure_user(db: Database, user: dict[str, Any], default_lang: str = "en") -> dict[str, Any]:
    """Upsert the Telegram user record and return it as a plain dict."""
    user_id = int(user["id"])
    name = " ".join(
        part for part in (user.get("first_name"), user.get("last_name")) if part
    ).strip()
    username = user.get("username") or ""
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    stamp = now()
    if row is None:
        lang = (user.get("language_code") or default_lang or "en")[:2].lower()
        if lang not in ("en", "ru"):
            lang = default_lang if default_lang in ("en", "ru") else "en"
        db.execute(
            "INSERT INTO users(id, created_at, last_seen, lang, name, username) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, stamp, stamp, lang, name, username),
        )
        row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    else:
        db.execute(
            "UPDATE users SET last_seen=?, name=?, username=? WHERE id=?",
            (stamp, name, username, user_id),
        )
    return dict(row) if row else {}


def get_user(db: Database, user_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    return dict(row) if row else None


def set_user(db: Database, user_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"lang", "screen_chat", "screen_msg", "state", "state_data", "digest", "banned"}
    columns = {k: v for k, v in fields.items() if k in allowed}
    if not columns:
        return
    assignments = ", ".join(f"{name}=?" for name in columns)
    db.execute(f"UPDATE users SET {assignments} WHERE id=?", (*columns.values(), user_id))


def get_or_create_artist(db: Database, name: str, user_id: int | None = None) -> int:
    """Resolve an artist by folded key, creating the row when new."""
    display = metadata.clean_text(name) or "Unknown"
    key = metadata.key_of(display) or metadata.key_of("unknown")
    row = db.one("SELECT id, user_id FROM artists WHERE key=?", (key,))
    if row is not None:
        if user_id and not row["user_id"]:
            db.execute("UPDATE artists SET user_id=? WHERE id=?", (user_id, row["id"]))
        return int(row["id"])
    cursor = db.execute(
        "INSERT INTO artists(name, key, user_id, created_at) VALUES(?,?,?,?)",
        (display, key, user_id, now()),
    )
    return int(cursor.lastrowid)


def artist_row(db: Database, artist_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM artists WHERE id=?", (artist_id,))
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Releases
# --------------------------------------------------------------------------

KIND_SINGLE = "single"
KIND_EP = "ep"
KIND_ALBUM = "album"

#: Above this many tracks a release stops being an EP.
EP_MAX_TRACKS = 6


def kind_for(count: int, is_single: bool = False) -> str:
    if is_single or count <= 1:
        return KIND_SINGLE
    return KIND_EP if count <= EP_MAX_TRACKS else KIND_ALBUM


def get_or_create_release(
    db: Database,
    artist_id: int,
    title: str,
    year: int | None = None,
    submitted_by: int | None = None,
    is_single: bool = False,
) -> int:
    """Resolve a release by folded title within one artist, creating it if new."""
    display = metadata.clean_title(title) or metadata.clean_text(title) or "Untitled"
    key = metadata.key_of(display) or "untitled"
    row = db.one("SELECT id FROM releases WHERE artist_id=? AND key=?", (artist_id, key))
    if row is not None:
        if year:
            db.execute("UPDATE releases SET year=COALESCE(year, ?) WHERE id=?", (year, row["id"]))
        return int(row["id"])
    cursor = db.execute(
        "INSERT INTO releases(artist_id, title, key, kind, year, status, "
        "submitted_by, submitted_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            artist_id,
            display,
            key,
            KIND_SINGLE if is_single else KIND_EP,
            year,
            STATUS_PENDING,
            submitted_by,
            now(),
        ),
    )
    return int(cursor.lastrowid)


def next_track_number(db: Database, release_id: int) -> int:
    return (
        int(
            db.scalar(
                "SELECT COALESCE(MAX(track_no), 0) FROM tracks WHERE release_id=?",
                (release_id,),
                default=0,
            )
        )
        + 1
    )


def refresh_release_kind(db: Database, release_id: int) -> str:
    """Keep single/EP/album in step with how many tracks the release holds."""
    count = int(
        db.scalar("SELECT COUNT(*) FROM tracks WHERE release_id=?", (release_id,), default=0)
    )
    kind = kind_for(count)
    db.execute("UPDATE releases SET kind=? WHERE id=?", (kind, release_id))
    return kind


def release_cover(db: Database, release_id: int) -> str | None:
    """The artwork for a release: its own, else the first one a track carried."""
    row = db.one("SELECT cover_file_id FROM releases WHERE id=?", (release_id,))
    if row is not None and row["cover_file_id"]:
        return str(row["cover_file_id"])
    fallback = db.scalar(
        "SELECT cover_file_id FROM tracks WHERE release_id=? AND cover_file_id IS NOT NULL "
        "ORDER BY track_no LIMIT 1",
        (release_id,),
    )
    return str(fallback) if fallback else None


def set_release_cover(db: Database, release_id: int, file_id: str) -> None:
    db.execute("UPDATE releases SET cover_file_id=? WHERE id=?", (file_id, release_id))


def release(db: Database, release_id: int, only_approved: bool = False) -> dict[str, Any] | None:
    """A release with its tracks, in track order."""
    row = db.one(
        "SELECT r.*, a.name AS artist FROM releases r JOIN artists a ON a.id = r.artist_id "
        "WHERE r.id=?",
        (release_id,),
    )
    if row is None:
        return None
    item = dict(row)
    clause = "AND t.status='approved'" if only_approved else ""
    item["tracks"] = [
        dict(track_row)
        for track_row in db.query(
            f"SELECT t.*, a.name AS artist FROM tracks t JOIN artists a ON a.id = t.artist_id "
            f"WHERE t.release_id=? {clause} ORDER BY COALESCE(t.track_no, t.id)",
            (release_id,),
        )
    ]
    item["cover_file_id"] = release_cover(db, release_id)
    item["duration"] = sum(int(t["duration"] or 0) for t in item["tracks"])
    item["missing"] = missing_for_release(db, release_id)
    return item


def release_of(db: Database, track_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT release_id FROM tracks WHERE id=?", (track_id,))
    if row is None or not row["release_id"]:
        return None
    return release(db, int(row["release_id"]))


def missing_for_release(db: Database, release_id: int) -> list[str]:
    """What a submission still lacks before a curator should ever see it.

    This is a completeness check on the artist's side, not a judgement. A
    release that fails it never enters the review queue: an incomplete
    submission is a message to its author, not a decision for a curator.
    """
    missing: list[str] = []
    if not db.scalar("SELECT COUNT(*) FROM tracks WHERE release_id=?", (release_id,), default=0):
        missing.append("no_tracks")
    if not release_cover(db, release_id):
        missing.append("no_cover")
    return missing


def is_complete(db: Database, release_id: int) -> bool:
    return not missing_for_release(db, release_id)


def update_release(db: Database, release_id: int, **fields: Any) -> None:
    allowed = {"title", "kind", "year", "note", "cover_file_id"}
    columns = {k: v for k, v in fields.items() if k in allowed}
    if not columns:
        return
    if "title" in columns:
        columns["key"] = metadata.key_of(str(columns["title"])) or "untitled"
    assignments = ", ".join(f"{name}=?" for name in columns)
    db.execute(f"UPDATE releases SET {assignments} WHERE id=?", (*columns.values(), release_id))


def pending_releases(db: Database, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """The review queue: complete submissions only, oldest first.

    Completeness is enforced here rather than at approval time, so a curator
    never opens something they cannot decide.
    """
    rows = db.query(
        "SELECT r.*, a.name AS artist, "
        "  (SELECT COUNT(*) FROM tracks t WHERE t.release_id=r.id AND t.status='pending') AS waiting, "
        "  (SELECT COUNT(*) FROM tracks t WHERE t.release_id=r.id) AS total, "
        "  (SELECT MIN(t.submitted_at) FROM tracks t WHERE t.release_id=r.id) AS first_at "
        "FROM releases r JOIN artists a ON a.id = r.artist_id "
        "WHERE r.status=? AND EXISTS (SELECT 1 FROM tracks t WHERE t.release_id=r.id "
        "                             AND t.status='pending') "
        "ORDER BY first_at ASC LIMIT ? OFFSET ?",
        (STATUS_PENDING, limit * 4, offset),
    )
    out = []
    for row in rows:
        if not is_complete(db, int(row["id"])):
            continue
        item = dict(row)
        item["cover_file_id"] = release_cover(db, int(row["id"]))
        item["tags"] = release_tags(db, int(row["id"]))
        out.append(item)
        if len(out) >= limit:
            break
    return out


def pending_releases_total(db: Database) -> int:
    rows = db.query(
        "SELECT r.id FROM releases r WHERE r.status=? AND EXISTS "
        "(SELECT 1 FROM tracks t WHERE t.release_id=r.id AND t.status='pending')",
        (STATUS_PENDING,),
    )
    return sum(1 for row in rows if is_complete(db, int(row["id"])))


def incomplete_releases(db: Database, user_id: int) -> list[dict[str, Any]]:
    """An artist's own submissions that are not yet ready to be reviewed."""
    rows = db.query(
        "SELECT DISTINCT r.*, a.name AS artist FROM releases r "
        "JOIN artists a ON a.id = r.artist_id "
        "JOIN tracks t ON t.release_id = r.id "
        "WHERE r.status=? AND t.submitted_by=? ORDER BY r.submitted_at DESC LIMIT 20",
        (STATUS_PENDING, user_id),
    )
    out = []
    for row in rows:
        missing = missing_for_release(db, int(row["id"]))
        if missing:
            out.append({**dict(row), "missing": missing})
    return out


def release_tags(db: Database, release_id: int) -> list[str]:
    """Tags on a release: the union of what its tracks carry."""
    rows = db.query(
        "SELECT DISTINCT g.name FROM track_tags tt "
        "JOIN tags g ON g.id = tt.tag_id "
        "JOIN tracks t ON t.id = tt.track_id "
        "WHERE t.release_id=? ORDER BY g.name",
        (release_id,),
    )
    return [row["name"] for row in rows]


def set_release_tags(db: Database, release_id: int, names: Sequence[str]) -> list[str]:
    """Tag a whole release.

    Tags are the station's vocabulary rather than the artist's — they feed the
    recommender, and an artist tags for promotion while a curator tags for the
    shelf. A release is normally one coherent record, so the set applies to
    every track in it.
    """
    applied: list[str] = []
    for row in db.query("SELECT id FROM tracks WHERE release_id=?", (release_id,)):
        applied = set_tags(db, int(row["id"]), names)
        _index(db, int(row["id"]))
    return applied


def approve_release(
    db: Database, release_id: int, curator_id: int, note: str = ""
) -> tuple[dict[str, Any] | None, str]:
    """Publish a release and every track still waiting inside it.

    Returns ``(release, error)``. The error is a code, not a sentence, so the
    caller can translate it.
    """
    item = release(db, release_id)
    if item is None:
        return None, "not_found"
    missing = missing_for_release(db, release_id)
    if missing:
        # Unreachable through any interface — the queue filters these out — but
        # kept so no caller can publish an unfinished release by accident.
        return None, missing[0]

    stamp = now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE releases SET status=?, reviewed_by=?, reviewed_at=?, published_at=?, "
            "note=COALESCE(NULLIF(?, ''), note), reject_reason=NULL WHERE id=?",
            (STATUS_APPROVED, curator_id, stamp, stamp, note.strip(), release_id),
        )
        conn.execute(
            "UPDATE tracks SET status=?, reviewed_by=?, reviewed_at=?, published_at=? "
            "WHERE release_id=? AND status=?",
            (STATUS_APPROVED, curator_id, stamp, stamp, release_id, STATUS_PENDING),
        )
    published = release(db, release_id, only_approved=True)
    for track_item in published["tracks"] if published else []:
        _index(db, int(track_item["id"]))
    return published, ""


def reject_release(
    db: Database, release_id: int, curator_id: int, reason: str = ""
) -> dict[str, Any] | None:
    item = release(db, release_id)
    if item is None:
        return None
    stamp = now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE releases SET status=?, reviewed_by=?, reviewed_at=?, reject_reason=?, "
            "published_at=NULL WHERE id=?",
            (STATUS_REJECTED, curator_id, stamp, reason.strip()[:400] or None, release_id),
        )
        # Every track, not only the waiting ones: a declined release must not
        # leave published tracks behind it, which would be a state no screen
        # in the product knows how to describe.
        conn.execute(
            "UPDATE tracks SET status=?, reviewed_by=?, reviewed_at=?, reject_reason=?, "
            "published_at=NULL WHERE release_id=?",
            (
                STATUS_REJECTED,
                curator_id,
                stamp,
                reason.strip()[:400] or None,
                release_id,
            ),
        )
    for track_item in item["tracks"]:
        search.remove_track(db, int(track_item["id"]))
    return release(db, release_id)


def artist_releases(db: Database, artist_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT r.*, a.name AS artist, "
        "  (SELECT COUNT(*) FROM tracks t WHERE t.release_id=r.id AND t.status='approved') AS n "
        "FROM releases r JOIN artists a ON a.id = r.artist_id "
        "WHERE r.artist_id=? AND r.status='approved' "
        "ORDER BY COALESCE(r.year, 0) DESC, r.published_at DESC LIMIT ?",
        (artist_id, limit),
    )
    return [
        {**dict(row), "cover_file_id": release_cover(db, int(row["id"]))}
        for row in rows
        if row["n"]
    ]


def newest_releases(db: Database, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT r.*, a.name AS artist, "
        "  (SELECT COUNT(*) FROM tracks t WHERE t.release_id=r.id AND t.status='approved') AS n "
        "FROM releases r JOIN artists a ON a.id = r.artist_id "
        "WHERE r.status='approved' ORDER BY r.published_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    return [
        {**dict(row), "cover_file_id": release_cover(db, int(row["id"]))}
        for row in rows
        if row["n"]
    ]


def _index(db: Database, track_id: int) -> None:
    """Put one approved track into the search index, release title included."""
    item = track(db, track_id)
    if item is None or item["status"] != STATUS_APPROVED:
        return
    album = item.get("release_title") or item.get("album") or ""
    search.index_track(
        db,
        track_id,
        item["title"],
        item["artist"],
        album,
        item["tags"],
        item.get("note") or "",
    )


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------


def submissions_today(db: Database, user_id: int, day: str) -> int:
    return int(
        db.scalar(
            "SELECT submissions FROM usage WHERE user_id=? AND day=?", (user_id, day), default=0
        )
    )


def note_submission(db: Database, user_id: int, day: str) -> None:
    db.execute(
        "INSERT INTO usage(user_id, day, submissions) VALUES(?,?,1) "
        "ON CONFLICT(user_id, day) DO UPDATE SET submissions = submissions + 1",
        (user_id, day),
    )


def pending_count(db: Database, user_id: int) -> int:
    return int(
        db.scalar(
            "SELECT COUNT(*) FROM tracks WHERE submitted_by=? AND status=?",
            (user_id, STATUS_PENDING),
            default=0,
        )
    )


def find_duplicate(
    db: Database, artist: str, title: str, duration: int, ignore_id: int | None = None
) -> int | None:
    """Find an existing track that is almost certainly the same recording."""
    key = metadata.key_of(f"{artist} {title}")
    if not key:
        return None
    exact = db.one(
        "SELECT id FROM tracks WHERE key=? AND status != ? AND id IS NOT ?",
        (key, STATUS_REJECTED, ignore_id),
    )
    if exact is not None:
        return int(exact["id"])
    if not duration:
        return None
    rows = db.query(
        "SELECT t.id, t.title, t.duration, a.name AS artist FROM tracks t "
        "JOIN artists a ON a.id = t.artist_id "
        "WHERE t.status != ? AND ABS(t.duration - ?) <= ? LIMIT 400",
        (STATUS_REJECTED, duration, DUPLICATE_SECONDS),
    )
    for row in rows:
        if ignore_id is not None and int(row["id"]) == ignore_id:
            continue
        combined = metadata.similarity(f"{artist} {title}", f"{row['artist']} {row['title']}")
        if combined >= DUPLICATE_RATIO:
            return int(row["id"])
    return None


def resolve_tags(
    payload: dict[str, Any], filename: str, blob: bytes | None, probed: dict[str, Any]
) -> metadata.Tags:
    """Reconcile every metadata source into one record."""
    tags = metadata.parse(blob) if blob else metadata.Tags()

    if probed:
        probe_tags = metadata.Tags(
            title=metadata.clean_text(probed.get("tags", {}).get("title", "")),
            artist=metadata.clean_text(probed.get("tags", {}).get("artist", "")),
            album=metadata.clean_text(probed.get("tags", {}).get("album", "")),
            duration=probed.get("duration"),
        )
        year = probed.get("tags", {}).get("date") or probed.get("tags", {}).get("year")
        if year:
            probe_tags.year = metadata._year_from(str(year))
        tags.merged_with(probe_tags)

    telegram_tags = metadata.Tags(
        title=metadata.clean_text(payload.get("title") or ""),
        artist=metadata.clean_text(payload.get("performer") or ""),
        duration=float(payload.get("duration") or 0) or None,
    )
    tags.merged_with(telegram_tags)
    tags.merged_with(metadata.parse_filename(filename))

    title, guests = metadata.split_feature(tags.title or filename or "Untitled")
    tags.title = title or "Untitled"
    if guests and not tags.extra.get("GUESTS"):
        tags.extra["GUESTS"] = ", ".join(guests)
    performers = metadata.split_artists(tags.artist or tags.album_artist or "")
    tags.artist = performers[0] if performers else (tags.artist or "Unknown")
    tags.album = metadata.clean_title(tags.album or "")
    return tags


def intake(
    db: Database,
    config: Config,
    api: Api | None,
    user_id: int,
    payload: dict[str, Any],
    filename: str = "",
) -> Intake:
    """Turn one uploaded audio message into a pending catalogue row."""
    result = Intake()
    file_id = payload.get("file_id")
    unique_id = payload.get("file_unique_id")
    if not file_id or not unique_id:
        result.error = "no_file"
        return result

    existing = db.one("SELECT id, status FROM tracks WHERE file_unique_id=?", (unique_id,))
    if existing is not None:
        result.duplicate_of = int(existing["id"])
        result.error = "duplicate_file"
        return result

    size = int(payload.get("file_size") or 0)
    if size and size > config.limits.max_audio_bytes:
        result.warnings.append("too_large_to_inspect")

    blob: bytes | None = None
    if api is not None and (not size or size <= config.limits.max_audio_bytes):
        try:
            blob = api.download(file_id, limit=config.limits.max_audio_bytes)
        except Exception as exc:  # network hiccups must not lose the submission
            log.warning("could not fetch %s: %s", filename or unique_id, exc)

    name = filename or payload.get("file_name") or ""
    features: dict[str, Any] = {}
    probed: dict[str, Any] = {}
    if blob:
        features = audio_mod.analyse(blob, name or "audio.mp3")
        probed = {"tags": features.pop("tags", {})} if "tags" in features else {}
    tags = resolve_tags(payload, name, blob, probed)

    duration = int(
        payload.get("duration") or (features.get("duration") or 0) or (tags.duration or 0)
    )
    if duration and duration < config.limits.min_duration:
        result.error = "too_short"
        return result
    if duration and duration > config.limits.max_duration:
        result.warnings.append("very_long")

    duplicate = find_duplicate(db, tags.artist, tags.title, duration)
    if duplicate is not None:
        result.duplicate_of = duplicate
        result.error = "duplicate_track"
        return result

    cover_file_id = _store_cover(api, config, payload, tags)

    artist_id = get_or_create_artist(db, tags.artist, user_id)
    if tags.duration and not features.get("duration"):
        features["duration"] = tags.duration

    # Every track belongs to a release. With an album tag the track joins (or
    # opens) that release; without one it becomes a single named after itself,
    # so the rest of the system never has to special-case a loose track.
    release_title = tags.album or tags.title
    release_id = get_or_create_release(
        db,
        artist_id,
        release_title,
        year=tags.year,
        submitted_by=user_id,
        is_single=not tags.album,
    )
    position = tags.track_no or next_track_number(db, release_id)

    cursor = db.execute(
        "INSERT INTO tracks("
        " artist_id, release_id, track_no, title, key, album, year, duration,"
        " file_id, file_unique_id, file_size, mime, cover_file_id, status,"
        " submitted_by, submitted_at, features"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            artist_id,
            release_id,
            position,
            tags.title,
            metadata.key_of(f"{tags.artist} {tags.title}"),
            tags.album or None,
            tags.year,
            duration,
            file_id,
            unique_id,
            size or None,
            payload.get("mime_type"),
            cover_file_id,
            STATUS_PENDING,
            user_id,
            now(),
            json.dumps(features, ensure_ascii=False) if features else None,
        ),
    )
    result.track_id = int(cursor.lastrowid)
    result.release_id = release_id
    result.track_no = position
    result.title = tags.title
    result.artist = tags.artist
    result.album = tags.album
    result.year = tags.year
    result.duration = duration
    result.cover_file_id = cover_file_id
    result.features = features

    # The first artwork to arrive becomes the release cover; a curator can
    # replace it later.
    if cover_file_id:
        db.execute(
            "UPDATE releases SET cover_file_id=COALESCE(cover_file_id, ?) WHERE id=?",
            (cover_file_id, release_id),
        )
    refresh_release_kind(db, release_id)
    result.release_title = release_title
    result.needs_cover = not release_cover(db, release_id)
    if tags.genre:
        set_tags(db, result.track_id, [tags.genre])
    return result


def _store_cover(
    api: Api | None, config: Config, payload: dict[str, Any], tags: metadata.Tags
) -> str | None:
    """Obtain a durable photo ``file_id`` for the cover.

    Telegram's own thumbnail is preferred because it already *is* a photo
    file_id — no upload, nothing to lose. Only when a file has embedded art and
    Telegram produced no thumbnail do we upload once, to the review chat, and
    keep the id. The audio file itself always keeps whatever art it was
    uploaded with, so the in-player artwork never depends on any of this.
    """
    thumb = payload.get("thumbnail") or payload.get("thumb") or {}
    if isinstance(thumb, dict) and thumb.get("file_id"):
        return str(thumb["file_id"])
    if not (tags.cover and api is not None and config.review_chat):
        return None
    blob = audio_mod.normalise_cover(tags.cover)
    if not blob:
        return None
    name = "cover" + metadata.image_extension(tags.cover_mime)
    try:
        message = api.send_photo(
            config.review_chat,
            blob,
            filename=name,
            caption="cover archive",
            disable_notification=True,
        )
    except Exception as exc:
        log.warning("cover archive upload failed: %s", exc)
        return None
    sizes = (message or {}).get("photo") or []
    return str(sizes[-1]["file_id"]) if sizes else None


# --------------------------------------------------------------------------
# Moderation
# --------------------------------------------------------------------------


def track(db: Database, track_id: int) -> dict[str, Any] | None:
    """A track joined with its artist, tags and parsed feature blobs."""
    row = db.one(
        "SELECT t.*, a.name AS artist, a.key AS artist_key, a.user_id AS artist_user, "
        "r.title AS release_title, r.kind AS release_kind, r.year AS release_year "
        "FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "LEFT JOIN releases r ON r.id = t.release_id WHERE t.id=?",
        (track_id,),
    )
    if row is None:
        return None
    item = dict(row)
    item["tags"] = tags_of(db, track_id)
    item["features"] = _json(item.get("features"))
    # Artwork resolves through the release, so a track never shows up bare
    # just because its own file carried no picture.
    if item.get("release_id"):
        item["cover_file_id"] = release_cover(db, int(item["release_id"]))
    return item


def tracks(db: Database, ids: Sequence[int]) -> list[dict[str, Any]]:
    """Fetch many tracks at once, preserving the order of ``ids``."""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.query(
        f"SELECT t.*, a.name AS artist, r.title AS release_title, r.kind AS release_kind, "
        f"COALESCE(r.cover_file_id, t.cover_file_id) AS cover_file_id "
        f"FROM tracks t JOIN artists a ON a.id = t.artist_id "
        f"LEFT JOIN releases r ON r.id = t.release_id "
        f"WHERE t.id IN ({marks})",
        tuple(ids),
    )
    by_id = {int(row["id"]): dict(row) for row in rows}
    out = []
    for track_id in ids:
        item = by_id.get(int(track_id))
        if item is not None:
            item["features"] = _json(item.get("features"))
            out.append(item)
    return out


def tags_of(db: Database, track_id: int) -> list[str]:
    return [
        row["name"]
        for row in db.query(
            "SELECT g.name FROM track_tags tt JOIN tags g ON g.id = tt.tag_id "
            "WHERE tt.track_id=? ORDER BY g.name",
            (track_id,),
        )
    ]


def set_tags(db: Database, track_id: int, names: Sequence[str]) -> list[str]:
    """Replace a track's tags. Returns the normalised set that was stored."""
    cleaned: list[str] = []
    for name in names:
        value = metadata.normalise_tag(name)
        if value and value not in cleaned:
            cleaned.append(value)
    cleaned = cleaned[:12]
    with db.transaction() as conn:
        conn.execute("DELETE FROM track_tags WHERE track_id=?", (track_id,))
        for name in cleaned:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
            conn.execute(
                "INSERT OR IGNORE INTO track_tags(track_id, tag_id) VALUES(?,?)",
                (track_id, tag_id),
            )
    return cleaned


def update_track(db: Database, track_id: int, **fields: Any) -> None:
    allowed = {"title", "album", "year", "note", "cover_file_id", "duration"}
    columns = {k: v for k, v in fields.items() if k in allowed}
    if not columns:
        return
    if "title" in columns:
        row = db.one(
            "SELECT a.name AS artist FROM tracks t JOIN artists a ON a.id=t.artist_id WHERE t.id=?",
            (track_id,),
        )
        artist = row["artist"] if row else ""
        columns["key"] = metadata.key_of(f"{artist} {columns['title']}")
    assignments = ", ".join(f"{name}=?" for name in columns)
    db.execute(f"UPDATE tracks SET {assignments} WHERE id=?", (*columns.values(), track_id))


def rename_artist(db: Database, track_id: int, name: str, claimed_by: int | None = None) -> int:
    """Move a track to a different (possibly new) artist."""
    artist_id = get_or_create_artist(db, name, claimed_by)
    row = db.one("SELECT title FROM tracks WHERE id=?", (track_id,))
    title = row["title"] if row else ""
    db.execute(
        "UPDATE tracks SET artist_id=?, key=? WHERE id=?",
        (artist_id, metadata.key_of(f"{name} {title}"), track_id),
    )
    return artist_id


def restore_release(db: Database, release_id: int) -> dict[str, Any] | None:
    """Undo a review decision, putting the whole release back in the queue.

    Every decision is reversible; that is what makes a fast keyboard workflow
    safe to offer at all.
    """
    if release(db, release_id) is None:
        return None
    with db.transaction() as conn:
        conn.execute(
            "UPDATE releases SET status=?, reviewed_by=NULL, reviewed_at=NULL, "
            "reject_reason=NULL, published_at=NULL WHERE id=?",
            (STATUS_PENDING, release_id),
        )
        conn.execute(
            "UPDATE tracks SET status=?, reviewed_by=NULL, reviewed_at=NULL, "
            "reject_reason=NULL, published_at=NULL WHERE release_id=?",
            (STATUS_PENDING, release_id),
        )
    for row in db.query("SELECT id FROM tracks WHERE release_id=?", (release_id,)):
        search.remove_track(db, int(row["id"]))
    return release(db, release_id)


def hide_release(db: Database, release_id: int, curator_id: int) -> dict[str, Any] | None:
    """Withdraw a published release without deleting its history.

    Release-level like every other decision. A curator never takes down one
    song out of somebody's record: the unit that was accepted is the unit that
    can be withdrawn.
    """
    item = release(db, release_id)
    if item is None:
        return None
    stamp = now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE releases SET status=?, reviewed_by=?, reviewed_at=? WHERE id=?",
            (STATUS_HIDDEN, curator_id, stamp, release_id),
        )
        conn.execute(
            "UPDATE tracks SET status=?, reviewed_by=?, reviewed_at=? WHERE release_id=?",
            (STATUS_HIDDEN, curator_id, stamp, release_id),
        )
    for track_item in item["tracks"]:
        search.remove_track(db, int(track_item["id"]))
    return release(db, release_id)


# --------------------------------------------------------------------------
# Reading the catalogue
# --------------------------------------------------------------------------


def artist_tracks(db: Database, artist_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT t.*, a.name AS artist FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "WHERE t.artist_id=? AND t.status=? ORDER BY COALESCE(t.year, 0) DESC, t.published_at DESC "
        "LIMIT ?",
        (artist_id, STATUS_APPROVED, limit),
    )
    return [dict(row) for row in rows]


def newest(db: Database, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT t.*, a.name AS artist FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "WHERE t.status=? ORDER BY t.published_at DESC LIMIT ? OFFSET ?",
        (STATUS_APPROVED, limit, offset),
    )
    return [dict(row) for row in rows]


def liked(db: Database, user_id: int, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT t.*, a.name AS artist FROM likes l "
        "JOIN tracks t ON t.id = l.track_id JOIN artists a ON a.id = t.artist_id "
        "WHERE l.user_id=? AND t.status=? ORDER BY l.ts DESC LIMIT ? OFFSET ?",
        (user_id, STATUS_APPROVED, limit, offset),
    )
    return [dict(row) for row in rows]


def is_liked(db: Database, user_id: int, track_id: int) -> bool:
    return bool(
        db.scalar("SELECT 1 FROM likes WHERE user_id=? AND track_id=?", (user_id, track_id))
    )


def set_like(db: Database, user_id: int, track_id: int, liked_now: bool) -> None:
    if liked_now:
        db.execute(
            "INSERT OR IGNORE INTO likes(user_id, track_id, ts) VALUES(?,?,?)",
            (user_id, track_id, now()),
        )
    else:
        db.execute("DELETE FROM likes WHERE user_id=? AND track_id=?", (user_id, track_id))
    db.execute(
        "UPDATE tracks SET likes = (SELECT COUNT(*) FROM likes WHERE track_id=?) WHERE id=?",
        (track_id, track_id),
    )


def is_following(db: Database, user_id: int, artist_id: int) -> bool:
    return bool(
        db.scalar("SELECT 1 FROM follows WHERE user_id=? AND artist_id=?", (user_id, artist_id))
    )


def set_follow(db: Database, user_id: int, artist_id: int, following: bool) -> None:
    if following:
        db.execute(
            "INSERT OR IGNORE INTO follows(user_id, artist_id, ts) VALUES(?,?,?)",
            (user_id, artist_id, now()),
        )
    else:
        db.execute("DELETE FROM follows WHERE user_id=? AND artist_id=?", (user_id, artist_id))


def followed_artists(db: Database, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT a.*, COUNT(t.id) AS n FROM follows f "
        "JOIN artists a ON a.id = f.artist_id "
        "LEFT JOIN tracks t ON t.artist_id = a.id AND t.status='approved' "
        "WHERE f.user_id=? GROUP BY a.id ORDER BY f.ts DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(row) for row in rows]


def submitted_by(db: Database, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT t.*, a.name AS artist FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "WHERE t.submitted_by=? ORDER BY t.submitted_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# Playlists
# --------------------------------------------------------------------------


def create_playlist(
    db: Database, title: str, owner: int | None, description: str = ""
) -> int | None:
    title = metadata.clean_text(title)[:80]
    if not title:
        return None
    slug = metadata.key_of(title)[:48] or f"list{now()}"
    if db.one("SELECT 1 FROM playlists WHERE slug=?", (slug,)):
        slug = f"{slug}{now() % 100000}"
    cursor = db.execute(
        "INSERT INTO playlists(title, slug, description, owner, created_at, published) "
        "VALUES(?,?,?,?,?,0)",
        (title, slug, description.strip()[:400] or None, owner, now()),
    )
    return int(cursor.lastrowid)


def playlist(db: Database, playlist_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM playlists WHERE id=?", (playlist_id,))
    if row is None:
        return None
    item = dict(row)
    item["tracks"] = [
        dict(r)
        for r in db.query(
            "SELECT t.*, a.name AS artist FROM playlist_tracks pt "
            "JOIN tracks t ON t.id = pt.track_id JOIN artists a ON a.id = t.artist_id "
            "WHERE pt.playlist_id=? AND t.status='approved' ORDER BY pt.position",
            (playlist_id,),
        )
    ]
    return item


def playlists(db: Database, published_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    clause = "WHERE p.published=1" if published_only else ""
    rows = db.query(
        "SELECT p.*, COUNT(pt.track_id) AS n FROM playlists p "
        "LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        f"{clause} GROUP BY p.id HAVING n > 0 ORDER BY p.created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in rows]


def add_to_playlist(db: Database, playlist_id: int, track_id: int) -> None:
    position = int(
        db.scalar(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_tracks WHERE playlist_id=?",
            (playlist_id,),
            default=1,
        )
    )
    db.execute(
        "INSERT OR IGNORE INTO playlist_tracks(playlist_id, track_id, position) VALUES(?,?,?)",
        (playlist_id, track_id, position),
    )


def remove_from_playlist(db: Database, playlist_id: int, track_id: int) -> None:
    db.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (playlist_id, track_id)
    )


def publish_playlist(db: Database, playlist_id: int, published: bool = True) -> None:
    db.execute("UPDATE playlists SET published=? WHERE id=?", (1 if published else 0, playlist_id))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def artist_stats(db: Database, artist_id: int) -> dict[str, int]:
    row = db.one(
        "SELECT COUNT(*) AS tracks, COALESCE(SUM(plays),0) AS plays, "
        "COALESCE(SUM(completes),0) AS completes, COALESCE(SUM(likes),0) AS likes, "
        "COALESCE(SUM(exposures),0) AS exposures "
        "FROM tracks WHERE artist_id=? AND status='approved'",
        (artist_id,),
    )
    stats = dict(row) if row else {}
    stats["followers"] = int(
        db.scalar("SELECT COUNT(*) FROM follows WHERE artist_id=?", (artist_id,), default=0)
    )
    return {k: int(v or 0) for k, v in stats.items()}


def curator_report(db: Database, days: int = 7) -> dict[str, Any]:
    since = now() - days * 86400
    return {
        "pending": pending_releases_total(db),
        "approved": int(
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE status='approved' AND reviewed_at>=?",
                (since,),
                default=0,
            )
        ),
        "rejected": int(
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE status='rejected' AND reviewed_at>=?",
                (since,),
                default=0,
            )
        ),
        "listeners": int(
            db.scalar("SELECT COUNT(DISTINCT user_id) FROM events WHERE ts>=?", (since,), default=0)
        ),
        "plays": int(
            db.scalar(
                "SELECT COUNT(*) FROM events WHERE kind='play' AND ts>=?", (since,), default=0
            )
        ),
        "likes": int(
            db.scalar(
                "SELECT COUNT(*) FROM events WHERE kind='like' AND ts>=?", (since,), default=0
            )
        ),
        "incomplete": int(
            db.scalar(
                "SELECT COUNT(*) FROM releases WHERE status='pending' "
                "AND (cover_file_id IS NULL AND NOT EXISTS "
                "  (SELECT 1 FROM tracks t WHERE t.release_id=releases.id "
                "   AND t.cover_file_id IS NOT NULL))",
                default=0,
            )
        ),
        "unheard": int(
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE status='approved' AND exposures=0", default=0
            )
        ),
        "catalogue": int(
            db.scalar("SELECT COUNT(*) FROM tracks WHERE status='approved'", default=0)
        ),
        "artists": int(
            db.scalar(
                "SELECT COUNT(DISTINCT artist_id) FROM tracks WHERE status='approved'", default=0
            )
        ),
    }


def _json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
