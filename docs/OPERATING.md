# Operating a station

Everything below assumes `tonearm setup` has been run once.

## Where things live

| | |
| --- | --- |
| macOS | `~/Library/Application Support/tonearm/` |
| Linux | `$XDG_DATA_HOME/tonearm/`, otherwise `~/.local/share/tonearm/` |
| Windows | `%APPDATA%\tonearm\` |

Override with the `TONEARM_HOME` environment variable. The directory holds
`config.json` (mode 0600, contains the token), `tonearm.db` and `tonearm.log`.

Back up the service by copying the database:

```bash
tonearm backup ~/backups/tonearm-$(date +%F).db
```

`tonearm backup` uses SQLite's online backup API, so it is safe to run while
the bot is serving traffic. A plain `cp` of a WAL database is not.

## Running it as a service

### systemd (Linux)

`/etc/systemd/system/tonearm.service`:

```ini
[Unit]
Description=Tonearm
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=tonearm
ExecStart=/usr/local/bin/tonearm run
Restart=always
RestartSec=10

# The service needs nothing outside its own state directory.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
StateDirectory=tonearm
Environment=TONEARM_HOME=/var/lib/tonearm

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tonearm
journalctl -u tonearm -f
```

### launchd (macOS)

`~/Library/LaunchAgents/com.tonearm.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.tonearm</string>
  <key>ProgramArguments</key> <array>
    <string>/usr/local/bin/tonearm</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/tmp/tonearm.out</string>
  <key>StandardErrorPath</key><string>/tmp/tonearm.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.tonearm.plist
```

### Docker

There is nothing to install, so the image is the base image plus the source:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
ENV TONEARM_HOME=/data
VOLUME /data
ENTRYPOINT ["python", "-m", "tonearm"]
CMD ["run"]
```

```bash
docker build -t tonearm .
docker run -d --name tonearm -v tonearm-data:/data \
  -e TONEARM_TOKEN=123456:AA... tonearm run
```

Drop the `ffmpeg` line for a smaller image; you lose the quality checks and
tempo estimation, nothing else.

## Health

```bash
tonearm doctor
```

Reports the Python version, the config and token state, curator count, whether
`ffmpeg` is on `PATH`, database integrity, catalogue size and whether the token
still authenticates.

```bash
tonearm doctor --reindex             # rebuild the FTS index
tonearm doctor --rebuild-similarity  # recompute recommendations now
```

Neither is normally needed — the search index is maintained on publish and
similarity rebuilds itself in the background — but they are the first thing to
try if search misses a track you know is published.

## Curators

```bash
tonearm curator list
tonearm curator add 123456789
tonearm curator remove 123456789
```

The first curator added becomes the owner. Anyone can find their own id by
sending `/whoami` to the bot.

For more than one curator, create a private Telegram group, add the bot, and
put the group's chat id in `review_chat`. Every submission then arrives there
as one card with the audio and the checks, and any curator can act on it.

## The queue in practice

Each review card carries the audio, the parsed metadata, technical details and
any quality flags. The buttons are Publish, Decline, Tags, Note and Edit.

- **Tags** drive recommendations. Two or three specific ones beat six vague
  ones. `search.suggest_tags` shows what the station already uses.
- **Note** is shown to listeners *before* they press play. It is the main thing
  distinguishing this from an upload folder — use it.
- **Edit** takes `Artist — Title` and rewrites both, moving the track to a
  different artist if needed.
- **Decline** asks for a reason, which the artist sees. Skipping the reason is
  allowed; a reason is better.

Nothing publishes without one of these taps. `tonearm doctor` reports how many
approved tracks have never been shown to anyone — that number going up means
the catalogue is growing faster than it is being heard, not that the ranker is
broken.

## Scale

The design target is a curated station: thousands of tracks, thousands of
listeners. At that size everything is comfortable — similarity rebuilds are
sub-second and ranking is a few sparse dot products.

Rough limits before something needs rethinking:

| | |
| --- | --- |
| Tracks | ~50,000 (ranking scans the newest 4,000 candidates) |
| Listeners | ~50,000 (SQLite writes are the ceiling, not reads) |
| Messages | ~25/second outbound, throttled |

Beyond that the honest answer is a different architecture, not a bigger
machine.

## Privacy

Listeners can erase their history and library from Settings; it deletes their
rows from `events`, `likes`, `follows`, `daily` and `usage` immediately.

The service stores Telegram user ids, display names, usernames and interaction
history. It never stores audio, message text, or anything from chats it is not
in. Tokens are never logged.

If you run a public station, say all of this somewhere your listeners can read
it.
