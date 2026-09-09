"""A small, dependency-free Telegram Bot API client.

Design notes
------------
* ``urllib.request`` only. No connection pooling: at this traffic profile the
  extra TLS handshake per call is cheaper than owning a pool's failure modes.
* Every call goes through :meth:`Api.call`, which is the single place that
  understands rate limits, retries and Telegram's error vocabulary.
* Long polling uses a socket timeout comfortably larger than the server-side
  ``timeout`` so a healthy long poll never looks like a network failure.
* The client is deliberately synchronous. The bot runs one polling thread and a
  small worker pool; ordering per chat is preserved by the outbound throttle.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from typing import Any

log = logging.getLogger("tonearm.telegram")

API_ROOT = "https://api.telegram.org"

#: Telegram truncates a text message here.
MAX_MESSAGE = 4096
#: Telegram truncates a media caption here.
MAX_CAPTION = 1024
#: Hard ceiling on ``callback_data``.
MAX_CALLBACK_DATA = 64
#: Bots may only download files up to this size through ``getFile``.
MAX_DOWNLOAD = 20 * 1024 * 1024

#: Updates we ask for. Anything omitted is never delivered, which keeps the
#: long poll cheap and makes the handler surface explicit.
ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "inline_query"]


class TelegramError(Exception):
    """A non-2xx reply from the Bot API."""

    def __init__(self, method: str, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after

    # Telegram reports semantically different situations through free-form
    # strings. These predicates keep the string matching in exactly one place.
    @property
    def blocked_by_user(self) -> bool:
        d = self.description.lower()
        return "bot was blocked" in d or "user is deactivated" in d or "chat not found" in d

    @property
    def not_modified(self) -> bool:
        return "message is not modified" in self.description.lower()

    @property
    def message_gone(self) -> bool:
        """The target message cannot be edited — deleted, too old, or media.

        "there is no text in the message to edit" is included deliberately: it
        is what Telegram says when a photo message is asked to become text, and
        the caller's response is the same as for a deleted message — send a new
        one.
        """
        d = self.description.lower()
        return (
            "message to edit not found" in d
            or "message to delete not found" in d
            or "message can't be edited" in d
            or "there is no text in the message to edit" in d
        )

    @property
    def bad_file(self) -> bool:
        d = self.description.lower()
        return (
            "wrong file identifier" in d
            or "wrong remote file identifier" in d
            or ("failed to get http url content" in d)
        )

    @property
    def fatal(self) -> bool:
        """True when retrying the identical request cannot possibly help.

        "message is not modified" belongs here even though callers treat it as
        success: re-sending an identical edit produces the identical error, so
        retrying it only burns a worker for the length of the backoff.
        """
        return self.code in (400, 401, 403, 404)


class NetworkError(Exception):
    """The request never reached Telegram, or the reply was unreadable."""


class Throttle:
    """Global + per-chat outbound pacing.

    Telegram does not publish exact limits. The widely observed contract is
    roughly 30 messages/second overall and about one message/second to a single
    chat. We stay under both with room to spare; when we are wrong, the 429
    handler in :meth:`Api.call` is the real safety net.
    """

    def __init__(self, per_second: float = 25.0, per_chat_interval: float = 1.05):
        self._lock = threading.Lock()
        self._interval = 1.0 / per_second
        self._per_chat_interval = per_chat_interval
        self._next_global = 0.0
        self._next_chat: dict[int, float] = {}

    def acquire(self, chat_id: int | None) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                earliest = max(self._next_global, now)
                if chat_id is not None:
                    earliest = max(earliest, self._next_chat.get(chat_id, 0.0))
                wait = earliest - now
                if wait <= 0:
                    self._next_global = now + self._interval
                    if chat_id is not None:
                        self._next_chat[chat_id] = now + self._per_chat_interval
                        if len(self._next_chat) > 4096:
                            self._prune(now)
                    return
            time.sleep(min(wait, 0.5))

    def _prune(self, now: float) -> None:
        stale = [cid for cid, t in self._next_chat.items() if t < now - 60]
        for cid in stale:
            self._next_chat.pop(cid, None)

    def penalise(self, chat_id: int | None, seconds: float) -> None:
        """Push back the next allowed send after a 429."""
        with self._lock:
            deadline = time.monotonic() + seconds
            self._next_global = max(self._next_global, deadline)
            if chat_id is not None:
                self._next_chat[chat_id] = max(self._next_chat.get(chat_id, 0.0), deadline)


def _encode_multipart(
    fields: dict[str, Any], files: dict[str, tuple[str, bytes]]
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body by hand.

    ``files`` maps a form field name to ``(filename, payload)``.
    """
    boundary = "----tonearm" + "".join(random.choice("0123456789abcdef") for _ in range(24))
    out: list[bytes] = []
    sep = f"--{boundary}\r\n".encode()
    for name, value in fields.items():
        if value is None:
            continue
        out.append(sep)
        out.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.append(_as_field(value).encode("utf-8"))
        out.append(b"\r\n")
    for name, (filename, payload) in files.items():
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
        out.append(sep)
        out.append(f'Content-Disposition: form-data; name="{name}"; filename="{safe}"\r\n'.encode())
        out.append(f"Content-Type: {ctype}\r\n\r\n".encode())
        out.append(payload)
        out.append(b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def _as_field(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Api:
    """Thin synchronous wrapper over one bot token."""

    def __init__(
        self,
        token: str,
        root: str = API_ROOT,
        throttle: Throttle | None = None,
        max_attempts: int = 5,
    ):
        if not token or ":" not in token:
            raise ValueError("a Telegram bot token looks like 123456789:AA...")
        self._token = token
        self._root = root.rstrip("/")
        self.throttle = throttle or Throttle()
        self.max_attempts = max_attempts
        self._opener = urllib.request.build_opener()
        self._opener.addheaders = [("User-Agent", "tonearm")]

    # ------------------------------------------------------------------ core
    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
        timeout: float = 30.0,
        throttled: bool = True,
    ) -> Any:
        """Invoke a Bot API method and return its ``result``.

        Raises :class:`TelegramError` for API-level failures and
        :class:`NetworkError` when the request could not be completed.
        """
        params = {k: v for k, v in (params or {}).items() if v is not None}
        chat_id = params.get("chat_id") if isinstance(params.get("chat_id"), int) else None
        url = f"{self._root}/bot{self._token}/{method}"
        attempt = 0
        while True:
            attempt += 1
            if throttled:
                self.throttle.acquire(chat_id)
            try:
                return self._once(url, method, params, files, timeout)
            except TelegramError as exc:
                if exc.code == 429:
                    delay = exc.retry_after or 1
                    self.throttle.penalise(chat_id, delay)
                    if attempt >= self.max_attempts:
                        raise
                    log.warning("429 on %s, sleeping %ss", method, delay)
                    time.sleep(delay)
                    continue
                if exc.fatal or attempt >= self.max_attempts:
                    raise
                time.sleep(self._backoff(attempt))
            except NetworkError:
                if attempt >= self.max_attempts:
                    raise
                time.sleep(self._backoff(attempt))

    def _once(
        self,
        url: str,
        method: str,
        params: dict[str, Any],
        files: dict[str, tuple[str, bytes]] | None,
        timeout: float,
    ) -> Any:
        if files:
            body, ctype = _encode_multipart(params, files)
        else:
            body = json.dumps(params, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _safe_json(raw)
            description = str(payload.get("description") or exc.reason or "http error")
            retry_after = int((payload.get("parameters") or {}).get("retry_after") or 0)
            raise TelegramError(method, exc.code, description, retry_after) from None
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise NetworkError(f"{method}: {exc}") from exc
        payload = _safe_json(raw)
        if not payload.get("ok"):
            description = str(payload.get("description") or "malformed reply")
            retry_after = int((payload.get("parameters") or {}).get("retry_after") or 0)
            raise TelegramError(
                method, int(payload.get("error_code") or 0), description, retry_after
            )
        return payload.get("result")

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(30.0, (2 ** (attempt - 1)) + random.random())

    # ------------------------------------------------------------- polling
    def get_updates(self, offset: int, timeout: int = 25, limit: int = 100) -> list[dict[str, Any]]:
        """One long poll. The socket timeout deliberately outlives the server's."""
        result = self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "limit": limit,
                "allowed_updates": ALLOWED_UPDATES,
            },
            timeout=timeout + 15,
            throttled=False,
        )
        return list(result or [])

    def drop_webhook(self) -> None:
        """A registered webhook makes ``getUpdates`` fail with 409 forever."""
        try:
            info = self.call("getWebhookInfo", timeout=15) or {}
            if info.get("url"):
                self.call("deleteWebhook", {"drop_pending_updates": False}, timeout=15)
                log.info("removed a webhook so long polling can start")
        except (TelegramError, NetworkError) as exc:
            log.warning("could not inspect webhook: %s", exc)

    # ------------------------------------------------------------- downloads
    def download(self, file_id: str, limit: int = MAX_DOWNLOAD) -> bytes | None:
        """Fetch a file by id, or ``None`` when it is too large or gone."""
        try:
            info = self.call("getFile", {"file_id": file_id}, timeout=30)
        except TelegramError as exc:
            log.info("getFile failed for %s: %s", file_id[:12], exc.description)
            return None
        size = int(info.get("file_size") or 0)
        path = info.get("file_path")
        if not path or (size and size > limit):
            return None
        url = f"{self._root}/file/bot{self._token}/{urllib.parse.quote(path)}"
        try:
            with self._opener.open(url, timeout=120) as resp:
                data = resp.read(limit + 1)
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            raise NetworkError(f"download: {exc}") from exc
        if len(data) > limit:
            return None
        return data

    # ------------------------------------------------------ convenience verbs
    def me(self) -> dict[str, Any]:
        return self.call("getMe", timeout=15) or {}

    def send_message(self, chat_id: int, text: str, **kw: Any) -> dict[str, Any] | None:
        params = {"chat_id": chat_id, "text": text[:MAX_MESSAGE], "parse_mode": "HTML"}
        params.update(kw)
        return self._forgiving("sendMessage", params)

    def edit_message(self, chat_id: int, message_id: int, text: str, **kw: Any) -> Any:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE],
            "parse_mode": "HTML",
        }
        params.update(kw)
        return self._forgiving("editMessageText", params)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: Any) -> Any:
        return self._forgiving(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        )

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        return bool(
            self._forgiving("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        )

    def send_audio(self, chat_id: int, audio: str, **kw: Any) -> dict[str, Any] | None:
        params = {"chat_id": chat_id, "audio": audio, "parse_mode": "HTML"}
        params.update(kw)
        return self._forgiving("sendAudio", params)

    def send_photo(
        self, chat_id: int, photo: Any, filename: str = "cover.jpg", **kw: Any
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"chat_id": chat_id, "parse_mode": "HTML"}
        params.update(kw)
        if isinstance(photo, bytes):
            return self._forgiving("sendPhoto", params, files={"photo": (filename, photo)})
        params["photo"] = photo
        return self._forgiving("sendPhoto", params)

    def answer_callback(
        self, callback_id: str, text: str = "", alert: bool = False, cache: int = 0
    ) -> None:
        # A callback query must be answered or the client spins forever.
        self._forgiving(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text[:200] or None,
                "show_alert": alert or None,
                "cache_time": cache or None,
            },
        )

    def answer_inline(self, inline_id: str, results: Iterable[dict[str, Any]], **kw: Any) -> None:
        params = {
            "inline_query_id": inline_id,
            "results": list(results),
            "cache_time": kw.pop("cache_time", 30),
            "is_personal": True,
        }
        params.update(kw)
        self._forgiving("answerInlineQuery", params)

    def set_commands(self, commands: list[dict[str, str]], language_code: str = "") -> None:
        params: dict[str, Any] = {"commands": commands}
        if language_code:
            params["language_code"] = language_code
        self._forgiving("setMyCommands", params)

    def _forgiving(
        self, method: str, params: dict[str, Any], files: dict[str, Any] | None = None
    ) -> Any:
        """Call a method, swallowing the failures a live bot must survive.

        A user blocking the bot, a screen the user already deleted, or an edit
        that changes nothing are all normal. Everything else propagates.
        """
        try:
            return self.call(method, params, files=files)
        except TelegramError as exc:
            if exc.not_modified or exc.message_gone or exc.blocked_by_user:
                log.debug("%s ignored: %s", method, exc.description)
                return None
            raise
        except NetworkError as exc:
            log.warning("%s dropped: %s", method, exc)
            return None


def _safe_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
