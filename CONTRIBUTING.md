# Contributing

Thanks for taking a look. This is a small project with strong opinions; the
notes below should make it clear quickly whether a change will land.

## Getting set up

```bash
git clone https://github.com/yalayoloyellow/tonearm && cd minimalma
uv venv && uv pip install pytest ruff
.venv/bin/python -m pytest      # ~4 seconds, no network required
.venv/bin/ruff check .
```

Optional: install `ffmpeg` to run the audio tests that need it. They skip
cleanly without it.

## Ground rules

**No dependencies.** The package imports the standard library and nothing else.
This is enforced by a test. `ffmpeg` and `ffprobe` are the only external tools,
they are optional, and every code path degrades when they are missing.

**Python 3.9 is the floor.** No `match`, no PEP 604 unions at runtime. Use
`from __future__ import annotations` for annotations.

**The invariants in `tests/test_invariants.py` are the product.** They pin
things like "no audio is sent without an explicit tap" and "listeners never see
play counts". A change that breaks one is not a bug fix — it is a change to
what this is. Say so in the pull request and explain the reasoning; do not
quietly adjust the test.

**Tests test scenarios, not functions.** The suite drives real updates through
the real router with a Telegram double. A test that cannot fail for a reason a
user would notice is not worth adding.

## Sending a change

1. Open an issue first for anything larger than a fix. It saves both of us time
   if the answer is "this belongs in a different project".
2. Keep the change focused. Unrelated refactoring in the same diff makes review
   slow.
3. Add or update tests. Bug fixes should come with the test that would have
   caught the bug.
4. Run `pytest` and `ruff check .` before pushing.
5. Write a commit message that says what was broken, what it is now, and how
   you know.

## What is likely to be accepted

- Bug fixes, especially in the metadata parsers — real-world files are endless
  and every fix should arrive with the bytes that broke it.
- New translations. Add the language code to `i18n.LANGUAGES` and a key to
  every entry in `i18n.STRINGS`; a test verifies none are missing and that
  format placeholders match across languages.
- Container support in `metadata.py`, and quality checks in `audio.py`.
- Documentation that corrects something wrong or unclear.
- Ranking improvements *with evidence* — describe the scenario that improves
  and add a test that fails without the change.

## What is unlikely to be accepted

- Anything that adds a runtime dependency.
- Autoplay, endless feeds, streaks, badges, public play counters, or
  notifications beyond the opt-in weekly note. See the table in the README.
- A web dashboard, an admin panel, or a second interface. The bot is the
  interface.
- Making moderation optional by default.
- Broad refactoring of working code for its own sake.

## Reporting bugs

Include: what you did, what happened, what you expected, `minimalma doctor`
output, and — for a metadata bug — the file, or at least its first few kilobytes.

Security issues go to [SECURITY.md](SECURITY.md), not the issue tracker.
