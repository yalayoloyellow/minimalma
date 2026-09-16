"""The running service: polling loop, workers and background maintenance."""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from . import catalog, handlers, recommend, ui
from .config import Config
from .db import Database
from .i18n import normalise, t
from .telegram import Api, NetworkError, TelegramError

log = logging.getLogger("minimalma")

#: Handlers run on a small pool. Updates are sharded by chat id so that two
#: messages from the same person can never be processed out of order.
WORKERS = 4
POLL_TIMEOUT = 25
MAINTENANCE_INTERVAL = 300
MAX_POLL_CONFLICTS = 3


def configure_logging(config: Config) -> None:
    level = {"quiet": logging.WARNING, "normal": logging.INFO, "debug": logging.DEBUG}.get(
        config.log_level, logging.INFO
    )
    root = logging.getLogger("minimalma")
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    try:
        config.home.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            config.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(rotating)
    except OSError as exc:  # pragma: no cover - read-only home
        log.warning("file logging disabled: %s", exc)


class Service:
    """Owns every long-lived object and the loop that drives them."""

    def __init__(self, config: Config, api: Api | None = None, db: Database | None = None):
        self.config = config
        self.db = db or Database(config.database_path)
        self.api = api or Api(config.token)
        self.engine = recommend.Engine(self.db, config)
        self._media_jobs = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media")
        self.bot = handlers.Bot(
            config, self.db, self.api, self.engine, background=self._media_jobs.submit
        )
        self._stop = threading.Event()
        self._queues: list[queue.Queue[dict[str, Any] | None]] = [
            queue.Queue(maxsize=256) for _ in range(WORKERS)
        ]
        self._threads: list[threading.Thread] = []
        self._instance_lock_fd: int | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> dict[str, Any]:
        """Validate the token, clear any webhook and register the command menu."""
        identity = self.api.me()
        self.api.drop_webhook()
        for lang in ("en", "ru"):
            try:
                self.api.set_commands(handlers.command_menu(lang), lang)
            except (TelegramError, NetworkError) as exc:
                log.debug("setMyCommands(%s) skipped: %s", lang, exc)
        return identity

    def run(self) -> None:
        if not self._acquire_instance_lock():
            log.error("minimalma is already running")
            return
        try:
            self._run_locked()
        finally:
            self._release_instance_lock()

    def _run_locked(self) -> None:
        identity = self.start()
        log.info(
            "%s is live as @%s · %s",
            self.config.station_name,
            identity.get("username", "?"),
            _summary(self.db),
        )
        if not self.config.curators and not self.config.owner:
            log.warning(
                "no curators configured — add one in the minimalma desk "
                "or nothing can be published"
            )

        for index in range(WORKERS):
            thread = threading.Thread(
                target=self._worker, args=(index,), name=f"worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        maintenance = threading.Thread(target=self._maintenance, name="maintenance", daemon=True)
        maintenance.start()

        self._install_signals()
        try:
            self._poll()
        finally:
            self.shutdown()

    def _acquire_instance_lock(self) -> bool:
        """Allow only one polling process for this station on this machine."""
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows has no flock
            return True
        path = self.config.home / "minimalma.lock"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.chmod(path, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                os.close(fd)
                return False
        except OSError:
            log.warning("could not create the instance lock; continuing")
            return True
        self._instance_lock_fd = fd
        return True

    def _release_instance_lock(self) -> None:
        if self._instance_lock_fd is None:
            return
        try:
            import fcntl
            fcntl.flock(self._instance_lock_fd, fcntl.LOCK_UN)
        except (ImportError, OSError):  # pragma: no cover
            pass
        try:
            os.close(self._instance_lock_fd)
        except OSError:  # pragma: no cover
            pass
        self._instance_lock_fd = None

    def _install_signals(self) -> None:
        def stop(signum: int, _frame: Any) -> None:
            log.info("stopping")
            self._stop.set()

        for name in ("SIGINT", "SIGTERM"):
            handler = getattr(signal, name, None)
            if handler is not None:
                try:
                    signal.signal(handler, stop)
                except ValueError:  # pragma: no cover - not the main thread
                    pass

    def shutdown(self) -> None:
        self._stop.set()
        for shard in self._queues:
            try:
                shard.put_nowait(None)
            except queue.Full:  # pragma: no cover
                pass
        for thread in self._threads:
            thread.join(timeout=5)
        self._media_jobs.shutdown(wait=True, cancel_futures=True)
        self.db.close()

    # ---------------------------------------------------------------- loop
    def _poll(self) -> None:
        offset = int(self.db.get_meta("update_offset", 0) or 0)
        failures = 0
        conflicts = 0
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(offset, timeout=POLL_TIMEOUT)
                failures = 0
                conflicts = 0
            except TelegramError as exc:
                if exc.code == 409:
                    conflicts += 1
                    if conflicts >= MAX_POLL_CONFLICTS:
                        log.error("another instance is polling this bot; stopping")
                        self._stop.set()
                        break
                    log.error("another instance is polling this bot; retrying in 15s")
                    self._stop.wait(15)
                    self.api.drop_webhook()
                    continue
                if exc.code == 401:
                    log.error("the bot token was rejected — reconnect it in minimalma")
                    self._stop.set()
                    break
                failures += 1
                log.warning("getUpdates failed: %s", exc)
                self._stop.wait(min(30, 2**failures))
                continue
            except NetworkError as exc:
                failures += 1
                log.warning("network unavailable: %s", exc)
                self._stop.wait(min(30, 2**failures))
                continue

            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                self._dispatch(update)
            if updates:
                self.db.set_meta("update_offset", offset)

    def _dispatch(self, update: dict[str, Any]) -> None:
        shard = _shard_for(update) % WORKERS
        try:
            self._queues[shard].put(update, timeout=5)
        except queue.Full:  # pragma: no cover - only under extreme load
            log.error("worker %d is saturated, dropping update", shard)

    def _worker(self, index: int) -> None:
        shard = self._queues[index]
        while not self._stop.is_set():
            try:
                update = shard.get(timeout=1)
            except queue.Empty:
                continue
            if update is None:
                return
            self.bot.handle(update)

    # -------------------------------------------------------- maintenance
    def _maintenance(self) -> None:
        """Similarity rebuilds and the weekly digest. Never blocks the loop."""
        while not self._stop.wait(MAINTENANCE_INTERVAL):
            try:
                self.engine.maybe_rebuild()
            except Exception:
                log.exception("similarity rebuild failed")
            try:
                removed = recommend.prune_events(self.db)
                if removed:
                    log.info("pruned %d old interaction events", removed)
            except Exception:
                log.exception("event retention failed")
            try:
                if self.config.weekly_digest:
                    self.send_digest()
            except Exception:
                log.exception("digest failed")

    def send_digest(self, force: bool = False) -> int:
        """Send the opt-in weekly note. Sunday only, once, to subscribers.

        This is the sole scheduled outbound message in the product, it must be
        turned on by the user, and it carries no counters or urgency.
        """
        stamp = datetime.now()
        if not force and stamp.weekday() != 6:
            return 0
        marker = stamp.strftime("%G-W%V")
        if not force and self.db.get_meta("digest_week") == marker:
            return 0
        self.db.set_meta("digest_week", marker)

        since = int(time.time()) - 7 * 86400
        rows = self.db.query(
            "SELECT t.id FROM tracks t WHERE t.status='approved' AND t.published_at>=? "
            "ORDER BY t.published_at DESC",
            (since,),
        )
        added = [int(row["id"]) for row in rows]
        subscribers = self.db.query("SELECT id, lang FROM users WHERE digest=1 AND banned=0")
        if not subscribers:
            return 0

        sent = 0
        for row in subscribers:
            lang = normalise(row["lang"])
            if not added:
                continue
            picks = catalog.tracks(self.db, added[:3])
            lines = [
                f"<b>{t(lang, 'digest.title')}</b>",
                "",
                t(lang, "digest.body", count=len(added)),
            ]
            lines.append("")
            for index, item in enumerate(picks, start=1):
                lines.append(ui.track_line(index, item))
            markup = ui.keyboard(*ui.numbered([item["id"] for item in picks], "play"))
            if self.api.send_message(int(row["id"]), "\n".join(lines), reply_markup=markup):
                sent += 1
        log.info("weekly note sent to %d listeners", sent)
        return sent


def _shard_for(update: dict[str, Any]) -> int:
    for key in ("message", "edited_message"):
        payload = update.get(key)
        if payload:
            return abs(int((payload.get("chat") or {}).get("id", 0)))
    callback = update.get("callback_query")
    if callback:
        message = callback.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        return abs(int(chat or (callback.get("from") or {}).get("id", 0)))
    inline = update.get("inline_query")
    if inline:
        return abs(int((inline.get("from") or {}).get("id", 0)))
    return 0


def _summary(db: Database) -> str:
    stats = db.stats()
    return (
        f"{stats['approved']} tracks · {stats['artists']} artists · {stats['pending']} in the queue"
    )
