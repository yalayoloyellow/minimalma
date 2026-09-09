# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- **Quality flags.** The review card no longer grades a submission — no bitrate
  warning, no "dull top end", no crest factor, no mono notice, no lossless
  container check. Most of those readings describe an aesthetic rather than a
  defect, so on a station for niche music they fired hardest on exactly the
  material it exists for, and a panel that reads like a verdict pushes a curator
  away from it. Whether a record belongs here is decided by ear.
- The measurements themselves stay and still feed the recommender through
  `feature_tokens()`; `audio.analyse()` now returns features alone rather than a
  `(features, quality)` pair, and `quality_flags()` and `describe_features()`
  are gone. An invariant test keeps the verdict from coming back.

## [0.2.0-alpha] - 2026-09-09

Releases become the unit of the catalogue, and a local curation console arrives
alongside the bot.

### Added

- **Releases.** Every track belongs to a single, an EP or an album. Submissions
  are grouped by their album tag within one artist, using the same folding rules
  as artist names; the kind follows the track count. Listeners get a release
  page — artwork, tracklist, one tap per track, and a "play it through" that
  still advances only on a tap.
- **Artwork is a publication requirement.** A release carries the cover and a
  track inherits it. Nothing publishes without one, in the bot or on the desk.
  Artists are asked for artwork the moment it is missing; curators can attach it
  themselves. Controlled by `require_cover`, on by default.
- **Release-level moderation.** The queue lists releases rather than loose
  tracks, one review card per release rather than one per track, and publishing
  or declining acts on every waiting track inside it.
- **The curation desk** (`tonearm desk`) — a local console: the queue with
  in-place playback, quality checks, metadata and note editing, drag-and-drop
  artwork, the catalogue, artists, playlists, station statistics and a bot
  start/stop switch. A stdlib HTTP server and a build-free page; `pywebview`
  gives it a native window when installed, otherwise it opens in a browser.
  Optional: delete `desk/` and the bot is unchanged.
- `catalog.restore_to_queue` and the desk's undo, so every moderation decision
  is reversible.

### Changed

- Schema version 2. Existing databases are migrated in one transaction: every
  track is grouped into a release, artwork and track numbers are backfilled, and
  a failure leaves a v1 database untouched.
- `tonearm` with no arguments now opens the desk instead of printing help.
- The bot's home screen gained a Releases entry and shows a release count.

### Fixed

- `Bad Request: message is not modified` was treated as retryable, costing about
  fifteen seconds of a worker every time a listener tapped the same navigation
  button twice.
- Sequence state was written to the database but not to the in-memory user row,
  so starting a playlist or a release and playing its first track in the same
  request read a stale queue.
- A mix could place two tracks by the same artist back to back.
- Item-item co-occurrence raised `KeyError` on its mirrored write.
- A photo screen could not be replaced by a text screen, leaving both in the
  chat.

## [0.1.0-alpha] - 2026-09-09

First public release.

### Added

- **Curated intake.** Artists submit audio directly to the bot; every
  submission enters a review queue and nothing is published without a curator.
  Approve, decline with a reason, retag, annotate and correct metadata from the
  queue. Artists are answered either way.
- **Metadata pipeline.** Pure-Python parsers for ID3v2.2/2.3/2.4, ID3v1, FLAC,
  Ogg Vorbis, Opus, MP4/M4A and WAV, including embedded cover art. Titles are
  cleaned of promotional debris, `feat.` credits are split out, and artist
  names are folded across case, script and diacritics. Duplicates are caught by
  file identity, by normalised name, and by fuzzy name plus duration.
- **Audio analysis.** Optional `ffmpeg`/`ffprobe` integration with a pure-Python
  FFT: EBU R128 loudness, tempo estimation, spectral descriptors, and quality
  checks for clipping, true-peak overs, over-compression and the spectral
  cutoff that reveals a lossy source in a lossless container.
- **Search.** SQLite FTS5 with bm25 column weighting, a transliterated column
  so Latin and Cyrillic queries find each other, safe query escaping, and a
  fuzzy fallback for typos.
- **Recommendations.** A hybrid of TF-IDF content similarity over curator tags
  and bucketed acoustic features, popularity-damped item-item collaborative
  filtering, and a UCB1-shaped exposure bonus, blended by interaction
  confidence and diversified with Maximal Marginal Relevance under a
  one-track-per-artist rule.
- **Listening.** A finite daily selection that cannot be re-rolled, capped
  discovery batches, saved tracks, followed artists, artist pages, curated
  playlists, and generated mixes that walk between related tracks.
- **Interface.** One self-editing screen message per chat plus audio messages
  appended only on an explicit tap. Inline mode for sharing tracks into other
  chats. English and Russian.
- **Operations.** `setup`, `run`, `curator`, `doctor`, `backup`, `export` and
  `digest` commands; online database backup; schema migrations by
  `user_version`; rotating logs; graceful shutdown; per-chat and global
  outbound throttling with 429 backoff.
- **Privacy.** Listeners can erase their history and library. An opt-in weekly
  note is the only scheduled outbound message.

### Notes

- Zero runtime dependencies. Python 3.9 or newer.
- 218 tests, including an invariants suite that pins the product's behavioural
  promises.

[Unreleased]: https://github.com/yalayoloyellow/tonearm/compare/v0.2.0-alpha...HEAD
[0.2.0-alpha]: https://github.com/yalayoloyellow/tonearm/compare/v0.1.0-alpha...v0.2.0-alpha
[0.1.0-alpha]: https://github.com/yalayoloyellow/tonearm/releases/tag/v0.1.0-alpha
