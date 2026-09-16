"""The API client: encoding, error classification and pacing."""

from __future__ import annotations

import io
import json
import time
import urllib.error
from typing import Any

import pytest

from minimalma import telegram


class TestMultipart:
    def test_body_is_well_formed(self) -> None:
        body, content_type = telegram._encode_multipart(
            {"chat_id": 42, "caption": "a & b", "flag": True, "markup": {"x": [1, 2]}},
            {"photo": ("cover.jpg", b"\xff\xd8binary\x00data")},
        )
        boundary = content_type.split("boundary=")[1]
        assert body.startswith(f"--{boundary}\r\n".encode())
        assert body.endswith(f"--{boundary}--\r\n".encode())
        assert b'name="chat_id"\r\n\r\n42' in body
        assert b"a & b" in body
        assert b'name="flag"\r\n\r\ntrue' in body
        assert b'{"x":[1,2]}' in body
        assert b'filename="cover.jpg"' in body
        assert b"Content-Type: image/jpeg" in body
        assert b"\xff\xd8binary\x00data" in body

    def test_none_fields_are_dropped(self) -> None:
        body, _ = telegram._encode_multipart({"a": 1, "b": None}, {})
        assert b'name="b"' not in body

    def test_filename_cannot_break_out_of_the_header(self) -> None:
        body, _ = telegram._encode_multipart({}, {"f": ('e"vil\r\nX: y', b"data")})
        assert b'filename="evilX: y"' in body

    def test_audio_and_thumbnail_are_uploaded_as_two_files(self) -> None:
        api = telegram.Api("1:test")
        captured: dict[str, Any] = {}

        def fake(method: str, params: dict[str, Any], files: Any = None) -> dict[str, Any]:
            captured.update(method=method, params=params, files=files)
            return {"audio": {"file_id": "new-audio"}}

        api._forgiving = fake  # type: ignore[method-assign]
        result = api.send_audio(
            42,
            b"audio-bytes",
            filename="track.mp3",
            thumbnail_bytes=b"cover-bytes",
            title="Track",
        )
        assert result == {"audio": {"file_id": "new-audio"}}
        assert captured["method"] == "sendAudio"
        assert captured["files"] == {
            "audio": ("track.mp3", b"audio-bytes"),
            "thumbnail": ("cover.jpg", b"cover-bytes"),
        }


class TestErrors:
    @pytest.mark.parametrize(
        "description,flag",
        [
            ("Forbidden: bot was blocked by the user", "blocked_by_user"),
            ("Forbidden: user is deactivated", "blocked_by_user"),
            ("Bad Request: chat not found", "blocked_by_user"),
            ("Bad Request: message is not modified", "not_modified"),
            ("Bad Request: message to edit not found", "message_gone"),
            ("Bad Request: message to delete not found", "message_gone"),
            ("Bad Request: wrong file identifier/HTTP URL specified", "bad_file"),
        ],
    )
    def test_classification(self, description: str, flag: str) -> None:
        error = telegram.TelegramError("sendMessage", 400, description)
        assert getattr(error, flag) is True

    def test_not_modified_is_never_retried(self) -> None:
        # It is survivable, but retrying it is pure latency.
        error = telegram.TelegramError("editMessageText", 400, "message is not modified")
        assert error.fatal is True
        assert error.not_modified is True

    def test_a_plain_bad_request_is_fatal(self) -> None:
        assert telegram.TelegramError("sendMessage", 400, "chat_id is empty").fatal is True

    def test_server_errors_are_retryable(self) -> None:
        assert telegram.TelegramError("getUpdates", 502, "Bad Gateway").fatal is False


class TestThrottle:
    def test_serialises_calls_to_one_chat(self) -> None:
        throttle = telegram.Throttle(per_second=1000.0, per_chat_interval=0.05)
        start = time.monotonic()
        for _ in range(3):
            throttle.acquire(7)
        assert time.monotonic() - start >= 0.09

    def test_different_chats_do_not_block_each_other(self) -> None:
        throttle = telegram.Throttle(per_second=1000.0, per_chat_interval=5.0)
        start = time.monotonic()
        for chat_id in range(20):
            throttle.acquire(chat_id)
        assert time.monotonic() - start < 0.5

    def test_a_429_pushes_the_next_send_back(self) -> None:
        throttle = telegram.Throttle(per_second=1000.0, per_chat_interval=0.0)
        throttle.penalise(3, 0.15)
        start = time.monotonic()
        throttle.acquire(3)
        assert time.monotonic() - start >= 0.1


class _Recorder:
    """Stands in for the urllib opener so no socket is ever created."""

    def __init__(self, replies: list[Any]):
        self.replies = list(replies)
        self.requests: list[Any] = []
        self.addheaders: list[Any] = []

    def open(self, request: Any, timeout: float = 0) -> Any:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return _Response(reply)


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self._data = json.dumps(payload).encode()

    def read(self, *_: Any) -> bytes:
        return self._data

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


class _NoThrottle(telegram.Throttle):
    """Pacing is tested separately; here it would only add wall-clock time."""

    def acquire(self, chat_id: Any) -> None:
        return None

    def penalise(self, chat_id: Any, seconds: float) -> None:
        return None


def _api(replies: list[Any]) -> telegram.Api:
    api = telegram.Api("123:abc", throttle=_NoThrottle())
    api._opener = _Recorder(replies)
    return api


class TestCall:
    def test_returns_the_result_field(self) -> None:
        api = _api([{"ok": True, "result": {"id": 1}}])
        assert api.call("getMe") == {"id": 1}

    def test_retries_a_429_using_retry_after(self, monkeypatch: Any) -> None:
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        error = urllib.error.HTTPError(
            "u",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 429,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 3},
                    }
                ).encode()
            ),
        )
        api = _api([error, {"ok": True, "result": "fine"}])
        assert api.call("sendMessage", {"chat_id": 1}) == "fine"
        assert slept == [3]

    def test_gives_up_on_a_fatal_error(self) -> None:
        error = urllib.error.HTTPError(
            "u",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                json.dumps(
                    {"ok": False, "error_code": 400, "description": "chat_id is empty"}
                ).encode()
            ),
        )
        api = _api([error])
        with pytest.raises(telegram.TelegramError):
            api.call("sendMessage", {})

    def test_network_failure_is_wrapped_and_retried(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(time, "sleep", lambda _: None)
        api = _api([urllib.error.URLError("down"), {"ok": True, "result": 1}])
        assert api.call("getMe") == 1

    def test_survivable_errors_are_swallowed_by_the_convenience_verbs(self) -> None:
        error = urllib.error.HTTPError(
            "u",
            400,
            "Bad Request",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: message is not modified",
                    }
                ).encode()
            ),
        )
        api = _api([error])
        assert api.edit_message(1, 2, "same text") is None

    def test_a_blocked_user_does_not_raise(self) -> None:
        error = urllib.error.HTTPError(
            "u",
            403,
            "Forbidden",
            {},
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 403,
                        "description": "Forbidden: bot was blocked by the user",
                    }
                ).encode()
            ),
        )
        api = _api([error])
        assert api.send_message(1, "hello") is None

    def test_a_malformed_token_is_rejected_immediately(self) -> None:
        with pytest.raises(ValueError):
            telegram.Api("not-a-token")

    def test_text_is_truncated_to_the_api_limit(self) -> None:
        api = _api([{"ok": True, "result": {}}])
        api.send_message(1, "x" * 9000)
        body = json.loads(api._opener.requests[0].data.decode())
        assert len(body["text"]) == telegram.MAX_MESSAGE

    def test_long_polling_asks_for_a_longer_socket_timeout(self) -> None:
        api = _api([{"ok": True, "result": []}])
        api.get_updates(offset=5, timeout=25)
        body = json.loads(api._opener.requests[0].data.decode())
        assert body["timeout"] == 25
        assert body["offset"] == 5
        assert body["allowed_updates"] == telegram.ALLOWED_UPDATES
