"""Releases: grouping, artwork as a publication requirement, album delivery."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

import tonearm.db as dbmod
from tonearm import catalog, handlers
from tonearm.config import Config
from tonearm.db import Database

from . import fake
from .conftest import ARTIST, CURATOR, LISTENER
from .fake import FakeApi, id3_file


def submit(bot: handlers.Bot, api: FakeApi, unique: str, **tags) -> int:
    """Submit a tagged file the way a real upload arrives."""
    api.files[f"f-{unique}"] = id3_file(**tags)
    bot.handle(
        fake.audio_message(ARTIST, f"f-{unique}", unique, thumbnail=True, file_name=f"{unique}.mp3")
    )
    return int(bot.db.scalar("SELECT id FROM tracks WHERE file_unique_id=?", (unique,)))


class TestGrouping:
    def test_a_track_always_lands_in_a_release(self, bot: handlers.Bot, api: FakeApi) -> None:
        track_id = submit(bot, api, "g1", title="Alone", artist="Solo", album="")
        item = catalog.track(bot.db, track_id)
        assert item["release_id"]
        assert item["release_kind"] == catalog.KIND_SINGLE
        assert item["release_title"] == "Alone"

    def test_the_album_tag_groups_tracks(self, bot: handlers.Bot, api: FakeApi) -> None:
        first = submit(bot, api, "g2", title="One", artist="Marsh", album="Harbour")
        second = submit(bot, api, "g3", title="Two", artist="Marsh", album="Harbour")
        a = catalog.track(bot.db, first)
        b = catalog.track(bot.db, second)
        assert a["release_id"] == b["release_id"]
        assert a["track_no"] == 1 and b["track_no"] == 2

    def test_kind_follows_the_track_count(self, bot: handlers.Bot, api: FakeApi) -> None:
        bot.config.limits.submissions_per_day = 50  # an album is more than a day's quota
        release_id = None
        for index in range(8):
            track_id = submit(
                bot, api, f"k{index}", title=f"T{index}", artist="Long", album="Long Player"
            )
            release_id = catalog.track(bot.db, track_id)["release_id"]
        kinds = []
        for count in (1, 4, 8):
            kinds.append(catalog.kind_for(count))
        assert kinds == [catalog.KIND_SINGLE, catalog.KIND_EP, catalog.KIND_ALBUM]
        assert bot.db.scalar("SELECT kind FROM releases WHERE id=?", (release_id,)) == "album"

    def test_the_same_album_by_different_artists_stays_separate(
        self, bot: handlers.Bot, api: FakeApi
    ) -> None:
        a = submit(bot, api, "s1", title="X", artist="One", album="Live")
        b = submit(bot, api, "s2", title="Y", artist="Two", album="Live")
        assert catalog.track(bot.db, a)["release_id"] != catalog.track(bot.db, b)["release_id"]

    def test_release_titles_fold_like_artist_names(self, bot: handlers.Bot, api: FakeApi) -> None:
        a = submit(bot, api, "f1", title="A", artist="Кино", album="Группа крови")
        b = submit(bot, api, "f2", title="B", artist="Кино", album="группа  крови")
        assert catalog.track(bot.db, a)["release_id"] == catalog.track(bot.db, b)["release_id"]


class TestArtwork:
    """Artwork completes a submission. It is supplied by the artist, and until
    it exists the release is not in anyone's queue."""

    def test_an_incomplete_release_never_reaches_the_queue(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "n1", "nn1", title="Bare", performer="A"))
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='nn1'"))

        assert catalog.missing_for_release(db, release_id) == ["no_cover"]
        assert catalog.is_complete(db, release_id) is False
        assert catalog.pending_releases(db) == []
        assert catalog.pending_releases_total(db) == 0

    def test_no_curator_is_told_about_an_incomplete_release(
        self, bot: handlers.Bot, api: FakeApi, config: Config
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "n6", "nn6", title="Bare", performer="A"))
        cards = [
            call
            for call in api.of("sendPhoto") + api.of("sendMessage")
            if call.get("chat_id") == config.review_chat
        ]
        assert cards == []

    def test_artwork_completes_it_and_summons_the_curators(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "n3", "nn3", title="Bare", performer="A"))
        assert "обложк" in api.texts[-1] or "artwork" in api.texts[-1]
        api.clear()

        photo = fake.message(ARTIST, "")
        photo["message"].pop("text")
        photo["message"]["photo"] = [{"file_id": "sent:cover", "width": 800, "height": 800}]
        bot.handle(photo)

        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='nn3'"))
        assert catalog.release_cover(db, release_id) == "sent:cover"
        assert catalog.is_complete(db, release_id)
        assert catalog.pending_releases_total(db) == 1
        cards = [
            call
            for call in api.of("sendPhoto") + api.of("sendMessage")
            if call.get("chat_id") == config.review_chat
        ]
        assert len(cards) == 1

    def test_release_artwork_is_materialised_on_each_coverless_audio(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config
    ) -> None:
        api.files["n-audio"] = b"audio-bytes"
        api.files["sent:cover"] = b"cover-bytes"
        bot.handle(fake.audio_message(ARTIST, "n-audio", "n-audio", title="Bare", performer="A"))
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='n-audio'"))
        photo = fake.message(ARTIST, "")
        photo["message"].pop("text")
        photo["message"]["photo"] = [{"file_id": "sent:cover", "width": 800, "height": 800}]
        bot.handle(photo)
        row = db.one("SELECT file_id, cover_file_id FROM tracks WHERE release_id=?", (release_id,))
        assert row["file_id"].startswith("recreated:")
        assert row["cover_file_id"] == "sent:cover"
        rebuilt = [call for call in api.of("sendAudio") if isinstance(call["audio"], bytes)]
        assert len(rebuilt) == 1 and rebuilt[0]["thumbnail_bytes"] == b"cover-bytes"

    def test_one_failed_audio_recreation_does_not_abort_the_release(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        api.files["first-audio"] = b"first"
        api.files["second-audio"] = b"second"
        bot.handle(
            fake.audio_message(
                ARTIST, "first-audio", "first-u", title="One", performer="A", album="Pair"
            )
        )
        bot.handle(
            fake.audio_message(
                ARTIST, "second-audio", "second-u", title="Two", performer="A", album="Pair"
            )
        )
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='first-u'"))
        api.files["sent:cover"] = b"cover"
        original = api.send_audio
        failed = {"value": True}

        def flaky(chat_id: int, audio: str | bytes, **kw: object) -> dict[str, object]:
            if isinstance(audio, bytes) and failed["value"]:
                failed["value"] = False
                raise RuntimeError("temporary Telegram error")
            return original(chat_id, audio, **kw)

        api.send_audio = flaky  # type: ignore[method-assign]
        photo = fake.message(ARTIST, "")
        photo["message"].pop("text")
        photo["message"]["photo"] = [{"file_id": "sent:cover", "width": 800, "height": 800}]
        bot.handle(photo)

        rows = db.query(
            "SELECT file_id FROM tracks WHERE release_id=? ORDER BY track_no", (release_id,)
        )
        assert rows[0]["file_id"] == "first-audio"
        assert str(rows[1]["file_id"]).startswith("recreated:")

    def test_only_the_submitter_can_set_the_artwork(self, bot: handlers.Bot, db: Database) -> None:
        bot.handle(fake.audio_message(ARTIST, "n4", "nn4", title="Bare", performer="A"))
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='nn4'"))
        for sender in (CURATOR, LISTENER):
            photo = fake.message(sender, "")
            photo["message"].pop("text")
            photo["message"]["photo"] = [
                {"file_id": f"other:{sender}", "width": 900, "height": 900}
            ]
            bot.handle(photo)
        assert catalog.release_cover(db, release_id) is None

    def test_the_artist_sees_what_is_missing(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "n7", "nn7", title="Bare", performer="A"))
        drafts = catalog.incomplete_releases(db, ARTIST)
        assert len(drafts) == 1 and drafts[0]["missing"] == ["no_cover"]
        bot.handle(fake.message(ARTIST, "/submit"))
        assert "нет обложки" in api.last_screen() or "artwork missing" in api.last_screen()

    def test_submit_screen_button_reenters_cover_upload(self, bot: handlers.Bot, api: FakeApi, db: Database) -> None:
        bot.handle(fake.audio_message(ARTIST, "n8", "nn8", title="Bare", performer="A"))
        api.clear()
        bot.handle(fake.message(ARTIST, "/submit"))
        callback = api.find_button("cov|")
        assert callback

        bot.handle(fake.callback(ARTIST, callback))

        assert db.scalar("SELECT state FROM users WHERE id=?", (ARTIST,)) == "covr:1"
        assert "обложк" in api.last_screen() or "artwork" in api.last_screen()

    def test_a_track_inherits_the_release_cover(self, bot: handlers.Bot, db: Database) -> None:
        bot.handle(fake.audio_message(ARTIST, "n5", "nn5", title="Bare", performer="A"))
        track_id = int(db.scalar("SELECT id FROM tracks WHERE file_unique_id='nn5'"))
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE id=?", (track_id,)))
        catalog.set_release_cover(db, release_id, "release:art")
        assert catalog.track(db, track_id)["cover_file_id"] == "release:art"

    def test_publishing_an_incomplete_release_is_refused_at_the_core(
        self, bot: handlers.Bot, db: Database
    ) -> None:
        """Unreachable through any interface, but the guard stays."""
        bot.handle(fake.audio_message(ARTIST, "n8", "nn8", title="Bare", performer="A"))
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='nn8'"))
        published, error = catalog.approve_release(db, release_id, CURATOR)
        assert published is None and error == "no_cover"


class TestReleaseModeration:
    def _ep(self, bot: handlers.Bot, api: FakeApi) -> int:
        submit(bot, api, "e1", title="One", artist="Marsh", album="Harbour")
        track_id = submit(bot, api, "e2", title="Two", artist="Marsh", album="Harbour")
        return int(catalog.track(bot.db, track_id)["release_id"])

    def test_publishing_a_release_publishes_every_waiting_track(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._ep(bot, api)
        bot.handle(fake.callback(CURATOR, f"rel|ok|{release_id}"))
        statuses = [
            row["status"]
            for row in db.query("SELECT status FROM tracks WHERE release_id=?", (release_id,))
        ]
        assert statuses == ["approved", "approved"]
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "approved"

    def test_declining_a_release_declines_its_tracks(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._ep(bot, api)
        bot.handle(fake.callback(CURATOR, f"rel|no|{release_id}"))
        bot.handle(fake.message(CURATOR, "Not this time."))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "rejected"
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE release_id=? AND status='rejected'",
                (release_id,),
            )
            == 2
        )

    def test_the_queue_lists_releases_not_tracks(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        self._ep(bot, api)
        submit(bot, api, "e3", title="Solo", artist="Other", album="")
        assert catalog.pending_releases_total(db) == 2
        bot.handle(fake.callback(CURATOR, "nav|queue"))
        screen = api.last_screen()
        assert "Harbour" in screen and "Solo" in screen

    def test_a_listener_cannot_publish_a_release(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._ep(bot, api)
        bot.handle(fake.callback(LISTENER, f"rel|ok|{release_id}"))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "pending"

    def test_the_artist_hears_once_about_a_release(self, bot: handlers.Bot, api: FakeApi) -> None:
        release_id = self._ep(bot, api)
        api.clear()
        bot.handle(fake.callback(CURATOR, f"rel|ok|{release_id}"))
        notices = [p for p in api.of("sendMessage") if p["chat_id"] == ARTIST]
        assert len(notices) == 1


class TestAlbumDelivery:
    def test_the_releases_screen_lists_them(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, "nav|releases"))
        screen = api.last_screen()
        assert "Stairwell" in screen and "Winterlight" in screen
        assert api.find_button("rl|")

    def test_a_release_page_shows_the_tracklist_with_its_cover(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Stairwell'"))
        bot.handle(fake.callback(LISTENER, f"rl|{release_id}"))
        photo = api.of("sendPhoto")[-1]
        assert photo["caption"].count("·") >= 2
        assert "Nightpost" in photo["caption"]
        assert photo["chat_id"] == LISTENER
        data = [b["callback_data"] for row in photo["reply_markup"]["inline_keyboard"] for b in row]
        assert any(d.startswith("play|") for d in data)
        assert any(d.startswith("relall|") for d in data)

    def test_opening_a_release_sends_no_audio(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Stairwell'"))
        bot.handle(fake.callback(LISTENER, f"rl|{release_id}"))
        assert api.audio_sent == []

    def test_playing_a_release_through_advances_one_tap_at_a_time(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Stairwell'"))
        bot.handle(fake.callback(LISTENER, f"relall|{release_id}"))
        assert len(api.audio_sent) == 1
        markup = api.audio_sent[0]["reply_markup"]["inline_keyboard"]
        nxt = [
            b["callback_data"]
            for row in markup
            for b in row
            if b["callback_data"].startswith("seq|")
        ]
        assert nxt
        bot.handle(fake.callback(LISTENER, nxt[0]))
        assert len(api.audio_sent) == 2

    def test_a_photo_screen_is_replaced_not_stacked(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Stairwell'"))
        bot.handle(fake.message(LISTENER, "/start"))
        api.clear()
        bot.handle(fake.callback(LISTENER, f"rl|{release_id}"))
        assert len(api.of("deleteMessage")) == 1  # the text screen was removed
        bot.handle(fake.callback(LISTENER, "nav|home"))
        # Going back to a text screen must not leave the photo behind.
        assert len(api.of("deleteMessage")) == 2


class TestMigration:
    def test_v1_databases_gain_releases(self) -> None:
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("BEGIN;\n" + dbmod.MIGRATIONS[0] + "\nPRAGMA user_version=1;\nCOMMIT;")
        conn.execute("INSERT INTO artists(name, key, created_at) VALUES('Кино','kino',1)")
        for index, (title, album) in enumerate(
            [("Группа крови", "Группа крови"), ("Закрой дверь", "Группа крови"), ("Сингл", None)]
        ):
            conn.execute(
                "INSERT INTO tracks(artist_id, title, key, album, duration, file_id, "
                "file_unique_id, cover_file_id, status, submitted_at) "
                "VALUES(1,?,?,?,200,?,?,'art','approved',1)",
                (title, title.lower(), album, f"f{index}", f"u{index}"),
            )
        conn.commit()
        conn.close()

        db = Database(path)
        assert db.scalar("PRAGMA user_version") == dbmod.SCHEMA_VERSION
        assert db.integrity() == "ok"
        assert db.scalar("SELECT COUNT(*) FROM tracks WHERE release_id IS NULL") == 0
        assert db.scalar("SELECT COUNT(*) FROM releases") == 2
        assert db.scalar("SELECT kind FROM releases WHERE title='Группа крови'") == "ep"
        assert db.scalar("SELECT cover_file_id FROM releases WHERE title='Сингл'") == "art"

    def test_the_upgrade_is_atomic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failing backfill must leave a v1 database untouched, not half-moved."""
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(str(path))
        conn.executescript("BEGIN;\n" + dbmod.MIGRATIONS[0] + "\nPRAGMA user_version=1;\nCOMMIT;")
        conn.commit()
        conn.close()

        def boom(_conn):
            raise RuntimeError("backfill exploded")

        monkeypatch.setattr(dbmod, "_backfill_releases", boom)
        with pytest.raises(RuntimeError):
            Database(path)

        check = sqlite3.connect(str(path))
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "releases" not in tables
        check.close()


class TestConfig:
    def test_artwork_is_required_by_default(self, tmp_path: Path) -> None:
        assert Config(home=tmp_path).require_cover is True
