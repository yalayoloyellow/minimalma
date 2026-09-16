"""Configuration, storage, translations and the command line."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from minimalma import VERSION_LABEL, app, catalog, cli, i18n, recommend, ui
from minimalma import config as config_mod
from minimalma.config import Config
from minimalma.db import SCHEMA_VERSION, Database

from . import fake
from .conftest import ARTIST, LISTENER
from .fake import FakeApi


class TestConfig:
    def test_defaults_are_the_conservative_ones(self, tmp_path: Path) -> None:
        cfg = Config(home=tmp_path)
        assert cfg.auto_approve is False
        assert cfg.limits.daily_selection == 5
        assert cfg.limits.discover_sessions_per_day == 3

    def test_round_trip(self, tmp_path: Path) -> None:
        cfg = Config(home=tmp_path)
        cfg.token = "123:abc"
        cfg.owner = 7
        cfg.curators = [8, 9]
        cfg.limits.daily_selection = 3
        cfg.weights.exploration = 0.9
        cfg.save()
        loaded = config_mod.load(tmp_path)
        assert loaded.token == "123:abc"
        assert loaded.owner == 7
        assert loaded.curators == [8, 9]
        assert loaded.limits.daily_selection == 3
        assert loaded.weights.exploration == 0.9

    def test_the_secret_file_is_not_world_readable(self, tmp_path: Path) -> None:
        cfg = Config(home=tmp_path)
        cfg.token = "123:abc"
        cfg.save()
        if os.name != "nt":
            assert cfg.config_path.stat().st_mode & 0o077 == 0

    def test_environment_overrides_the_stored_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        Config(home=tmp_path).save()
        monkeypatch.setenv(config_mod.TOKEN_ENV, "999:zzz")
        assert config_mod.load(tmp_path).token == "999:zzz"

    def test_junk_values_are_ignored_rather_than_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text(
            json.dumps({"token": "1:a", "limits": {"daily_selection": "nonsense", "bogus": 1}})
        )
        cfg = config_mod.load(tmp_path)
        assert cfg.limits.daily_selection == 5

    def test_broken_json_is_reported_clearly(self, tmp_path: Path) -> None:
        (tmp_path / "config.json").write_text("{not json")
        with pytest.raises(config_mod.ConfigError):
            config_mod.load(tmp_path)

    def test_curator_predicates(self, tmp_path: Path) -> None:
        cfg = Config(home=tmp_path, owner=1, curators=[2])
        assert cfg.is_curator(1) and cfg.is_curator(2)
        assert not cfg.is_curator(3)
        assert cfg.is_owner(1) and not cfg.is_owner(2)

    def test_home_is_platform_specific(self) -> None:
        assert "minimalma" in str(config_mod.default_home()).lower()


class TestDatabase:
    def test_migrations_are_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "x.db"
        Database(path)
        second = Database(path)
        assert second.scalar("PRAGMA user_version") == SCHEMA_VERSION
        assert second.integrity() == "ok"

    def test_a_newer_schema_is_refused_rather_than_corrupted(self, tmp_path: Path) -> None:
        path = tmp_path / "future.db"
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA user_version=99")
        connection.close()
        with pytest.raises(RuntimeError, match="newer"):
            Database(path)

    def test_wal_and_foreign_keys_are_on(self, db: Database) -> None:
        assert db.scalar("PRAGMA journal_mode").lower() == "wal"
        assert db.scalar("PRAGMA foreign_keys") == 1

    def test_deleting_a_track_cascades(self, db: Database, seeded: list[int]) -> None:
        db.execute("DELETE FROM tracks WHERE id=?", (seeded[0],))
        assert db.scalar("SELECT COUNT(*) FROM track_tags WHERE track_id=?", (seeded[0],)) == 0

    def test_meta_round_trip(self, db: Database) -> None:
        db.set_meta("k", {"a": [1, 2]})
        assert db.get_meta("k") == {"a": [1, 2]}
        assert db.get_meta("missing", "fallback") == "fallback"

    def test_backup_is_a_usable_database(
        self, db: Database, seeded: list[int], tmp_path: Path
    ) -> None:
        target = db.backup_to(tmp_path / "copy.db")
        copy = Database(target)
        assert copy.stats()["approved"] == len(seeded)

    def test_a_failed_transaction_rolls_back(self, db: Database) -> None:
        with pytest.raises(sqlite3.IntegrityError), db.transaction() as conn:
            conn.execute("INSERT INTO artists(name, key, created_at) VALUES('A','dupe',1)")
            conn.execute("INSERT INTO artists(name, key, created_at) VALUES('B','dupe',1)")
        assert db.scalar("SELECT COUNT(*) FROM artists WHERE key='dupe'") == 0

    def test_concurrent_writers_do_not_corrupt_anything(self, db: Database) -> None:
        errors: list[Exception] = []

        def writer(index: int) -> None:
            try:
                for step in range(25):
                    db.execute(
                        "INSERT INTO events(user_id, track_id, kind, ts, weight) VALUES(?,?,?,?,?)",
                        (index, None, "play", step, 1.0),
                    )
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert db.scalar("SELECT COUNT(*) FROM events") == 150
        assert db.integrity() == "ok"


class TestTranslations:
    def test_every_string_exists_in_every_language(self) -> None:
        assert i18n.missing() == {}

    def test_placeholders_match_across_languages(self) -> None:
        import re

        pattern = re.compile(r"\{(\w+)\}")
        for key, entry in i18n.STRINGS.items():
            expected = set(pattern.findall(entry["en"]))
            for lang, text in entry.items():
                assert set(pattern.findall(text)) == expected, f"{key}/{lang}"

    def test_lookup_falls_back_instead_of_raising(self) -> None:
        assert i18n.t("de", "nav.home") == "Home"
        assert i18n.t("en", "no.such.key") == "no.such.key"
        assert i18n.t("en", "home.catalogue") == i18n.t("en", "home.catalogue", wrong="arg")

    def test_normalise(self) -> None:
        assert i18n.normalise("RU") == "ru"
        assert i18n.normalise("pt-BR") == "en"
        assert i18n.normalise("") == "en"

    def test_no_html_is_left_unclosed(self) -> None:
        import re

        for key, entry in i18n.STRINGS.items():
            for lang, text in entry.items():
                opened = re.findall(r"<(\w+)>", text)
                closed = re.findall(r"</(\w+)>", text)
                assert sorted(opened) == sorted(closed), f"{key}/{lang}"


class TestRendering:
    def test_html_in_a_track_title_is_escaped(self) -> None:
        caption = ui.track_caption(
            {"title": "<b>evil</b> & co", "artist": "A", "duration": 60, "tags": []}, "en"
        )
        assert "&lt;b&gt;evil&lt;/b&gt; &amp; co" in caption
        assert "<b>evil</b>" not in caption

    def test_captions_respect_the_api_limit(self) -> None:
        caption = ui.track_caption(
            {
                "title": "T" * 500,
                "artist": "A" * 500,
                "duration": 1,
                "note": "N" * 2000,
                "tags": ["x"] * 50,
            },
            "en",
        )
        assert len(caption) < 1024

    def test_duration_formatting(self) -> None:
        assert ui.hms(0) == "0:00"
        assert ui.hms(61) == "1:01"
        assert ui.hms(3661) == "1:01:01"
        assert ui.hms(None) == "0:00"
        assert ui.hms("nonsense") == "0:00"

    def test_callback_round_trip(self) -> None:
        assert ui.unpack(ui.pack("mod", "ok", 12)) == ["mod", "ok", "12"]


class TestCommandLine:
    def _home(self, tmp_path: Path) -> list[str]:
        return ["--home", str(tmp_path)]

    def test_version(self, capsys: pytest.CaptureFixture) -> None:
        with pytest.raises(SystemExit):
            cli.main(["--version"])
        assert VERSION_LABEL in capsys.readouterr().out

    def test_no_arguments_prints_help_without_the_desk(
        self, capsys: pytest.CaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_desk_available", lambda: False)
        assert cli.main(self._home(tmp_path)) == 0
        assert "usage" in capsys.readouterr().out

    def test_no_arguments_opens_the_desk_when_it_is_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened: list = []
        monkeypatch.setattr(cli, "_desk_available", lambda: True)
        monkeypatch.setattr(cli, "_desk", lambda args, cfg: opened.append(args.command) or 0)
        assert cli.main(self._home(tmp_path)) == 0
        assert opened == ["desk"]

    def test_run_without_a_token_fails_loudly(
        self, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        assert cli.main([*self._home(tmp_path), "run"]) == 2
        assert "minimalma" in capsys.readouterr().err

    def test_curator_management(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        assert cli.main([*self._home(tmp_path), "curator", "add", "42"]) == 0
        assert config_mod.load(tmp_path).is_curator(42)
        cli.main([*self._home(tmp_path), "curator", "add", "43"])
        cli.main([*self._home(tmp_path), "curator", "list"])
        assert "43" in capsys.readouterr().out
        cli.main([*self._home(tmp_path), "curator", "remove", "43"])
        assert not config_mod.load(tmp_path).is_curator(43)

    def test_doctor_reports_without_a_token(
        self, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        assert cli.main([*self._home(tmp_path), "doctor"]) == 0
        out = capsys.readouterr().out
        assert "MISSING" in out
        assert "integrity      ok" in out
        assert "nothing can be published" in out

    def test_export_writes_the_catalogue(
        self, capsys: pytest.CaptureFixture, tmp_path: Path
    ) -> None:
        cfg = Config(home=tmp_path)
        cfg.save()
        db = Database(cfg.database_path)
        artist_id = catalog.get_or_create_artist(db, "Exported Artist")
        db.execute(
            "INSERT INTO tracks(artist_id, title, key, duration, file_id, file_unique_id, "
            "status, submitted_at, published_at) VALUES(?,?,?,?,?,?,'approved',1,1)",
            (artist_id, "Exported Track", "k", 100, "f", "u"),
        )
        db.close()
        assert cli.main([*self._home(tmp_path), "export"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tracks"][0]["title"] == "Exported Track"

    def test_backup(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        assert cli.main([*self._home(tmp_path), "backup", str(tmp_path / "b.db")]) == 0
        assert (tmp_path / "b.db").exists()

    def test_setup_is_non_interactive_with_flags(
        self, capsys: pytest.CaptureFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.Api, "me", lambda self: {"username": "b", "first_name": "B"})
        code = cli.main(
            [
                *self._home(tmp_path),
                "setup",
                "--token",
                "1:x",
                "--owner",
                "5",
                "--review-chat",
                "-100",
                "--name",
                "Night Radio",
            ]
        )
        assert code == 0
        cfg = config_mod.load(tmp_path)
        assert cfg.token == "1:x" and cfg.owner == 5 and cfg.station_name == "Night Radio"
        assert cfg.database_path.exists()

    def test_setup_rejects_a_bad_token(self, capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
        assert cli.main([*self._home(tmp_path), "setup", "--token", "garbage"]) == 2
        assert "token" in capsys.readouterr().err


class TestServiceLoop:
    def test_repeated_poll_conflicts_stop_the_worker(
        self, config: Config, db: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from minimalma.telegram import TelegramError

        class ConflictApi(FakeApi):
            def get_updates(self, *args, **kwargs):
                raise TelegramError("getUpdates", 409, "Conflict")

        service = app.Service(config, api=ConflictApi(), db=db)
        monkeypatch.setattr(service._stop, "wait", lambda _seconds: None)
        service._poll()
        assert service._stop.is_set()

    def test_run_token_override_is_not_persisted(self, tmp_path: Path, monkeypatch) -> None:
        parser = cli._parser()
        args = parser.parse_args(["run", "--token", "1:one-time"])
        cfg = Config(home=tmp_path)
        assert args.token == "1:one-time"
        class StopService:
            def __init__(self, cfg):
                pass

            def run(self):
                raise KeyboardInterrupt()

        monkeypatch.setattr(cli, "Service", StopService)
        assert cli._run(args, cfg) == 0
        assert cfg.token == "1:one-time"
        assert not cfg.config_path.exists()

    def test_desk_token_override_is_not_persisted(self, tmp_path: Path, monkeypatch) -> None:
        import desk.launch

        parser = cli._parser()
        args = parser.parse_args(["desk", "--token", "1:one-time", "--no-bot", "--browser"])
        cfg = Config(home=tmp_path)
        monkeypatch.setattr(desk.launch, "open_desk", lambda *args, **kwargs: 0)
        assert cli._desk(args, cfg) == 0
        assert cfg.token == "1:one-time"
        assert not cfg.config_path.exists()

    def test_event_retention_removes_only_old_interactions(self, db: Database) -> None:
        old = int(time.time()) - 200 * 86400
        db.execute(
            "INSERT INTO events(user_id, track_id, kind, ts, weight) VALUES(?,?,?,?,?)",
            (1, None, "play", old, 1.0),
        )
        db.execute(
            "INSERT INTO events(user_id, track_id, kind, ts, weight) VALUES(?,?,?,?,?)",
            (1, None, "play", int(time.time()), 1.0),
        )
        assert recommend.prune_events(db) == 1
        assert db.scalar("SELECT COUNT(*) FROM events") == 1

    def test_updates_are_sharded_by_chat(self) -> None:
        first = app._shard_for(fake.message(111, "a"))
        second = app._shard_for(fake.message(111, "b"))
        assert first == second == 111
        assert app._shard_for(fake.callback(222, "nav|home")) == 222
        assert app._shard_for(fake.inline(333, "q")) == 333
        assert app._shard_for({"update_id": 1}) == 0

    def test_the_weekly_note_only_reaches_subscribers(
        self, config: Config, db: Database, api: FakeApi, seeded: list[int]
    ) -> None:
        service = app.Service(config, api=api, db=db)
        catalog.ensure_user(db, fake.user(LISTENER))
        catalog.ensure_user(db, fake.user(ARTIST))
        catalog.set_user(db, LISTENER, digest=1)
        sent = service.send_digest(force=True)
        assert sent == 1
        recipients = {p["chat_id"] for p in api.of("sendMessage")}
        assert recipients == {LISTENER}

    def test_the_weekly_note_is_sent_once_per_week(
        self, config: Config, db: Database, api: FakeApi, seeded: list[int]
    ) -> None:
        service = app.Service(config, api=api, db=db)
        catalog.ensure_user(db, fake.user(LISTENER))
        catalog.set_user(db, LISTENER, digest=1)
        service.send_digest(force=True)
        api.clear()
        assert service.send_digest(force=False) == 0

    def test_startup_clears_a_webhook_and_registers_commands(
        self, config: Config, db: Database, api: FakeApi
    ) -> None:
        service = app.Service(config, api=api, db=db)
        service.start()
        assert api.of("deleteWebhook")
        languages = {p["language_code"] for p in api.of("setMyCommands")}
        assert languages == {"en", "ru"}
