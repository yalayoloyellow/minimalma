"""A Telegram API double and fixture builders.

The bot talks to Telegram through exactly one object, so replacing it here
gives the tests the whole surface: every outbound call is recorded and can be
asserted on, including the negative assertions that matter most in this
project ("no audio was sent without a tap").
"""

from __future__ import annotations

import struct
from typing import Any


class FakeApi:
    """Records outbound calls and hands back plausible Telegram replies."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[int, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self._next_id = 1000

    # ------------------------------------------------------------ recording
    def _record(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, params))

    def of(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.calls if name == method]

    @property
    def audio_sent(self) -> list[dict[str, Any]]:
        return self.of("sendAudio")

    @property
    def texts(self) -> list[str]:
        return [p.get("text", "") for n, p in self.calls if n in ("sendMessage", "editMessageText")]

    def last_screen(self) -> str:
        for method, params in reversed(self.calls):
            if method in ("sendMessage", "editMessageText"):
                return params.get("text", "")
        return ""

    def last_markup(self) -> dict[str, Any]:
        for method, params in reversed(self.calls):
            if method in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
                return params.get("reply_markup") or {}
        return {}

    def buttons(self) -> list[dict[str, str]]:
        rows = self.last_markup().get("inline_keyboard") or []
        return [button for row in rows for button in row]

    def find_button(self, data_prefix: str) -> str | None:
        for button in self.buttons():
            if str(button.get("callback_data", "")).startswith(data_prefix):
                return button["callback_data"]
        return None

    def clear(self) -> None:
        self.calls.clear()

    # -------------------------------------------------------------- surface
    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        timeout: float = 30.0,
        throttled: bool = True,
    ) -> Any:
        params = params or {}
        self._record(method, params)
        if method == "getMe":
            return {"id": 1, "username": "tonearm_test_bot", "first_name": "Tonearm"}
        return True

    def _message(self, chat_id: int, **extra: Any) -> dict[str, Any]:
        self._next_id += 1
        message = {"message_id": self._next_id, "chat": {"id": chat_id}, **extra}
        self.messages[self._next_id] = message
        return message

    def send_message(self, chat_id: int, text: str, **kw: Any) -> dict[str, Any]:
        self._record("sendMessage", {"chat_id": chat_id, "text": text, **kw})
        return self._message(chat_id, text=text)

    def edit_message(self, chat_id: int, message_id: int, text: str, **kw: Any) -> Any:
        self._record(
            "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text, **kw}
        )
        if message_id not in self.messages:
            return None
        self.messages[message_id]["text"] = text
        return self.messages[message_id]

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: Any) -> Any:
        self._record(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        )
        return self.messages.get(message_id)

    def delete_message(self, chat_id: int, message_id: int) -> bool:
        self._record("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        return self.messages.pop(message_id, None) is not None

    def send_audio(self, chat_id: int, audio: str, **kw: Any) -> dict[str, Any]:
        self._record("sendAudio", {"chat_id": chat_id, "audio": audio, **kw})
        return self._message(chat_id, audio={"file_id": audio})

    def send_photo(
        self, chat_id: int, photo: Any, filename: str = "cover.jpg", **kw: Any
    ) -> dict[str, Any]:
        self._record("sendPhoto", {"chat_id": chat_id, "filename": filename, **kw})
        return self._message(
            chat_id, photo=[{"file_id": "photo:archived", "width": 320, "height": 320}]
        )

    def answer_callback(
        self, callback_id: str, text: str = "", alert: bool = False, cache: int = 0
    ) -> None:
        self._record("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def answer_inline(self, inline_id: str, results: Any, **kw: Any) -> None:
        self._record("answerInlineQuery", {"inline_query_id": inline_id, "results": list(results)})

    def set_commands(self, commands: Any, language_code: str = "") -> None:
        self._record("setMyCommands", {"commands": commands, "language_code": language_code})

    def drop_webhook(self) -> None:
        self._record("deleteWebhook", {})

    def me(self) -> dict[str, Any]:
        return self.call("getMe")

    def download(self, file_id: str, limit: int = 0) -> bytes | None:
        self._record("getFile", {"file_id": file_id})
        return self.files.get(file_id)


# --------------------------------------------------------------------------
# Update builders
# --------------------------------------------------------------------------

_UPDATE_ID = [1]


def _next_update() -> int:
    _UPDATE_ID[0] += 1
    return _UPDATE_ID[0]


def user(user_id: int, name: str = "Listener", lang: str = "en") -> dict[str, Any]:
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": name,
        "username": f"u{user_id}",
        "language_code": lang,
    }


def message(user_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": _next_update(),
        "message": {
            "message_id": _next_update(),
            "from": user(user_id),
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


def audio_message(
    user_id: int,
    file_id: str,
    unique_id: str,
    title: str = "",
    performer: str = "",
    duration: int = 180,
    file_name: str = "track.mp3",
    thumbnail: bool = False,
    size: int = 4_000_000,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "file_id": file_id,
        "file_unique_id": unique_id,
        "duration": duration,
        "mime_type": "audio/mpeg",
        "file_name": file_name,
        "file_size": size,
    }
    if title:
        payload["title"] = title
    if performer:
        payload["performer"] = performer
    if thumbnail:
        payload["thumbnail"] = {
            "file_id": f"thumb:{unique_id}",
            "file_unique_id": f"tu{unique_id}",
            "width": 320,
            "height": 320,
        }
    return {
        "update_id": _next_update(),
        "message": {
            "message_id": _next_update(),
            "from": user(user_id),
            "chat": {"id": user_id, "type": "private"},
            "audio": payload,
        },
    }


def callback(user_id: int, data: str, message_id: int = 5000) -> dict[str, Any]:
    return {
        "update_id": _next_update(),
        "callback_query": {
            "id": f"cb{_next_update()}",
            "from": user(user_id),
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": user_id, "type": "private"},
            },
        },
    }


def inline(user_id: int, query: str) -> dict[str, Any]:
    return {
        "update_id": _next_update(),
        "inline_query": {"id": "iq1", "from": user(user_id), "query": query, "offset": ""},
    }


# --------------------------------------------------------------------------
# Synthetic audio files
# --------------------------------------------------------------------------


def _synchsafe(value: int) -> bytes:
    return bytes(((value >> 21) & 0x7F, (value >> 14) & 0x7F, (value >> 7) & 0x7F, value & 0x7F))


def _text_frame(name: str, text: str, version: int = 4) -> bytes:
    payload = b"\x03" + text.encode("utf-8") + b"\x00"
    size = _synchsafe(len(payload)) if version == 4 else struct.pack(">I", len(payload))
    return name.encode("latin-1") + size + b"\x00\x00" + payload


def _apic_frame(image: bytes, version: int = 4) -> bytes:
    payload = b"\x03" + b"image/jpeg\x00" + bytes([3]) + b"front\x00" + image
    size = _synchsafe(len(payload)) if version == 4 else struct.pack(">I", len(payload))
    return b"APIC" + size + b"\x00\x00" + payload


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 400 + b"\xff\xd9"


def id3_file(
    title: str = "Nightpost",
    artist: str = "Anna Voskresenskaya",
    album: str = "Stairwell",
    year: str = "2024",
    cover: bool = True,
    version: int = 4,
) -> bytes:
    """A minimal but structurally valid MP3 carrying an ID3v2 tag."""
    frames = b"".join(
        [
            _text_frame("TIT2", title, version),
            _text_frame("TPE1", artist, version),
            _text_frame("TALB", album, version),
            _text_frame("TDRC" if version == 4 else "TYER", year, version),
            _text_frame("TCON", "(26)", version),
        ]
    )
    if cover:
        frames += _apic_frame(JPEG, version)
    header = b"ID3" + bytes([version, 0, 0]) + _synchsafe(len(frames))
    # A frame of silence so the payload is not obviously empty.
    return header + frames + b"\xff\xfb\x90\x00" + b"\x00" * 2048


def id3v1_file(title: str = "Old Track", artist: str = "Tape Artist") -> bytes:
    block = bytearray(b"\x00" * 128)
    block[0:3] = b"TAG"
    block[3 : 3 + len(title)] = title.encode("latin-1")
    block[33 : 33 + len(artist)] = artist.encode("latin-1")
    block[93:97] = b"1998"
    block[127] = 17
    return b"\xff\xfb\x90\x00" + b"\x00" * 512 + bytes(block)
