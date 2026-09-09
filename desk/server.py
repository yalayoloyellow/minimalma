"""The curation desk: a local HTTP server over the same catalogue the bot uses.

Deliberately thin. Every operation here calls the same functions in
``tonearm.catalog`` that the bot calls, so there is one implementation of
"approve a track" and not two. The desk owns no state; it reads and writes the
same SQLite file, which is safe from a second process because the database runs
in WAL mode.

Nothing outside the standard library is imported. The server binds to loopback
only and requires a per-session key, because "localhost" is not by itself an
authorisation boundary — any other program on the machine can reach it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import mimetypes
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from tonearm import audio as audio_mod
from tonearm import catalog, recommend, search
from tonearm.config import Config
from tonearm.db import Database
from tonearm.telegram import Api, NetworkError, TelegramError

log = logging.getLogger("tonearm.desk")

STATIC = Path(__file__).resolve().parent / "static"

#: Downloaded audio is cached here so a curator can scrub a track without
#: re-fetching it from Telegram on every seek.
CACHE_LIMIT_BYTES = 512 * 1024 * 1024

_ROUTE = re.compile(r"^/api/(?P<name>[a-z_]+)(?:/(?P<arg>[^/?]+))?$")


class Desk:
    """Holds the objects the request handler needs. One per process."""

    def __init__(self, config: Config, db: Database | None = None, api: Any | None = None):
        self.config = config
        self.db = db or Database(config.database_path)
        self.api = api
        if self.api is None and config.token:
            try:
                self.api = Api(config.token)
            except ValueError:
                self.api = None
        self.engine = recommend.Engine(self.db, config)
        self.key = secrets.token_urlsafe(24)
        self.cache = config.home / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._bot: Any | None = None
        self._bot_thread: threading.Thread | None = None
        self._bot_error = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ bot
    @property
    def bot_running(self) -> bool:
        return bool(self._bot_thread and self._bot_thread.is_alive())

    def start_bot(self) -> dict:
        """Run the polling service in a background thread.

        Telegram allows exactly one long poller per token; a second one gets
        409 Conflict forever. The service already handles that by backing off
        and saying so, and the status this returns surfaces it.
        """
        with self._lock:
            if self.bot_running:
                return {"running": True, "error": ""}
            if not self.config.token:
                return {"running": False, "error": "no bot token — run tonearm setup"}
            from tonearm.app import Service

            self._bot_error = ""
            try:
                service = Service(self.config, api=self.api, db=self.db)
            except ValueError as exc:
                self._bot_error = str(exc)
                return {"running": False, "error": self._bot_error}

            def run() -> None:
                try:
                    service.run()
                except Exception as exc:  # surfaced through /api/state
                    self._bot_error = str(exc)
                    log.exception("bot stopped")

            self._bot = service
            self._bot_thread = threading.Thread(target=run, name="bot", daemon=True)
            self._bot_thread.start()
            return {"running": True, "error": ""}

    def stop_bot(self) -> dict:
        with self._lock:
            if self._bot is not None:
                self._bot.shutdown()
            self._bot = None
            self._bot_thread = None
            return {"running": False, "error": self._bot_error}

    # ---------------------------------------------------------------- media
    def audio_path(self, track_id: int) -> Path | None:
        """Fetch and cache a track's audio so the desk can play it."""
        row = self.db.one(
            "SELECT file_id, file_unique_id, mime FROM tracks WHERE id=?", (track_id,)
        )
        if row is None or self.api is None:
            return None
        suffix = mimetypes.guess_extension(row["mime"] or "") or ".mp3"
        if suffix == ".mpga":
            suffix = ".mp3"
        target = self.cache / f"{row['file_unique_id']}{suffix}"
        if target.exists() and target.stat().st_size:
            return target
        try:
            blob = self.api.download(row["file_id"])
        except (TelegramError, NetworkError) as exc:
            log.warning("audio fetch failed for %s: %s", track_id, exc)
            return None
        if not blob:
            return None
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(blob)
        temporary.replace(target)
        self._trim_cache()
        return target

    def cover_bytes(self, track_id: int) -> bytes | None:
        """Artwork for a track, resolved through its release like everywhere else."""
        row = catalog.track(self.db, track_id)
        if row is None or not row.get("cover_file_id") or self.api is None:
            return None
        cached = self.cache / f"cover-{track_id}.img"
        if cached.exists():
            return cached.read_bytes()
        try:
            blob = self.api.download(row["cover_file_id"], limit=10 * 1024 * 1024)
        except (TelegramError, NetworkError):
            return None
        if blob:
            cached.write_bytes(blob)
        return blob

    def _trim_cache(self) -> None:
        files = sorted(
            (p for p in self.cache.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        total = sum(p.stat().st_size for p in files)
        while total > CACHE_LIMIT_BYTES and files:
            victim = files.pop(0)
            total -= victim.stat().st_size
            try:
                victim.unlink()
            except OSError:  # pragma: no cover
                pass


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------


def _track_payload(desk: Desk, track_id: int) -> dict | None:
    item = catalog.track(desk.db, track_id)
    if item is None:
        return None
    features = item.get("features") or {}
    return {
        "id": item["id"],
        "title": item["title"],
        "artist": item["artist"],
        "artist_id": item["artist_id"],
        "album": item.get("album") or "",
        "year": item.get("year"),
        "duration": item.get("duration") or 0,
        "track_no": item.get("track_no"),
        "release_id": item.get("release_id"),
        "release_title": item.get("release_title") or "",
        "status": item["status"],
        "note": item.get("note") or "",
        "tags": item["tags"],
        "submitted_by": item.get("submitted_by"),
        "submitted_at": item.get("submitted_at"),
        "reject_reason": item.get("reject_reason") or "",
        "has_cover": bool(item.get("cover_file_id")),
        "features": features,
        "plays": item.get("plays") or 0,
        "likes": item.get("likes") or 0,
        "exposures": item.get("exposures") or 0,
    }


def _row_payload(item: dict) -> dict:
    return {
        "id": item["id"],
        "title": item["title"],
        "artist": item.get("artist", ""),
        "artist_id": item.get("artist_id"),
        "duration": item.get("duration") or 0,
        "status": item.get("status", ""),
        "note": item.get("note") or "",
        "plays": item.get("plays") or 0,
        "likes": item.get("likes") or 0,
        "exposures": item.get("exposures") or 0,
        "year": item.get("year"),
    }


def api_state(desk: Desk, _arg: str | None, _body: dict, _query: dict) -> dict:
    report = catalog.curator_report(desk.db)
    return {
        "station": desk.config.station_name,
        "tagline": desk.config.station_tagline,
        "lang": desk.config.lang,
        "report": report,
        "bot": {"running": desk.bot_running, "error": desk._bot_error},
        "has_token": bool(desk.config.token),
        "curators": sorted(
            {*desk.config.curators, *([desk.config.owner] if desk.config.owner else [])}
        ),
        "tags": search.suggest_tags(desk.db, limit=40),
    }


def _release_row(item: dict) -> dict:
    return {
        "id": item["id"],
        "title": item["title"],
        "artist": item.get("artist", ""),
        "artist_id": item.get("artist_id"),
        "kind": item.get("kind", "single"),
        "year": item.get("year"),
        "status": item.get("status", ""),
        "note": item.get("note") or "",
        "has_cover": bool(item.get("cover_file_id")),
        "blockers": item.get("blockers") or [],
        "waiting": item.get("waiting", 0),
        "total": item.get("total") or item.get("n") or 0,
    }


def api_queue(desk: Desk, _arg: str | None, _body: dict, query: dict) -> dict:
    """The queue is releases, matching what a curator sees in the bot."""
    limit = _int(query.get("limit"), 50)
    items = catalog.pending_releases(desk.db, limit=limit)
    return {
        "total": catalog.pending_releases_total(desk.db),
        "items": [_release_row(item) for item in items],
    }


def api_release(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict | None:
    item = catalog.release(desk.db, int(arg or 0))
    if item is None:
        return None
    payload = _release_row(item)
    payload["tracks"] = [_track_payload(desk, int(t["id"])) for t in item["tracks"]]
    payload["duration"] = item.get("duration", 0)
    return payload


def api_releases(desk: Desk, _arg: str | None, _body: dict, query: dict) -> dict:
    limit = _int(query.get("limit"), 60)
    return {"items": [_release_row(item) for item in catalog.newest_releases(desk.db, limit=limit)]}


def api_approve_release(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    release_id = int(arg or 0)
    published, error = catalog.approve_release(
        desk.db, release_id, desk.config.owner or 0, note=str(body.get("note") or "")
    )
    desk.engine.invalidate()
    if published and published["tracks"]:
        _notify(desk, catalog.track(desk.db, int(published["tracks"][0]["id"])), approved=True)
    return {"ok": bool(published), "error": error, "release": api_release(desk, arg, {}, {})}


def api_reject_release(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    release_id = int(arg or 0)
    item = catalog.reject_release(
        desk.db, release_id, desk.config.owner or 0, reason=str(body.get("reason") or "")
    )
    desk.engine.invalidate()
    if item and item["tracks"]:
        _notify(desk, catalog.track(desk.db, int(item["tracks"][0]["id"])), approved=False)
    return {"ok": bool(item), "release": api_release(desk, arg, {}, {})}


def api_edit_release(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    release_id = int(arg or 0)
    if catalog.release(desk.db, release_id) is None:
        return {"ok": False}
    fields: dict = {}
    for name in ("title", "note", "kind"):
        if name in body:
            fields[name] = str(body[name] or "").strip()
    if "year" in body:
        try:
            fields["year"] = int(body["year"]) if body["year"] else None
        except (TypeError, ValueError):
            pass
    if fields:
        catalog.update_release(desk.db, release_id, **fields)
    desk.engine.invalidate()
    return {"ok": True, "release": api_release(desk, arg, {}, {})}


def api_track(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict | None:
    return _track_payload(desk, int(arg or 0))


def api_catalogue(desk: Desk, _arg: str | None, _body: dict, query: dict) -> dict:
    text = (query.get("q") or "").strip()
    status = query.get("status") or "approved"
    limit = _int(query.get("limit"), 60)
    if text:
        ids = [track_id for track_id, _ in search.search(desk.db, text, limit=limit)]
        items = catalog.tracks(desk.db, ids)
    elif status == "approved":
        items = catalog.newest(desk.db, limit=limit)
    else:
        rows = desk.db.query(
            "SELECT t.*, a.name AS artist FROM tracks t JOIN artists a ON a.id=t.artist_id "
            "WHERE t.status=? ORDER BY t.submitted_at DESC LIMIT ?",
            (status, limit),
        )
        items = [dict(row) for row in rows]
    return {"items": [_row_payload(item) for item in items]}


def api_artists(desk: Desk, _arg: str | None, _body: dict, query: dict) -> dict:
    text = (query.get("q") or "").strip()
    if text:
        ids = search.search_artists(desk.db, text, limit=60)
        rows = [
            desk.db.one(
                "SELECT a.id, a.name, COUNT(t.id) AS n FROM artists a "
                "LEFT JOIN tracks t ON t.artist_id=a.id AND t.status='approved' "
                "WHERE a.id=? GROUP BY a.id",
                (artist_id,),
            )
            for artist_id in ids
        ]
        rows = [row for row in rows if row]
    else:
        rows = desk.db.query(
            "SELECT a.id, a.name, COUNT(t.id) AS n FROM artists a "
            "JOIN tracks t ON t.artist_id=a.id AND t.status='approved' "
            "GROUP BY a.id ORDER BY a.name COLLATE NOCASE LIMIT 300"
        )
    return {"items": [{"id": r["id"], "name": r["name"], "tracks": r["n"]} for r in rows]}


def api_artist(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict | None:
    artist_id = int(arg or 0)
    artist = catalog.artist_row(desk.db, artist_id)
    if artist is None:
        return None
    return {
        "id": artist["id"],
        "name": artist["name"],
        "bio": artist.get("bio") or "",
        "link": artist.get("link") or "",
        "stats": catalog.artist_stats(desk.db, artist_id),
        "tracks": [_row_payload(item) for item in catalog.artist_tracks(desk.db, artist_id, 200)],
    }


def api_approve(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    track_id = int(arg or 0)
    tags = body.get("tags")
    item = catalog.approve(
        desk.db,
        track_id,
        desk.config.owner or 0,
        note=str(body.get("note") or ""),
        tag_names=tags if isinstance(tags, list) else None,
    )
    desk.engine.invalidate()
    if item:
        _notify(desk, item, approved=True)
    return {"ok": bool(item), "track": _track_payload(desk, track_id)}


def api_reject(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    track_id = int(arg or 0)
    item = catalog.reject(
        desk.db, track_id, desk.config.owner or 0, reason=str(body.get("reason") or "")
    )
    desk.engine.invalidate()
    if item:
        _notify(desk, item, approved=False)
    return {"ok": bool(item), "track": _track_payload(desk, track_id)}


def api_hide(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict:
    track_id = int(arg or 0)
    catalog.hide(desk.db, track_id, desk.config.owner or 0)
    desk.engine.invalidate()
    return {"ok": True, "track": _track_payload(desk, track_id)}


def api_setcover(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    """Attach artwork sent from the desk as base64.

    Telegram only serves media it hosts, so the image has to be uploaded once
    to obtain a photo ``file_id``. It goes to the review chat — or, failing
    that, to the owner — which doubles as an archive of every cover accepted.
    """
    release_id = int(arg or 0)
    if catalog.release(desk.db, release_id) is None:
        return {"ok": False, "error": "not_found"}
    try:
        blob = base64.b64decode(str(body.get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": "bad_image"}
    if len(blob) < 100:
        return {"ok": False, "error": "bad_image"}

    target = desk.config.review_chat or desk.config.owner
    if desk.api is None or not target:
        return {"ok": False, "error": "no_upload_target"}
    blob = audio_mod.normalise_cover(blob) or blob
    try:
        message = desk.api.send_photo(
            target, blob, filename="cover.jpg", caption="cover", disable_notification=True
        )
    except (TelegramError, NetworkError) as exc:
        log.warning("cover upload failed: %s", exc)
        return {"ok": False, "error": "upload_failed"}
    sizes = (message or {}).get("photo") or []
    if not sizes:
        return {"ok": False, "error": "upload_failed"}
    catalog.set_release_cover(desk.db, release_id, str(sizes[-1]["file_id"]))
    # A freshly uploaded cover invalidates whatever was cached for this release.
    for track in catalog.release(desk.db, release_id)["tracks"]:
        cached = desk.cache / f"cover-{track['id']}.img"
        if cached.exists():
            cached.unlink()
    return {"ok": True, "release": api_release(desk, arg, {}, {})}


def api_restore(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict:
    track_id = int(arg or 0)
    item = catalog.restore_to_queue(desk.db, track_id)
    desk.engine.invalidate()
    return {"ok": bool(item), "track": _track_payload(desk, track_id)}


def api_edit(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    """Update the editable fields of a track. Absent keys are left alone."""
    track_id = int(arg or 0)
    if catalog.track(desk.db, track_id) is None:
        return {"ok": False}
    fields: dict = {}
    for name in ("title", "album", "note"):
        if name in body:
            fields[name] = str(body[name] or "").strip()
    if "year" in body:
        try:
            fields["year"] = int(body["year"]) if body["year"] else None
        except (TypeError, ValueError):
            pass
    if fields:
        catalog.update_track(desk.db, track_id, **fields)
    if body.get("artist"):
        catalog.rename_artist(desk.db, track_id, str(body["artist"]))
    if isinstance(body.get("tags"), list):
        catalog.set_tags(desk.db, track_id, body["tags"])
    item = catalog.track(desk.db, track_id)
    if item and item["status"] == catalog.STATUS_APPROVED:
        search.index_track(
            desk.db,
            track_id,
            item["title"],
            item["artist"],
            item.get("album") or "",
            item["tags"],
            item.get("note") or "",
        )
    desk.engine.invalidate()
    return {"ok": True, "track": _track_payload(desk, track_id)}


def api_playlists(desk: Desk, _arg: str | None, _body: dict, _query: dict) -> dict:
    return {
        "items": [
            {
                "id": item["id"],
                "title": item["title"],
                "description": item.get("description") or "",
                "published": bool(item["published"]),
                "count": item.get("n", 0),
            }
            for item in catalog.playlists(desk.db, published_only=False, limit=100)
        ]
    }


def api_playlist(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict | None:
    item = catalog.playlist(desk.db, int(arg or 0))
    if item is None:
        return None
    return {
        "id": item["id"],
        "title": item["title"],
        "description": item.get("description") or "",
        "published": bool(item["published"]),
        "tracks": [_row_payload(track) for track in item["tracks"]],
    }


def api_playlist_new(desk: Desk, _arg: str | None, body: dict, _query: dict) -> dict:
    playlist_id = catalog.create_playlist(
        desk.db,
        str(body.get("title") or ""),
        desk.config.owner,
        str(body.get("description") or ""),
    )
    return {"ok": playlist_id is not None, "id": playlist_id}


def api_playlist_edit(desk: Desk, arg: str | None, body: dict, _query: dict) -> dict:
    playlist_id = int(arg or 0)
    if "add" in body:
        catalog.add_to_playlist(desk.db, playlist_id, int(body["add"]))
    if "remove" in body:
        catalog.remove_from_playlist(desk.db, playlist_id, int(body["remove"]))
    if "published" in body:
        catalog.publish_playlist(desk.db, playlist_id, bool(body["published"]))
    return {"ok": True, "playlist": api_playlist(desk, str(playlist_id), {}, {})}


def api_bot(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict:
    if arg == "start":
        return desk.start_bot()
    if arg == "stop":
        return desk.stop_bot()
    return {"running": desk.bot_running, "error": desk._bot_error}


def api_similar(desk: Desk, arg: str | None, _body: dict, _query: dict) -> dict:
    ids = desk.engine.similar_to(int(arg or 0), count=8)
    return {"items": [_row_payload(item) for item in catalog.tracks(desk.db, ids)]}


def _notify(desk: Desk, item: dict, approved: bool) -> None:
    """Tell the artist, exactly as the bot would. Never blocks the response."""
    if desk.api is None or not item.get("submitted_by"):
        return
    from tonearm import ui
    from tonearm.i18n import normalise, t

    row = catalog.get_user(desk.db, int(item["submitted_by"]))
    lang = normalise((row or {}).get("lang") or desk.config.lang)
    if approved:
        text = t(lang, "submit.approved_notice", title=ui.esc(item["title"]))
    else:
        text = t(lang, "submit.rejected_notice", title=ui.esc(item["title"]))
        if item.get("reject_reason"):
            text += "\n" + t(lang, "submit.rejected_reason", reason=ui.esc(item["reject_reason"]))

    def send() -> None:
        try:
            desk.api.send_message(int(item["submitted_by"]), text)
        except (TelegramError, NetworkError) as exc:
            log.warning("could not notify artist: %s", exc)

    threading.Thread(target=send, daemon=True).start()


ROUTES: dict = {
    "state": api_state,
    "queue": api_queue,
    "release": api_release,
    "releases": api_releases,
    "approve_release": api_approve_release,
    "reject_release": api_reject_release,
    "edit_release": api_edit_release,
    "setcover": api_setcover,
    "track": api_track,
    "catalogue": api_catalogue,
    "artists": api_artists,
    "artist": api_artist,
    "approve": api_approve,
    "reject": api_reject,
    "hide": api_hide,
    "restore": api_restore,
    "edit": api_edit,
    "playlists": api_playlists,
    "playlist": api_playlist,
    "playlist_new": api_playlist_new,
    "playlist_edit": api_playlist_edit,
    "bot": api_bot,
    "similar": api_similar,
}


def _int(value: Any, default: int) -> int:
    try:
        return max(1, min(500, int(value)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "tonearm-desk"
    desk: Desk = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ----------------------------------------------------------------- auth
    def _authorised(self, query: dict) -> bool:
        supplied = self.headers.get("X-Tonearm-Key") or query.get("k") or ""
        return secrets.compare_digest(str(supplied), self.desk.key)

    # ------------------------------------------------------------- requests
    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send_static("index.html")
            return
        if path.startswith("/static/"):
            self._send_static(path[len("/static/") :])
            return
        if not self._authorised(query):
            self._send_json({"error": "unauthorised"}, HTTPStatus.FORBIDDEN)
            return
        if path.startswith("/api/audio/"):
            self._send_audio(path.rsplit("/", 1)[-1])
            return
        if path.startswith("/api/cover/"):
            self._send_cover(path.rsplit("/", 1)[-1])
            return
        self._dispatch(path, {}, query)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not self._authorised(query):
            self._send_json({"error": "unauthorised"}, HTTPStatus.FORBIDDEN)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if 0 < length <= 4 * 1024 * 1024 else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._send_json({"error": "bad json"}, HTTPStatus.BAD_REQUEST)
            return
        self._dispatch(parsed.path, body if isinstance(body, dict) else {}, query)

    def _dispatch(self, path: str, body: dict, query: dict) -> None:
        match = _ROUTE.match(path)
        handler: Callable | None = ROUTES.get(match.group("name")) if match else None
        if handler is None:
            self._send_json({"error": "no such endpoint"}, HTTPStatus.NOT_FOUND)
            return
        try:
            result = handler(self.desk, match.group("arg"), body, query)
        except Exception as exc:
            log.exception("desk endpoint %s failed", path)
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if result is None:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._send_json(result)

    # --------------------------------------------------------------- output
    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _send_static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        blob = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{ctype}; charset=utf-8" if "text" in ctype or "javascript" in ctype else ctype,
        )
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def _send_cover(self, raw_id: str) -> None:
        try:
            blob = self.desk.cover_bytes(int(raw_id))
        except (TypeError, ValueError):
            blob = None
        if not blob:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_audio(self, raw_id: str) -> None:
        """Serve cached audio with Range support.

        Range is not optional: WebKit refuses to seek — and in some builds
        refuses to play at all — when a media response arrives without it.
        """
        try:
            path = self.desk.audio_path(int(raw_id))
        except (TypeError, ValueError):
            path = None
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
        start, end = 0, size - 1
        status = HTTPStatus.OK

        header = self.headers.get("Range") or ""
        match = re.match(r"bytes=(\d*)-(\d*)$", header.strip())
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = min(int(first), size - 1)
                end = min(int(last), size - 1) if last else size - 1
            elif last:  # suffix range: the final N bytes
                start = max(0, size - int(last))
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # the player seeked away; not an error
                remaining -= len(chunk)


def serve(config: Config, host: str = "127.0.0.1", port: int = 0, desk: Desk | None = None):
    """Start the desk server. Returns ``(httpd, desk, url)``."""
    desk = desk or Desk(config)
    handler = type("BoundHandler", (Handler,), {"desk": desk})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    actual = httpd.server_address[1]
    return httpd, desk, f"http://{host}:{actual}/?k={desk.key}"
