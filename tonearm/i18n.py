"""User-facing strings.

Every string lives here with all of its translations side by side, so a change
to the English copy makes the missing translation obvious in the same diff.
Adding a language means adding a code to :data:`LANGUAGES` and a key to each
entry; anything missing silently falls back to English rather than crashing or
showing a raw key.

The register is deliberately plain: no exclamation marks, no emoji, no
urgency. The interface should read like a card in a record shop.
"""

from __future__ import annotations

from typing import Any

LANGUAGES = ("en", "ru")
DEFAULT = "en"

STRINGS: dict[str, dict[str, str]] = {
    # ------------------------------------------------------------- shell
    "app.tagline": {
        "en": "Hand-picked music. Nothing plays itself.",
        "ru": "Музыка, отобранная вручную. Ничто не играет само.",
    },
    "nav.home": {"en": "Home", "ru": "Главная"},
    "nav.today": {"en": "Today", "ru": "Сегодня"},
    "nav.discover": {"en": "Discover", "ru": "Найти"},
    "nav.search": {"en": "Search", "ru": "Поиск"},
    "nav.library": {"en": "Library", "ru": "Библиотека"},
    "nav.artists": {"en": "Artists", "ru": "Артисты"},
    "nav.mixes": {"en": "Mixes", "ru": "Миксы"},
    "nav.submit": {"en": "Submit", "ru": "Прислать"},
    "nav.settings": {"en": "Settings", "ru": "Настройки"},
    "nav.back": {"en": "Back", "ru": "Назад"},
    "nav.more": {"en": "More", "ru": "Ещё"},
    "nav.queue": {"en": "Review queue", "ru": "Очередь"},
    "nav.help": {"en": "About", "ru": "О сервисе"},
    "common.cancel": {"en": "Cancel", "ru": "Отмена"},
    "common.done": {"en": "Done", "ru": "Готово"},
    "common.skip": {"en": "Skip", "ru": "Пропустить"},
    "common.next": {"en": "Next", "ru": "Дальше"},
    "common.prev": {"en": "Previous", "ru": "Назад"},
    "common.none": {"en": "Nothing here yet.", "ru": "Здесь пока пусто."},
    "common.unknown": {"en": "Unknown", "ru": "Неизвестно"},
    "common.error": {
        "en": "Something went wrong. Nothing was lost — try again.",
        "ru": "Что-то пошло не так. Ничего не потеряно — попробуйте снова.",
    },
    "common.not_found": {"en": "That is no longer available.", "ru": "Этого больше нет."},
    "common.stale": {
        "en": "That button belongs to an older screen. Open the menu again.",
        "ru": "Эта кнопка от старого экрана. Откройте меню заново.",
    },
    # ------------------------------------------------------------- start
    "start.title": {"en": "Welcome", "ru": "Здравствуйте"},
    "start.body": {
        "en": (
            "Every track here was listened to by a person before it reached you.\n\n"
            "There is a small selection each day. When it runs out, it runs out — "
            "that is the point."
        ),
        "ru": (
            "Каждый трек здесь прослушал человек, прежде чем тот дошёл до вас.\n\n"
            "Каждый день — небольшая подборка. Когда она заканчивается, она "
            "заканчивается: в этом и смысл."
        ),
    },
    "home.catalogue": {
        "en": "{releases} releases · {tracks} tracks · {artists} artists",
        "ru": "{releases} релизов · {tracks} треков · {artists} артистов",
    },
    "home.today_left": {
        "en": "Today · {left} of {total} left",
        "ru": "Сегодня · осталось {left} из {total}",
    },
    "home.today_done": {"en": "Today · finished", "ru": "Сегодня · прослушано"},
    # ------------------------------------------------------------- today
    "today.title": {"en": "TODAY", "ru": "СЕГОДНЯ"},
    "today.intro": {
        "en": "{count} tracks, chosen for you today.",
        "ru": "{count} треков, отобранных для вас сегодня.",
    },
    "today.empty": {
        "en": "The catalogue is empty. Nothing has been approved yet.",
        "ru": "Каталог пуст. Пока ничего не одобрено.",
    },
    "today.finished": {
        "en": (
            "That is all for today.\n\n"
            "A new selection appears tomorrow. Nothing is being withheld from you — "
            "there simply is no more."
        ),
        "ru": (
            "На сегодня всё.\n\n"
            "Новая подборка появится завтра. От вас ничего не прячут — "
            "просто больше нет."
        ),
    },
    # ---------------------------------------------------------- discover
    "discover.title": {"en": "DISCOVER", "ru": "НАЙТИ"},
    "discover.intro": {
        "en": "{count} more, picked on request. {left} requests left today.",
        "ru": "Ещё {count} по запросу. Сегодня осталось запросов: {left}.",
    },
    "discover.spent": {
        "en": (
            "You have used today's discovery requests.\n\n"
            "Your daily selection is still there, and so is your library."
        ),
        "ru": ("Запросы на сегодня исчерпаны.\n\nДневная подборка и библиотека никуда не делись."),
    },
    "discover.exhausted": {
        "en": "You have heard everything in the catalogue. Genuinely.",
        "ru": "Вы прослушали весь каталог. Правда.",
    },
    "discover.request": {"en": "Bring {count} more", "ru": "Принести ещё {count}"},
    # ------------------------------------------------------------ search
    "search.prompt": {
        "en": "Send a title, an artist, or a tag.",
        "ru": "Отправьте название, имя артиста или тег.",
    },
    "search.results": {"en": "Results for “{query}”", "ru": "Результаты по «{query}»"},
    "search.none": {"en": "Nothing matched “{query}”.", "ru": "По «{query}» ничего не найдено."},
    "search.tags": {"en": "Tags in use", "ru": "Используемые теги"},
    # ----------------------------------------------------------- library
    "library.title": {"en": "LIBRARY", "ru": "БИБЛИОТЕКА"},
    "library.saved": {"en": "Saved", "ru": "Сохранённое"},
    "library.following": {"en": "Following", "ru": "Подписки"},
    "library.history": {"en": "Recently played", "ru": "Недавно звучало"},
    "library.empty": {
        "en": "Nothing saved yet. Use Save under any track.",
        "ru": "Пока ничего не сохранено. Нажмите «Сохранить» под треком.",
    },
    "library.no_follows": {
        "en": "You are not following anyone yet.",
        "ru": "Вы пока ни на кого не подписаны.",
    },
    # ------------------------------------------------------------ artists
    "artists.title": {"en": "ARTISTS", "ru": "АРТИСТЫ"},
    "artist.tracks": {"en": "{count} tracks", "ru": "{count} треков"},
    "artist.follow": {"en": "Follow", "ru": "Подписаться"},
    "artist.unfollow": {"en": "Following", "ru": "Вы подписаны"},
    "artist.followed": {"en": "Following {name}.", "ru": "Вы подписались на {name}."},
    "artist.unfollowed": {"en": "No longer following.", "ru": "Подписка отменена."},
    # -------------------------------------------------------------- mixes
    "mixes.title": {"en": "MIXES", "ru": "МИКСЫ"},
    "mixes.auto": {"en": "A sequence for you", "ru": "Последовательность для вас"},
    "mixes.auto_hint": {
        "en": "Ten tracks that lead into one another. It ends where it ends.",
        "ru": "Десять треков, переходящих один в другой. Кончается там, где кончается.",
    },
    "mixes.curated": {"en": "Curated", "ru": "Кураторские"},
    "mixes.empty": {"en": "No curated mixes yet.", "ru": "Кураторских миксов пока нет."},
    "mixes.play_all": {"en": "Send the whole mix", "ru": "Прислать весь микс"},
    # ------------------------------------------------------------- track
    "track.saved": {"en": "Saved", "ru": "Сохранено"},
    "track.save": {"en": "Save", "ru": "Сохранить"},
    "track.similar": {"en": "Similar", "ru": "Похожее"},
    "track.artist": {"en": "Artist", "ru": "Артист"},
    "track.share": {"en": "Share", "ru": "Поделиться"},
    "track.was_saved": {"en": "Saved to your library.", "ru": "Сохранено в библиотеку."},
    "track.was_unsaved": {"en": "Removed from your library.", "ru": "Убрано из библиотеки."},
    "track.similar_for": {"en": "Close to “{title}”", "ru": "Рядом с «{title}»"},
    "track.similar_none": {
        "en": "Nothing close enough yet. The catalogue is still small.",
        "ru": "Пока ничего достаточно близкого. Каталог ещё маленький.",
    },
    # ------------------------------------------------------------ submit
    "submit.title": {"en": "SUBMIT", "ru": "ПРИСЛАТЬ"},
    "submit.intro": {
        "en": (
            "Send an audio file. One track per message.\n\n"
            "A person listens to everything before it is published. "
            "You will hear back either way."
        ),
        "ru": (
            "Отправьте аудиофайл. По одному треку в сообщении.\n\n"
            "Всё прослушивает человек, прежде чем это будет опубликовано. "
            "Ответ придёт в любом случае."
        ),
    },
    "submit.not_audio": {
        "en": "Send it as an audio file, not a voice message or a document.",
        "ru": "Отправьте именно аудиофайл, не голосовое и не документ.",
    },
    "submit.received": {
        "en": "In the queue.\n\n{title}\n{artist} · {duration}",
        "ru": "В очереди.\n\n{title}\n{artist} · {duration}",
    },
    "submit.queued_position": {
        "en": "{count} submissions ahead of yours.",
        "ru": "Перед вашей — заявок: {count}.",
    },
    "submit.limit_day": {
        "en": "You have reached today's limit of {count} submissions.",
        "ru": "Достигнут дневной лимит: {count} заявок.",
    },
    "submit.limit_pending": {
        "en": "You already have {count} submissions waiting. Wait for a decision first.",
        "ru": "У вас уже {count} заявок в ожидании. Дождитесь решения.",
    },
    "submit.duplicate": {
        "en": "That recording is already here.",
        "ru": "Эта запись здесь уже есть.",
    },
    "submit.too_short": {
        "en": "Under {seconds} seconds. Send the full track.",
        "ru": "Короче {seconds} секунд. Пришлите трек целиком.",
    },
    "submit.failed": {
        "en": "That file could not be read. Send it again, or send a different export.",
        "ru": "Не удалось прочитать файл. Пришлите ещё раз или другой экспорт.",
    },
    "submit.edit_hint": {
        "en": "Wrong title or artist? Send them back as: <code>Artist — Title</code>",
        "ru": "Не то название или артист? Пришлите так: <code>Артист — Название</code>",
    },
    "submit.fixed": {"en": "Corrected.", "ru": "Исправлено."},
    "submit.mine": {"en": "My submissions", "ru": "Мои заявки"},
    "submit.status_pending": {"en": "waiting", "ru": "ждёт"},
    "submit.status_approved": {"en": "published", "ru": "опубликовано"},
    "submit.status_rejected": {"en": "not taken", "ru": "не взято"},
    "submit.status_hidden": {"en": "withdrawn", "ru": "снято"},
    "submit.approved_notice": {
        "en": "Published: {title}",
        "ru": "Опубликовано: {title}",
    },
    "submit.rejected_notice": {
        "en": "Not taken this time: {title}",
        "ru": "В этот раз не взяли: {title}",
    },
    "submit.rejected_reason": {"en": "Reason: {reason}", "ru": "Причина: {reason}"},
    # -------------------------------------------------------- moderation
    "mod.title": {"en": "REVIEW QUEUE", "ru": "ОЧЕРЕДЬ"},
    "mod.empty": {"en": "The queue is empty.", "ru": "Очередь пуста."},
    "mod.count": {"en": "{count} waiting", "ru": "Ожидают: {count}"},
    "mod.approve": {"en": "Publish", "ru": "Опубликовать"},
    "mod.reject": {"en": "Decline", "ru": "Отклонить"},
    "mod.edit": {"en": "Edit", "ru": "Правка"},
    "mod.tags": {"en": "Tags", "ru": "Теги"},
    "mod.note": {"en": "Note", "ru": "Заметка"},
    "mod.later": {"en": "Later", "ru": "Позже"},
    "mod.hide": {"en": "Withdraw", "ru": "Снять"},
    "mod.approved": {"en": "Published.", "ru": "Опубликовано."},
    "mod.rejected": {"en": "Declined.", "ru": "Отклонено."},
    "mod.ask_reason": {
        "en": "Send a short reason, or press Skip. The artist will see it.",
        "ru": "Пришлите короткую причину или нажмите «Пропустить». Артист её увидит.",
    },
    "mod.ask_tags": {
        "en": "Send tags separated by commas. These drive recommendations.",
        "ru": "Пришлите теги через запятую. По ним строятся рекомендации.",
    },
    "mod.ask_note": {
        "en": "Send one or two sentences about this track. Listeners see it before they press play.",
        "ru": "Пришлите одно-два предложения о треке. Слушатели видят это до нажатия «играть».",
    },
    "mod.ask_edit": {
        "en": "Send the correction as: <code>Artist — Title</code>",
        "ru": "Пришлите исправление в виде: <code>Артист — Название</code>",
    },
    "mod.saved": {"en": "Saved.", "ru": "Сохранено."},
    "mod.not_curator": {"en": "That is for curators.", "ru": "Это для кураторов."},
    "mod.new_submission": {"en": "New submission", "ru": "Новая заявка"},
    # ----------------------------------------------------------- settings
    "settings.title": {"en": "SETTINGS", "ru": "НАСТРОЙКИ"},
    "settings.language": {"en": "Language", "ru": "Язык"},
    "settings.digest_on": {"en": "Weekly note: on", "ru": "Еженедельная сводка: вкл"},
    "settings.digest_off": {"en": "Weekly note: off", "ru": "Еженедельная сводка: выкл"},
    "settings.digest_hint": {
        "en": (
            "One message a week, on Sunday, listing what was added. "
            "There are no other notifications, and there will not be."
        ),
        "ru": (
            "Одно сообщение в неделю, по воскресеньям, о том, что добавилось. "
            "Других уведомлений нет и не будет."
        ),
    },
    "settings.data": {"en": "My data", "ru": "Мои данные"},
    "settings.forget": {"en": "Erase my history", "ru": "Стереть мою историю"},
    "settings.forget_done": {
        "en": "Your listening history and saved tracks have been erased.",
        "ru": "История прослушивания и сохранённое стёрты.",
    },
    "settings.forget_confirm": {
        "en": "This erases your library and history for good. Press again to confirm.",
        "ru": "Это навсегда сотрёт библиотеку и историю. Нажмите ещё раз для подтверждения.",
    },
    # --------------------------------------------------------------- help
    "help.title": {"en": "ABOUT", "ru": "О СЕРВИСЕ"},
    "help.body": {
        "en": (
            "<b>What this is</b>\n"
            "A small station. Everything is chosen by a person, one track at a time.\n\n"
            "<b>What it does not do</b>\n"
            "It does not autoplay. It does not have an endless feed. "
            "It does not show play counts, streaks or badges, and it will not "
            "notify you unless you ask it to.\n\n"
            "<b>Sending your own music</b>\n"
            "Use Submit. Everything is listened to. You will get an answer either way.\n\n"
            "<b>Commands</b>\n"
            "/today · /discover · /search · /library · /submit · /settings"
        ),
        "ru": (
            "<b>Что это</b>\n"
            "Небольшая станция. Всё отбирает человек, трек за треком.\n\n"
            "<b>Чего здесь нет</b>\n"
            "Нет автовоспроизведения. Нет бесконечной ленты. "
            "Не показываются счётчики прослушиваний, серии и значки, "
            "и уведомлений не будет, пока вы сами их не включите.\n\n"
            "<b>Своя музыка</b>\n"
            "Кнопка «Прислать». Всё прослушивается. Ответ придёт в любом случае.\n\n"
            "<b>Команды</b>\n"
            "/today · /discover · /search · /library · /submit · /settings"
        ),
    },
    # ------------------------------------------------------------ releases
    "nav.releases": {"en": "Releases", "ru": "Релизы"},
    "releases.title": {"en": "RELEASES", "ru": "РЕЛИЗЫ"},
    "releases.intro": {
        "en": "Singles, EPs and albums, newest first.",
        "ru": "Синглы, EP и альбомы, свежие сверху.",
    },
    "release.single": {"en": "single", "ru": "сингл"},
    "release.ep": {"en": "EP", "ru": "EP"},
    "release.album": {"en": "album", "ru": "альбом"},
    "release.tracks": {"en": "{count} tracks", "ru": "{count} треков"},
    "release.play_all": {"en": "Play it through", "ru": "Слушать целиком"},
    "release.open": {"en": "Release", "ru": "Релиз"},
    "release.none": {"en": "No releases yet.", "ru": "Релизов пока нет."},
    # --------------------------------------------------------------- cover
    "cover.needed": {
        "en": (
            "This release has no artwork yet. Send a square image and it becomes "
            "the cover — nothing is published without one."
        ),
        "ru": (
            "У релиза пока нет обложки. Пришлите квадратную картинку — она станет "
            "обложкой. Без неё ничего не публикуется."
        ),
    },
    "cover.saved": {"en": "Artwork saved.", "ru": "Обложка сохранена."},
    "cover.send": {"en": "Send the artwork", "ru": "Прислать обложку"},
    "cover.not_image": {
        "en": "Send it as a photo or an image file.",
        "ru": "Пришлите картинкой или файлом изображения.",
    },
    "submit.release_hint": {
        "en": (
            "Sending several tracks of one release? Send them one after another — "
            "the album tag groups them. A track with no album becomes a single."
        ),
        "ru": (
            "Присылаете несколько треков одного релиза? Шлите подряд — они "
            "группируются по тегу альбома. Трек без альбома становится синглом."
        ),
    },
    "submit.in_release": {
        "en": "Release: {title} · track {n}",
        "ru": "Релиз: {title} · трек {n}",
    },
    # ---------------------------------------------------- release moderation
    "mod.release": {"en": "Release", "ru": "Релиз"},
    "mod.publish_release": {"en": "Publish the release", "ru": "Опубликовать релиз"},
    "mod.reject_release": {"en": "Decline the release", "ru": "Отклонить релиз"},
    "mod.cover": {"en": "Artwork", "ru": "Обложка"},
    "mod.ask_cover": {
        "en": "Send an image. It becomes this release's cover.",
        "ru": "Пришлите картинку. Она станет обложкой этого релиза.",
    },
    "mod.no_cover": {
        "en": "No artwork — a release cannot be published without it.",
        "ru": "Нет обложки — без неё релиз опубликовать нельзя.",
    },
    "mod.no_tracks": {"en": "This release has no tracks.", "ru": "В релизе нет треков."},
    "mod.release_published": {"en": "Release published.", "ru": "Релиз опубликован."},
    "mod.release_rejected": {"en": "Release declined.", "ru": "Релиз отклонён."},
    "mod.waiting_tracks": {"en": "{n} of {total} waiting", "ru": "ждут {n} из {total}"},
    # ------------------------------------------------------------- digest
    "digest.title": {"en": "This week", "ru": "За неделю"},
    "digest.body": {
        "en": "{count} tracks were added. Here are three of them.",
        "ru": "Добавилось треков: {count}. Вот три из них.",
    },
    "digest.none": {"en": "Nothing new this week.", "ru": "На этой неделе ничего нового."},
    # -------------------------------------------------------------- stats
    "stats.title": {"en": "STATION", "ru": "СТАНЦИЯ"},
    "stats.mine": {"en": "My tracks", "ru": "Мои треки"},
    "stats.line": {
        "en": "{title} · {listeners} listeners · {likes} saved",
        "ru": "{title} · слушателей: {listeners} · сохранений: {likes}",
    },
    "stats.no_tracks": {
        "en": "You have no published tracks yet.",
        "ru": "У вас пока нет опубликованных треков.",
    },
    "stats.unheard": {
        "en": "{count} approved tracks have not been shown to anyone yet.",
        "ru": "Треков, которые ещё никому не показывались: {count}.",
    },
}


def t(lang: str, key: str, **kwargs: Any) -> str:
    """Look up a string, falling back to English and then to the key itself."""
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry.get(DEFAULT) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def normalise(lang: str) -> str:
    lang = (lang or DEFAULT)[:2].lower()
    return lang if lang in LANGUAGES else DEFAULT


def missing() -> dict[str, list]:
    """Keys that lack a translation, for the test suite to assert on."""
    gaps: dict[str, list] = {}
    for lang in LANGUAGES:
        absent = [key for key, entry in STRINGS.items() if not entry.get(lang)]
        if absent:
            gaps[lang] = absent
    return gaps
