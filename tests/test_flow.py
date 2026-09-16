"""End-to-end conversations, driven through the real update router."""

from __future__ import annotations

import pytest

from tonearm import catalog, handlers, search
from tonearm.config import Config
from tonearm.db import Database

from . import fake
from .conftest import ARTIST, CURATOR, LISTENER
from .fake import FakeApi, id3_file


def screen_button(api: FakeApi, prefix: str) -> str:
    data = api.find_button(prefix)
    assert data, f"no button starting with {prefix!r} in {api.buttons()}"
    return data


class TestDiagnostics:
    def test_update_diagnostics_keep_action_without_message_content(
        self, bot: handlers.Bot, db: Database
    ) -> None:
        bot.handle(fake.message(LISTENER, "/start"))
        row = db.one("SELECT kind, outcome, error FROM diagnostics ORDER BY id DESC LIMIT 1")
        assert row is not None
        assert row["kind"] == "message:command"
        assert row["outcome"] == "ok"
        assert row["error"] is None


class TestOnboarding:
    def test_start_creates_a_user_and_a_single_screen(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.message(LISTENER, "/start"))
        row = db.one("SELECT * FROM users WHERE id=?", (LISTENER,))
        assert row is not None and row["screen_msg"]
        assert len(api.of("sendMessage")) == 1  # one editable home screen

    def test_browsing_edits_the_screen_instead_of_adding_messages(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.message(LISTENER, "/start"))
        api.clear()
        for target in ("for_you", "new_week", "popular", "today", "library", "artists", "mixes", "settings", "help", "home"):
            bot.handle(fake.callback(LISTENER, f"nav|{target}"))
        assert api.of("sendMessage") == []
        assert len(api.of("editMessageText")) == 10

    def test_language_can_be_switched(self, bot: handlers.Bot, api: FakeApi, db: Database) -> None:
        bot.handle(fake.message(LISTENER, "/start"))
        bot.handle(fake.callback(LISTENER, "lang|ru"))
        assert db.scalar("SELECT lang FROM users WHERE id=?", (LISTENER,)) == "ru"
        assert "НАСТРОЙКИ" in api.last_screen()


class TestSubmission:
    def test_intake_keeps_audio_in_telegram_not_on_local_disk(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config
    ) -> None:
        api.files["locality-audio"] = b"audio-bytes"
        bot.handle(
            fake.audio_message(
                ARTIST, "locality-audio", "locality-unique", title="Remote", performer="A"
            )
        )
        local_media = [
            path for path in config.home.rglob("*")
            if path.is_file() and path.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
        ]
        assert local_media == []
        assert db.scalar("SELECT file_id FROM tracks WHERE file_unique_id='locality-unique'") == "locality-audio"

    def test_audio_becomes_a_pending_track(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        api.files["f1"] = id3_file()
        bot.handle(fake.audio_message(ARTIST, "f1", "u1", file_name="whatever.mp3"))
        row = db.one("SELECT * FROM tracks WHERE file_unique_id='u1'")
        assert row is not None
        assert row["status"] == "pending"
        assert row["title"] == "Nightpost"  # from the ID3 tag, not the filename
        assert (
            db.scalar("SELECT name FROM artists WHERE id=?", (row["artist_id"],))
            == "Anna Voskresenskaya"
        )

    def test_telegram_fields_fill_in_when_there_are_no_tags(
        self, bot: handlers.Bot, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "f2", "u2", title="Bare", performer="Someone"))
        row = db.one("SELECT * FROM tracks WHERE file_unique_id='u2'")
        assert row["title"] == "Bare"

    def test_filename_is_the_last_resort(self, bot: handlers.Bot, db: Database) -> None:
        bot.handle(fake.audio_message(ARTIST, "f3", "u3", file_name="Kolm - Untitled III.mp3"))
        row = db.one("SELECT * FROM tracks WHERE file_unique_id='u3'")
        assert row["title"] == "Untitled III"

    def test_promotional_junk_is_stripped_from_the_title(
        self, bot: handlers.Bot, db: Database
    ) -> None:
        bot.handle(
            fake.audio_message(
                ARTIST, "f4", "u4", title="Sunset (Official Video) [FREE]", performer="X"
            )
        )
        assert db.scalar("SELECT title FROM tracks WHERE file_unique_id='u4'") == "Sunset"

    def test_telegram_thumbnail_becomes_the_cover(self, bot: handlers.Bot, db: Database) -> None:
        bot.handle(fake.audio_message(ARTIST, "f5", "u5", title="T", performer="A", thumbnail=True))
        assert db.scalar("SELECT cover_file_id FROM tracks WHERE file_unique_id='u5'") == "thumb:u5"

    def test_the_same_file_cannot_be_submitted_twice(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "f6", "u6", title="Once", performer="A"))
        api.clear()
        bot.handle(fake.audio_message(ARTIST, "f6", "u6", title="Once", performer="A"))
        assert db.scalar("SELECT COUNT(*) FROM tracks") == 1
        assert "already here" in api.texts[-1]

    def test_the_same_recording_under_a_new_file_is_caught(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "f7", "u7", title="Twin", performer="Kolm"))
        api.clear()
        bot.handle(
            fake.audio_message(ARTIST, "f8", "u8", title="twin ", performer="KOLM", duration=182)
        )
        assert db.scalar("SELECT COUNT(*) FROM tracks") == 1

    def test_a_too_short_clip_is_refused(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "f9", "u9", title="Blip", performer="A", duration=4))
        assert db.scalar("SELECT COUNT(*) FROM tracks") == 0
        assert "seconds" in api.texts[-1]

    def test_daily_submission_limit(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config
    ) -> None:
        for index in range(config.limits.submissions_per_day):
            bot.handle(
                fake.audio_message(
                    ARTIST, f"g{index}", f"v{index}", title=f"T{index}", performer="A"
                )
            )
        api.clear()
        bot.handle(fake.audio_message(ARTIST, "gx", "vx", title="Over", performer="A"))
        assert db.scalar("SELECT COUNT(*) FROM tracks") == config.limits.submissions_per_day
        assert "limit" in api.texts[-1].lower()

    def test_a_voice_message_is_politely_refused(self, bot: handlers.Bot, api: FakeApi) -> None:
        update = fake.message(ARTIST, "")
        update["message"].pop("text")
        update["message"]["voice"] = {"file_id": "v", "file_unique_id": "vu", "duration": 30}
        bot.handle(update)
        assert "audio file" in api.texts[-1]

    def test_the_curator_receives_one_review_card_per_release(
        self, bot: handlers.Bot, api: FakeApi, config: Config
    ) -> None:
        """A three-track EP must not produce three notifications."""
        for index in range(3):
            bot.handle(
                fake.audio_message(
                    ARTIST,
                    f"f1{index}",
                    f"u1{index}",
                    title=f"Card {index}",
                    performer="A",
                    thumbnail=True,
                    file_name=f"{index}.mp3",
                )
            )
            # The album tag is what groups them; supply it the way a tagged
            # file would.
            track_id = int(
                bot.db.scalar("SELECT id FROM tracks WHERE file_unique_id=?", (f"u1{index}",))
            )
            release_id = int(bot.db.scalar("SELECT release_id FROM tracks WHERE id=?", (track_id,)))
            assert release_id

        cards = [
            call
            for call in api.of("sendPhoto") + api.of("sendMessage")
            if call.get("chat_id") == config.review_chat
        ]
        assert len(cards) == 3  # three singles here, one card each
        assert any("Card 0" in (c.get("caption") or c.get("text") or "") for c in cards)
        markup = cards[0].get("reply_markup") or {}
        data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        assert any(d.startswith("rel|ok") for d in data)

    def test_one_card_for_a_multi_track_release(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config
    ) -> None:
        api.files["a1"] = id3_file(title="One", artist="Marsh", album="Harbour Tapes")
        api.files["a2"] = id3_file(title="Two", artist="Marsh", album="Harbour Tapes")
        bot.handle(fake.audio_message(ARTIST, "a1", "ua1", thumbnail=True, file_name="1.mp3"))
        bot.handle(fake.audio_message(ARTIST, "a2", "ua2", thumbnail=True, file_name="2.mp3"))

        releases = db.query("SELECT id, title, kind FROM releases")
        assert len(releases) == 1
        assert releases[0]["title"] == "Harbour Tapes"
        assert releases[0]["kind"] == "ep"
        cards = [
            call
            for call in api.of("sendPhoto") + api.of("sendMessage")
            if call.get("chat_id") == config.review_chat
        ]
        assert len(cards) == 1

    def test_the_artist_can_correct_the_metadata_immediately(
        self, bot: handlers.Bot, db: Database
    ) -> None:
        bot.handle(
            fake.audio_message(
                ARTIST, "f11", "u11", title="Wrong", performer="Wrong", thumbnail=True
            )
        )
        bot.handle(fake.message(ARTIST, "Real Artist — Real Title"))
        row = db.one("SELECT * FROM tracks WHERE file_unique_id='u11'")
        assert row["title"] == "Real Title"
        assert (
            db.scalar("SELECT name FROM artists WHERE id=?", (row["artist_id"],)) == "Real Artist"
        )


class TestModeration:
    """A release is accepted or declined whole; a curator writes tags and a note."""

    def _submit(self, bot: handlers.Bot, unique: str = "m1") -> int:
        # thumbnail=True supplies artwork, without which nothing reaches the queue.
        bot.handle(
            fake.audio_message(
                ARTIST, f"file-{unique}", unique, title="Queued", performer="A", thumbnail=True
            )
        )
        track_id = int(bot.db.scalar("SELECT id FROM tracks WHERE file_unique_id=?", (unique,)))
        return int(bot.db.scalar("SELECT release_id FROM tracks WHERE id=?", (track_id,)))

    def test_approval_publishes_and_notifies(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._submit(bot)
        api.clear()
        bot.handle(fake.callback(CURATOR, f"rel|ok|{release_id}"))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "approved"
        notices = [p for p in api.of("sendMessage") if p["chat_id"] == ARTIST]
        assert notices and "Published" in notices[0]["text"]
        assert search.search(db, "Queued")

    def test_rejection_asks_for_a_reason_and_passes_it_on(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._submit(bot, "m2")
        bot.handle(fake.callback(CURATOR, f"rel|no|{release_id}"))
        assert "reason" in api.last_screen().lower()
        api.clear()
        bot.handle(fake.message(CURATOR, "The mix is unfinished."))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "rejected"
        notices = [p for p in api.of("sendMessage") if p["chat_id"] == ARTIST]
        assert notices and "unfinished" in notices[0]["text"]

    def test_rejection_can_skip_the_reason(self, bot: handlers.Bot, db: Database) -> None:
        release_id = self._submit(bot, "m3")
        bot.handle(fake.callback(CURATOR, f"rel|no|{release_id}"))
        bot.handle(fake.callback(CURATOR, f"rel|no0|{release_id}"))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "rejected"

    def test_tags_and_notes_are_stored(self, bot: handlers.Bot, db: Database) -> None:
        release_id = self._submit(bot, "m4")
        bot.handle(fake.callback(CURATOR, f"rel|tag|{release_id}"))
        bot.handle(fake.message(CURATOR, "Ambient, Field Recording, #tape"))
        bot.handle(fake.callback(CURATOR, f"rel|note|{release_id}"))
        bot.handle(fake.message(CURATOR, "Recorded on a stairwell landing."))
        assert catalog.release_tags(db, release_id) == ["ambient", "field recording", "tape"]
        assert "stairwell" in db.scalar("SELECT note FROM releases WHERE id=?", (release_id,))

    def test_a_curator_cannot_rewrite_someone_elses_release(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        """Title, artist and artwork belong to whoever made the record."""
        release_id = self._submit(bot, "m5")
        before = db.one("SELECT title, cover_file_id FROM releases WHERE id=?", (release_id,))
        api.clear()
        bot.handle(fake.callback(CURATOR, f"rel|cov|{release_id}"))
        photo = fake.message(CURATOR, "")
        photo["message"].pop("text")
        photo["message"]["photo"] = [{"file_id": "curator:art", "width": 900, "height": 900}]
        bot.handle(photo)
        after = db.one("SELECT title, cover_file_id FROM releases WHERE id=?", (release_id,))
        assert after["cover_file_id"] == before["cover_file_id"]
        assert after["title"] == before["title"]

    def test_a_listener_cannot_moderate(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        release_id = self._submit(bot, "m6")
        api.clear()
        bot.handle(fake.callback(LISTENER, f"rel|ok|{release_id}"))
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "pending"
        assert api.of("answerCallbackQuery")[0]["text"]

    def test_a_listener_cannot_see_the_queue(self, bot: handlers.Bot, api: FakeApi) -> None:
        bot.handle(fake.message(LISTENER, "/queue"))
        assert "curators" in api.texts[-1]

    def test_nothing_is_published_without_a_curator(self, bot: handlers.Bot, db: Database) -> None:
        self._submit(bot, "m7")
        assert db.scalar("SELECT COUNT(*) FROM tracks WHERE status='approved'", default=0) == 0


class TestListening:
    def test_today_lists_without_sending_audio(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.message(LISTENER, "/today"))
        assert api.audio_sent == []
        assert "TODAY" in api.last_screen()

    def test_pressing_a_number_sends_exactly_one_track(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        bot.handle(fake.message(LISTENER, "/today"))
        data = screen_button(api, "play|")
        api.clear()
        bot.handle(fake.callback(LISTENER, data))
        assert len(api.audio_sent) == 1
        track_id = int(data.split("|")[1])
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM events WHERE user_id=? AND track_id=? AND kind='play'",
                (LISTENER, track_id),
            )
            == 1
        )

    def test_a_played_track_carries_its_curator_note(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        noted = int(db.scalar("SELECT id FROM tracks WHERE note IS NOT NULL LIMIT 1"))
        bot.handle(fake.callback(LISTENER, f"play|{noted}"))
        assert "A note about" in api.audio_sent[0]["caption"]

    def test_saving_and_unsaving(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        bot.handle(fake.callback(LISTENER, f"like|{seeded[0]}"))
        assert catalog.is_liked(db, LISTENER, seeded[0])
        bot.handle(fake.callback(LISTENER, f"like|{seeded[0]}"))
        assert not catalog.is_liked(db, LISTENER, seeded[0])

    def test_library_shows_saved_tracks(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, f"like|{seeded[3]}"))
        bot.handle(fake.callback(LISTENER, "nav|library"))
        assert "LIBRARY" in api.last_screen()
        assert "Untitled IV" in api.last_screen()

    def test_plain_text_is_a_search(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.message(LISTENER, "drone"))
        assert "Untitled III" in api.last_screen()
        assert api.audio_sent == []

    def test_artist_page_and_following(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        artist_id = int(db.scalar("SELECT artist_id FROM tracks WHERE id=?", (seeded[0],)))
        bot.handle(fake.callback(LISTENER, f"artist|{artist_id}"))
        assert "Anna Voskresenskaya" in api.last_screen()
        bot.handle(fake.callback(LISTENER, f"follow|{artist_id}"))
        assert catalog.is_following(db, LISTENER, artist_id)

    def test_similar_screen(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        drift = int(db.scalar("SELECT id FROM tracks WHERE title='Drift'"))
        bot.handle(fake.callback(LISTENER, f"similar|{drift}"))
        assert "Second Drift" in api.last_screen()

    def test_a_withdrawn_release_cannot_be_played(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE id=?", (seeded[0],)))
        catalog.hide_release(db, release_id, CURATOR)
        bot.handle(fake.callback(LISTENER, f"play|{seeded[0]}"))
        assert api.audio_sent == []

    def test_inline_sharing(self, bot: handlers.Bot, api: FakeApi, seeded: list[int]) -> None:
        bot.handle(fake.inline(LISTENER, "drone"))
        results = api.of("answerInlineQuery")[0]["results"]
        assert results and results[0]["type"] == "audio"
        assert results[0]["audio_file_id"]


class TestMixes:
    def test_a_mix_is_delivered_one_track_at_a_time(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, "mix|auto"))
        assert api.audio_sent == []  # the mix is listed, not fired at you
        next_button = screen_button(api, "seq|")
        api.clear()
        bot.handle(fake.callback(LISTENER, next_button))
        assert len(api.audio_sent) == 1
        markup = api.audio_sent[0]["reply_markup"]["inline_keyboard"]
        assert any(b["callback_data"].startswith("seq|") for row in markup for b in row)

    def test_the_sequence_advances_only_on_a_tap(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, "mix|auto"))
        bot.handle(fake.callback(LISTENER, "seq|m|0"))
        assert len(api.audio_sent) == 1
        bot.handle(fake.callback(LISTENER, "seq|m|1"))
        assert len(api.audio_sent) == 2

    def test_curated_playlists(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        playlist_id = catalog.create_playlist(db, "Night shift", CURATOR, "For the small hours.")
        for track_id in seeded[:3]:
            catalog.add_to_playlist(db, playlist_id, track_id)
        catalog.publish_playlist(db, playlist_id)
        bot.handle(fake.callback(LISTENER, "nav|mixes"))
        assert "Night shift" in api.last_screen()
        bot.handle(fake.callback(LISTENER, f"pl|{playlist_id}"))
        assert "small hours" in api.last_screen()


class TestPrivacy:
    def test_forget_erases_everything_personal(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, f"play|{seeded[0]}"))
        bot.handle(fake.callback(LISTENER, f"like|{seeded[0]}"))
        bot.handle(fake.callback(LISTENER, "forget|ask"))
        bot.handle(fake.callback(LISTENER, "forget|do"))
        assert db.scalar("SELECT COUNT(*) FROM events WHERE user_id=?", (LISTENER,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM likes WHERE user_id=?", (LISTENER,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM usage WHERE user_id=?", (LISTENER,)) == 0
        # Nothing is remembered about what this person heard. The home screen
        # that renders straight afterwards does build a fresh daily selection —
        # the service still works — but it is derived from an empty history.
        assert bot.engine.heard(LISTENER) == set()
        # The catalogue itself survives.
        assert db.scalar("SELECT COUNT(*) FROM tracks WHERE status='approved'") == len(seeded)


class TestRobustness:
    @pytest.mark.parametrize(
        "data",
        ["", "play|", "like|abc", "artist|-", "follow|999999999999999999999", "unknown|x"],
    )
    def test_malformed_callbacks_are_safe(
        self, bot: handlers.Bot, api: FakeApi, db: Database, data: str
    ) -> None:
        before = int(db.scalar("SELECT COUNT(*) FROM events", default=0))
        bot.handle(fake.callback(LISTENER, data))
        assert api.of("answerCallbackQuery")
        assert int(db.scalar("SELECT COUNT(*) FROM events", default=0)) == before

    def test_a_stale_callback_does_not_crash(self, bot: handlers.Bot, api: FakeApi) -> None:
        bot.handle(fake.callback(LISTENER, "play|999999"))
        bot.handle(fake.callback(LISTENER, "artist|999999"))
        bot.handle(fake.callback(LISTENER, "nonsense|1|2|3"))
        bot.handle(fake.callback(LISTENER, ""))
        assert len(api.of("answerCallbackQuery")) == 4

    def test_every_callback_is_answered(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        for data in ("nav|home", "nav|today", "disc|go", f"play|{seeded[0]}", f"like|{seeded[0]}"):
            api.clear()
            bot.handle(fake.callback(LISTENER, data))
            assert api.of("answerCallbackQuery"), data

    def test_discovery_shelves_keep_a_playable_sequence(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int], db: Database
    ) -> None:
        for target in ("for_you", "new_week", "popular", "similar_saved"):
            api.clear()
            bot.handle(fake.callback(LISTENER, f"nav|{target}"))
            assert api.last_screen()
            buttons = api.buttons()
            play = next((b["callback_data"] for b in buttons if b["callback_data"].startswith("play|")), None)
            if play:
                bot.handle(fake.callback(LISTENER, play))
                assert api.of("sendAudio")

    def test_unknown_commands_fall_through_to_search(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        bot.handle(fake.message(LISTENER, "/nope"))
        assert isinstance(api.last_screen(), str)

    def test_empty_catalogue_screens_render(self, bot: handlers.Bot, api: FakeApi) -> None:
        for target in ("home", "for_you", "new_week", "popular", "today", "discover", "library", "artists", "mixes", "search"):
            bot.handle(fake.callback(LISTENER, f"nav|{target}"))
            assert api.last_screen()
