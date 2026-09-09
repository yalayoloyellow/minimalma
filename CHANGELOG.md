# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/yalayoloyellow/tonearm/compare/v0.1.0-alpha...HEAD
[0.1.0-alpha]: https://github.com/yalayoloyellow/tonearm/releases/tag/v0.1.0-alpha
