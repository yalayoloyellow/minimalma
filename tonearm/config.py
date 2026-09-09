"""Configuration and on-disk layout.

One JSON file holds every operator-tunable value. It lives next to the
database in a per-OS application data directory so that a ``git pull`` never
clobbers it, and it is written with ``0600`` permissions because it contains
the bot token.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_NAME = "config.json"
DATABASE_NAME = "tonearm.db"

#: Environment variable that relocates the whole data directory.
HOME_ENV = "TONEARM_HOME"
#: Environment variable that overrides the stored bot token.
TOKEN_ENV = "TONEARM_TOKEN"


def default_home() -> Path:
    """Return the per-user data directory for the current platform."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tonearm"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "tonearm"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tonearm"


@dataclass
class Limits:
    """Numbers that bound both abuse and compulsive use.

    The defaults are deliberately small. Every one of them is a product
    decision, not a performance tuning knob — see ``docs/DESIGN.md``.
    """

    #: Tracks in the hand-sized daily selection.
    daily_selection: int = 5
    #: Tracks handed out per explicit discovery request.
    discover_batch: int = 3
    #: Discovery requests allowed per user per day. ``0`` disables discovery.
    discover_sessions_per_day: int = 3
    #: Submissions a single account may queue per day.
    submissions_per_day: int = 5
    #: Total pending submissions a single account may have at once.
    pending_per_artist: int = 10
    #: Rows on one browse screen.
    page_size: int = 8
    #: Tracks in a generated mix.
    mix_length: int = 10
    #: Hard ceiling on an audio upload we will accept, in bytes (Telegram caps
    #: bot downloads at 20 MiB, so anything above that cannot be inspected).
    max_audio_bytes: int = 20 * 1024 * 1024
    #: Seconds a submission must last to be accepted at all.
    min_duration: int = 20
    #: Seconds a submission may last before it is flagged for the curator.
    max_duration: int = 60 * 30


@dataclass
class Weights:
    """Ranking weights. Documented in ``tonearm/recommend.py``."""

    collaborative: float = 1.0
    content: float = 1.0
    curator: float = 0.6
    freshness: float = 0.35
    exploration: float = 0.5
    #: Interaction count at which collaborative signal reaches half strength.
    cf_confidence_k: float = 12.0
    #: MMR trade-off: 1.0 is pure relevance, 0.0 is pure diversity.
    mmr_lambda: float = 0.7
    #: Half-life of a user's taste profile, in days.
    profile_half_life_days: float = 45.0
    #: Popularity damping exponent for item-item similarity.
    popularity_alpha: float = 0.5


@dataclass
class Config:
    """The full operator configuration."""

    home: Path
    token: str = ""
    #: Telegram user ids allowed to moderate.
    curators: list[int] = field(default_factory=list)
    #: The single account allowed to add or remove curators.
    owner: int | None = None
    #: Chat that receives the moderation queue and the cover-art archive.
    review_chat: int | None = None
    #: Default interface language for new users.
    lang: str = "en"
    #: Public name shown in the bot's own copy.
    station_name: str = "Tonearm"
    #: One line shown on the start screen.
    station_tagline: str = ""
    limits: Limits = field(default_factory=Limits)
    weights: Weights = field(default_factory=Weights)
    #: Set false only if you accept unreviewed material. Off by default and
    #: loudly discouraged: curation is the product.
    auto_approve: bool = False
    #: Refuse to publish a release with no artwork. On by default: a release
    #: without a cover looks broken everywhere it appears, and review is the
    #: one moment when someone will actually fix it.
    require_cover: bool = True
    #: Opt-in weekly digest. There is no other outbound notification.
    weekly_digest: bool = True
    #: Log verbosity: "quiet", "normal" or "debug".
    log_level: str = "normal"

    # ------------------------------------------------------------------ paths
    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_NAME

    @property
    def database_path(self) -> Path:
        return self.home / DATABASE_NAME

    @property
    def log_path(self) -> Path:
        return self.home / "tonearm.log"

    # ------------------------------------------------------------------- auth
    def is_curator(self, user_id: int) -> bool:
        return user_id == self.owner or user_id in self.curators

    def is_owner(self, user_id: int) -> bool:
        return self.owner is not None and user_id == self.owner

    # --------------------------------------------------------------- (de)ser
    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "curators": sorted(set(self.curators)),
            "owner": self.owner,
            "review_chat": self.review_chat,
            "lang": self.lang,
            "station_name": self.station_name,
            "station_tagline": self.station_tagline,
            "auto_approve": self.auto_approve,
            "require_cover": self.require_cover,
            "weekly_digest": self.weekly_digest,
            "log_level": self.log_level,
            "limits": vars(self.limits),
            "weights": vars(self.weights),
        }

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)
        tmp.write_text(payload + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - unusual filesystems
            pass
        tmp.replace(self.config_path)


def _coerce(section: Any, raw: Any) -> Any:
    """Overlay a dict of known keys onto a dataclass instance, ignoring junk."""
    if not isinstance(raw, dict):
        return section
    for key, value in raw.items():
        if hasattr(section, key) and value is not None:
            current = getattr(section, key)
            try:
                setattr(section, key, type(current)(value))
            except (TypeError, ValueError):
                continue
    return section


def load(home: Path | None = None) -> Config:
    """Read the configuration, tolerating a missing or partial file."""
    root = Path(home).expanduser() if home else default_home()
    cfg = Config(home=root)
    path = root / CONFIG_NAME
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{path} is not readable JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        cfg.token = str(raw.get("token") or "")
        cfg.curators = [int(x) for x in raw.get("curators") or [] if str(x).lstrip("-").isdigit()]
        owner = raw.get("owner")
        cfg.owner = int(owner) if owner else None
        review = raw.get("review_chat")
        cfg.review_chat = int(review) if review else None
        cfg.lang = str(raw.get("lang") or "en")
        cfg.station_name = str(raw.get("station_name") or "Tonearm")
        cfg.station_tagline = str(raw.get("station_tagline") or "")
        cfg.auto_approve = bool(raw.get("auto_approve", False))
        cfg.require_cover = bool(raw.get("require_cover", True))
        cfg.weekly_digest = bool(raw.get("weekly_digest", True))
        cfg.log_level = str(raw.get("log_level") or "normal")
        _coerce(cfg.limits, raw.get("limits"))
        _coerce(cfg.weights, raw.get("weights"))
    env_token = os.environ.get(TOKEN_ENV)
    if env_token:
        cfg.token = env_token.strip()
    return cfg


class ConfigError(Exception):
    """Raised when the configuration on disk cannot be used as-is."""
