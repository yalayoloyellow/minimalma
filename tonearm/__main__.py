"""Allow ``python -m tonearm`` for people who have not installed the script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
