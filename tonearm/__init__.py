"""Tonearm — a curated music streaming service that runs inside a Telegram bot.

The package is dependency-free: everything below the ``tonearm`` namespace uses
nothing but the Python standard library. ``ffmpeg``/``ffprobe`` are used when
present and every code path degrades cleanly when they are not.
"""

__all__ = ["__version__"]

__version__ = "0.1.0a1"
"""Distribution version (PEP 440). The human-facing label is ``0.1.0-alpha``."""

VERSION_LABEL = "0.1.0-alpha"
