from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tonearm import catalog, handlers, metadata, recommend, search  # noqa: E402
from tonearm.config import Config  # noqa: E402
from tonearm.db import Database, now  # noqa: E402

from .fake import FakeApi  # noqa: E402

CURATOR = 900001
ARTIST = 900002
LISTENER = 900003
OTHER = 900004


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path)
    cfg.token = "1:test"
    cfg.owner = CURATOR
    cfg.review_chat = -100500
    cfg.station_name = "Tonearm"
    return cfg


@pytest.fixture()
def db(config: Config) -> Database:
    return Database(config.database_path)


@pytest.fixture()
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture()
def engine(db: Database, config: Config) -> recommend.Engine:
    return recommend.Engine(db, config)


@pytest.fixture()
def bot(config: Config, db: Database, api: FakeApi, engine: recommend.Engine) -> handlers.Bot:
    return handlers.Bot(config, db, api, engine)


#: artist, title, tags, duration, acoustic features
CATALOGUE = [
    (
        "Anna Voskresenskaya",
        "Nightpost",
        ["ambient", "field recording"],
        212,
        {"tempo": 78.0, "centroid": 900.0, "lufs": -18.0},
    ),
    (
        "Anna Voskresenskaya",
        "Stairwell",
        ["ambient", "drone"],
        340,
        {"tempo": 70.0, "centroid": 800.0, "lufs": -19.0},
    ),
    (
        "Kolm",
        "Untitled III",
        ["drone", "ambient"],
        405,
        {"tempo": 66.0, "centroid": 850.0, "lufs": -20.0},
    ),
    ("Kolm", "Untitled IV", ["drone"], 380, {"tempo": 68.0, "centroid": 780.0, "lufs": -20.5}),
    (
        "Кино",
        "Группа крови",
        ["post-punk", "rock"],
        285,
        {"tempo": 128.0, "centroid": 2600.0, "lufs": -12.0},
    ),
    (
        "Nocturne",
        "Drift",
        ["post-punk", "cold wave"],
        198,
        {"tempo": 132.0, "centroid": 2800.0, "lufs": -11.0},
    ),
    (
        "Nocturne",
        "Second Drift",
        ["cold wave"],
        205,
        {"tempo": 130.0, "centroid": 2750.0, "lufs": -11.5},
    ),
    (
        "Tape Artist",
        "Loop One",
        ["tape", "loop"],
        150,
        {"tempo": 96.0, "centroid": 1600.0, "lufs": -15.0},
    ),
    (
        "Tape Artist",
        "Loop Two",
        ["tape", "loop"],
        160,
        {"tempo": 98.0, "centroid": 1650.0, "lufs": -15.2},
    ),
    (
        "Siren Works",
        "Harbour",
        ["field recording", "tape"],
        260,
        {"tempo": 84.0, "centroid": 1200.0, "lufs": -17.0},
    ),
    (
        "Siren Works",
        "Foghorn",
        ["field recording"],
        300,
        {"tempo": 80.0, "centroid": 1100.0, "lufs": -17.5},
    ),
    (
        "Low Ceiling",
        "Basement",
        ["post-punk"],
        240,
        {"tempo": 140.0, "centroid": 3000.0, "lufs": -10.0},
    ),
]


#: Which release each catalogue entry belongs to. Two artists ship a
#: multi-track release so the album paths are exercised, the rest are singles.
RELEASE_OF = {
    "Nightpost": "Stairwell",
    "Stairwell": "Stairwell",
    "Untitled III": "Winterlight",
    "Untitled IV": "Winterlight",
}


@pytest.fixture()
def seeded(db: Database) -> list[int]:
    """A small but realistic approved catalogue: 12 tracks in 10 releases."""
    ids: list[int] = []
    stamp = now()
    for index, (artist, title, tags, duration, features) in enumerate(CATALOGUE):
        artist_id = catalog.get_or_create_artist(db, artist)
        published = stamp - 86400 * (len(CATALOGUE) - index)
        release_title = RELEASE_OF.get(title, title)
        release_id = catalog.get_or_create_release(
            db, artist_id, release_title, submitted_by=ARTIST, is_single=title not in RELEASE_OF
        )
        catalog.set_release_cover(db, release_id, f"cover{release_id}")
        db.execute(
            "UPDATE releases SET status='approved', reviewed_by=?, reviewed_at=?, "
            "published_at=? WHERE id=?",
            (CURATOR, published, published, release_id),
        )
        cursor = db.execute(
            "INSERT INTO tracks(artist_id, release_id, track_no, title, key, album, duration, "
            "file_id, file_unique_id, status, submitted_by, submitted_at, published_at, "
            "reviewed_by, note, features) "
            "VALUES(?,?,?,?,?,?,?,?,?,'approved',?,?,?,?,?,?)",
            (
                artist_id,
                release_id,
                catalog.next_track_number(db, release_id),
                title,
                metadata.key_of(f"{artist} {title}"),
                release_title,
                duration,
                f"file{index}",
                f"uniq{index}",
                ARTIST,
                published,
                published,
                CURATOR,
                f"A note about {title}." if index % 2 == 0 else None,
                json.dumps(features),
            ),
        )
        track_id = int(cursor.lastrowid)
        catalog.set_tags(db, track_id, tags)
        catalog.refresh_release_kind(db, release_id)
        search.index_track(db, track_id, title, artist, release_title, tags, "")
        ids.append(track_id)
    return ids
