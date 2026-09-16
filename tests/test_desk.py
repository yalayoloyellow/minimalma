"""The curation desk, driven over real HTTP against a real server socket.

Range handling, the session key and path traversal are exactly the things a
mocked handler would not catch, so nothing here is mocked below the socket.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tonearm import catalog
from tonearm.config import Config
from tonearm.db import Database

from . import fake
from .conftest import ARTIST
from .fake import FakeApi, id3_file

desk_module = pytest.importorskip("desk.server")


@pytest.fixture()
def running(config: Config, db: Database, api: FakeApi):
    """A live desk server on a free loopback port."""
    desk = desk_module.Desk(config, db=db, api=api)
    httpd, desk, url = desk_module.serve(config, port=0, desk=desk)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = url.split("/?")[0]
    try:
        yield desk, base, desk.key
    finally:
        httpd.shutdown()
        httpd.server_close()


def _url(base: str, path: str, key: str) -> str:
    return f"{base}{path}{'&' if '?' in path else '?'}k={key}"


def get(base: str, path: str, key: str, headers: dict = None):
    request = urllib.request.Request(_url(base, path, key), headers=headers or {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.headers, response.read()


def post(base: str, path: str, key: str, body: dict):
    request = urllib.request.Request(
        _url(base, path, key),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def api_get(base: str, path: str, key: str) -> dict:
    return json.loads(get(base, path, key)[2])


class TestAuth:
    def test_a_wrong_key_is_refused(self, running) -> None:
        _desk, base, _key = running
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, "/api/state", "not-the-key")
        assert caught.value.code == 403

    def test_a_missing_key_is_refused(self, running) -> None:
        _desk, base, _key = running
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"{base}/api/state", timeout=5)
        assert caught.value.code == 403

    def test_the_key_also_works_as_a_header(self, running) -> None:
        _desk, base, key = running
        request = urllib.request.Request(f"{base}/api/state", headers={"X-Tonearm-Key": key})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200

    def test_the_page_itself_needs_no_key(self, running) -> None:
        _desk, base, _key = running
        with urllib.request.urlopen(f"{base}/", timeout=5) as response:
            assert b"minimalma" in response.read().lower()


class TestUsers:
    def test_curator_can_be_added_and_removed(self, config, db, api):
        desk = desk_module.Desk(config, db=db, api=api)
        user_id = 123456
        config.owner = 999999
        assert desk_module.api_curator(desk, str(user_id), {"action": "add"}, {})["ok"]
        assert user_id in config.curators
        assert desk_module.api_curator(desk, str(user_id), {"action": "remove"}, {})["ok"]
        assert user_id not in config.curators

    def test_user_role_changes_through_the_http_api(self, running, db: Database) -> None:
        _desk, base, key = running
        db.execute(
            "INSERT INTO users(id, created_at, last_seen, name, username) VALUES(?,?,?,?,?)",
            (123457, 1, 2, "Test User", "test_user"),
        )
        users = api_get(base, "/api/users", key)["items"]
        assert any(item["id"] == 123457 and item["role"] == "user" for item in users)
        assert post(base, "/api/curator/123457", key, {"action": "add"})["ok"]
        users = api_get(base, "/api/users", key)["items"]
        assert any(item["id"] == 123457 and item["role"] == "curator" for item in users)
        assert post(base, "/api/curator/123457", key, {"action": "remove"})["ok"]
        users = api_get(base, "/api/users", key)["items"]
        assert any(item["id"] == 123457 and item["role"] == "user" for item in users)


class TestStatic:
    def test_assets_are_served(self, running) -> None:
        _desk, base, key = running
        for name, needle in (("app.css", b"--canvas"), ("app.js", b"function render")):
            status, headers, body = get(base, f"/static/{name}", key)
            assert status == 200 and needle in body

    def test_path_traversal_is_refused(self, running) -> None:
        _desk, base, key = running
        for attempt in ("/static/../../tonearm/config.py", "/static/..%2f..%2fsetup.py"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                get(base, attempt, key)
            assert caught.value.code == 404


class TestReading:
    def test_state(self, running, seeded) -> None:
        _desk, base, key = running
        payload = api_get(base, "/api/state", key)
        assert payload["report"]["catalogue"] == len(seeded)
        assert payload["bot"]["running"] is False
        assert "ambient" in payload["tags"]

    def test_queue_is_empty_until_something_is_submitted(self, running, seeded) -> None:
        _desk, base, key = running
        assert api_get(base, "/api/queue", key)["items"] == []

    def test_queue_lists_complete_submissions(self, running, bot, api: FakeApi) -> None:
        _desk, base, key = running
        bot.handle(
            fake.audio_message(ARTIST, "d1", "dd1", title="Waiting", performer="A", thumbnail=True)
        )
        payload = api_get(base, "/api/queue", key)
        assert payload["total"] == 1
        assert payload["items"][0]["title"] == "Waiting"

    def test_track_detail(self, running, seeded) -> None:
        _desk, base, key = running
        payload = api_get(base, f"/api/track/{seeded[0]}", key)
        assert payload["title"] == "Nightpost"
        assert payload["tags"] == ["ambient", "field recording"]
        # Measurements are kept for ranking; the desk is not shown a verdict.
        assert "quality" not in payload
        assert "technical" not in payload

    def test_missing_track_is_404(self, running) -> None:
        _desk, base, key = running
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, "/api/track/999999", key)
        assert caught.value.code == 404

    def test_artists(self, running, seeded) -> None:
        _desk, base, key = running
        items = api_get(base, "/api/artists", key)["items"]
        assert any(a["name"] == "Kolm" and a["tracks"] == 2 for a in items)

    def test_artist_detail(self, running, seeded, db: Database) -> None:
        _desk, base, key = running
        artist_id = int(db.scalar("SELECT artist_id FROM tracks WHERE title='Drift'"))
        payload = api_get(base, f"/api/artist/{artist_id}", key)
        assert payload["name"] == "Nocturne"
        assert len(payload["tracks"]) == 2
        assert "followers" in payload["stats"]

    def test_unknown_endpoint_is_404(self, running) -> None:
        _desk, base, key = running
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, "/api/nonsense", key)
        assert caught.value.code == 404


class TestCuratorialFields:
    """A curator writes the tags and the note. Nothing else."""

    def _release(self, bot, api: FakeApi, unique="p1", title="Fresh") -> int:
        bot.handle(
            fake.audio_message(
                ARTIST, f"f-{unique}", unique, title=title, performer="A", thumbnail=True
            )
        )
        track_id = int(bot.db.scalar("SELECT id FROM tracks WHERE file_unique_id=?", (unique,)))
        return int(bot.db.scalar("SELECT release_id FROM tracks WHERE id=?", (track_id,)))

    def test_tags_and_note(self, running, bot, db: Database, api: FakeApi) -> None:
        _desk, base, key = running
        release_id = self._release(bot, api)
        result = post(
            base,
            f"/api/curate/{release_id}",
            key,
            {"note": "Recorded on a rooftop.", "tags": ["Ambient", "tape"]},
        )
        assert result["ok"]
        assert catalog.release_tags(db, release_id) == ["ambient", "tape"]
        assert "rooftop" in db.scalar("SELECT note FROM releases WHERE id=?", (release_id,))

    def test_tags_reach_every_track_of_the_release(
        self, running, bot, db: Database, api: FakeApi
    ) -> None:
        _desk, base, key = running
        for index in range(2):
            api.files[f"m{index}"] = id3_file(title=f"T{index}", artist="Marsh", album="Set")
            bot.handle(
                fake.audio_message(
                    ARTIST, f"m{index}", f"mu{index}", thumbnail=True, file_name=f"{index}.mp3"
                )
            )
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Set'"))
        post(base, f"/api/curate/{release_id}", key, {"tags": ["drone"]})
        for row in db.query("SELECT id FROM tracks WHERE release_id=?", (release_id,)):
            assert catalog.tags_of(db, int(row["id"])) == ["drone"]

    def test_there_is_no_endpoint_for_rewriting_a_release(self, running, bot, api: FakeApi) -> None:
        _desk, base, key = running
        release_id = self._release(bot, api, "p9")
        for path in (
            f"/api/edit_release/{release_id}",
            f"/api/setcover/{release_id}",
            f"/api/edit/{release_id}",
            f"/api/approve/{release_id}",
        ):
            with pytest.raises(urllib.error.HTTPError) as caught:
                post(base, path, key, {"title": "Renamed"})
            assert caught.value.code == 404

    def test_withdrawing_takes_down_the_whole_release(self, running, seeded, db: Database) -> None:
        _desk, base, key = running
        release_id = int(db.scalar("SELECT id FROM releases WHERE title='Stairwell'"))
        assert db.scalar("SELECT COUNT(*) FROM tracks WHERE release_id=?", (release_id,)) == 2
        post(base, f"/api/withdraw/{release_id}", key, {})
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "hidden"
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE release_id=? AND status='hidden'",
                (release_id,),
            )
            == 2
        )

    def test_the_catalogue_lists_releases(self, running, seeded, db: Database) -> None:
        _desk, base, key = running
        items = api_get(base, "/api/catalogue", key)["items"]
        assert any(item["title"] == "Stairwell" and item["kind"] == "ep" for item in items)

    def test_a_catalogue_search_folds_tracks_back_to_releases(self, running, seeded) -> None:
        _desk, base, key = running
        items = api_get(base, "/api/catalogue?q=drone", key)["items"]
        assert any(item["title"] == "Winterlight" for item in items)


class TestReleases:
    def _ep(self, bot, api: FakeApi) -> int:
        for index, title in enumerate(("One", "Two")):
            api.files[f"r{index}"] = id3_file(title=title, artist="Marsh", album="Harbour")
            bot.handle(
                fake.audio_message(
                    ARTIST, f"r{index}", f"ru{index}", thumbnail=True, file_name=f"{index}.mp3"
                )
            )
        return int(bot.db.scalar("SELECT id FROM releases WHERE title='Harbour'"))

    def test_the_queue_returns_releases(self, running, bot, api: FakeApi) -> None:
        _desk, base, key = running
        release_id = self._ep(bot, api)
        payload = api_get(base, "/api/queue", key)
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == release_id
        assert payload["items"][0]["kind"] == "ep"
        assert payload["items"][0]["total"] == 2

    def test_release_detail_carries_its_tracks(self, running, bot, api: FakeApi) -> None:
        _desk, base, key = running
        release_id = self._ep(bot, api)
        payload = api_get(base, f"/api/release/{release_id}", key)
        assert [track["title"] for track in payload["tracks"]] == ["One", "Two"]
        assert payload["has_cover"] is True

    def test_publishing_a_release_publishes_its_tracks(
        self, running, bot, api: FakeApi, db: Database
    ) -> None:
        _desk, base, key = running
        release_id = self._ep(bot, api)
        result = post(base, f"/api/approve_release/{release_id}", key, {"note": "Two takes."})
        assert result["ok"] and result["error"] == ""
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE release_id=? AND status='approved'",
                (release_id,),
            )
            == 2
        )

    def test_an_incomplete_release_is_not_in_the_queue(self, running, bot, db: Database) -> None:
        _desk, base, key = running
        bot.handle(fake.audio_message(ARTIST, "bare", "bareu", title="Bare", performer="A"))
        assert api_get(base, "/api/queue", key)["items"] == []
        assert api_get(base, "/api/queue", key)["total"] == 0

    def test_declining_a_release_is_reversible(
        self, running, bot, api: FakeApi, db: Database
    ) -> None:
        _desk, base, key = running
        release_id = self._ep(bot, api)
        post(base, f"/api/reject_release/{release_id}", key, {"reason": "Not yet."})
        assert db.scalar("SELECT status FROM releases WHERE id=?", (release_id,)) == "rejected"
        assert post(base, f"/api/restore_release/{release_id}", key, {})["ok"]
        assert (
            db.scalar(
                "SELECT COUNT(*) FROM tracks WHERE release_id=? AND status='pending'",
                (release_id,),
            )
            == 2
        )


class TestPlaylists:
    def test_create_add_publish_remove(self, running, seeded, db: Database) -> None:
        _desk, base, key = running
        created = post(base, "/api/playlist_new", key, {"title": "Night shift"})
        assert created["ok"]
        playlist_id = created["id"]
        post(base, f"/api/playlist_edit/{playlist_id}", key, {"add": seeded[0]})
        post(base, f"/api/playlist_edit/{playlist_id}", key, {"add": seeded[2]})
        result = post(base, f"/api/playlist_edit/{playlist_id}", key, {"published": True})
        assert len(result["playlist"]["tracks"]) == 2
        assert result["playlist"]["published"] is True
        result = post(base, f"/api/playlist_edit/{playlist_id}", key, {"remove": seeded[0]})
        assert len(result["playlist"]["tracks"]) == 1


class TestMedia:
    def test_audio_is_fetched_cached_and_ranged(self, running, seeded, api: FakeApi) -> None:
        desk, base, key = running
        payload = b"ID3" + bytes(range(256)) * 40  # 10243 bytes of stand-in audio
        api.files["file0"] = payload

        status, headers, body = get(base, f"/api/audio/{seeded[0]}", key)
        assert status == 200
        assert headers["Accept-Ranges"] == "bytes"
        assert body == payload

        # A second request must come from the cache, not from Telegram again.
        before = len(api.of("getFile"))
        get(base, f"/api/audio/{seeded[0]}", key)
        assert len(api.of("getFile")) == before

        status, headers, body = get(
            base, f"/api/audio/{seeded[0]}", key, {"Range": "bytes=100-199"}
        )
        assert status == 206
        assert headers["Content-Range"] == f"bytes 100-199/{len(payload)}"
        assert body == payload[100:200]

    def test_an_open_ended_range(self, running, seeded, api: FakeApi) -> None:
        _desk, base, key = running
        api.files["file0"] = b"0123456789"
        status, headers, body = get(base, f"/api/audio/{seeded[0]}", key, {"Range": "bytes=4-"})
        assert status == 206 and body == b"456789"

    def test_a_suffix_range(self, running, seeded, api: FakeApi) -> None:
        _desk, base, key = running
        api.files["file0"] = b"0123456789"
        status, _headers, body = get(base, f"/api/audio/{seeded[0]}", key, {"Range": "bytes=-3"})
        assert status == 206 and body == b"789"

    def test_audio_that_cannot_be_fetched_is_404(self, running, seeded) -> None:
        _desk, base, key = running
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, f"/api/audio/{seeded[1]}", key)
        assert caught.value.code == 404

    def test_cover_is_served_from_the_release(
        self, running, seeded, db: Database, api: FakeApi
    ) -> None:
        _desk, base, key = running
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE id=?", (seeded[0],)))
        catalog.set_release_cover(db, release_id, "cov1")
        api.files["cov1"] = b"\xff\xd8\xff" + b"\x00" * 500
        status, headers, body = get(base, f"/api/cover/{seeded[0]}", key)
        assert status == 200 and headers["Content-Type"] == "image/jpeg" and len(body) > 100

    def test_cover_absent_is_404(self, running, seeded, db: Database) -> None:
        _desk, base, key = running
        release_id = int(db.scalar("SELECT release_id FROM tracks WHERE id=?", (seeded[0],)))
        db.execute("UPDATE releases SET cover_file_id=NULL WHERE id=?", (release_id,))
        db.execute("UPDATE tracks SET cover_file_id=NULL WHERE release_id=?", (release_id,))
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(base, f"/api/cover/{seeded[0]}", key)
        assert caught.value.code == 404


class TestBot:
    def test_status_without_a_token(self, config: Config, db: Database, api: FakeApi) -> None:
        config.token = ""
        desk = desk_module.Desk(config, db=db, api=None)
        assert desk.start_bot()["error"]
        assert desk.bot_running is False

    def test_start_and_stop(self, running) -> None:
        desk, base, key = running
        assert post(base, "/api/bot/start", key, {})["running"] is True
        assert api_get(base, "/api/state", key)["bot"]["running"] is True
        assert post(base, "/api/bot/stop", key, {})["running"] is False


class TestIsolation:
    def test_the_package_does_not_import_the_desk(self) -> None:
        """`desk/` is optional. Nothing in `tonearm/` may depend on it."""
        import re

        source = Path(__file__).resolve().parents[1] / "tonearm"
        # Column zero only: a lazy, ImportError-guarded import inside a
        # function is how an optional package is meant to be reached, and
        # cli.py uses exactly that.
        pattern = re.compile(r"^(?:from|import)\s+desk\b", re.MULTILINE)
        offenders = [
            path.name
            for path in source.glob("*.py")
            if pattern.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []
