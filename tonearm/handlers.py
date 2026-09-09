"""Update routing.

One class, :class:`Bot`, owns the whole conversation model:

* a single **screen** message per chat, edited in place for all browsing;
* **audio messages** appended only when the listener explicitly asks for one.

That second rule is the load-bearing one. There is no code path in this file
that sends a track without a tap that means "play this track" — no autoplay, no
queue that drains itself, no "up next" that arrives on its own.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

from . import catalog, recommend, search, ui
from .config import Config
from .db import Database
from .i18n import LANGUAGES, normalise, t
from .telegram import Api, TelegramError
from .ui import pack, unpack

log = logging.getLogger("tonearm.handlers")

COMMANDS = {
    "start",
    "help",
    "about",
    "today",
    "discover",
    "search",
    "library",
    "submit",
    "settings",
    "queue",
    "stats",
    "cancel",
    "whoami",
}


class Bot:
    """Stateless-per-update handler; all state lives in SQLite."""

    def __init__(self, config: Config, db: Database, api: Api, engine: recommend.Engine):
        self.config = config
        self.db = db
        self.api = api
        self.engine = engine

    # ------------------------------------------------------------ dispatch
    def handle(self, update: dict[str, Any]) -> None:
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "inline_query" in update:
                self._on_inline(update["inline_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except TelegramError as exc:
            if exc.blocked_by_user:
                log.info("dropped update: %s", exc.description)
            else:
                log.exception("telegram error while handling update: %s", exc)
        except Exception:
            log.exception("handler crashed on update %s", update.get("update_id"))

    # --------------------------------------------------------------- state
    def _user(self, tg_user: dict[str, Any]) -> dict[str, Any]:
        return catalog.ensure_user(self.db, tg_user, self.config.lang)

    def _lang(self, user: dict[str, Any]) -> str:
        return normalise(user.get("lang") or self.config.lang)

    def _data(self, user: dict[str, Any]) -> dict[str, Any]:
        raw = user.get("state_data")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _set_data(self, user: dict[str, Any], data: dict[str, Any]) -> None:
        """Store per-chat state and keep the in-memory user row in step.

        The caller usually reads that state back in the same request — starting
        a sequence and immediately playing its first track, for instance — so
        writing only to the database would hand the next call a stale copy.
        """
        payload = json.dumps(data, ensure_ascii=False)
        catalog.set_user(self.db, int(user["id"]), state_data=payload)
        user["state_data"] = payload

    def _set_state(self, user_id: int, state: str = "", **data: Any) -> None:
        catalog.set_user(
            self.db,
            user_id,
            state=state or None,
            state_data=json.dumps(data, ensure_ascii=False) if data else None,
        )

    # -------------------------------------------------------------- screen
    def show(
        self, chat_id: int, user: dict[str, Any], screen: ui.Screen, fresh: bool = False
    ) -> None:
        """Render the screen, editing the existing one unless told otherwise."""
        user_id = int(user["id"])
        message_id = user.get("screen_msg")
        same_chat = user.get("screen_chat") == chat_id

        if screen.photo:
            # A text message cannot be edited into a photo, so a cover-bearing
            # screen always replaces whatever screen was there.
            if message_id and same_chat:
                self.api.delete_message(chat_id, int(message_id))
            sent = self.api.send_photo(
                chat_id,
                screen.photo,
                caption=screen.text,
                reply_markup=screen.markup,
            )
            if sent:
                catalog.set_user(
                    self.db, user_id, screen_chat=chat_id, screen_msg=int(sent["message_id"])
                )
                user["screen_chat"] = chat_id
                user["screen_msg"] = int(sent["message_id"])
            else:
                # The cover id went stale; fall back to a plain text screen
                # rather than leaving the listener with nothing.
                self.show(chat_id, user, ui.Screen(screen.text, screen.markup), fresh=True)
            return

        replace = fresh
        if message_id and same_chat and not fresh:
            result = self.api.edit_message(
                chat_id, int(message_id), screen.text, reply_markup=screen.markup
            )
            if result is not None:
                return
            # Deleted, too old, or a photo screen that cannot become text.
            # Either way the old message has to go so only one screen remains.
            replace = True
        if message_id and same_chat and replace:
            self.api.delete_message(chat_id, int(message_id))
        sent = self.api.send_message(chat_id, screen.text, reply_markup=screen.markup)
        if sent:
            catalog.set_user(
                self.db, user_id, screen_chat=chat_id, screen_msg=int(sent["message_id"])
            )
            user["screen_chat"] = chat_id
            user["screen_msg"] = int(sent["message_id"])

    # ------------------------------------------------------------ messages
    def _on_message(self, message: dict[str, Any]) -> None:
        tg_user = message.get("from") or {}
        if not tg_user or tg_user.get("is_bot"):
            return
        chat = message.get("chat") or {}
        chat_id = int(chat["id"])
        user = self._user(tg_user)
        if user.get("banned"):
            return
        lang = self._lang(user)

        text = (message.get("text") or message.get("caption") or "").strip()
        if text.startswith("/"):
            command = text[1:].split()[0].split("@")[0].lower()
            argument = text[len(command) + 1 :].strip()
            if command in COMMANDS:
                self._on_command(chat_id, user, command, argument)
                return

        if message.get("audio"):
            self._on_submission(chat_id, user, message)
            return
        if message.get("voice") or message.get("video_note"):
            self.api.send_message(chat_id, t(lang, "submit.not_audio"))
            return
        if message.get("photo"):
            self._on_photo(chat_id, user, message)
            return
        if message.get("document"):
            mime = (message["document"].get("mime_type") or "").lower()
            if mime.startswith("audio/"):
                payload = dict(message["document"])
                payload.setdefault("duration", 0)
                self._on_submission(chat_id, user, {**message, "audio": payload})
            elif mime.startswith("image/"):
                self._on_photo(chat_id, user, message)
            else:
                self.api.send_message(chat_id, t(lang, "submit.not_audio"))
            return

        if not text:
            return

        state = user.get("state") or ""
        if state:
            self._on_stateful_text(chat_id, user, state, text)
            return

        # A bare message is a search. It is the lowest-friction thing a chat
        # interface can offer and costs the user nothing to discover.
        self._render_search(chat_id, user, text)

    def _on_command(self, chat_id: int, user: dict[str, Any], command: str, argument: str) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        if command == "cancel":
            self._set_state(user_id)
            self._render_home(chat_id, user)
            return
        self._set_state(user_id)
        if command == "start":
            if argument.startswith("t") and argument[1:].isdigit():
                self._render_track_page(chat_id, user, int(argument[1:]))
                return
            if argument.startswith("a") and argument[1:].isdigit():
                self._render_artist(chat_id, user, int(argument[1:]))
                return
            self._render_start(chat_id, user)
        elif command in ("help", "about"):
            self.show(chat_id, user, ui.help_screen(lang, self.config))
        elif command == "today":
            self._render_today(chat_id, user)
        elif command == "discover":
            self._render_discover(chat_id, user)
        elif command == "search":
            if argument:
                self._render_search(chat_id, user, argument)
            else:
                self._set_state(user_id, "search")
                self.show(chat_id, user, ui.search_prompt(lang, search.suggest_tags(self.db)))
        elif command == "library":
            self._render_library(chat_id, user)
        elif command == "submit":
            self._render_submit(chat_id, user)
        elif command == "settings":
            self.show(
                chat_id,
                user,
                ui.settings_screen(lang, bool(user.get("digest")), self.config.is_curator(user_id)),
            )
        elif command == "queue":
            self._render_queue(chat_id, user)
        elif command == "stats":
            self._render_stats(chat_id, user)
        elif command == "whoami":
            self.api.send_message(chat_id, f"<code>{user_id}</code>")

    def _on_stateful_text(self, chat_id: int, user: dict[str, Any], state: str, text: str) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        kind, _, argument = state.partition(":")
        track_id = int(argument) if argument.isdigit() else 0

        if kind == "search":
            self._set_state(user_id)
            self._render_search(chat_id, user, text)
            return

        if kind == "fix" and track_id:
            item = catalog.track(self.db, track_id)
            self._set_state(user_id)
            if item and item.get("submitted_by") == user_id and item["status"] == "pending":
                self._apply_correction(track_id, text, user_id)
                self.api.send_message(chat_id, t(lang, "submit.fixed"))
                self._render_submit(chat_id, user)
            return

        if not self.config.is_curator(user_id):
            self._set_state(user_id)
            return

        if kind == "relreason" and track_id:
            self._set_state(user_id)
            self._finish_release_rejection(chat_id, user, track_id, text)
            return
        if kind == "relnote" and track_id:
            self._set_state(user_id)
            catalog.update_release(self.db, track_id, note=text.strip()[:600])
            self.api.send_message(chat_id, t(lang, "mod.saved"))
            self._render_release_review(chat_id, user, track_id)
            return

        if kind == "modreason" and track_id:
            self._set_state(user_id)
            self._finish_rejection(chat_id, user, track_id, text)
        elif kind == "modtags" and track_id:
            self._set_state(user_id)
            catalog.set_tags(self.db, track_id, list(text.replace("\n", ",").split(",")))
            self.engine.invalidate()
            self.api.send_message(chat_id, t(lang, "mod.saved"))
            self._render_review(chat_id, user, track_id)
        elif kind == "modnote" and track_id:
            self._set_state(user_id)
            catalog.update_track(self.db, track_id, note=text.strip()[:600])
            self.api.send_message(chat_id, t(lang, "mod.saved"))
            self._render_review(chat_id, user, track_id)
        elif kind == "moded" and track_id:
            self._set_state(user_id)
            self._apply_correction(track_id, text, None)
            self.engine.invalidate()
            self.api.send_message(chat_id, t(lang, "mod.saved"))
            self._render_review(chat_id, user, track_id)
        else:
            self._set_state(user_id)

    def _apply_correction(self, track_id: int, text: str, claimed_by: int | None) -> None:
        """Accept ``Artist — Title`` and rewrite both fields."""
        for separator in (" — ", " – ", " - ", " — ", "—", "–", " | "):
            if separator in text:
                artist, _, title = text.partition(separator)
                artist, title = artist.strip(), title.strip()
                if artist and title:
                    catalog.rename_artist(self.db, track_id, artist, claimed_by)
                    catalog.update_track(self.db, track_id, title=title)
                    return
        catalog.update_track(self.db, track_id, title=text.strip()[:200])

    # ---------------------------------------------------------- submission
    def _on_submission(self, chat_id: int, user: dict[str, Any], message: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        day = recommend.today()
        limits = self.config.limits

        if catalog.submissions_today(self.db, user_id, day) >= limits.submissions_per_day:
            self.api.send_message(
                chat_id, t(lang, "submit.limit_day", count=limits.submissions_per_day)
            )
            return
        if catalog.pending_count(self.db, user_id) >= limits.pending_per_artist:
            self.api.send_message(
                chat_id, t(lang, "submit.limit_pending", count=limits.pending_per_artist)
            )
            return

        payload = dict(message["audio"])
        filename = payload.get("file_name") or ""
        self.api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=15)
        result = catalog.intake(self.db, self.config, self.api, user_id, payload, filename)

        if result.error == "duplicate_file" or result.error == "duplicate_track":
            self.api.send_message(chat_id, t(lang, "submit.duplicate"))
            return
        if result.error == "too_short":
            self.api.send_message(chat_id, t(lang, "submit.too_short", seconds=limits.min_duration))
            return
        if not result.ok:
            self.api.send_message(chat_id, t(lang, "submit.failed"))
            return

        catalog.note_submission(self.db, user_id, day)
        ahead = max(0, catalog.pending_releases_total(self.db) - 1)
        body = t(
            lang,
            "submit.received",
            title=ui.esc(result.title),
            artist=ui.esc(result.artist),
            duration=ui.hms(result.duration),
        )
        if result.release_title:
            body += "\n" + t(
                lang,
                "submit.in_release",
                title=ui.esc(result.release_title),
                n=result.track_no or 1,
            )
        body += "\n\n" + t(lang, "submit.queued_position", count=ahead)

        # Artwork is a publication requirement, so ask for it now rather than
        # letting the release sit in the queue blocked on something the artist
        # could have fixed in one message.
        if result.needs_cover and self.config.require_cover:
            body += "\n\n" + t(lang, "cover.needed")
            self._set_state(user_id, f"covr:{result.release_id}")
        else:
            body += "\n\n" + t(lang, "submit.edit_hint")
            self._set_state(user_id, f"fix:{result.track_id}")
        if (result.track_no or 1) == 1:
            body += "\n\n" + t(lang, "submit.release_hint")
        self.api.send_message(chat_id, body)

        # One card per release, not one per track: a ten-track album should not
        # produce ten notifications.
        if (result.track_no or 1) == 1 and result.release_id:
            self._notify_curators(result.release_id)

    def _notify_curators(self, release_id: int) -> None:
        """Push a review card to the review chat, or to each curator directly."""
        item = catalog.release(self.db, release_id)
        if item is None:
            return
        targets: list[int] = []
        if self.config.review_chat:
            targets.append(self.config.review_chat)
        else:
            targets.extend(self.config.curators)
            if self.config.owner:
                targets.append(self.config.owner)
        lang = self.config.lang
        screen = ui.review_release(lang, item)
        text = f"<b>{t(lang, 'mod.new_submission')}</b>\n\n{screen.text}"
        for chat_id in dict.fromkeys(targets):
            if screen.photo:
                self.api.send_photo(
                    chat_id,
                    screen.photo,
                    caption=text[:1024],
                    reply_markup=screen.markup,
                    disable_notification=True,
                )
            else:
                self.api.send_message(
                    chat_id, text, reply_markup=screen.markup, disable_notification=True
                )

    # ------------------------------------------------------------ callbacks
    def _on_callback(self, query: dict[str, Any]) -> None:
        tg_user = query.get("from") or {}
        message = query.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or tg_user.get("id") or 0)
        callback_id = query.get("id") or ""
        if not chat_id:
            self.api.answer_callback(callback_id)
            return
        user = self._user(tg_user)
        lang = self._lang(user)
        if user.get("banned"):
            self.api.answer_callback(callback_id)
            return
        parts = unpack(query.get("data") or "")
        action = parts[0] if parts else ""
        args = parts[1:]

        try:
            self._route_callback(chat_id, user, callback_id, action, args, message)
        except TelegramError as exc:
            if not exc.blocked_by_user:
                raise
        except Exception:
            log.exception("callback %s failed", action)
            self.api.answer_callback(callback_id, t(lang, "common.error"))

    def _route_callback(
        self,
        chat_id: int,
        user: dict[str, Any],
        callback_id: str,
        action: str,
        args: list[str],
        message: dict[str, Any],
    ) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        answered = False

        def ack(text: str = "", alert: bool = False) -> None:
            nonlocal answered
            if not answered:
                self.api.answer_callback(callback_id, text, alert)
                answered = True

        if action == "nav":
            ack()
            self._set_state(user_id)
            target = args[0] if args else "home"
            self._render_named(chat_id, user, target)

        elif action == "play" and args:
            ack()
            self._play(chat_id, user, int(args[0]))

        elif action == "seq" and len(args) >= 2:
            ack()
            self._play_sequence_step(chat_id, user, int(args[1]))

        elif action == "like" and args:
            track_id = int(args[0])
            liked_now = not catalog.is_liked(self.db, user_id, track_id)
            catalog.set_like(self.db, user_id, track_id, liked_now)
            recommend.record(self.db, user_id, track_id, "like" if liked_now else "skip")
            ack(t(lang, "track.was_saved" if liked_now else "track.was_unsaved"))
            item = catalog.track(self.db, track_id)
            if item and message.get("message_id"):
                data = self._data(user)
                sequence = data.get("seq") or []
                position = sequence.index(track_id) if track_id in sequence else None
                self.api.edit_markup(
                    chat_id,
                    int(message["message_id"]),
                    ui.track_buttons(
                        item,
                        lang,
                        liked_now,
                        context="seq" if sequence else "",
                        position=position,
                        total=len(sequence) if sequence else None,
                    ),
                )

        elif action == "artist" and args:
            ack()
            self._render_artist(chat_id, user, int(args[0]))

        elif action == "follow" and args:
            artist_id = int(args[0])
            following = not catalog.is_following(self.db, user_id, artist_id)
            catalog.set_follow(self.db, user_id, artist_id, following)
            recommend.record(self.db, user_id, None, "follow" if following else "skip")
            artist = catalog.artist_row(self.db, artist_id) or {}
            ack(
                t(lang, "artist.followed", name=artist.get("name", ""))
                if following
                else t(lang, "artist.unfollowed")
            )
            self._render_artist(chat_id, user, artist_id)

        elif action == "similar" and args:
            ack()
            self._render_similar(chat_id, user, int(args[0]))

        elif action == "disc":
            ack()
            self._do_discover(chat_id, user)

        elif action == "tag" and args:
            ack()
            self._render_tag(chat_id, user, args[0])

        elif action == "rl" and args:
            ack()
            self._render_release(chat_id, user, int(args[0]))

        elif action == "relall" and args:
            ack()
            self._start_release(chat_id, user, int(args[0]))

        elif action == "rev" and args:
            ack()
            if self.config.is_curator(user_id):
                self._render_release_review(chat_id, user, int(args[0]))

        elif action == "rel" and len(args) >= 2:
            if not self.config.is_curator(user_id):
                ack(t(lang, "mod.not_curator"), alert=True)
                return
            self._moderate_release(chat_id, user, ack, args[0], int(args[1]))

        elif action == "mix":
            ack()
            self._render_mix(chat_id, user)

        elif action == "pl" and args:
            ack()
            self._render_playlist(chat_id, user, int(args[0]))

        elif action == "plall" and args:
            ack()
            self._start_playlist(chat_id, user, int(args[0]))

        elif action == "lang" and args:
            new_lang = normalise(args[0])
            if new_lang in LANGUAGES:
                catalog.set_user(self.db, user_id, lang=new_lang)
                user["lang"] = new_lang
            ack()
            self.show(
                chat_id,
                user,
                ui.settings_screen(
                    new_lang, bool(user.get("digest")), self.config.is_curator(user_id)
                ),
            )

        elif action == "digest":
            new_value = 0 if user.get("digest") else 1
            catalog.set_user(self.db, user_id, digest=new_value)
            user["digest"] = new_value
            ack()
            self.show(
                chat_id,
                user,
                ui.settings_screen(lang, bool(new_value), self.config.is_curator(user_id)),
            )

        elif action == "forget":
            if args and args[0] == "do":
                self._forget(user_id)
                ack(t(lang, "settings.forget_done"), alert=True)
                self._render_home(chat_id, user)
            else:
                ack()
                self.show(
                    chat_id,
                    user,
                    ui.Screen(
                        t(lang, "settings.forget_confirm"),
                        ui.keyboard(
                            [ui.button(t(lang, "settings.forget"), pack("forget", "do"))],
                            [ui.button(t(lang, "common.cancel"), pack("nav", "settings"))],
                        ),
                    ),
                )

        elif action == "review" and args:
            ack()
            if self.config.is_curator(user_id):
                self._render_review(chat_id, user, int(args[0]))

        elif action == "mod" and len(args) >= 2:
            if not self.config.is_curator(user_id):
                ack(t(lang, "mod.not_curator"), alert=True)
                return
            self._moderate(chat_id, user, ack, args[0], int(args[1]), message)

        else:
            ack(t(lang, "common.stale"))

        ack()

    # --------------------------------------------------------------- render
    def _render_named(self, chat_id: int, user: dict[str, Any], target: str) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        if target == "home":
            self._render_home(chat_id, user)
        elif target == "today":
            self._render_today(chat_id, user)
        elif target == "discover":
            self._render_discover(chat_id, user)
        elif target == "search":
            self._set_state(user_id, "search")
            self.show(chat_id, user, ui.search_prompt(lang, search.suggest_tags(self.db)))
        elif target == "library":
            self._render_library(chat_id, user)
        elif target == "follows":
            self.show(
                chat_id,
                user,
                ui.artists_screen(
                    lang,
                    catalog.followed_artists(self.db, user_id),
                    t(lang, "library.following"),
                ),
            )
        elif target == "releases":
            self.show(
                chat_id,
                user,
                ui.releases_screen(lang, catalog.newest_releases(self.db, limit=12)),
            )
        elif target == "artists":
            self._render_artists(chat_id, user)
        elif target == "mixes":
            self.show(chat_id, user, ui.mixes_screen(lang, catalog.playlists(self.db)))
        elif target == "submit":
            self._render_submit(chat_id, user)
        elif target == "settings":
            self.show(
                chat_id,
                user,
                ui.settings_screen(lang, bool(user.get("digest")), self.config.is_curator(user_id)),
            )
        elif target == "help":
            self.show(chat_id, user, ui.help_screen(lang, self.config))
        elif target == "queue":
            self._render_queue(chat_id, user)
        else:
            self._render_home(chat_id, user)

    def _counts(self) -> dict[str, int]:
        return {
            "releases": int(
                self.db.scalar("SELECT COUNT(*) FROM releases WHERE status='approved'", default=0)
            ),
            "tracks": int(
                self.db.scalar("SELECT COUNT(*) FROM tracks WHERE status='approved'", default=0)
            ),
            "artists": int(
                self.db.scalar(
                    "SELECT COUNT(DISTINCT artist_id) FROM tracks WHERE status='approved'",
                    default=0,
                )
            ),
        }

    def _daily_progress(self, user_id: int) -> Sequence[Any]:
        ids = self.engine.daily_selection(user_id)
        if not ids:
            return [], 0
        marks = ",".join("?" * len(ids))
        played = int(
            self.db.scalar(
                f"SELECT COUNT(DISTINCT track_id) FROM events WHERE user_id=? "
                f"AND kind='play' AND track_id IN ({marks})",
                (user_id, *ids),
                default=0,
            )
        )
        return ids, played

    def _render_start(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        text = (
            f"<b>{ui.esc(self.config.station_name)}</b>\n"
            f"<i>{ui.esc(self.config.station_tagline or t(lang, 'app.tagline'))}</i>\n\n"
            f"{t(lang, 'start.body')}"
        )
        self.api.send_message(chat_id, text)
        self._render_home(chat_id, user, fresh=True)

    def _render_home(self, chat_id: int, user: dict[str, Any], fresh: bool = False) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        ids, played = self._daily_progress(user_id)
        self.show(
            chat_id,
            user,
            ui.home(
                lang,
                self.config,
                self._counts(),
                max(0, len(ids) - played),
                len(ids),
                self.config.is_curator(user_id),
                catalog.pending_total(self.db) if self.config.is_curator(user_id) else 0,
            ),
            fresh=fresh,
        )

    def _render_today(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        ids, played = self._daily_progress(user_id)
        items = catalog.tracks(self.db, ids)
        finished = bool(items) and played >= len(items)
        self.show(chat_id, user, ui.today_screen(lang, items, played, finished))

    def _render_discover(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        used = int(
            self.db.scalar(
                "SELECT discovers FROM usage WHERE user_id=? AND day=?",
                (user_id, recommend.today()),
                default=0,
            )
        )
        left = max(0, self.config.limits.discover_sessions_per_day - used)
        self.show(chat_id, user, ui.discover_screen(lang, left, self.config.limits.discover_batch))

    def _do_discover(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        day = recommend.today()
        used = int(
            self.db.scalar(
                "SELECT discovers FROM usage WHERE user_id=? AND day=?", (user_id, day), default=0
            )
        )
        if used >= self.config.limits.discover_sessions_per_day:
            self.show(chat_id, user, ui.discover_screen(lang, 0, 0))
            return
        picks = self.engine.discover(user_id)
        if not picks:
            self.show(chat_id, user, ui.discover_screen(lang, 0, 0, exhausted=True))
            return
        self.db.execute(
            "INSERT INTO usage(user_id, day, discovers) VALUES(?,?,1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET discovers = discovers + 1",
            (user_id, day),
        )
        items = catalog.tracks(self.db, picks)
        left = max(0, self.config.limits.discover_sessions_per_day - used - 1)
        self.show(
            chat_id,
            user,
            ui.track_list(
                lang,
                t(lang, "discover.title"),
                t(lang, "discover.intro", count=len(items), left=left),
                items,
                "play",
                extra_rows=(
                    [
                        [
                            ui.button(
                                t(
                                    lang,
                                    "discover.request",
                                    count=self.config.limits.discover_batch,
                                ),
                                pack("disc", "go"),
                            )
                        ]
                    ]
                    if left
                    else None
                ),
            ),
        )

    def _render_search(self, chat_id: int, user: dict[str, Any], query: str) -> None:
        lang = self._lang(user)
        results = search.search(self.db, query, limit=self.config.limits.page_size)
        items = catalog.tracks(self.db, [track_id for track_id, _ in results])
        artists = search.search_artists(self.db, query, limit=5)
        extra: list[list[dict[str, str]]] = []
        if artists:
            row = []
            for artist_id in artists[:3]:
                artist = catalog.artist_row(self.db, artist_id)
                if artist:
                    row.append(ui.button(artist["name"][:24], pack("artist", artist_id)))
            if row:
                extra.append(row)
        self.show(
            chat_id,
            user,
            ui.track_list(
                lang,
                t(lang, "nav.search").upper(),
                t(lang, "search.results", query=ui.esc(query[:40])) if items else "",
                items,
                "play",
                extra_rows=extra or None,
                empty=t(lang, "search.none", query=query[:40]),
            ),
        )

    def _render_tag(self, chat_id: int, user: dict[str, Any], tag: str) -> None:
        lang = self._lang(user)
        items = catalog.tracks(self.db, search.tracks_by_tag(self.db, tag, limit=12))
        self.show(
            chat_id,
            user,
            ui.track_list(lang, tag.upper(), "", items, "play"),
        )

    def _render_library(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        saved = catalog.liked(self.db, user_id, limit=self.config.limits.page_size)
        follows = catalog.followed_artists(self.db, user_id, limit=12)
        self.show(chat_id, user, ui.library_screen(lang, saved, follows))

    def _render_artists(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        rows = self.db.query(
            "SELECT a.id, a.name, COUNT(t.id) AS n FROM artists a "
            "JOIN tracks t ON t.artist_id = a.id AND t.status='approved' "
            "GROUP BY a.id ORDER BY a.name COLLATE NOCASE LIMIT 20"
        )
        self.show(chat_id, user, ui.artists_screen(lang, [dict(r) for r in rows]))

    def _render_artist(self, chat_id: int, user: dict[str, Any], artist_id: int) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        artist = catalog.artist_row(self.db, artist_id)
        if artist is None:
            self.show(chat_id, user, ui.Screen(t(lang, "common.not_found")))
            return
        items = catalog.artist_tracks(self.db, artist_id)
        own = artist.get("user_id") == user_id
        self.show(
            chat_id,
            user,
            ui.artist_screen(
                lang,
                artist,
                items,
                catalog.is_following(self.db, user_id, artist_id),
                catalog.artist_stats(self.db, artist_id) if own else None,
                own,
            ),
        )

    def _render_similar(self, chat_id: int, user: dict[str, Any], track_id: int) -> None:
        lang = self._lang(user)
        source = catalog.track(self.db, track_id)
        if source is None:
            return
        items = catalog.tracks(self.db, self.engine.similar_to(track_id, count=6))
        self.show(
            chat_id,
            user,
            ui.track_list(
                lang,
                t(lang, "track.similar").upper(),
                t(lang, "track.similar_for", title=ui.esc(source["title"])),
                items,
                "play",
                empty=t(lang, "track.similar_none"),
            ),
            fresh=True,
        )

    def _render_release(self, chat_id: int, user: dict[str, Any], release_id: int) -> None:
        """The album page: artwork, tracklist, one tap per track."""
        lang = self._lang(user)
        item = catalog.release(self.db, release_id, only_approved=True)
        if item is None or item["status"] != catalog.STATUS_APPROVED or not item["tracks"]:
            self.show(chat_id, user, ui.Screen(t(lang, "common.not_found")))
            return
        # The release becomes the current sequence, so "play it through" and
        # the Next button on each track agree about what comes after.
        self._set_data(
            user, {"seq": [track["id"] for track in item["tracks"]], "label": item["title"]}
        )
        self.show(chat_id, user, ui.release_screen(lang, item))

    def _start_release(self, chat_id: int, user: dict[str, Any], release_id: int) -> None:
        item = catalog.release(self.db, release_id, only_approved=True)
        if not item or not item["tracks"]:
            return
        self._set_data(
            user, {"seq": [track["id"] for track in item["tracks"]], "label": item["title"]}
        )
        self._play_sequence_step(chat_id, user, 0)

    def _render_mix(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        order = self.engine.mix(user_id)
        if not order:
            self.show(chat_id, user, ui.mixes_screen(lang, catalog.playlists(self.db)))
            return
        self._set_data(user, {"seq": order, "label": t(lang, "mixes.auto")})
        items = catalog.tracks(self.db, order)
        self.show(
            chat_id,
            user,
            ui.track_list(
                lang,
                t(lang, "mixes.auto").upper(),
                t(lang, "mixes.auto_hint"),
                items,
                "play",
                extra_rows=[[ui.button(t(lang, "common.next"), pack("seq", "m", 0))]],
            ),
        )

    def _render_playlist(self, chat_id: int, user: dict[str, Any], playlist_id: int) -> None:
        lang = self._lang(user)
        item = catalog.playlist(self.db, playlist_id)
        if item is None:
            return
        self.show(chat_id, user, ui.playlist_screen(lang, item))

    def _start_playlist(self, chat_id: int, user: dict[str, Any], playlist_id: int) -> None:
        item = catalog.playlist(self.db, playlist_id)
        if not item or not item["tracks"]:
            return
        self._set_data(user, {"seq": [t["id"] for t in item["tracks"]], "label": item["title"]})
        self._play_sequence_step(chat_id, user, 0)

    def _render_submit(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        mine = catalog.submitted_by(self.db, user_id, limit=10)
        used = catalog.submissions_today(self.db, user_id, recommend.today())
        self.show(
            chat_id,
            user,
            ui.submit_screen(lang, mine, max(0, self.config.limits.submissions_per_day - used)),
        )

    def _render_stats(self, chat_id: int, user: dict[str, Any]) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        if not self.config.is_curator(user_id):
            rows = self.db.query("SELECT id FROM artists WHERE user_id=? LIMIT 1", (user_id,))
            if rows:
                self._render_artist(chat_id, user, int(rows[0]["id"]))
            else:
                self.api.send_message(chat_id, t(lang, "stats.no_tracks"))
            return
        self.show(chat_id, user, ui.stats_screen(lang, catalog.curator_report(self.db)))

    # ------------------------------------------------------------- playback
    def _play(self, chat_id: int, user: dict[str, Any], track_id: int) -> None:
        item = catalog.track(self.db, track_id)
        if item is None or item["status"] != catalog.STATUS_APPROVED:
            self.api.send_message(chat_id, t(self._lang(user), "common.not_found"))
            return
        int(user["id"])
        data = self._data(user)
        sequence = data.get("seq") or []
        position = sequence.index(track_id) if track_id in sequence else None
        self._send_track(chat_id, user, item, position, len(sequence) if sequence else None)
        self._render_home(chat_id, user, fresh=True)

    def _play_sequence_step(self, chat_id: int, user: dict[str, Any], index: int) -> None:
        int(user["id"])
        data = self._data(user)
        sequence = [int(x) for x in (data.get("seq") or [])]
        if not sequence or index < 0 or index >= len(sequence):
            self._render_home(chat_id, user)
            return
        item = catalog.track(self.db, sequence[index])
        if item is None or item["status"] != catalog.STATUS_APPROVED:
            # Skip a withdrawn track rather than stalling the sequence.
            if index + 1 < len(sequence):
                self._play_sequence_step(chat_id, user, index + 1)
            return
        self._send_track(chat_id, user, item, index, len(sequence))

    def _send_track(
        self,
        chat_id: int,
        user: dict[str, Any],
        item: dict[str, Any],
        position: int | None = None,
        total: int | None = None,
    ) -> None:
        """Send one audio message. The only place a track leaves the service."""
        lang = self._lang(user)
        user_id = int(user["id"])
        liked = catalog.is_liked(self.db, user_id, item["id"])
        sent = self.api.send_audio(
            chat_id,
            item["file_id"],
            caption=ui.track_caption(item, lang),
            reply_markup=ui.track_buttons(
                item,
                lang,
                liked,
                context="seq" if total else "",
                position=position,
                total=total,
            ),
            title=item["title"],
            performer=item["artist"],
            duration=item["duration"] or None,
        )
        if sent is None:
            return
        recommend.record(self.db, user_id, item["id"], "play")
        recommend.record_exposure(self.db, [item["id"]])

    def _render_track_page(self, chat_id: int, user: dict[str, Any], track_id: int) -> None:
        item = catalog.track(self.db, track_id)
        if item is None or item["status"] != catalog.STATUS_APPROVED:
            self.api.send_message(chat_id, t(self._lang(user), "common.not_found"))
            self._render_home(chat_id, user)
            return
        self._send_track(chat_id, user, item)
        self._render_home(chat_id, user, fresh=True)

    # ----------------------------------------------------------- moderation
    def _render_queue(self, chat_id: int, user: dict[str, Any]) -> None:
        """The queue is a list of releases, not of loose tracks."""
        lang = self._lang(user)
        if not self.config.is_curator(int(user["id"])):
            self.api.send_message(chat_id, t(lang, "mod.not_curator"))
            return
        items = catalog.pending_releases(self.db, limit=self.config.limits.page_size)
        self.show(
            chat_id,
            user,
            ui.release_queue_screen(lang, items, catalog.pending_releases_total(self.db)),
        )

    def _render_release_review(self, chat_id: int, user: dict[str, Any], release_id: int) -> None:
        lang = self._lang(user)
        item = catalog.release(self.db, release_id)
        if item is None:
            self._render_queue(chat_id, user)
            return
        self.show(chat_id, user, ui.review_release(lang, item))

    def _moderate_release(
        self, chat_id: int, user: dict[str, Any], ack, verb: str, release_id: int
    ) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        if verb == "ok":
            published, error = catalog.approve_release(self.db, release_id, user_id)
            if error:
                ack(t(lang, f"mod.{error}"), alert=True)
                return
            self.engine.invalidate()
            ack(t(lang, "mod.release_published"))
            for track_item in (published or {}).get("tracks", []):
                self._notify_artist(catalog.track(self.db, int(track_item["id"])), approved=True)
                break  # one message per release, not one per track
            self._render_queue(chat_id, user)
        elif verb == "no":
            self._set_state(user_id, f"relreason:{release_id}")
            ack()
            self.show(
                chat_id,
                user,
                ui.Screen(
                    t(lang, "mod.ask_reason"),
                    ui.keyboard(
                        [ui.button(t(lang, "common.skip"), pack("rel", "no0", release_id))],
                        [ui.button(t(lang, "common.cancel"), pack("nav", "queue"))],
                    ),
                ),
            )
        elif verb == "no0":
            self._set_state(user_id)
            ack(t(lang, "mod.release_rejected"))
            self._finish_release_rejection(chat_id, user, release_id, "")
        elif verb == "cov":
            self._set_state(user_id, f"relcover:{release_id}")
            ack()
            self.show(chat_id, user, ui.prompt(lang, t(lang, "mod.ask_cover"), cancel_to="queue"))
        elif verb == "note":
            self._set_state(user_id, f"relnote:{release_id}")
            ack()
            self.show(chat_id, user, ui.prompt(lang, t(lang, "mod.ask_note"), cancel_to="queue"))

    def _finish_release_rejection(
        self, chat_id: int, user: dict[str, Any], release_id: int, reason: str
    ) -> None:
        lang = self._lang(user)
        item = catalog.reject_release(self.db, release_id, int(user["id"]), reason)
        self.engine.invalidate()
        if item and item["tracks"]:
            self._notify_artist(
                catalog.track(self.db, int(item["tracks"][0]["id"])), approved=False
            )
        self.api.send_message(chat_id, t(lang, "mod.release_rejected"))
        self._render_queue(chat_id, user)

    def _on_photo(self, chat_id: int, user: dict[str, Any], message: dict[str, Any]) -> None:
        """A picture is only ever meaningful as artwork, and only when asked for."""
        lang = self._lang(user)
        user_id = int(user["id"])
        state = user.get("state") or ""
        kind, _, argument = state.partition(":")
        if kind not in ("relcover", "covr") or not argument.isdigit():
            return
        release_id = int(argument)
        if kind == "relcover" and not self.config.is_curator(user_id):
            return
        if kind == "covr":
            owner = self.db.scalar("SELECT submitted_by FROM releases WHERE id=?", (release_id,))
            if owner is not None and int(owner) != user_id:
                return

        file_id = _largest_photo(message)
        if not file_id:
            self.api.send_message(chat_id, t(lang, "cover.not_image"))
            return
        catalog.set_release_cover(self.db, release_id, file_id)
        self._set_state(user_id)
        self.api.send_message(chat_id, t(lang, "cover.saved"))
        if self.config.is_curator(user_id):
            self._render_release_review(chat_id, user, release_id)
        else:
            self._render_submit(chat_id, user)

    def _render_review(self, chat_id: int, user: dict[str, Any], track_id: int) -> None:
        lang = self._lang(user)
        item = catalog.track(self.db, track_id)
        if item is None:
            self._render_queue(chat_id, user)
            return
        self.api.send_audio(
            chat_id,
            item["file_id"],
            caption=ui.review_caption(lang, item),
            reply_markup=ui.review_buttons(lang, track_id),
            title=item["title"],
            performer=item["artist"],
            duration=item["duration"] or None,
            disable_notification=True,
        )

    def _moderate(
        self,
        chat_id: int,
        user: dict[str, Any],
        ack,
        verb: str,
        track_id: int,
        message: dict[str, Any],
    ) -> None:
        lang = self._lang(user)
        user_id = int(user["id"])
        if verb == "ok":
            item = catalog.approve(self.db, track_id, user_id)
            self.engine.invalidate()
            ack(t(lang, "mod.approved"))
            if message.get("message_id"):
                self.api.edit_markup(chat_id, int(message["message_id"]), None)
            if item:
                self._notify_artist(item, approved=True)
            self._render_queue(chat_id, user)
        elif verb == "no":
            self._set_state(user_id, f"modreason:{track_id}")
            ack()
            self.show(
                chat_id,
                user,
                ui.Screen(
                    t(lang, "mod.ask_reason"),
                    ui.keyboard(
                        [ui.button(t(lang, "common.skip"), pack("mod", "no0", track_id))],
                        [ui.button(t(lang, "common.cancel"), pack("nav", "queue"))],
                    ),
                ),
            )
        elif verb == "no0":
            self._set_state(user_id)
            ack(t(lang, "mod.rejected"))
            self._finish_rejection(chat_id, user, track_id, "")
        elif verb in ("tag", "note", "ed"):
            key = {"tag": "modtags", "note": "modnote", "ed": "moded"}[verb]
            prompt_key = {"tag": "mod.ask_tags", "note": "mod.ask_note", "ed": "mod.ask_edit"}[verb]
            self._set_state(user_id, f"{key}:{track_id}")
            ack()
            self.show(chat_id, user, ui.prompt(lang, t(lang, prompt_key), cancel_to="queue"))

    def _finish_rejection(
        self, chat_id: int, user: dict[str, Any], track_id: int, reason: str
    ) -> None:
        lang = self._lang(user)
        item = catalog.reject(self.db, track_id, int(user["id"]), reason)
        self.engine.invalidate()
        if item:
            self._notify_artist(item, approved=False)
        self.api.send_message(chat_id, t(lang, "mod.rejected"))
        self._render_queue(chat_id, user)

    def _notify_artist(self, item: dict[str, Any], approved: bool) -> None:
        """The one message this service sends without being asked.

        An artist who submitted work is owed an answer; that is a reply, not a
        notification, and it is the only unsolicited message in the product.
        """
        target = item.get("submitted_by")
        if not target:
            return
        row = catalog.get_user(self.db, int(target))
        lang = normalise((row or {}).get("lang") or self.config.lang)
        if approved:
            text = t(lang, "submit.approved_notice", title=ui.esc(item["title"]))
            markup = ui.keyboard(
                [ui.button(t(lang, "track.artist"), pack("artist", item["artist_id"]))]
            )
        else:
            text = t(lang, "submit.rejected_notice", title=ui.esc(item["title"]))
            if item.get("reject_reason"):
                text += "\n" + t(
                    lang, "submit.rejected_reason", reason=ui.esc(item["reject_reason"])
                )
            markup = None
        self.api.send_message(int(target), text, reply_markup=markup)

    # --------------------------------------------------------------- inline
    def _on_inline(self, query: dict[str, Any]) -> None:
        tg_user = query.get("from") or {}
        user = self._user(tg_user)
        lang = self._lang(user)
        text = (query.get("query") or "").strip()
        if text:
            ids = [track_id for track_id, _ in search.search(self.db, text, limit=12)]
        else:
            ids = [item["id"] for item in catalog.newest(self.db, limit=12)]
        items = catalog.tracks(self.db, ids)
        self.api.answer_inline(query["id"], ui.inline_results(items, lang))

    # ----------------------------------------------------------------- data
    def _forget(self, user_id: int) -> None:
        """Erase everything personal. Aggregate counters are left intact."""
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM likes WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM follows WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM daily WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM usage WHERE user_id=?", (user_id,))
            conn.execute("UPDATE users SET state=NULL, state_data=NULL WHERE id=?", (user_id,))


def _largest_photo(message: dict[str, Any]) -> str | None:
    """The best available ``file_id`` for an image, however it was sent."""
    sizes = message.get("photo") or []
    if sizes:
        return str(sizes[-1]["file_id"])
    document = message.get("document") or {}
    if str(document.get("mime_type") or "").startswith("image/"):
        return str(document["file_id"])
    return None


def command_menu(lang: str) -> list[dict[str, str]]:
    """The ``/`` menu Telegram shows in the compose box."""
    return [
        {"command": "today", "description": t(lang, "nav.today")},
        {"command": "discover", "description": t(lang, "nav.discover")},
        {"command": "search", "description": t(lang, "nav.search")},
        {"command": "library", "description": t(lang, "nav.library")},
        {"command": "submit", "description": t(lang, "nav.submit")},
        {"command": "settings", "description": t(lang, "nav.settings")},
        {"command": "help", "description": t(lang, "nav.help")},
    ]
