"""``python -m desk`` — open the curation desk without installing anything."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from minimalma import config as config_mod  # noqa: E402

from .launch import open_desk  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(open_desk(config_mod.load()))
