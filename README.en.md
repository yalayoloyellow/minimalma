# minimalma

A curated music streaming service that runs entirely inside a Telegram bot.

Artists submit tracks. A person listens to every one of them and decides. What
gets published lands in a catalogue with search, artist pages, playlists and
recommendations — all of it inside the chat window, with no app to install and
nothing to host beyond the bot itself.

Start it with a bot token and nothing else:

```bash
uv tool install git+https://github.com/yalayoloyellow/minimalma && minimalma setup && minimalma desk
```

[![CI](https://github.com/yalayoloyellow/minimalma/actions/workflows/ci.yml/badge.svg)](https://github.com/yalayoloyellow/minimalma/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](pyproject.toml)

[Русская версия](README.md) — the fuller document; this one tracks it.

---

## Why this exists

Independent artists are not badly served by streaming platforms; they are
served *incidentally*. The catalogue is infinite, the ranking optimises for
time spent, and an unknown record has no route to a listener that does not run
through an algorithm tuned for something else entirely.

A small station with a human at the front solves a different problem, and it
turns out to be a small program. Telegram already supplies the hard parts:
identity, file hosting, an audio player that works on every platform,
background playback, and a chat that doubles as a listening history.

## Three roles, and the line between them

One rule explains most of the design:

> **The release belongs to the artist. The shelf belongs to the curator.**

**The artist** owns *what the record is*: title, artwork, running order. They
send an audio file and nothing else is required; releases assemble themselves
from the album tag, and a track with no album becomes a single. Artwork is
required and it is theirs to supply — until it exists the release sits as a
draft, no curator is told about it, and it is not in anybody's queue. Metadata
is corrected in one message. Every submission gets an answer, one per release.

**The curator** owns *whether it is on the shelf and how it is labelled*. The
queue holds only complete releases, so nothing opens that cannot be decided.
Four actions and no more: publish the release, decline it with a reason, set the
tags, write the note — plus withdrawing a published one. **A single track is
never decided on its own**: the unit an artist submits is the unit a curator
answers. They cannot rename a release, reorder it or supply its
artwork — a wrong title is grounds to decline with a reason, not to rewrite
somebody's record. Tags stay with the curator because they are the station's
vocabulary and they feed the recommender: an artist tags for promotion, a
curator tags for the shelf.

**The listener** gets a finite daily selection that cannot be re-rolled,
discovery in small capped batches, release pages with artwork and a tracklist,
search, a library, and mixes. Nothing plays itself. They never see play counts,
like counts or follower counts, and the first thing on a track card is the
curator's note — context before the button.

The full version of this, with every screen, is in the
[Russian README](README.md).

## What it deliberately does not do

These are enforced in code and pinned by tests in
[`tests/test_invariants.py`](tests/test_invariants.py). They are the product,
not a configuration accident.

| Not this | Instead |
| --- | --- |
| Autoplay, endless queues | Every track requires a tap. No exceptions in the codebase. |
| An infinite feed | A finite daily selection that ends with "that is all for today". |
| Re-rollable recommendations | The daily selection is written once and cannot be regenerated. |
| Play counts, like counts, follower counts | Listeners see none of them. Artists see their own. |
| Streaks, badges, levels, "you're on fire" | Nothing. A test fails if such a string appears. |
| Push notifications | One optional weekly note, off by default. The only other unsolicited message is the answer to your own submission. |
| Ranking by skip rate or session length | Ranking by whether people keep a track. |
| Emoji-heavy UI | Monochrome text. |
| Grading a file by its numbers | The curator's ear decides. Measurements feed ranking, never a verdict. |

The exploration term in the ranker exists for the same reason: an unheard track
gets a bonus proportional to how few people have been shown it, and every daily
selection reserves a slot for the least-exposed track in the catalogue. A
record nobody has played yet is a feature to fix, not a signal of low quality.

## Install

Any one of these works. The first is the shortest.

```bash
# uv (recommended — installs an isolated tool, no virtualenv to manage)
uv tool install git+https://github.com/yalayoloyellow/minimalma
```

```bash
# pipx
pipx install git+https://github.com/yalayoloyellow/minimalma
```

```bash
# pip, into a virtualenv you control
pip install git+https://github.com/yalayoloyellow/minimalma
```

```bash
# or just clone it — there is nothing to install
git clone https://github.com/yalayoloyellow/minimalma && cd minimalma
python3 -m minimalma setup
python3 -m minimalma run
```

Requires **Python 3.9 or newer** and nothing else. `ffmpeg` is optional: with
it you get loudness, tempo and spectral measurements that feed the recommender;
without it every other feature behaves identically.

## Set up

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Run `minimalma setup`. It validates the token, asks who the first curator is,
   and writes the configuration.
3. Run `minimalma run`.

If you do not know your Telegram user id, start the bot and send it `/whoami`,
then `minimalma curator add <id>`.

Submissions can be routed to a private group so several curators see the same
queue — give its chat id during setup. Otherwise review cards go to each
curator directly.

```
minimalma                           the same as minimalma desk
minimalma desk                      the curation console, and the bot with it
minimalma run                       the bot alone, headless (for a server)
minimalma setup                     first-run configuration
minimalma curator add|remove|list   manage who can publish
minimalma doctor                    check the installation
minimalma doctor --reindex          rebuild the search index
minimalma backup [path]             consistent copy of the database
minimalma export [path]             the catalogue as JSON
minimalma digest                    send the weekly note now
```

Everything lives in one directory — `~/Library/Application Support/minimalma` on
macOS, `$XDG_DATA_HOME/minimalma` on Linux, `%APPDATA%\minimalma` on Windows.
Override it with `MINIMALMA_HOME`. Backing up the service means copying one
SQLite file.

## Configuration

`config.json` in that directory. Every value has a working default; the ones
worth knowing:

```jsonc
{
  "station_name": "minimalma",
  "station_tagline": "",
  "review_chat": -1001234567890,   // where the queue goes
  "auto_approve": false,           // leave this alone; see below
  "weekly_digest": true,           // allows listeners to opt in
  "limits": {
    "daily_selection": 5,          // tracks per day, per listener
    "discover_batch": 3,           // tracks per discovery request
    "discover_sessions_per_day": 3,
    "submissions_per_day": 5,
    "pending_per_artist": 10,
    "min_duration": 20
  },
  "weights": {
    "collaborative": 1.0,
    "content": 1.0,
    "curator": 0.6,
    "freshness": 0.35,
    "exploration": 0.5,            // how hard to push unheard tracks
    "mmr_lambda": 0.7              // 1.0 relevance, 0.0 diversity
  }
}
```

`auto_approve` exists because operators ask for it. Turning it on makes this a
different product: the curation is the only thing separating a small station
from an upload folder.

## How the recommendations work

Three signals, blended by how much the listener has actually done, then
diversified.

**Content** — every track becomes a sparse TF‑IDF vector over curator tags, the
artist, the decade, and acoustic descriptors bucketed into coarse bands (tempo,
brightness, loudness, texture, dynamics). Cosine similarity against a
time-decayed profile of what you kept. This works on a track's first day, which
is the only thing that matters here.

**Collaborative** — item-item co-occurrence over likes, saves and completions
with popularity damping:

```
sim(i, j) = cooc(i, j) / (pop(i)^a · pop(j)^(1-a))          a = 0.5
```

Without the denominator, the most-played track becomes everyone's neighbour.
The top 40 neighbours per track are materialised and rebuilt in the background.

**Exposure** — a UCB1-shaped bonus:

```
explore(t) = sqrt( ln(E + 2) / (shown(t) + 1) )
```

where `E` is total catalogue exposure. New and overlooked tracks surface
without anyone having to promote them.

The blend is confidence-weighted — `conf = mass / (mass + 12)` — so a cold
listener is ranked almost entirely on content and curation, and collaborative
signal grows in as it earns the right to. Final selection runs Maximal Marginal
Relevance (`λ·score − (1−λ)·max similarity to what is already chosen`) with a
hard one-track-per-artist rule.

Mixes are a greedy nearest-neighbour walk with a tempo-jump penalty and a ban
on consecutive tracks by the same artist, so a mix has a shape.

## Metadata

Covers and tags go wrong in specific, boring ways, so intake reconciles four
sources in descending order of trust: tags embedded in the file, then
`ffprobe`, then the `performer`/`title` Telegram parsed, then the filename.

The parsers are byte-level and pure Python — synchsafe integers, ID3v2.2/2.3/2.4
frame layout differences, unsynchronisation, all four text encodings, APIC
picture-type priority, FLAC metadata blocks, `METADATA_BLOCK_PICTURE`, Ogg page
reassembly, the MP4 atom tree. Titles are cleaned of `(Official Video)`,
`[FREE]` and trailing producer credits; `feat.` is split off; artist names are
folded across case, script and diacritics so `Аквариум` and `Akvarium` are one
artist.

Cover art never "slips off" because the service does not re-host it. Telegram's
own thumbnail — already a durable photo `file_id` — is preferred, and the audio
file keeps whatever art it was uploaded with, so the artwork in the player is
the artwork in the file.

Duplicates are caught three ways: identical `file_unique_id`, identical folded
`artist + title` key, or a fuzzy name match within four seconds of duration.

## Architecture

```
minimalma/
  telegram.py   Bot API client: long polling, multipart, 429 backoff, error vocabulary
  db.py         SQLite schema, migrations, per-thread connections, WAL
  metadata.py   ID3/FLAC/Ogg/MP4/WAV parsing, normalisation, transliteration
  audio.py      ffprobe/ffmpeg + a pure-Python FFT: tempo, spectrum, loudness
  catalog.py    Intake, deduplication, moderation, the catalogue itself
  search.py     FTS5 with a transliterated column and a fuzzy fallback
  recommend.py  Vectors, item-item CF, exposure fairness, MMR, daily sets, mixes
  ui.py         Message rendering and keyboards
  handlers.py   Update routing and conversation state
  app.py        Polling loop, worker shards, background maintenance
  cli.py        setup / run / curator / doctor / backup / export / digest
```

Plus an optional `desk/` — a local curation console served by a stdlib HTTP
server with a build-free page; delete the directory and the bot is unchanged.
No dependencies, 285 tests. The conversation model is two
objects: **one screen message per chat**, edited in place for all browsing, and
**audio messages** appended only when someone taps play — so the chat stays a
readable listening log instead of a wall of menus.

[`docs/DESIGN.md`](docs/DESIGN.md) records why each of these is built the way it
is, including the alternatives that were tried and rejected.
[`docs/OPERATING.md`](docs/OPERATING.md) covers running it as a service.

## Development

```bash
git clone https://github.com/yalayoloyellow/minimalma && cd minimalma
uv venv && uv pip install pytest ruff
.venv/bin/python -m pytest      # 285 tests, ~30 seconds, no network
.venv/bin/ruff check .
```

The test suite drives the real update router through a Telegram double, so
submission, moderation, playback and privacy are exercised end to end without a
network. Contributions are welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Status

`0.2.0-alpha`. The data model is stable and migrated by version, but this has
not yet run a large public station. Report what breaks.

## License

MIT. See [LICENSE](LICENSE).
