"""Message rendering and keyboards.

The interface is two objects:

**The screen** — exactly one message per chat, edited in place. Menus, lists,
search results and artist pages all live in it, so browsing never lengthens the
conversation.

**Track messages** — real Telegram audio messages, appended as they are sent.
They are not deleted: the chat becomes a plain listening log you can scroll
back through and replay, which is the one thing a chat client does better than
an app.

Formatting is HTML, not MarkdownV2. MarkdownV2 requires escaping fifteen
characters and one missed underscore in a track title takes the whole message
down with a 400; ``html.escape`` on three characters cannot fail that way.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from . import audio as audio_mod
from .config import Config
from .i18n import t
from .telegram import MAX_CALLBACK_DATA, MAX_CAPTION

#: Separator between callback fields. Not valid inside any id we emit.
SEP = "|"


def pack(*parts: Any) -> str:
    """Build ``callback_data``, guarding Telegram's 64-byte ceiling."""
    data = SEP.join(str(part) for part in parts)
    encoded = data.encode("utf-8")
    if len(encoded) > MAX_CALLBACK_DATA:
        raise ValueError(f"callback data too long ({len(encoded)}B): {data!r}")
    return data


def unpack(data: str) -> list[str]:
    return (data or "").split(SEP)


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


def hms(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "0:00"
    if total <= 0:
        return "0:00"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(*rows: Sequence[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows if row]}


def numbered(items: Sequence[Any], action: str, per_row: int = 5) -> list[list[dict[str, str]]]:
    """A compact row of index buttons under a list — the whole navigation model."""
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for index, item in enumerate(items, start=1):
        row.append(button(str(index), pack(action, item)))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


class Screen:
    """A rendered screen: text plus an optional keyboard."""

    __slots__ = ("text", "markup")

    def __init__(self, text: str, markup: dict[str, Any] | None = None):
        self.text = text
        self.markup = markup

    def as_tuple(self) -> tuple[str, dict[str, Any] | None]:
        return self.text, self.markup


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------


def track_line(index: int | None, item: dict[str, Any], show_duration: bool = True) -> str:
    """One row in a list: ``3 · Artist — Title  4:12``."""
    prefix = f"{index} · " if index is not None else ""
    duration = f"  <code>{hms(item.get('duration'))}</code>" if show_duration else ""
    return f"{prefix}{esc(item.get('artist'))} — <b>{esc(item.get('title'))}</b>{duration}"


def track_caption(item: dict[str, Any], lang: str, with_note: bool = True) -> str:
    """The caption under an audio message.

    Context first: the curator's note is the reason this track is in front of
    you, and it is shown before anything a machine computed.
    """
    lines = [f"<b>{esc(item.get('title'))}</b>", esc(item.get("artist"))]
    meta: list[str] = [hms(item.get("duration"))]
    if item.get("album"):
        meta.append(esc(item["album"]))
    if item.get("year"):
        meta.append(str(item["year"]))
    lines[1] = f"{lines[1]} · " + " · ".join(meta)
    note = (item.get("note") or "").strip()
    if with_note and note:
        lines.append("")
        lines.append(f"<blockquote>{esc(note)}</blockquote>")
    tags = item.get("tags") or []
    if tags:
        lines.append("")
        lines.append("<i>" + esc(" · ".join(tags)) + "</i>")
    text = "\n".join(lines)
    return text[: MAX_CAPTION - 1]


def track_buttons(
    item: dict[str, Any],
    lang: str,
    liked: bool,
    context: str = "",
    position: int | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    """Controls under a track. ``Next`` only exists inside a finite set."""
    track_id = item["id"]
    first = [
        button(t(lang, "track.saved" if liked else "track.save"), pack("like", track_id)),
        button(t(lang, "track.artist"), pack("artist", item["artist_id"])),
        button(t(lang, "track.similar"), pack("similar", track_id)),
    ]
    rows = [first]
    if context and position is not None and total is not None and position + 1 < total:
        rows.append(
            [
                button(
                    f"{t(lang, 'common.next')} · {position + 2}/{total}",
                    pack("seq", context, position + 1),
                )
            ]
        )
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return keyboard(*rows)


def quality_summary(item: dict[str, Any], lang: str) -> str:
    """The advisory line on a review card."""
    quality = item.get("quality") or {}
    if not quality:
        return ""
    labels = {
        "low_bitrate": "low bitrate {v}k",
        "lossless_from_lossy": "lossless container, spectrum stops at {v} Hz",
        "dull_top_end": "no content above {v} Hz",
        "clipping": "clipped samples {v}%",
        "true_peak_over": "true peak +{v} dBTP",
        "very_loud": "{v} LUFS (very loud)",
        "very_quiet": "{v} LUFS (very quiet)",
        "over_compressed": "crest factor {v}",
        "mono": "mono",
        "unprobed": "could not be analysed",
    }
    parts = [
        labels.get(key, key + " {v}").format(v=value)
        for key, value in quality.items()
        if key in labels
    ]
    return " · ".join(parts)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def home(
    lang: str,
    config: Config,
    counts: dict[str, int],
    today_left: int | None,
    today_total: int,
    is_curator: bool = False,
    pending: int = 0,
) -> Screen:
    lines = [f"<b>{esc(config.station_name.upper())}</b>"]
    tagline = config.station_tagline or t(lang, "app.tagline")
    lines.append(f"<i>{esc(tagline)}</i>")
    lines.append("")
    if today_total:
        if today_left:
            lines.append(t(lang, "home.today_left", left=today_left, total=today_total))
        else:
            lines.append(t(lang, "home.today_done"))
    lines.append(
        t(lang, "home.catalogue", tracks=counts.get("tracks", 0), artists=counts.get("artists", 0))
    )
    rows = [
        [button(t(lang, "nav.today"), pack("nav", "today"))],
        [
            button(t(lang, "nav.discover"), pack("nav", "discover")),
            button(t(lang, "nav.search"), pack("nav", "search")),
        ],
        [
            button(t(lang, "nav.library"), pack("nav", "library")),
            button(t(lang, "nav.artists"), pack("nav", "artists")),
        ],
        [
            button(t(lang, "nav.mixes"), pack("nav", "mixes")),
            button(t(lang, "nav.submit"), pack("nav", "submit")),
        ],
        [
            button(t(lang, "nav.settings"), pack("nav", "settings")),
            button(t(lang, "nav.help"), pack("nav", "help")),
        ],
    ]
    if is_curator:
        label = t(lang, "nav.queue")
        if pending:
            label = f"{label} · {pending}"
        rows.append([button(label, pack("nav", "queue"))])
    return Screen("\n".join(lines), keyboard(*rows))


def track_list(
    lang: str,
    title: str,
    intro: str,
    items: Sequence[dict[str, Any]],
    action: str,
    extra_rows: Sequence[Sequence[dict[str, str]]] | None = None,
    empty: str = "",
) -> Screen:
    lines = [f"<b>{esc(title)}</b>"]
    if intro:
        lines.append(intro)
    lines.append("")
    if items:
        for index, item in enumerate(items, start=1):
            lines.append(track_line(index, item))
    else:
        lines.append(esc(empty) if empty else t(lang, "common.none"))
    rows: list[Sequence[dict[str, str]]] = []
    if items:
        rows.extend(numbered([item["id"] for item in items], action))
    if extra_rows:
        rows.extend(extra_rows)
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def today_screen(lang: str, items: Sequence[dict[str, Any]], served: int, finished: bool) -> Screen:
    if finished:
        text = f"<b>{t(lang, 'today.title')}</b>\n\n{t(lang, 'today.finished')}"
        rows = [
            [button(t(lang, "nav.library"), pack("nav", "library"))],
            [button(t(lang, "nav.home"), pack("nav", "home"))],
        ]
        return Screen(text, keyboard(*rows))
    if not items:
        return Screen(
            f"<b>{t(lang, 'today.title')}</b>\n\n{t(lang, 'today.empty')}",
            keyboard([button(t(lang, "nav.home"), pack("nav", "home"))]),
        )
    intro = t(lang, "today.intro", count=len(items))
    lines = [f"<b>{t(lang, 'today.title')}</b>", intro, ""]
    for index, item in enumerate(items, start=1):
        mark = "·" if index <= served else "▸"
        lines.append(f"{index} {mark} {esc(item.get('artist'))} — <b>{esc(item.get('title'))}</b>")
    rows: list[Sequence[dict[str, str]]] = list(numbered([item["id"] for item in items], "play"))
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def discover_screen(lang: str, left: int, batch: int, exhausted: bool = False) -> Screen:
    if exhausted:
        body = t(lang, "discover.exhausted")
        rows = [[button(t(lang, "nav.home"), pack("nav", "home"))]]
    elif left <= 0:
        body = t(lang, "discover.spent")
        rows = [[button(t(lang, "nav.home"), pack("nav", "home"))]]
    else:
        body = t(lang, "discover.intro", count=batch, left=left)
        rows = [
            [button(t(lang, "discover.request", count=batch), pack("disc", "go"))],
            [button(t(lang, "nav.home"), pack("nav", "home"))],
        ]
    return Screen(f"<b>{t(lang, 'discover.title')}</b>\n\n{body}", keyboard(*rows))


def search_prompt(lang: str, tags: Sequence[str]) -> Screen:
    lines = [f"<b>{t(lang, 'nav.search').upper()}</b>", t(lang, "search.prompt")]
    rows: list[Sequence[dict[str, str]]] = []
    if tags:
        lines.append("")
        lines.append(f"<i>{t(lang, 'search.tags')}</i>")
        lines.append(esc(" · ".join(tags[:18])))
        row: list[dict[str, str]] = []
        for tag in tags[:6]:
            row.append(button(tag, pack("tag", tag[:40])))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def library_screen(
    lang: str, saved: Sequence[dict[str, Any]], follows: Sequence[dict[str, Any]]
) -> Screen:
    lines = [f"<b>{t(lang, 'library.title')}</b>", ""]
    if saved:
        lines.append(f"<i>{t(lang, 'library.saved')}</i>")
        for index, item in enumerate(saved, start=1):
            lines.append(track_line(index, item))
    else:
        lines.append(t(lang, "library.empty"))
    if follows:
        lines.append("")
        lines.append(f"<i>{t(lang, 'library.following')}</i>")
        lines.append(esc(" · ".join(item["name"] for item in follows[:12])))
    rows: list[Sequence[dict[str, str]]] = []
    if saved:
        rows.extend(numbered([item["id"] for item in saved], "play"))
    if follows:
        rows.append([button(t(lang, "library.following"), pack("nav", "follows"))])
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def artists_screen(lang: str, artists: Sequence[dict[str, Any]], title: str = "") -> Screen:
    lines = [f"<b>{esc(title or t(lang, 'artists.title'))}</b>", ""]
    if not artists:
        lines.append(t(lang, "common.none"))
    rows: list[Sequence[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for index, artist in enumerate(artists, start=1):
        count = artist.get("n") or artist.get("tracks") or 0
        lines.append(f"{index} · <b>{esc(artist['name'])}</b>  <code>{count}</code>")
        row.append(button(str(index), pack("artist", artist["id"])))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def artist_screen(
    lang: str,
    artist: dict[str, Any],
    items: Sequence[dict[str, Any]],
    following: bool,
    stats: dict[str, int] | None = None,
    own: bool = False,
) -> Screen:
    lines = [f"<b>{esc(artist['name'])}</b>"]
    if artist.get("bio"):
        lines.append(esc(artist["bio"]))
    lines.append(t(lang, "artist.tracks", count=len(items)))
    if own and stats:
        lines.append("")
        lines.append(
            f"<i>{esc(t(lang, 'stats.mine'))}</i> · "
            f"{stats.get('plays', 0)} plays · {stats.get('likes', 0)} saved · "
            f"{stats.get('followers', 0)} following"
        )
    lines.append("")
    for index, item in enumerate(items, start=1):
        lines.append(track_line(index, item))
    rows: list[Sequence[dict[str, str]]] = list(numbered([i["id"] for i in items], "play"))
    rows.append(
        [
            button(
                t(lang, "artist.unfollow" if following else "artist.follow"),
                pack("follow", artist["id"]),
            ),
            button(t(lang, "nav.home"), pack("nav", "home")),
        ]
    )
    return Screen("\n".join(lines), keyboard(*rows))


def mixes_screen(lang: str, curated: Sequence[dict[str, Any]]) -> Screen:
    lines = [f"<b>{t(lang, 'mixes.title')}</b>", "", t(lang, "mixes.auto_hint")]
    rows: list[Sequence[dict[str, str]]] = [[button(t(lang, "mixes.auto"), pack("mix", "auto"))]]
    if curated:
        lines.append("")
        lines.append(f"<i>{t(lang, 'mixes.curated')}</i>")
        for index, item in enumerate(curated, start=1):
            lines.append(f"{index} · <b>{esc(item['title'])}</b>  <code>{item.get('n', 0)}</code>")
        rows.extend(numbered([item["id"] for item in curated], "pl"))
    else:
        lines.append("")
        lines.append(t(lang, "mixes.empty"))
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def playlist_screen(lang: str, item: dict[str, Any]) -> Screen:
    lines = [f"<b>{esc(item['title'])}</b>"]
    if item.get("description"):
        lines.append(f"<blockquote>{esc(item['description'])}</blockquote>")
    lines.append("")
    for index, track in enumerate(item["tracks"], start=1):
        lines.append(track_line(index, track))
    rows: list[Sequence[dict[str, str]]] = list(
        numbered([track["id"] for track in item["tracks"]], "play")
    )
    if item["tracks"]:
        rows.append([button(t(lang, "mixes.play_all"), pack("plall", item["id"]))])
    rows.append([button(t(lang, "nav.mixes"), pack("nav", "mixes"))])
    return Screen("\n".join(lines), keyboard(*rows))


def submit_screen(lang: str, mine: Sequence[dict[str, Any]], limit_left: int) -> Screen:
    lines = [f"<b>{t(lang, 'submit.title')}</b>", "", t(lang, "submit.intro")]
    if mine:
        lines.append("")
        lines.append(f"<i>{t(lang, 'submit.mine')}</i>")
        for item in mine[:10]:
            status = t(lang, f"submit.status_{item['status']}")
            lines.append(f"· <b>{esc(item['title'])}</b> — {esc(status)}")
    rows = [[button(t(lang, "nav.home"), pack("nav", "home"))]]
    return Screen("\n".join(lines), keyboard(*rows))


def settings_screen(lang: str, digest: bool, is_curator: bool) -> Screen:
    lines = [
        f"<b>{t(lang, 'settings.title')}</b>",
        "",
        t(lang, "settings.digest_hint"),
    ]
    rows = [
        [
            button("English" + (" ·" if lang == "en" else ""), pack("lang", "en")),
            button("Русский" + (" ·" if lang == "ru" else ""), pack("lang", "ru")),
        ],
        [
            button(
                t(lang, "settings.digest_on" if digest else "settings.digest_off"),
                pack(
                    "digest",
                ),
            )
        ],
        [button(t(lang, "settings.forget"), pack("forget", "ask"))],
        [button(t(lang, "nav.home"), pack("nav", "home"))],
    ]
    return Screen("\n".join(lines), keyboard(*rows))


def help_screen(lang: str, config: Config) -> Screen:
    text = f"<b>{esc(config.station_name)}</b>\n\n{t(lang, 'help.body')}"
    return Screen(text, keyboard([button(t(lang, "nav.home"), pack("nav", "home"))]))


def queue_screen(lang: str, items: Sequence[dict[str, Any]], total: int) -> Screen:
    lines = [f"<b>{t(lang, 'mod.title')}</b>", t(lang, "mod.count", count=total), ""]
    if not items:
        lines.append(t(lang, "mod.empty"))
    for index, item in enumerate(items, start=1):
        lines.append(track_line(index, item))
    rows: list[Sequence[dict[str, str]]] = list(numbered([i["id"] for i in items], "review"))
    rows.append([button(t(lang, "nav.home"), pack("nav", "home"))])
    return Screen("\n".join(lines), keyboard(*rows))


def review_caption(lang: str, item: dict[str, Any]) -> str:
    """The caption on a moderation card: everything a curator needs, once."""
    lines = [
        f"<b>{esc(item['title'])}</b>",
        f"{esc(item['artist'])} · {hms(item.get('duration'))}",
    ]
    details: list[str] = []
    if item.get("album"):
        details.append(esc(item["album"]))
    if item.get("year"):
        details.append(str(item["year"]))
    technical = audio_mod.describe_features(item.get("features") or {})
    if technical:
        details.append(esc(technical))
    if details:
        lines.append(" · ".join(details))
    flags = quality_summary(item, lang)
    if flags:
        lines.append("")
        lines.append(f"<b>{t(lang, 'mod.flags')}</b> · {esc(flags)}")
    if item.get("tags"):
        lines.append("")
        lines.append("<i>" + esc(" · ".join(item["tags"])) + "</i>")
    if (item.get("note") or "").strip():
        lines.append("")
        lines.append(f"<blockquote>{esc(item['note'])}</blockquote>")
    return "\n".join(lines)[: MAX_CAPTION - 1]


def review_buttons(lang: str, track_id: int) -> dict[str, Any]:
    return keyboard(
        [
            button(t(lang, "mod.approve"), pack("mod", "ok", track_id)),
            button(t(lang, "mod.reject"), pack("mod", "no", track_id)),
        ],
        [
            button(t(lang, "mod.tags"), pack("mod", "tag", track_id)),
            button(t(lang, "mod.note"), pack("mod", "note", track_id)),
            button(t(lang, "mod.edit"), pack("mod", "ed", track_id)),
        ],
        [button(t(lang, "nav.queue"), pack("nav", "queue"))],
    )


def stats_screen(lang: str, report: dict[str, Any]) -> Screen:
    lines = [
        f"<b>{t(lang, 'stats.title')}</b>",
        "",
        f"catalogue · {report.get('catalogue', 0)} tracks, {report.get('artists', 0)} artists",
        f"queue · {report.get('pending', 0)} waiting",
        f"last 7 days · {report.get('approved', 0)} published, {report.get('rejected', 0)} declined",
        f"listeners · {report.get('listeners', 0)}",
        "",
        t(lang, "stats.unheard", count=report.get("unheard", 0)),
    ]
    return Screen(
        "\n".join(lines),
        keyboard(
            [button(t(lang, "nav.queue"), pack("nav", "queue"))],
            [button(t(lang, "nav.home"), pack("nav", "home"))],
        ),
    )


def prompt(lang: str, body: str, cancel_to: str = "home") -> Screen:
    """A screen that waits for a text reply."""
    return Screen(
        body,
        keyboard([button(t(lang, "common.cancel"), pack("nav", cancel_to))]),
    )


def inline_results(items: Sequence[dict[str, Any]], lang: str) -> list[dict[str, Any]]:
    """Results for inline mode, so a track can be shared into any chat."""
    out: list[dict[str, Any]] = []
    for item in items[:20]:
        out.append(
            {
                "type": "audio",
                "id": f"t{item['id']}",
                "audio_file_id": item["file_id"],
                "caption": track_caption(item, lang, with_note=False),
                "parse_mode": "HTML",
            }
        )
    return out
