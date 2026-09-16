"""Product invariants.

These are not implementation tests. Each one pins a promise made in the README
that could otherwise be eroded by a well-meaning change. If a future commit
makes one of these fail, the commit is changing what this product *is*, and it
should have to say so out loud.
"""

from __future__ import annotations

import re
from pathlib import Path

from tonearm import catalog, handlers, i18n, recommend, ui
from tonearm.config import Config
from tonearm.db import Database

from . import fake
from .conftest import ARTIST, CURATOR, LISTENER
from .fake import FakeApi, id3_file

SOURCE = Path(__file__).resolve().parents[1] / "tonearm"


class TestNothingPlaysItself:
    def test_no_audio_is_ever_sent_without_an_explicit_tap(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        """Walk the entire interface. Only play/seq taps may produce audio."""
        browsing = [
            "/start",
            "/today",
            "/discover",
            "/library",
            "/settings",
            "/help",
            "/submit",
            "drone",
            "ambient",
        ]
        for text in browsing:
            bot.handle(fake.message(LISTENER, text))
        for data in (
            "nav|home",
            "nav|today",
            "nav|discover",
            "disc|go",
            "nav|search",
            "nav|library",
            "nav|artists",
            "nav|mixes",
            "mix|auto",
            "nav|settings",
            "nav|help",
            f"like|{seeded[0]}",
            f"similar|{seeded[1]}",
            "tag|drone",
        ):
            bot.handle(fake.callback(LISTENER, data))
        assert api.audio_sent == [], "something sent audio without being asked"

    def test_one_tap_yields_exactly_one_track(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        for track_id in seeded[:4]:
            bot.handle(fake.callback(LISTENER, f"play|{track_id}"))
        assert len(api.audio_sent) == 4

    def test_a_track_card_never_offers_an_endless_continuation(
        self, bot: handlers.Bot, api: FakeApi, seeded: list[int]
    ) -> None:
        """Outside a finite sequence there is no Next button at all."""
        bot.handle(fake.callback(LISTENER, f"play|{seeded[0]}"))
        markup = api.audio_sent[0]["reply_markup"]["inline_keyboard"]
        assert not any(b["callback_data"].startswith("seq|") for row in markup for b in row)

    def test_a_sequence_ends(self, bot: handlers.Bot, api: FakeApi, seeded: list[int]) -> None:
        bot.handle(fake.callback(LISTENER, "mix|auto"))
        length = len(bot._data(catalog.get_user(bot.db, LISTENER))["seq"])
        for index in range(length):
            bot.handle(fake.callback(LISTENER, f"seq|m|{index}"))
        last = api.audio_sent[-1]["reply_markup"]["inline_keyboard"]
        assert not any(b["callback_data"].startswith("seq|") for row in last for b in row)


class TestFiniteByDesign:
    def test_the_daily_selection_cannot_be_re_rolled(
        self, bot: handlers.Bot, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        first = engine.daily_selection(LISTENER)
        for _ in range(5):
            bot.handle(fake.callback(LISTENER, "nav|today"))
        assert engine.daily_selection(LISTENER) == first

    def test_today_ends_with_a_boundary_not_with_more(
        self, bot: handlers.Bot, api: FakeApi, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        for track_id in engine.daily_selection(LISTENER):
            bot.handle(fake.callback(LISTENER, f"play|{track_id}"))
        api.clear()
        bot.handle(fake.callback(LISTENER, "nav|today"))
        text = api.last_screen()
        assert "That is all for today" in text
        assert not api.find_button("play|")

    def test_discovery_is_capped_per_day(
        self, bot: handlers.Bot, api: FakeApi, db: Database, config: Config, seeded: list[int]
    ) -> None:
        for _ in range(config.limits.discover_sessions_per_day + 3):
            bot.handle(fake.callback(LISTENER, "disc|go"))
        used = db.scalar(
            "SELECT discovers FROM usage WHERE user_id=? AND day=?",
            (LISTENER, recommend.today()),
        )
        assert used == config.limits.discover_sessions_per_day
        assert "used today" in api.last_screen()

    def test_a_discovery_batch_is_small(
        self, bot: handlers.Bot, api: FakeApi, config: Config, seeded: list[int]
    ) -> None:
        bot.handle(fake.callback(LISTENER, "disc|go"))
        plays = [b for b in api.buttons() if b["callback_data"].startswith("play|")]
        assert len(plays) <= config.limits.discover_batch


class TestNoEngagementTheatre:
    def test_listeners_never_see_play_counts_or_like_counts(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        db.execute("UPDATE tracks SET plays=4242, likes=777, exposures=9999")
        for data in ("nav|home", "nav|today", "nav|library", "nav|artists", "nav|mixes"):
            bot.handle(fake.callback(LISTENER, data))
            assert "4242" not in api.last_screen()
            assert "777" not in api.last_screen()
        bot.handle(fake.callback(LISTENER, f"play|{seeded[0]}"))
        assert "4242" not in api.audio_sent[0]["caption"]

    def test_an_artist_sees_their_own_numbers(
        self, bot: handlers.Bot, api: FakeApi, db: Database, seeded: list[int]
    ) -> None:
        artist_id = int(db.scalar("SELECT artist_id FROM tracks WHERE id=?", (seeded[0],)))
        db.execute("UPDATE artists SET user_id=? WHERE id=?", (ARTIST, artist_id))
        db.execute("UPDATE tracks SET requests=12 WHERE artist_id=?", (artist_id,))
        bot.handle(fake.callback(ARTIST, f"artist|{artist_id}"))
        assert "24 requests" in api.last_screen()  # two tracks by this artist

    def test_no_streaks_badges_or_urgency_in_any_string(self) -> None:
        banned = re.compile(
            r"\b(streak|badge|level up|don'?t miss|hurry|last chance|only today|"
            r"trending now|going viral|you'?re on fire)\b",
            re.IGNORECASE,
        )
        for key, entry in i18n.STRINGS.items():
            for lang, text in entry.items():
                assert not banned.search(text), f"{key}/{lang}: {text!r}"

    def test_no_emoji_in_the_interface(self) -> None:
        for key, entry in i18n.STRINGS.items():
            for lang, text in entry.items():
                for char in text:
                    assert not (0x1F000 <= ord(char) <= 0x1FAFF), f"{key}/{lang}: {char!r}"


class TestOnlySolicitedMessages:
    def test_the_bot_never_writes_first_except_about_your_own_submission(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(fake.message(LISTENER, "/start"))
        bot.handle(
            fake.audio_message(ARTIST, "z1", "zz1", title="Mine", performer="A", thumbnail=True)
        )
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='zz1'"))
        api.clear()
        bot.handle(fake.callback(CURATOR, f"rel|ok|{release_id}"))
        recipients = {p["chat_id"] for p in api.of("sendMessage")}
        # Publishing reaches the artist who submitted it and nobody else.
        assert LISTENER not in recipients
        assert ARTIST in recipients

    def test_the_weekly_note_is_opt_in(self, db: Database) -> None:
        assert db.scalar("SELECT COUNT(*) FROM users WHERE digest=1", default=0) == 0
        row = db.one("PRAGMA table_info(users)")
        assert row is not None  # schema exists; default for `digest` is 0
        columns = {r["name"]: r["dflt_value"] for r in db.query("PRAGMA table_info(users)")}
        assert columns["digest"] == "0"


class TestTheReleaseIsTheUnit:
    """A single track is never accepted, declined or withdrawn on its own.

    The unit an artist submits is the unit a curator decides on. Anything finer
    means a curator editing somebody's record.
    """

    def test_the_core_offers_no_per_track_decision(self) -> None:
        for name in ("approve", "reject", "hide", "restore_to_queue"):
            assert not hasattr(catalog, name), f"catalog.{name} decides one track"
        for name in ("approve_release", "reject_release", "hide_release", "restore_release"):
            assert hasattr(catalog, name)

    def test_declining_takes_the_whole_release_down(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        for index in range(2):
            api.files[f"w{index}"] = id3_file(title=f"T{index}", artist="Marsh", album="Set")
            bot.handle(
                fake.audio_message(
                    ARTIST, f"w{index}", f"wu{index}", thumbnail=True, file_name=f"{index}.mp3"
                )
            )
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Set'"))
        bot.handle(fake.callback(CURATOR, f"rel|ok|{release_id}"))
        bot.handle(fake.callback(CURATOR, f"rel|no|{release_id}"))
        bot.handle(fake.callback(CURATOR, f"rel|no0|{release_id}"))
        statuses = {
            row["status"]
            for row in db.query("SELECT status FROM tracks WHERE release_id=?", (release_id,))
        }
        assert statuses == {"rejected"}, "a declined release kept live tracks"

    def test_withdrawing_takes_the_whole_release_down(
        self, db: Database, seeded: list[int]
    ) -> None:
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE id=?", (seeded[0],)))
        catalog.hide_release(db, release_id, CURATOR)
        statuses = {
            row["status"]
            for row in db.query("SELECT status FROM tracks WHERE release_id=?", (release_id,))
        }
        assert statuses == {"hidden"}


class TestMeasurementsDoNotJudge:
    """Numbers feed the ranking. They never grade a submission.

    An earlier version put "quality flags" — low bitrate, dull top end,
    over-compression, mono — on the review card. Most of those describe an
    aesthetic rather than a defect: a cassette, a lo-fi mix or a field recording
    on a cheap microphone trip all of them, so on a station for niche music the
    warnings fired hardest on exactly the material the station exists for.
    Whether a record belongs here is decided by ear.
    """

    def test_the_analyser_returns_measurements_only(self) -> None:
        from tonearm import audio as audio_mod

        assert not hasattr(audio_mod, "quality_flags")
        assert not hasattr(audio_mod, "describe_features")

    def test_no_verdict_reaches_a_curator(
        self, bot: handlers.Bot, api: FakeApi, db: Database
    ) -> None:
        bot.handle(
            fake.audio_message(ARTIST, "j1", "jj1", title="Rough", performer="A", thumbnail=True)
        )
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE file_unique_id='jj1'"))
        # Pretend the file measured badly by every old rule.
        db.execute(
            "UPDATE tracks SET features=? WHERE release_id=?",
            (
                '{"bitrate": 64000, "codec": "flac", "cutoff_hz": 9000.0, '
                '"crest": 1.2, "channels": 1, "lufs": -3.0, "clip_ratio": 0.4}',
                release_id,
            ),
        )
        api.clear()
        bot.handle(fake.callback(CURATOR, f"rev|{release_id}"))
        card = api.last_screen() + " ".join(
            str(p.get("caption") or "") for p in api.of("sendPhoto")
        )
        for word in ("bitrate", "битрейт", "LUFS", "crest", "mono", "моно", "cutoff", "срез"):
            assert word not in card, f"the review card still grades the file: {word}"

    def test_the_measurements_are_still_used_for_ranking(
        self, engine: recommend.Engine, seeded: list[int]
    ) -> None:
        tokens = engine.tokens(seeded[0])
        assert any(token.startswith("~tempo:") for token in tokens)
        assert any(token.startswith("~tone:") for token in tokens)


class TestCurationIsMandatory:
    def test_auto_approve_is_off_by_default(self) -> None:
        assert Config(home=Path(".")).auto_approve is False

    def test_a_submission_is_invisible_until_a_human_acts(
        self, bot: handlers.Bot, engine: recommend.Engine, db: Database
    ) -> None:
        bot.handle(fake.audio_message(ARTIST, "q1", "qq1", title="Unreviewed", performer="A"))
        assert engine.eligible() == []
        assert engine.daily_selection(LISTENER) == []
        from tonearm import search as search_mod

        assert search_mod.search(db, "Unreviewed") == []


def site_directories() -> set:
    """Every directory into which a third-party package could be installed.

    Only entries actually named ``site-packages`` or ``dist-packages`` count.
    ``site.getsitepackages()`` on Windows also returns the interpreter prefix
    itself, and treating that as "installed" would classify the whole standard
    library as third-party.
    """
    import site
    import sysconfig

    candidates = [
        *(getattr(site, "getsitepackages", list)() or []),
        getattr(site, "getusersitepackages", str)() or "",
        sysconfig.get_paths().get("purelib", ""),
        sysconfig.get_paths().get("platlib", ""),
    ]
    return {
        Path(directory).resolve()
        for directory in candidates
        if directory and Path(directory).name in ("site-packages", "dist-packages")
    }


def is_installed_package(origin: Path, site_dirs: set) -> bool:
    """True when a module's file lives inside an install directory."""
    origin = Path(origin)
    return any(directory in origin.parents for directory in site_dirs)


class TestSourceHygiene:
    def test_the_dependency_check_classifies_paths_correctly(self) -> None:
        """Guard the guard, including the layouts CI runs on.

        Windows keeps compiled standard modules in ``DLLs\\`` rather than
        ``Lib\\``, so "is it under the stdlib path" is the wrong question;
        "is it under site-packages" is the right one. Both layouts are checked
        here so the real test below cannot pass for the wrong reason.
        """
        posix = {Path("/usr/lib/python3.12/site-packages")}
        assert is_installed_package(
            Path("/usr/lib/python3.12/site-packages/pytest/__init__.py"), posix
        )
        assert not is_installed_package(Path("/usr/lib/python3.12/json/__init__.py"), posix)

        windows = {Path("C:/Python312/Lib/site-packages")}
        assert is_installed_package(
            Path("C:/Python312/Lib/site-packages/pytest/__init__.py"), windows
        )
        assert not is_installed_package(Path("C:/Python312/DLLs/unicodedata.pyd"), windows)
        assert not is_installed_package(Path("C:/Python312/Lib/logging/__init__.py"), windows)

        # The interpreter prefix must never be treated as an install directory.
        assert Path("C:/Python312") not in site_directories()

    def test_no_module_imports_a_third_party_package(self) -> None:
        """The install story is "clone and run". Keep it that way.

        Rather than maintain a list of blessed module names, resolve every
        import and require that it does not come from an install directory. A
        new standard-library import needs no change here; a dependency fails
        immediately.
        """
        import importlib.util
        import sys

        site_dirs = site_directories()
        assert site_dirs, "could not locate site-packages; the check would be vacuous"
        imports = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

        for path in sorted(SOURCE.glob("*.py")):
            for match in imports.finditer(path.read_text(encoding="utf-8")):
                root = match.group(1).split(".")[0]
                if root in ("tonearm", "__future__") or root in sys.builtin_module_names:
                    continue
                spec = importlib.util.find_spec(root)
                assert spec is not None, f"{path.name} imports unresolvable {root}"
                if spec.origin in (None, "built-in", "frozen"):
                    continue
                origin = Path(spec.origin).resolve()
                assert not is_installed_package(origin, site_dirs), (
                    f"{path.name} imports {root}, which is an installed "
                    f"package rather than part of the standard library ({origin})"
                )

    def test_the_check_would_catch_a_real_dependency(self) -> None:
        """pytest is genuinely installed, so it must be classified as such."""
        import importlib.util

        spec = importlib.util.find_spec("pytest")
        assert spec and spec.origin
        assert is_installed_package(Path(spec.origin).resolve(), site_directories())

    def test_callback_payloads_fit_telegram_s_limit(self) -> None:
        # 64 bytes is a hard API limit; long ids and tags are the usual way to
        # blow past it unnoticed.
        assert len(ui.pack("mod", "ok", 9_999_999_999).encode()) <= 64
        assert len(ui.pack("tag", "a" * 40).encode()) <= 64
        try:
            ui.pack("tag", "x" * 200)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("oversized callback data was accepted")
