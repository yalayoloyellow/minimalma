"""The curation desk — an optional local window over the same catalogue.

The package below ``tonearm`` stays dependency-free and knows nothing about
this directory. Delete ``desk/`` and the bot is unchanged.
"""

from .launch import open_desk
from .server import Desk, serve

__all__ = ["Desk", "serve", "open_desk"]
