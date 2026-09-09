"""Command line entry point.

``tonearm setup`` then ``tonearm run`` is the whole operator experience. Every
other subcommand is maintenance you should rarely need.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import VERSION_LABEL, __version__, audio, search
from . import config as config_mod
from .app import Service, configure_logging
from .db import Database
from .telegram import Api, NetworkError, TelegramError

BANNER = "tonearm " + VERSION_LABEL


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        # Bare `tonearm` opens the desk when it is present, because that is
        # what someone double-clicking or typing the name wants. Global flags
        # already parsed (--home) are carried over rather than re-parsed.
        if _desk_available():
            args.command = "desk"
            args.handler = _desk
            args.port = 0
            args.browser = False
            args.no_bot = False
        else:
            parser.print_help()
            return 0
    home = Path(args.home).expanduser() if args.home else None
    try:
        cfg = config_mod.load(home)
    except config_mod.ConfigError as exc:
        _fail(str(exc))
        return 2
    return int(args.handler(args, cfg) or 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tonearm",
        description="A curated music streaming service that runs inside a Telegram bot.",
    )
    parser.add_argument("--version", action="version", version=BANNER)
    parser.add_argument(
        "--home", metavar="DIR", help="data directory (default: the per-user application folder)"
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="start the bot (headless)")
    run.set_defaults(handler=_run)

    desk = sub.add_parser("desk", help="open the curation desk")
    desk.add_argument("--port", type=int, default=0, help="fixed port instead of a free one")
    desk.add_argument("--browser", action="store_true", help="skip the native window")
    desk.add_argument("--no-bot", action="store_true", help="do not start the bot with the desk")
    desk.set_defaults(handler=_desk)

    setup = sub.add_parser("setup", help="first-run configuration")
    setup.add_argument("--token", help="bot token, if you would rather not be prompted")
    setup.add_argument("--owner", type=int, help="your Telegram user id")
    setup.add_argument("--review-chat", type=int, help="chat id for the review queue")
    setup.add_argument("--name", help="station name")
    setup.set_defaults(handler=_setup)

    curator = sub.add_parser("curator", help="manage curators")
    curator.add_argument("action", choices=("add", "remove", "list"))
    curator.add_argument("user_id", nargs="?", type=int)
    curator.set_defaults(handler=_curator)

    doctor = sub.add_parser("doctor", help="check the installation")
    doctor.add_argument("--reindex", action="store_true", help="rebuild the search index")
    doctor.add_argument(
        "--rebuild-similarity", action="store_true", help="recompute recommendations"
    )
    doctor.set_defaults(handler=_doctor)

    backup = sub.add_parser("backup", help="write a consistent copy of the database")
    backup.add_argument("path", nargs="?", help="destination file")
    backup.set_defaults(handler=_backup)

    export = sub.add_parser("export", help="dump the approved catalogue as JSON")
    export.add_argument("path", nargs="?", help="destination file (default: stdout)")
    export.set_defaults(handler=_export)

    digest = sub.add_parser("digest", help="send the weekly note now")
    digest.set_defaults(handler=_digest)

    return parser


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _run(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    if not cfg.token:
        _fail("no bot token configured. Run: tonearm setup")
        return 2
    configure_logging(cfg)
    try:
        service = Service(cfg)
    except ValueError as exc:
        _fail(str(exc))
        return 2
    try:
        service.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


def _desk_available() -> bool:
    """True when the optional ``desk/`` directory ships with this install."""
    try:
        import desk  # noqa: F401
    except ImportError:
        return False
    return True


def _desk(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    """Open the desk, and start the bot alongside it unless told not to.

    Running both from one command is the point: a curator should be able to
    launch one thing and have a working station.
    """
    try:
        from desk.launch import open_desk
        from desk.server import Desk
    except ImportError as exc:
        _fail(f"the desk is not available in this installation: {exc}")
        return 2
    configure_logging(cfg)
    desk = Desk(cfg)
    if not args.no_bot and cfg.token:
        status = desk.start_bot()
        if status.get("error"):
            _fail(status["error"])
    return open_desk(cfg, port=args.port, prefer_window=not args.browser, desk=desk)


def _setup(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    print(BANNER)
    print()
    token = args.token or cfg.token
    if not token:
        print("Create a bot with @BotFather and paste the token it gives you.")
        token = _ask("Bot token")
    if not token:
        _fail("a token is required")
        return 2

    try:
        identity = Api(token).me()
    except ValueError as exc:
        _fail(str(exc))
        return 2
    except TelegramError as exc:
        _fail(f"Telegram rejected that token: {exc.description}")
        return 2
    except NetworkError as exc:
        _fail(f"could not reach Telegram: {exc}")
        return 2

    print(f"  connected as @{identity.get('username')} ({identity.get('first_name')})")
    cfg.token = token

    owner = args.owner or cfg.owner
    if not owner:
        print()
        print("Your own Telegram user id makes you the first curator.")
        print("Send /whoami to the bot after it starts if you do not know it.")
        raw = _ask("Your Telegram user id (blank to set later)")
        owner = int(raw) if raw.lstrip("-").isdigit() else None
    cfg.owner = owner

    review = args.review_chat if args.review_chat is not None else cfg.review_chat
    if review is None:
        print()
        print("Submissions can go to a private group so several curators see them.")
        raw = _ask("Review chat id (blank: send them to curators directly)")
        review = int(raw) if raw.lstrip("-").isdigit() else None
    cfg.review_chat = review

    name = args.name or _ask(f"Station name [{cfg.station_name}]") or cfg.station_name
    cfg.station_name = name
    cfg.save()

    Database(cfg.database_path)
    print()
    print(f"  configuration  {cfg.config_path}")
    print(f"  database       {cfg.database_path}")
    tools = audio.toolchain()
    if tools["ffmpeg"] and tools["ffprobe"]:
        print("  ffmpeg         found — loudness, tempo and quality checks enabled")
    else:
        print("  ffmpeg         not found — audio analysis disabled (everything else works)")
    print()
    print("Open the curation desk with:  tonearm desk")
    print("Or run headless with:          tonearm run")
    return 0


def _curator(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    if args.action == "list":
        if cfg.owner:
            print(f"{cfg.owner}  (owner)")
        for user_id in sorted(set(cfg.curators)):
            print(user_id)
        if not cfg.owner and not cfg.curators:
            print("no curators configured")
        return 0
    if args.user_id is None:
        _fail("a Telegram user id is required")
        return 2
    if args.action == "add":
        if args.user_id not in cfg.curators and args.user_id != cfg.owner:
            cfg.curators.append(args.user_id)
        if cfg.owner is None:
            cfg.owner = args.user_id
        cfg.save()
        print(f"{args.user_id} can now review submissions")
    else:
        cfg.curators = [c for c in cfg.curators if c != args.user_id]
        if cfg.owner == args.user_id:
            cfg.owner = cfg.curators[0] if cfg.curators else None
        cfg.save()
        print(f"{args.user_id} removed")
    return 0


def _doctor(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    print(BANNER)
    print(f"  python         {sys.version.split()[0]}")
    print(f"  home           {cfg.home}")
    print(f"  config         {'present' if cfg.config_path.exists() else 'missing'}")
    print(f"  token          {'set' if cfg.token else 'MISSING — run tonearm setup'}")
    curators = len(set(cfg.curators) | ({cfg.owner} if cfg.owner else set()))
    print(f"  curators       {curators}" + ("" if curators else "  — nothing can be published"))

    tools = audio.toolchain()
    print(f"  ffmpeg         {tools['ffmpeg'] or 'not found (optional)'}")
    print(f"  ffprobe        {tools['ffprobe'] or 'not found (optional)'}")

    db = Database(cfg.database_path)
    stats = db.stats()
    print(f"  database       {cfg.database_path}")
    print(f"  integrity      {db.integrity()}")
    print(
        "  catalogue      "
        f"{stats['approved']} approved · {stats['pending']} pending · "
        f"{stats['rejected']} declined"
    )
    print(f"  listeners      {stats['users']}  ·  {stats['likes']} saved tracks")

    if cfg.token:
        try:
            identity = Api(cfg.token).me()
            print(f"  telegram       @{identity.get('username')}")
        except (TelegramError, NetworkError, ValueError) as exc:
            print(f"  telegram       unreachable: {exc}")

    if args.reindex:
        print(f"  reindexed      {search.rebuild(db)} tracks")
    if args.rebuild_similarity:
        from .recommend import Engine

        print(f"  similarity     {Engine(db, cfg).rebuild_similarity()} edges")
    return 0


def _backup(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    db = Database(cfg.database_path)
    target = Path(args.path).expanduser() if args.path else cfg.home / "backup.db"
    db.backup_to(target)
    print(target)
    return 0


def _export(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    db = Database(cfg.database_path)
    rows = db.query(
        "SELECT t.id, t.title, a.name AS artist, t.album, t.year, t.duration, "
        "t.note, t.published_at FROM tracks t JOIN artists a ON a.id = t.artist_id "
        "WHERE t.status='approved' ORDER BY t.published_at"
    )
    payload: list[dict] = []
    for row in rows:
        item = dict(row)
        item["tags"] = [
            r["name"]
            for r in db.query(
                "SELECT g.name FROM track_tags tt JOIN tags g ON g.id = tt.tag_id "
                "WHERE tt.track_id=?",
                (row["id"],),
            )
        ]
        payload.append(item)
    text = json.dumps({"version": __version__, "tracks": payload}, ensure_ascii=False, indent=2)
    if args.path:
        Path(args.path).expanduser().write_text(text + "\n", encoding="utf-8")
        print(args.path)
    else:
        print(text)
    return 0


def _digest(args: argparse.Namespace, cfg: config_mod.Config) -> int:
    if not cfg.token:
        _fail("no bot token configured")
        return 2
    configure_logging(cfg)
    service = Service(cfg)
    print(f"sent to {service.send_digest(force=True)} listeners")
    return 0


# --------------------------------------------------------------------------


def _ask(prompt: str) -> str:
    try:
        return input(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def _fail(message: str) -> None:
    print(f"tonearm: {message}", file=sys.stderr)


def entry() -> None:  # pragma: no cover - console script shim
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
