# Design notes

Why each part is built the way it is, and what was rejected. Read this before
changing anything structural; several of the decisions below look arbitrary
until you know what they are avoiding.

---

## 1. Why a Telegram bot rather than a service with a client

A streaming service normally needs: accounts, file storage, a CDN, an audio
player for three platforms, background playback, and push delivery. Telegram
supplies all six, for free, in an app the audience already has.

What is left is the part that actually distinguishes one station from another:
the catalogue, the curation and the ranking. That fits in one process and one
SQLite file, which is why this project can be a single dependency-free program
rather than a deployment.

**Rejected: a Telegram Mini App.** A Mini App is a web page, which means HTTPS
hosting, a certificate, `initData` signature validation and a second codebase.
It would buy a nicer player and cost the "runs anywhere with just a token"
property, which is the reason this is usable by a person running a small label
from a laptop.

**Rejected: Subsonic API compatibility.** Tempting — it would open the catalogue
to existing clients — but it implies a server, authentication and a very
different data model. It is a good idea for a different project.

---

## 2. Audio is never re-hosted

When an artist uploads a file, Telegram stores it and returns a `file_id`. The
service records that id and re-sends it. It never downloads and re-uploads
audio for delivery.

Consequences, all of them good:

- Storage cost is zero and there is no CDN.
- The artwork embedded in the file is the artwork in the player, forever,
  because the bytes are never touched again.
- Sending a track is one API call with no upload.

The file *is* downloaded once, at intake, to read its tags and analyse it —
capped at 20 MiB because that is the ceiling for bot downloads. Above that the
service falls back to what Telegram reported and flags the submission for the
curator.

**Consequence to know:** `file_id` values are scoped to the bot that received
them. Moving a catalogue to a different bot token invalidates every one of
them. If that ever needs solving, the answer is to re-upload from an archive
chat, not to store audio.

---

## 3. The conversation model: one screen, plus a listening log

A chat is append-only. Every bot that renders menus as new messages turns the
conversation into a scrollback of dead UI.

The model here is two kinds of message:

- **The screen.** One message per chat, its id stored on the user row, edited
  in place for every menu, list, search result and artist page. Browsing seven
  screens produces seven `editMessageText` calls and zero new messages — there
  is a test for exactly that.
- **Track messages.** Real Telegram audio messages, appended when someone taps
  play, and never deleted. The chat becomes a listening log you can scroll back
  through and replay. This is the one thing a chat client does better than an
  app, and fighting it would be a mistake.

After a track is sent the screen is deleted and re-sent so the controls stay
below the music. That is the only time a browsing action creates a message.

**Rejected: `editMessageMedia` as a player.** A single audio message edited from
track to track keeps the chat at one message, but it resets the player position
on every edit, loses the listening history, and produces a "Next" affordance
that invites exactly the behaviour this project is trying not to encourage.

**Rejected: reply keyboards.** They cover the compose box and cannot be
attached to a specific message.

---

## 4. HTML, not MarkdownV2

MarkdownV2 requires escaping fifteen characters. A track called `Sunset_2` or
an artist called `A. B.` will take a message down with a 400 the first time
someone forgets. `html.escape` handles three characters and cannot be got
wrong. Every user-supplied string passes through `ui.esc()`.

---

## 5. Callback data is a 64-byte budget

Telegram caps `callback_data` at 64 bytes and gives no useful error when you
exceed it. `ui.pack()` raises at build time instead, so a too-long tag fails in
tests rather than in production. The scheme is `verb|arg|arg` with short verbs
(`nav`, `play`, `seq`, `mod`, `like`).

Sequence state — the ordered list of a mix — does **not** live in callback data.
It lives in `users.state_data` as JSON, so the button only needs an index.

---

## 6. Storage

One SQLite file. WAL, `synchronous=NORMAL`, `busy_timeout=8000`,
`foreign_keys=ON` per connection (it is a per-connection pragma, which is the
usual reason constraints appear to be ignored). One connection per thread via
`threading.local`, one process-wide write lock, because SQLite serialises
writers anyway and an explicit lock makes the transaction boundaries obvious.

Migrations are a list of SQL scripts indexed by `PRAGMA user_version`. A
database written by a newer version is refused rather than opened.

**Gotcha, already fixed once:** `executescript()` commits any open transaction
before running. Wrapping it in `BEGIN`/`COMMIT` from Python raises "cannot
commit - no transaction is active". The transaction has to be inside the script
string.

---

## 7. Metadata: four sources, one order of trust

1. Tags embedded in the file.
2. `ffprobe`, when available.
3. `performer` / `title` as Telegram parsed them.
4. The filename.

Each fills only the blanks left by the one above it (`Tags.merged_with`).

The parsers are byte-level because the alternative is a dependency. Points that
took care to get right, and which the tests pin:

- **Synchsafe integers.** ID3v2 tag sizes and *v2.4 frame sizes* use 7 bits per
  byte; v2.3 frame sizes are plain big-endian. Reading a v2.4 file with v2.3
  rules silently drops every frame after the first once a size exceeds 0x7F.
- **v2.2** uses 3-character frame ids and 3-byte sizes, and `PIC` instead of
  `APIC` with a 3-character image format instead of a MIME type.
- **Unsynchronisation** at both tag and frame level: `FF 00` → `FF`.
- **Text encodings** 0–3, including UTF-16 terminators, which are two zero
  bytes on an even boundary — searching for a single `\x00` truncates
  every UTF-16 string mid-character.
- **APIC priority.** Files often carry several images; picture type 3 (front
  cover) wins, then 0, then the rest. The declared MIME type is not trusted —
  the magic bytes are.
- **FLAC** picture blocks and the base64 `METADATA_BLOCK_PICTURE` comment used
  by Ogg.
- **Ogg** requires reassembling packets across pages via the segment table.
- **MP4** `meta` is a full box: four bytes of version/flags before its children.
  Miss them and the atom walk lands in the middle of a child header.

Normalisation folds NFKC, strips zero-width characters, unifies quotes and
dashes, removes `(Official Video)` / `[FREE]` / trailing `prod. by …`, splits
`feat.`, and transliterates Cyrillic so `Аквариум` and `Akvarium` collapse to
one artist key.

---

## 8. Audio analysis is optional by construction

`ffmpeg` gets used when present and every function returns an empty result when
it is not. Nothing downstream requires a feature to exist.

Costs were measured, not guessed. Analysis decodes at most 90 seconds from the
middle of the track at 22050 Hz mono, then:

- **Spectral pass** — FFT size 1024, hop 4096, so roughly 480 frames. An
  iterative radix-2 FFT over two float lists (not `complex`, which allocates)
  is about 3 ms per frame in CPython, so ~1.5 s per track in a worker thread.
  Averaged centroid, roll-off, flatness, bandwidth, and the highest bin still
  above −56 dB of the peak, which is the spectral cutoff.
- **Envelope pass** — RMS per 512-sample block, no FFT: levels, crest factor,
  zero-crossing rate, DC offset, clipping ratio. Then tempo from the
  autocorrelation of the differentiated log envelope over lags corresponding to
  55–200 BPM, refined by parabolic interpolation. Accurate to a couple of BPM
  on percussive material and unreliable on ambient — which is why the value is
  only ever consumed as a coarse band and never shown to anyone.

Loudness comes from `ffmpeg -af loudnorm=print_format=json`, which reports
`input_i`, `input_tp`, `input_lra` and `input_thresh` as JSON on stderr.

**These numbers never reach a curator.** They exist to feed the recommender —
tempo, brightness, level and texture become the bucketed tokens in
`feature_tokens()` — and for nothing else.

An earlier version derived "quality flags" from the same measurements and put
them on the review card: low bitrate, dull top end, over-compression, mono, a
lossless container whose spectrum stops early. It was removed, and the reasoning
is worth keeping.

Ask of any indicator: does it tell the curator something their ears cannot, and
does it change what they do? Almost none of them passed. A cutoff at 13 kHz
might be a bad encode — or a cassette, a lo-fi mix, or a field recording on a
cheap microphone. On a station whose whole point is niche and experimental
music, that flag fires hardest on exactly the material the station exists for.
Crest factor and mono are aesthetic choices, not defects. True-peak overs matter
when audio is transcoded, and nothing here transcodes: Telegram re-sends the
original file.

Only the lossless-from-lossy case survived that test on its own merits, and it
went too — because if the decision is made by ear, a file that sounds right *is*
right, and the contradiction between container and content is bookkeeping rather
than judgement. What remains is worth stating plainly: **a panel that reads like
a verdict quietly pushes a curator away from the music the station was built
for.** There is an invariant test pinning this.

Subprocesses are always argument lists, never a shell, always with a timeout,
and temporary files are removed in a `finally`.

---

## 9. Search

FTS5 with `unicode61 remove_diacritics 2`, six columns weighted by `bm25()`.

Two decisions worth keeping:

- **A transliterated `alt` column.** Cyrillic rows also store their Latin form,
  and Cyrillic queries are transliterated before matching. Both directions
  work, and a mixed-script catalogue behaves like one search space.
- **User input is never an FTS expression.** It is tokenised with
  `[^\W_]+` and re-quoted, with a prefix wildcard on the final token. A stray
  `"` or the word `NEAR` would otherwise raise `OperationalError` in the user's
  face. There is a test that throws nine hostile strings at it.

When FTS returns nothing, a `difflib` scan over the newest 2000 tracks catches
typos. Bounded deliberately: past that size the latency stops being worth it,
and by then FTS will have matched.

**Rejected: the trigram tokenizer.** Better substring behaviour, noticeably
larger index, and the fuzzy fallback already covers the case.

---

## 10. Ranking

Full formulas are in the README. The reasoning behind the shape:

- **Content-first for cold users.** `conf = mass / (mass + 12)` weights the
  collaborative term. A new listener is ranked on curation and content, which
  is the only thing that can work on day one and the only thing that can work
  for a track nobody has played.
- **Content never fully gives way.** Its weight is `1 − 0.5·conf`, so even a
  heavy user keeps half the content signal. Pure CF converges on whatever is
  already popular, which on a small station means the first fifty tracks
  approved.
- **Popularity damping** in the item-item similarity, or the most-played track
  becomes everyone's neighbour.
- **Exposure bonus** as UCB1 over how often a track has been *shown*, not
  played. Shown is the fair denominator: a track nobody was offered has not
  failed at anything.
- **A reserved slot.** Every daily selection replaces its last pick with the
  least-exposed eligible track outright. Scores alone are not enough; without
  a hard rule the tail never gets a turn.
- **MMR plus one-track-per-artist**, so five tracks are five artists.
- **Skips count for −0.25, not −1.** Weighting skips heavily is precisely how
  a recommender starts optimising for stickiness.

The daily selection is written to a table on first request and never
recomputed. This is a product decision, not a cache: a selection you can
re-roll is a slot machine.

**Rejected: EASE^R and matrix factorisation.** Both are better at scale and
both need a dense linear-algebra library. At a few thousand tracks, damped
item-item CF with a content hybrid is within noise of them and is forty lines.

**Gotcha, already fixed once:** in `a[k] = a[k].get(...) + x`, Python evaluates
the right-hand side first, so the key must already exist. The mirrored
co-occurrence write needs two statements.

---

## 11. Concurrency

One polling thread, four worker threads, one maintenance thread.

Updates are sharded to workers by `abs(chat_id) % 4`, so two messages from the
same person can never be processed out of order — the reason for sharding
rather than a shared queue.

Outbound calls pass through a throttle: ~25/second globally and ~1/second per
chat. Telegram does not publish exact limits; the 429 handler is the real
safety net and the throttle exists to stay away from it.

**Gotcha, already fixed once:** `Bad Request: message is not modified` is
survivable but *permanent*. Treating it as retryable meant five attempts with
exponential backoff — about fifteen seconds of a worker — every time a user
tapped the same nav button twice.

---

## 12. The invariants are tests

[`tests/test_invariants.py`](../tests/test_invariants.py) asserts the product
promises rather than the implementation: no audio without a tap, sequences that
end, no counters shown to listeners, no urgency vocabulary in any string, no
emoji, nothing published without a curator, and no third-party import anywhere
in the package.

They exist because these are exactly the properties a well-meaning refactor
erodes one commit at a time. If one fails, the change is altering what this
product is, and it should have to say so out loud.

---

## 13. Releases (schema v2)

A track is never loose. It belongs to a single, an EP or an album; the release
carries the artwork; and the release, not the track, is what a curator accepts.

**Grouping** happens at intake, by the album tag, folded with the same rules as
artist names — so `Группа крови` and `группа  крови` are one release, and the
same album title by two different artists stays two releases. A track with no
album tag becomes a single named after itself, which is what keeps every later
code path free of a "loose track" special case. The kind follows the count:
1 → single, 2–6 → EP, 7+ → album.

**Artwork is a hard requirement.** `release_blockers()` returns `no_cover` and
both `approve_release()` and the single-track `approve()` refuse. This is
deliberate and it is the one place the project chooses friction: a release with
no cover looks broken in every surface that shows it, and review is the only
moment when someone will actually fix it. The artist is asked for artwork the
instant it is missing, because they can solve it in one message.

Cover resolution is layered — Telegram's own thumbnail first (it is already a
durable photo `file_id`), then art embedded in the file, then an image sent by
hand. Release artwork and an audio's own thumbnail are separate states. When a
track has no Telegram thumbnail, the bot re-uploads that audio once with a
compact JPEG thumbnail after the release cover exists; the old `file_id` stays
available if Telegram cannot download or recreate the file.

**Delivery.** A release page is a photo message with the tracklist as its
caption, which is why `ui.Screen` grew a `photo` field. Telegram cannot edit a
text message into a photo message or the reverse, so a photo screen always
replaces the previous one — and `TelegramError.message_gone` had to learn
"there is no text in the message to edit", or navigating away from a release
left two screens in the chat.

**Migration.** `_migration_2` is a function rather than a SQL string because it
ends in a backfill that needs Python's folding rules, and because a function
keeps the DDL and the data move inside one transaction. `executescript()` would
have committed the schema change before the backfill could fail, leaving a
half-migrated database with no way back. There is a test that kills the backfill
mid-flight and asserts the v1 database is untouched.

---

## 14. The curation desk

`desk/` is optional, and the package below `tonearm/` does not import it — a
test enforces that at column zero, allowing only the lazy, `ImportError`-guarded
import in `cli.py`. Delete the directory and the bot is unchanged.

It is a stdlib `ThreadingHTTPServer` serving one hand-written page. No Flask, no
React, no npm: the design is flat lists and hairline rules, so a build step
would buy nothing and cost the "clone and run" property. `pywebview` gives it a
native window when present; otherwise it opens a browser tab. Neither is a
dependency.

**Loopback is not an authorisation boundary.** Any other process on the machine
can reach `127.0.0.1`, so the server mints a per-session key, hands it out in
the URL it opens, and compares it with `secrets.compare_digest` on every API
call. The page and its assets are served without one; nothing else is.

**Audio is served with Range support**, which is not optional: WebKit refuses to
seek — and in some builds refuses to play at all — when a media response arrives
without it. Files are fetched once through `getFile` and cached under the data
directory, with an LRU trim, so scrubbing a track does not re-download it.

**Destructive keys need a modifier.** The first version bound approve to `a` and
decline to `d`, with auto-advance to the next item. During testing three
submissions were declined in three seconds without anyone meaning to. Whatever
sent those keystrokes, the lesson stands: a single bare letter that performs an
irreversible-looking action and immediately moves on is a footgun. Publishing is
`⌘↵`, declining is `⌘⌫`, and both offer an undo afterwards — which is the real
fix, because a reversible decision is what makes a fast workflow safe to offer
at all.

---

## 15. Who owns what

One rule settles most of the interface:

> The release belongs to the artist. The shelf belongs to the curator.

The artist owns what the record *is* — title, artwork, running order. The
curator owns whether it is on the shelf and how it is labelled — publish or
decline, tags, note. Neither reaches into the other's half.

An earlier version got this wrong. A curator could rewrite titles, rename
artists and drag artwork onto a release, and a release missing its cover sat in
the queue behind a red banner. Both are the same mistake: work that is not
curation, handed to the person whose only job is to decide.

Two consequences follow, and they are the load-bearing ones:

**An incomplete submission never enters the queue.** `missing_for_release()` is
a completeness check on the artist's side, not a judgement, and
`pending_releases()` filters on it. A queue should contain only things that can
be decided; a release with no artwork is a message to its author. The artist is
told exactly what is missing on the Submit screen, and the moment the picture
arrives the release joins the queue — which is also the first time any curator
hears about it. `_notify_curators` is gated on the same check, so an unfinished
record produces no notification at all.

**A curator writes two fields.** Tags and the note. Tags stay with the curator
because they are the station's vocabulary and they feed the recommender: an
artist tags for promotion, a curator tags for the shelf, and handing the
vocabulary to the artist means the ranking starts believing their marketing.
The note is the curator's voice — the one thing a listener reads before pressing
play, and the whole difference between this and an upload folder. Tags are set
on the release and fan out to its tracks, because a curator thinks "this is an
ambient record", not track by track.

The desk API expresses the boundary directly: there is one write endpoint,
`curate`, it accepts `{tags, note}`, and there is a test asserting that no
endpoint for renaming a release or uploading its cover exists at all.

Declining a release takes down every track in it, not only the waiting ones —
otherwise a declined release could leave published tracks behind, a state no
screen in the product knows how to describe.

**A single track is never decided on its own.** Not accepted, not declined, not
withdrawn. The unit an artist submits is the unit a curator answers, and
anything finer is a curator editing somebody's record. `catalog` exposes only
`approve_release`, `reject_release`, `hide_release` and `restore_release`; an
invariant test asserts the per-track verbs do not exist. The desk's catalogue
browses releases for the same reason — a text query still searches tracks,
because that is what a person types, but the results fold back to the releases
holding them.
