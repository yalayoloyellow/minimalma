"""SQLite storage: connection policy, schema and migrations.

The whole catalogue lives in one file so that "back up the service" means
"copy one file". Concurrency is handled by WAL plus a generous busy timeout;
writes are short and the worker pool is small, so no external write queue is
warranted.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger("tonearm.db")

SCHEMA_VERSION = 4

#: Applied to every connection. ``foreign_keys`` is per-connection in SQLite,
#: which is the usual reason constraints appear to be silently ignored.
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=8000",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-16000",
)

#: One entry per schema version. A plain string is executed as a script; a
#: callable receives the connection and drives its own statements, which is
#: what a step with a data backfill needs in order to stay atomic.
MIGRATIONS: list = [
    # ---------------------------------------------------------------- v1
    """
    CREATE TABLE users (
        id           INTEGER PRIMARY KEY,
        created_at   INTEGER NOT NULL,
        last_seen    INTEGER NOT NULL,
        lang         TEXT    NOT NULL DEFAULT 'en',
        name         TEXT,
        username     TEXT,
        screen_chat  INTEGER,
        screen_msg   INTEGER,
        state        TEXT,
        state_data   TEXT,
        digest       INTEGER NOT NULL DEFAULT 0,
        banned       INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE artists (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        key         TEXT    NOT NULL UNIQUE,
        bio         TEXT,
        link        TEXT,
        user_id     INTEGER,
        created_at  INTEGER NOT NULL
    );
    CREATE INDEX artists_user ON artists(user_id);

    CREATE TABLE tracks (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_id      INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
        title          TEXT    NOT NULL,
        key            TEXT    NOT NULL,
        album          TEXT,
        year           INTEGER,
        duration       INTEGER NOT NULL DEFAULT 0,
        file_id        TEXT    NOT NULL,
        file_unique_id TEXT    NOT NULL UNIQUE,
        file_size      INTEGER,
        mime           TEXT,
        cover_file_id  TEXT,
        status         TEXT    NOT NULL DEFAULT 'pending',
        submitted_by   INTEGER,
        submitted_at   INTEGER NOT NULL,
        reviewed_by    INTEGER,
        reviewed_at    INTEGER,
        reject_reason  TEXT,
        note           TEXT,
        features       TEXT,
        quality        TEXT,
        published_at   INTEGER,
        exposures      INTEGER NOT NULL DEFAULT 0,
        plays          INTEGER NOT NULL DEFAULT 0,
        completes      INTEGER NOT NULL DEFAULT 0,
        likes          INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX tracks_status   ON tracks(status, published_at);
    CREATE INDEX tracks_artist   ON tracks(artist_id, status);
    CREATE INDEX tracks_key      ON tracks(key);
    CREATE INDEX tracks_pending  ON tracks(status, submitted_at);
    CREATE INDEX tracks_exposure ON tracks(status, exposures);

    CREATE TABLE tags (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    );
    CREATE TABLE track_tags (
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        PRIMARY KEY (track_id, tag_id)
    );
    CREATE INDEX track_tags_tag ON track_tags(tag_id);

    CREATE TABLE events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  INTEGER NOT NULL,
        track_id INTEGER,
        kind     TEXT    NOT NULL,
        ts       INTEGER NOT NULL,
        weight   REAL    NOT NULL DEFAULT 1.0
    );
    CREATE INDEX events_user  ON events(user_id, ts);
    CREATE INDEX events_track ON events(track_id, kind);

    CREATE TABLE likes (
        user_id  INTEGER NOT NULL,
        track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        ts       INTEGER NOT NULL,
        PRIMARY KEY (user_id, track_id)
    );
    CREATE INDEX likes_track ON likes(track_id);

    CREATE TABLE follows (
        user_id   INTEGER NOT NULL,
        artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
        ts        INTEGER NOT NULL,
        PRIMARY KEY (user_id, artist_id)
    );
    CREATE INDEX follows_artist ON follows(artist_id);

    CREATE TABLE playlists (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        title       TEXT    NOT NULL,
        slug        TEXT    NOT NULL UNIQUE,
        description TEXT,
        owner       INTEGER,
        created_at  INTEGER NOT NULL,
        published   INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE playlist_tracks (
        playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        track_id    INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
        position    INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, track_id)
    );
    CREATE INDEX playlist_tracks_order ON playlist_tracks(playlist_id, position);

    -- One finite selection per user per day. Regenerating it is not possible
    -- by design: the row is written once and only consumed afterwards.
    CREATE TABLE daily (
        user_id INTEGER NOT NULL,
        day     TEXT    NOT NULL,
        ids     TEXT    NOT NULL,
        served  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );

    CREATE TABLE usage (
        user_id     INTEGER NOT NULL,
        day         TEXT    NOT NULL,
        discovers   INTEGER NOT NULL DEFAULT 0,
        submissions INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, day)
    );

    -- Materialised item-item neighbourhood, top-K per track.
    CREATE TABLE similar (
        track_id INTEGER NOT NULL,
        other_id INTEGER NOT NULL,
        score    REAL    NOT NULL,
        PRIMARY KEY (track_id, other_id)
    );
    CREATE INDEX similar_track ON similar(track_id, score DESC);

    CREATE TABLE meta (
        k TEXT PRIMARY KEY,
        v TEXT
    );

    CREATE VIRTUAL TABLE search USING fts5(
        title, artist, album, tags, note, alt,
        tokenize = "unicode61 remove_diacritics 2"
    );
    """,
]


def _migration_2(conn: sqlite3.Connection) -> None:
    """Releases.

    A track is never loose: it belongs to a single, an EP or an album, the
    release carries the artwork, and the release is the unit a curator accepts
    or declines.

    This step is a function rather than a SQL string because it ends in a
    backfill that needs Python's folding rules. Being a function also means the
    schema change and the data move share one transaction — ``executescript``
    would have committed the DDL before the backfill could fail.
    """
    conn.execute(
        """
        CREATE TABLE releases (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id     INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
            title         TEXT    NOT NULL,
            key           TEXT    NOT NULL,
            kind          TEXT    NOT NULL DEFAULT 'single',
            year          INTEGER,
            cover_file_id TEXT,
            note          TEXT,
            status        TEXT    NOT NULL DEFAULT 'pending',
            submitted_by  INTEGER,
            submitted_at  INTEGER NOT NULL,
            reviewed_by   INTEGER,
            reviewed_at   INTEGER,
            reject_reason TEXT,
            published_at  INTEGER
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX releases_key ON releases(artist_id, key)")
    conn.execute("CREATE INDEX releases_status ON releases(status, published_at)")
    conn.execute(
        "ALTER TABLE tracks ADD COLUMN release_id INTEGER REFERENCES releases(id) ON DELETE CASCADE"
    )
    conn.execute("ALTER TABLE tracks ADD COLUMN track_no INTEGER")
    conn.execute("CREATE INDEX tracks_release ON tracks(release_id, track_no)")
    _backfill_releases(conn)


MIGRATIONS.append(_migration_2)


def _migration_3(conn: sqlite3.Connection) -> None:
    """Name the observable signal: an audio request, never a listen."""
    conn.execute("ALTER TABLE tracks ADD COLUMN requests INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE tracks ADD COLUMN last_requested_at INTEGER")
    conn.execute("UPDATE tracks SET requests=plays")
    conn.execute(
        "UPDATE tracks SET last_requested_at=(SELECT MAX(ts) FROM events "
        "WHERE events.track_id=tracks.id AND events.kind='play')"
    )


MIGRATIONS.append(_migration_3)

MIGRATIONS.append(
    """
    CREATE TABLE diagnostics (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         INTEGER NOT NULL,
        update_id  INTEGER,
        kind       TEXT NOT NULL,
        user_id    INTEGER,
        outcome    TEXT NOT NULL,
        duration   INTEGER NOT NULL DEFAULT 0,
        error      TEXT
    );
    CREATE INDEX diagnostics_ts ON diagnostics(ts);
    CREATE INDEX diagnostics_kind ON diagnostics(kind, ts);
    """
)


def _backfill_releases(conn: sqlite3.Connection) -> None:
    """Give every pre-v2 track a release.

    Tracks are grouped by ``(artist, album)`` using the same folded key the
    catalogue uses elsewhere, so ``Аквариум`` and ``Akvarium`` land in one
    release. A track with no album becomes a single named after itself.
    """
    from . import metadata

    rows = conn.execute(
        "SELECT id, artist_id, title, album, year, cover_file_id, status, "
        "submitted_by, submitted_at, reviewed_by, reviewed_at, published_at "
        "FROM tracks ORDER BY id"
    ).fetchall()

    seen: dict[tuple, int] = {}
    for row in rows:
        title = (row["album"] or "").strip() or row["title"]
        key = metadata.key_of(title) or metadata.key_of(row["title"]) or f"r{row['id']}"
        identity = (row["artist_id"], key)
        release_id = seen.get(identity)
        if release_id is None:
            cursor = conn.execute(
                "INSERT INTO releases(artist_id, title, key, kind, year, cover_file_id, "
                "status, submitted_by, submitted_at, reviewed_by, reviewed_at, published_at) "
                "VALUES(?,?,?,'single',?,?,?,?,?,?,?,?)",
                (
                    row["artist_id"],
                    title,
                    key,
                    row["year"],
                    row["cover_file_id"],
                    row["status"],
                    row["submitted_by"],
                    row["submitted_at"],
                    row["reviewed_by"],
                    row["reviewed_at"],
                    row["published_at"],
                ),
            )
            release_id = int(cursor.lastrowid)
            seen[identity] = release_id
        conn.execute("UPDATE tracks SET release_id=? WHERE id=?", (release_id, row["id"]))

    conn.execute(
        "UPDATE tracks SET track_no = (SELECT COUNT(*) FROM tracks t2 "
        "WHERE t2.release_id = tracks.release_id AND t2.id <= tracks.id) "
        "WHERE release_id IS NOT NULL"
    )
    conn.execute(
        "UPDATE releases SET cover_file_id = ("
        "  SELECT t.cover_file_id FROM tracks t WHERE t.release_id = releases.id "
        "  AND t.cover_file_id IS NOT NULL LIMIT 1) "
        "WHERE cover_file_id IS NULL"
    )
    conn.execute(
        "UPDATE releases SET kind = CASE "
        "  WHEN (SELECT COUNT(*) FROM tracks WHERE release_id = releases.id) = 1 THEN 'single' "
        "  WHEN (SELECT COUNT(*) FROM tracks WHERE release_id = releases.id) <= 6 THEN 'ep' "
        "  ELSE 'album' END"
    )


def now() -> int:
    """Whole seconds since the epoch, used for every timestamp column."""
    return int(time.time())


class Database:
    """A per-thread connection pool over a single SQLite file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.migrate()
        # The database contains Telegram ids, names and interaction history.
        # Match the config's private permissions; SQLite creates the file
        # before migrations have anything else to do.
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - unusual filesystems
            pass

    # ------------------------------------------------------------ plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            for pragma in PRAGMAS:
                conn.execute(pragma)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def migrate(self) -> None:
        conn = self.conn
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database at {self.path} was written by a newer Tonearm "
                f"(schema {current} > {SCHEMA_VERSION}); upgrade or point "
                f"TONEARM_HOME elsewhere"
            )
        with self._write_lock:
            for index in range(current, SCHEMA_VERSION):
                version = index + 1
                step = MIGRATIONS[index]
                log.info("applying schema migration %d", version)
                if callable(step):
                    # A callable step drives its own statements, so schema and
                    # data changes commit or roll back together.
                    conn.execute("BEGIN")
                    try:
                        step(conn)
                        conn.execute(f"PRAGMA user_version={version}")
                        conn.execute("COMMIT")
                    except Exception:
                        if conn.in_transaction:
                            conn.execute("ROLLBACK")
                        raise
                else:
                    # executescript() commits any open transaction first, so
                    # the transaction has to live inside the script itself.
                    try:
                        conn.executescript(
                            "BEGIN;\n" + step + f"\nPRAGMA user_version={version};\nCOMMIT;\n"
                        )
                    except Exception:
                        if conn.in_transaction:
                            conn.executescript("ROLLBACK;")
                        raise

    # --------------------------------------------------------------- access
    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, args))

    def one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def scalar(self, sql: str, args: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.conn.execute(sql, args).fetchone()
        return row[0] if row is not None and row[0] is not None else default

    def execute(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.conn.execute(sql, args)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        batch = list(rows)
        if not batch:
            return
        with self._write_lock:
            self.conn.executemany(sql, batch)

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def diagnostic(
        self,
        kind: str,
        outcome: str,
        *,
        update_id: int | None = None,
        user_id: int | None = None,
        duration: int = 0,
        error: str | None = None,
    ) -> None:
        """Persist a compact, secret-free record of one update lifecycle."""
        self.execute(
            "INSERT INTO diagnostics(ts, update_id, kind, user_id, outcome, duration, error) "
            "VALUES(?,?,?,?,?,?,?)",
            (now(), update_id, kind[:40], user_id, outcome[:24], max(0, duration), error),
        )
        self.execute(
            "DELETE FROM diagnostics WHERE id <= "
            "(SELECT COALESCE(MAX(id), 0) - 10000 FROM diagnostics)"
        )

    # ----------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Any = None) -> Any:
        raw = self.scalar("SELECT v FROM meta WHERE k=?", (key,))
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default

    def set_meta(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    # ------------------------------------------------------------ operations
    def backup_to(self, target: Path) -> Path:
        """Consistent online copy, safe to run while the bot serves traffic."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(target))
        try:
            with dest:
                self.conn.backup(dest)
        finally:
            dest.close()
        return target

    def integrity(self) -> str:
        return str(self.scalar("PRAGMA integrity_check", default="unknown"))

    def stats(self) -> dict[str, int]:
        return {
            "users": int(self.scalar("SELECT COUNT(*) FROM users", default=0)),
            "artists": int(self.scalar("SELECT COUNT(*) FROM artists", default=0)),
            "releases": int(
                self.scalar("SELECT COUNT(*) FROM releases WHERE status='approved'", default=0)
            ),
            "approved": int(
                self.scalar("SELECT COUNT(*) FROM tracks WHERE status='approved'", default=0)
            ),
            "pending": int(
                self.scalar("SELECT COUNT(*) FROM tracks WHERE status='pending'", default=0)
            ),
            "rejected": int(
                self.scalar("SELECT COUNT(*) FROM tracks WHERE status='rejected'", default=0)
            ),
            "likes": int(self.scalar("SELECT COUNT(*) FROM likes", default=0)),
            "events": int(self.scalar("SELECT COUNT(*) FROM events", default=0)),
            "diagnostics": int(self.scalar("SELECT COUNT(*) FROM diagnostics", default=0)),
            "diagnostic_errors": int(
                self.scalar(
                    "SELECT COUNT(*) FROM diagnostics WHERE outcome IN ('telegram_error','crash')",
                    default=0,
                )
            ),
        }


class _Transaction:
    """Context manager wrapping one write transaction and the write lock."""

    def __init__(self, db: Database):
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db._write_lock.acquire()
        self._db.conn.execute("BEGIN IMMEDIATE")
        return self._db.conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None:
                self._db.conn.execute("COMMIT")
            else:
                self._db.conn.execute("ROLLBACK")
        finally:
            self._db._write_lock.release()
        return False
