# Instagram Media Tools — Telegram Bot (v2.0)

A private Telegram bot built with **python-telegram-bot**, **yt-dlp**, **ShazamIO**, and **Whisper** that receives Instagram links (Reels, Posts, Carousels) — shared directly from the Instagram app or pasted as URLs — downloads the media, and provides interactive inline action buttons under every video.

## Features

- **Direct In-App Share Support**: Recognizes Instagram URLs embedded in share-sheet text from the mobile Instagram app (`instagram.com/reel/`, `/p/`, `/tv/`, `/share/r/`, `/share/p/`, `/stories/`, `m.instagram.com`).
- **High Quality Media**: Downloads best-quality video (MP4) and photo carousels via yt-dlp + ffmpeg.
- **🎙 Extract Voice/Audio**: Inline button converts the reel into a native Telegram voice message (`.ogg` Opus) + a high-quality `.mp3` audio file.
- **🎵 Identify Music**: Inline button recognizes the song via Shazam, returns title, artist, cover art, and deep links to YouTube Music, Spotify, and popular Telegram music bots (@ahangifybot, @melobot). If unrecognized, the raw extracted audio is delivered so the user can easily forward it to any music bot.
- **📝 Speech-to-Text**: Inline button transcribes spoken words using multi-tier AI engines: Manus STT CLI → Groq Whisper (`whisper-large-v3`) → OpenAI Whisper-1 → local `faster-whisper` (100% offline, free). Auto language detection with full Persian and English support.
- **Direct Voice/Audio Messages**: Send any voice note or audio file to the bot and run music recognition or transcription on it.
- **Access Control**: Optional `ALLOWED_USERS` whitelist to keep the bot strictly private.
- **Proxy Support**: Both Telegram polling proxy (`TELEGRAM_PROXY`) and Instagram download proxy (`INSTAGRAM_PROXY`).
- **Instagram Cookies**: `COOKIES_FILE` support to bypass login walls.
- **Production Ready**: `systemd` service, `Dockerfile`, `docker-compose.yml`, and 15 passing unit tests.

## Quick Start

```bash
cd insta_telegram_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg flac

cp .env.example .env
# Edit .env and set your BOT_TOKEN from @BotFather

python bot.py
```

## Running Tests

```bash
venv/bin/python test_bot.py
# 15 tests covering regex extraction, share texts, access control, cache,
# audio conversion and keyboard builders
```

## Configuration (.env)

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Token from @BotFather |
| `ALLOWED_USERS` | No | Comma-separated Telegram user IDs |
| `COOKIES_FILE` | No | Path to exported Instagram `cookies.txt` |
| `CACHE_TTL_HOURS` | No | Button action cache lifetime in hours (default: 2) |
| `WHISPER_MODEL` | No | Local Whisper model: `tiny`/`base`/`small` |
| `GROQ_API_KEY` | No | Free fast cloud STT via Groq |
| `OPENAI_API_KEY` | No | OpenAI Whisper-1 |
| `TELEGRAM_PROXY` | No | Proxy URL for Telegram polling (e.g. `socks5://127.0.0.1:10808`) |
| `INSTAGRAM_PROXY` | No | Proxy URL for yt-dlp Instagram requests |
| `MAX_FILE_SIZE_MB` | No | Max file size (default: 50) |
| `LOG_LEVEL` | No | INFO / DEBUG / WARNING / ERROR |

## Deployment

### systemd (Linux / Cloud Computer 24/7)

```bash
sudo cp insta_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now insta_bot
journalctl -u insta_bot -f
```

### Docker Compose

```bash
docker compose up -d --build
docker compose logs -f
```

## Troubleshooting: Instagram Login Wall

Datacenter IPs are often blocked by Instagram without authentication. Export cookies from a logged-in browser session using the "Get cookies.txt LOCALLY" extension, save as `cookies.txt`, and enable `COOKIES_FILE=./cookies.txt` in `.env`.

Keep yt-dlp updated: `pip install --upgrade yt-dlp`.
