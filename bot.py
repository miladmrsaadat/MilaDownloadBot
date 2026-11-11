"""
Instagram Downloader & Audio Tools Telegram Bot
===============================================
A private Telegram bot that:
  1. Receives Instagram links (Reels, Posts, Carousels) shared from the Instagram
     app or pasted as URLs, downloads the media and sends it back.
  2. Under every video, provides 3 inline action buttons:
     - 🎙 Extract voice/audio (Telegram voice message + MP3 audio file)
     - 🎵 Identify music (via Shazam + links to Telegram music bots)
     - 📝 Transcribe audio to text (speech-to-text with auto language detection)
  3. Accepts voice and audio messages directly for music recognition and STT.

Author: Houshi & Milad (هوشی)
Version: 2.0.0
Date: November 2026
"""

import asyncio
import glob
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp
from shazamio import Shazam

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS = {
    int(uid.strip())
    for uid in ALLOWED_USERS_RAW.split(",")
    if uid.strip().isdigit()
}

CACHE_DIR = Path(os.getenv("CACHE_DIR", "/tmp/insta_bot_cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_HOURS = float(os.getenv("CACHE_TTL_HOURS", "2"))  # keep files 2 hours for buttons

COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip()
INSTAGRAM_PROXY = os.getenv("INSTAGRAM_PROXY", "").strip()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "tiny")  # tiny/base/small/medium

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("insta_bot")

thread_pool = ThreadPoolExecutor(max_workers=3)

# Regex to match Instagram URLs anywhere inside message text
INSTAGRAM_URL_REGEX = re.compile(
    r"https?://(?:(?:www|m)\.)?instagram\.com/(?:reel|p|tv|share/[rp]|stories/[\w\-.]+)/[\w\-.]+/?(?:\?[^\s]*)?",
    re.IGNORECASE,
)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# ---------------------------------------------------------------------------
# Media Cache (media_key -> metadata) for inline button actions
# ---------------------------------------------------------------------------
# MEDIA_CACHE = {
#     key: {
#         "file": Path, "created_at": float, "title": str, "uploader": str,
#         "url": str, "media_type": str
#     }
# }
MEDIA_CACHE: Dict[str, dict] = {}


def register_cache_media(media_file: Path, title: str, uploader: str, url: str) -> str:
    key = uuid.uuid4().hex[:12]
    ext = media_file.suffix.lower()
    media_type = "video" if ext in VIDEO_EXTS else "audio"
    MEDIA_CACHE[key] = {
        "file": media_file,
        "created_at": time.time(),
        "title": title,
        "uploader": uploader,
        "url": url,
        "media_type": media_type,
    }
    return key


def get_cached_media(key: str) -> Optional[Path]:
    entry = MEDIA_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["created_at"] > CACHE_TTL_HOURS * 3600:
        return None
    f = entry["file"]
    return f if f.exists() else None


async def cleanup_cache_loop():
    """Periodically remove cached media older than TTL."""
    while True:
        try:
            await asyncio.sleep(600)  # every 10 min
            now = time.time()
            expired = []
            for key, entry in list(MEDIA_CACHE.items()):
                if now - entry["created_at"] > CACHE_TTL_HOURS * 3600:
                    expired.append(key)
            for key in expired:
                entry = MEDIA_CACHE.pop(key, None)
                if entry and entry["file"].parent.exists():
                    shutil.rmtree(entry["file"].parent, ignore_errors=True)
            if expired:
                logger.info("Cleaned up %d expired cache entries", len(expired))
        except Exception:
            logger.exception("Cache cleanup error")


# ---------------------------------------------------------------------------
# Helper: Access Control
# ---------------------------------------------------------------------------
def is_user_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Instagram URL extraction & downloader (yt-dlp)
# ---------------------------------------------------------------------------
def extract_instagram_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = INSTAGRAM_URL_REGEX.search(text)
    if match:
        url = match.group(0)
        url = re.sub(r"\?.*$", "", url)
        if not url.endswith("/"):
            url += "/"
        return url
    return None


def download_instagram_media(url: str, out_dir: Path) -> dict:
    ydl_opts = {
        "outtmpl": str(out_dir / "%(id)s_%(playlist_index)s.%(ext)s"),
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "concurrent_fragment_downloads": 3,
        "socket_timeout": 30,
        "retries": 3,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE
    if INSTAGRAM_PROXY:
        ydl_opts["proxy"] = INSTAGRAM_PROXY

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "login required" in err or "private" in err:
            return {"success": False, "error": "private_or_login"}
        if "file exceeds maximum allowed size" in err:
            return {"success": False, "error": "file_too_large"}
        return {"success": False, "error": f"download_failed: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

    files = []
    for f in sorted(out_dir.glob("*")):
        if f.suffix.lower() in (VIDEO_EXTS | PHOTO_EXTS) and f.stat().st_size > 0:
            files.append(f)

    if not files:
        return {"success": False, "error": "no_media_found"}

    title = info.get("description") or info.get("title") or ""
    if title and len(title) > 900:
        title = title[:900] + "..."

    return {
        "success": True,
        "title": title,
        "files": files,
        "uploader": info.get("uploader") or "",
        "webpage_url": info.get("webpage_url") or url,
    }


# ---------------------------------------------------------------------------
# Audio extraction (ffmpeg)
# ---------------------------------------------------------------------------
def extract_audio(media_path: Path, out_dir: Path) -> Tuple[Path, Path]:
    """
    Extract audio from video/audio input to:
      1) .ogg Opus voice message (native Telegram voice)
      2) .mp3 audio file (forwardable audio)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ogg_path = out_dir / f"{media_path.stem}_voice.ogg"
    mp3_path = out_dir / f"{media_path.stem}.mp3"

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media_path), "-vn",
            "-c:a", "libopus", "-b:a", "64k", "-application", "voip",
            str(ogg_path), "-loglevel", "error",
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media_path), "-vn",
            "-c:a", "libmp3lame", "-b:a", "192k",
            str(mp3_path), "-loglevel", "error",
        ],
        check=True,
    )
    return ogg_path, mp3_path


def to_stt_wav(media_path: Path, out_dir: Path) -> Path:
    """16kHz mono WAV for speech recognition engines."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{media_path.stem}_stt.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(media_path), "-vn",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(wav_path), "-loglevel", "error",
        ],
        check=True,
    )
    return wav_path


# ---------------------------------------------------------------------------
# Music recognition (ShazamIO)
# ---------------------------------------------------------------------------
_shazam_instance: Optional[Shazam] = None


def _get_shazam() -> Shazam:
    global _shazam_instance
    if _shazam_instance is None:
        _shazam_instance = Shazam()
    return _shazam_instance


async def recognize_music(audio_path: Path) -> dict:
    """
    Identify music using Shazam.
    Returns {recognized: bool, title, artist, shazam_url, cover_url, raw}
    """
    try:
        shazam = _get_shazam()
        out = await shazam.recognize(str(audio_path))
        track = out.get("track") if isinstance(out, dict) else None
        if not track:
            return {"recognized": False}
        return {
            "recognized": True,
            "title": track.get("title") or "نامشخص",
            "artist": track.get("subtitle") or "نامشخص",
            "shazam_url": track.get("url") or "",
            "cover_url": (track.get("images") or {}).get("coverarthq") or "",
            "raw": track,
        }
    except Exception as e:
        logger.exception("Music recognition error")
        return {"recognized": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Speech-to-Text (multi-engine)
# ---------------------------------------------------------------------------
_whisper_local_model = None


def _get_local_whisper():
    global _whisper_local_model
    if _whisper_local_model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading local faster-whisper model: %s", WHISPER_MODEL)
        _whisper_local_model = WhisperModel(
            WHISPER_MODEL, device="cpu", compute_type="int8"
        )
    return _whisper_local_model


async def _stt_groq(audio_path: Path) -> Optional[dict]:
    if not GROQ_API_KEY:
        return None
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    try:
        async with aiohttp.ClientSession() as session:
            with open(audio_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=audio_path.name)
                data.add_field("model", "whisper-large-v3")
                headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
                async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        return {"text": res.get("text", "").strip(), "engine": "Groq Whisper (whisper-large-v3)", "lang": "auto"}
                    else:
                        logger.warning("Groq STT HTTP %d: %s", resp.status, await resp.text())
    except Exception as e:
        logger.warning("Groq STT failed: %s", e)
    return None


async def _stt_openai(audio_path: Path) -> Optional[dict]:
    if not OPENAI_API_KEY or not OPENAI_API_KEY.startswith("sk-"):
        return None
    url = "https://api.openai.com/v1/audio/transcriptions"
    try:
        async with aiohttp.ClientSession() as session:
            with open(audio_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=audio_path.name)
                data.add_field("model", "whisper-1")
                headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
                async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        return {"text": res.get("text", "").strip(), "engine": "OpenAI Whisper-1", "lang": "auto"}
                    else:
                        logger.warning("OpenAI STT HTTP %d", resp.status)
    except Exception as e:
        logger.warning("OpenAI STT failed: %s", e)
    return None


async def _stt_manus_cli(audio_path: Path) -> Optional[dict]:
    """Use the manus-speech-to-text CLI when available."""
    if not shutil.which("manus-speech-to-text"):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "manus-speech-to-text", str(audio_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        out = stdout.decode("utf-8", errors="ignore")
        lang_m = re.search(r"Language:\s*(\w+)", out)
        text_m = re.search(r"Text content:\s*(.*)", out, re.DOTALL)
        if text_m:
            text = text_m.group(1).strip()
            if text:
                return {"text": text, "lang": (lang_m.group(1) if lang_m else "auto"), "engine": "Manus STT"}
    except Exception as e:
        logger.warning("manus-speech-to-text failed: %s", e)
    return None


def _stt_local_faster_whisper(wav_path: Path) -> dict:
    try:
        model = _get_local_whisper()
        segments, info = model.transcribe(str(wav_path), beam_size=5)
        text = "".join(s.text for s in segments).strip()
        return {"text": text, "lang": info.language or "auto", "engine": f"Local Whisper ({WHISPER_MODEL})"}
    except Exception as e:
        logger.exception("local whisper error")
        return {"text": "", "error": str(e), "engine": "Local Whisper"}


async def transcribe_audio(audio_path: Path) -> dict:
    """Multi-tier STT: manus CLI -> Groq -> OpenAI -> local faster-whisper."""
    # 1) manus-speech-to-text (needs wav/mp3/mp4, not ogg)
    stt_input = audio_path
    if audio_path.suffix.lower() == ".ogg":
        wav = to_stt_wav(audio_path, audio_path.parent)
        stt_input = wav
    res = await _stt_manus_cli(stt_input)
    if res and res.get("text"):
        return {"success": True, **res}

    # 2) Groq
    res = await _stt_groq(stt_input)
    if res and res.get("text"):
        return {"success": True, **res}

    # 3) OpenAI
    res = await _stt_openai(stt_input)
    if res and res.get("text"):
        return {"success": True, **res}

    # 4) Local faster-whisper (needs wav)
    if stt_input.suffix.lower() != ".wav":
        wav = to_stt_wav(stt_input, audio_path.parent)
    else:
        wav = stt_input
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, _stt_local_faster_whisper, wav)
    if res.get("text"):
        return {"success": True, **res}
    return {
        "success": False,
        "text": "",
        "lang": res.get("lang", "auto"),
        "engine": res.get("engine", "Local Whisper"),
        "error": res.get("error", "هیچ گفتاری در صدا تشخیص داده نشد."),
    }


# ---------------------------------------------------------------------------
# Telegram UI: Keyboards
# ---------------------------------------------------------------------------
def make_media_keyboard(media_key: str, media_type: str = "video") -> InlineKeyboardMarkup:
    """Inline keyboard under a downloaded media item."""
    if media_type == "video":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎙 استخراج صدا (ویس)", callback_data=f"voice:{media_key}")],
            [InlineKeyboardButton("🎵 پیدا کردن موزیک", callback_data=f"shazam:{media_key}")],
            [InlineKeyboardButton("📝 تبدیل به متن", callback_data=f"stt:{media_key}")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🎵 پیدا کردن موزیک", callback_data=f"shazam:{media_key}")],
            [InlineKeyboardButton("📝 تبدیل به متن", callback_data=f"stt:{media_key}")],
        ])


def make_music_bot_links_keyboard(query: str) -> InlineKeyboardMarkup:
    """Links to popular Telegram music bots and web search for a song."""
    from urllib.parse import quote_plus
    q = quote_plus(query)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 آهنگیفای (@ahangifybot)", url="https://t.me/ahangifybot"),
            InlineKeyboardButton("🤖 ملوبات (@melobot)", url="https://t.me/melobot"),
        ],
        [
            InlineKeyboardButton("▶️ YouTube Music", url=f"https://music.youtube.com/search?q={q}"),
            InlineKeyboardButton("🎧 Spotify", url=f"https://open.spotify.com/search/{q}"),
        ],
    ])


# ---------------------------------------------------------------------------
# Telegram Handlers: Commands
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_user_allowed(user.id):
        await update.message.reply_text(
            "⛔ دسترسی شما به این ربات مجاز نیست. این ربات خصوصی است."
        )
        return

    text = (
        f"سلام {user.first_name}! 👋\n\n"
        "من **ربات اختصاصی دانلودر اینستاگرام و ابزار صوتی** شما هستم 🤖\n\n"
        "**روش استفاده خیلی ساده است:**\n"
        "۱. در اینستاگرام روی هر ریلز یا پست دکمه **Share (اشتراک‌گذاری)** را بزنید.\n"
        "۲. **تلگرام** و بعد **این ربات** را انتخاب کنید و بفرستید.\n"
        "۳. یا کافیه همین‌جا لینک را پیست کنید.\n\n"
        "زیر هر ویدیوی دانلودی ۳ دکمه کاربردی دریافت می‌کنید:\n"
        "• 🎙 **گرفتن صدای ریلز** (ویس تلگرام + فایل MP3)\n"
        "• 🎵 **پیدا کردن موزیک ریلز** (تشخیص موزیک + لینک بات‌های موزیک)\n"
        "• 📝 **تبدیل ویدیو/ویس به متن**\n\n"
        "دستورات:\n"
        "/help - راهنمای کامل\n"
        "/myid - نمایش شناسه عددی تلگرام شما\n"
        "/stats - وضعیت موقت سرور ربات"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 **راهنمای کامل:**\n\n"
        "• **ریلز، پست ویدیویی و عکس:** لینک را بفرستید یا از خود اینستاگرام Share کنید.\n"
        "• **استخراج ویس:** دکمه «🎙 استخراج صدا» صدا را هم به صورت ویس تلگرام و هم فایل MP3 می‌فرستد.\n"
        "• **پیدا کردن موزیک:** دکمه «🎵 پیدا کردن موزیک» آهنگ را با Shazam شناسایی می‌کند و لینک‌های جست‌وجو در یوتیوب موزیک، اسپاتیفای و بات‌های محبوب تلگرام (آهنگیفای @ahangifybot و ملوبات @melobot) را می‌دهد. اگر موزیک ناشناخته باشد، خود فایل صوتی ارسال می‌شود تا به راحتی به هر بتی فوروارد کنید.\n"
        "• **تبدیل به متن:** دکمه «📝 تبدیل به متن» گفتار ویدیو/ویس را با هوش مصنوعی متن‌نویسی می‌کند (پشتیبانی از فارسی و انگلیسی).\n"
        "• **ارسال مستقیم ویس/صوت:** هر ویس یا فایل صوتی هم مستقیماً بفرستید، می‌توانید برایش موزیک پیدا کنید یا به متن تبدیلش کنید.\n\n"
        "⚠️ **نکات مهم:**\n"
        "• پست‌های خصوصی به فایل `cookies.txt` روی سرور نیاز دارند.\n"
        "• سقف حجم فایل ارسالی تلگرام ۵۰ مگابایت است.\n"
        "• دکمه‌های هر پست تا ۲ ساعت پس از دانلود فعال می‌مانند."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"🆔 شناسه عددی تلگرام شما:\n`{user.id}`\n\n"
        "برای خصوصی‌سازی ربات، این عدد را در فایل `.env` جلوی `ALLOWED_USERS` بگذارید.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cached = len([k for k, v in MEDIA_CACHE.items() if time.time() - v["created_at"] <= CACHE_TTL_HOURS * 3600])
    text = (
        "📊 **وضعیت ربات:**\n"
        f"• محتواهای فعال در حافظه دکمه‌ها: {cached}\n"
        f"• مدل متن‌نویسی پیش‌فرض: `{WHISPER_MODEL}`\n"
        f"• کوکی اینستاگرام: {'فعال ✅' if COOKIES_FILE and os.path.exists(COOKIES_FILE) else 'غیرفعال ❌'}\n"
        f"• Groq STT: {'فعال ✅' if GROQ_API_KEY else 'غیرفعال ❌'}\n"
        f"• OpenAI STT: {'فعال ✅' if OPENAI_API_KEY else 'غیرفعال ❌'}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Telegram Handlers: Instagram URL messages
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    content_text = (message.text or "") + " " + (message.caption or "")
    if not content_text.strip():
        return

    user = update.effective_user
    if not is_user_allowed(user.id):
        await message.reply_text("⛔ دسترسی شما به این ربات مجاز نیست. این ربات خصوصی است.")
        return

    insta_url = extract_instagram_url(content_text)
    if not insta_url:
        await message.reply_text(
            "یک لینک اینستاگرام (ریلز، پست یا IGTV) بفرستید، یا از داخل خود اینستاگرام Share کنید. 📥"
        )
        return

    status_msg = await message.reply_text("⏳ در حال دریافت و آماده‌سازی محتوا از اینستاگرام...")
    await update.message.chat.send_action(ChatAction.RECORD_VIDEO)

    tmp_dir = Path(tempfile.mkdtemp(prefix="insta_", dir=CACHE_DIR))
    loop = asyncio.get_running_loop()

    try:
        result = await loop.run_in_executor(thread_pool, download_instagram_media, insta_url, tmp_dir)

        if not result["success"]:
            err = result["error"]
            if err == "private_or_login":
                await status_msg.edit_text(
                    "❌ این پست مربوط به یک پیج **خصوصی (Private)** است یا اینستاگرام برای مشاهده آن نیاز به ورود به حساب دارد.\n"
                    "برای دانلود از پیج‌های خصوصی، باید فایل `cookies.txt` اکانت اینستاگرام را در سرور قرار دهید.",
                    parse_mode=ParseMode.MARKDOWN,
                )
            elif err == "file_too_large":
                await status_msg.edit_text(f"❌ حجم ویدیو از سقف مجاز ربات ({MAX_FILE_SIZE_MB} مگابایت) بیشتر است.")
            elif err == "no_media_found":
                await status_msg.edit_text("❌ هیچ رسانه‌ای در این لینک پیدا نشد. لینک را دوباره بررسی کنید.")
            else:
                await status_msg.edit_text(f"❌ دانلود ناموفق بود.\nجزئیات: `{err[:300]}`", parse_mode=ParseMode.MARKDOWN)
            return

        files: List[Path] = result["files"]
        caption = result.get("title") or ""
        uploader = result.get("uploader") or ""
        footer = "\n" + "\n".join(filter(None, [
            f"👤 @{uploader}" if uploader else "",
            f"🔗 [لینک پست]({result.get('webpage_url')})",
        ]))
        full_caption = caption
        if full_caption and len(full_caption) + len(footer) > 1024:
            full_caption = full_caption[: 1024 - len(footer) - 3] + "..."
        full_caption = (full_caption + footer).strip() if full_caption else footer.strip()

        await status_msg.edit_text("📤 در حال ارسال به تلگرام...")

        for idx, media_file in enumerate(files):
            file_size_mb = media_file.stat().st_size / (1024 * 1024)
            if file_size_mb > MAX_FILE_SIZE_MB:
                await message.reply_text(
                    f"⚠️ فایل شماره {idx+1} ({file_size_mb:.1f} MB) از سقف ۵۰ مگابایت تلگرام بزرگ‌تر است و ارسال نشد."
                )
                continue

            cap = full_caption if idx == 0 else ""
            ext = media_file.suffix.lower()

            if ext in VIDEO_EXTS:
                media_key = register_cache_media(
                    media_file, caption, uploader, result.get("webpage_url") or insta_url
                )
                keyboard = make_media_keyboard(media_key, media_type="video")
                await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
                with open(media_file, "rb") as vf:
                    await message.reply_video(
                        video=vf,
                        caption=cap,
                        parse_mode=ParseMode.MARKDOWN,
                        supports_streaming=True,
                        reply_markup=keyboard,
                        read_timeout=180,
                        write_timeout=180,
                    )
            else:
                await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
                with open(media_file, "rb") as pf:
                    await message.reply_photo(
                        photo=pf,
                        caption=cap,
                        parse_mode=ParseMode.MARKDOWN,
                        read_timeout=60,
                        write_timeout=60,
                    )

        try:
            await status_msg.delete()
        except TelegramError:
            pass

    except Exception as e:
        logger.exception("Error handling Instagram URL")
        try:
            await status_msg.edit_text(f"❌ خطای غیرمنتظره: `{str(e)[:300]}`", parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass


# ---------------------------------------------------------------------------
# Telegram Handlers: Direct Voice/Audio messages
# ---------------------------------------------------------------------------
async def handle_audio_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    user = update.effective_user
    if not is_user_allowed(user.id):
        await message.reply_text("⛔ دسترسی شما به این ربات مجاز نیست. این ربات خصوصی است.")
        return

    status = await message.reply_text("⏳ در حال دریافت فایل صوتی...")
    tmp_dir = Path(tempfile.mkdtemp(prefix="audio_", dir=CACHE_DIR))

    try:
        if message.voice:
            tg_file = await message.voice.get_file()
            out_file = tmp_dir / "voice.ogg"
        else:
            tg_file = await message.audio.get_file()
            name = (message.audio.file_name or "audio.mp3").replace("/", "_")
            out_file = tmp_dir / name

        await tg_file.download_to_drive(str(out_file))

        key = register_cache_media(out_file, "Audio", user.first_name, "")
        keyboard = make_media_keyboard(key, media_type="audio")
        await status.edit_text(
            "🎵 فایل صوتی دریافت شد! یکی از گزینه‌های زیر را انتخاب کنید:",
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.exception("Error handling audio message")
        await status.edit_text(f"❌ خطا در پردازش صدا: `{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Telegram Handlers: Inline Button Callbacks
# ---------------------------------------------------------------------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    user = update.effective_user
    if not is_user_allowed(user.id):
        await query.message.reply_text("⛔ دسترسی شما به این ربات مجاز نیست.")
        return

    data = query.data
    action, _, key = data.partition(":")
    media_file = get_cached_media(key)
    entry = MEDIA_CACHE.get(key, {})

    if not media_file:
        await query.message.reply_text(
            "⚠️ محتوای این پست به دلیل گذشت زمان منقضی شده است. لطفاً لینک را دوباره ارسال کنید."
        )
        return

    loop = asyncio.get_running_loop()

    # ------------------ Action 1: Extract voice/audio ------------------
    if action == "voice":
        status = await query.message.reply_text("⏳ در حال استخراج صدا از ویدیو...")
        out_dir = Path(tempfile.mkdtemp(prefix="extract_", dir=CACHE_DIR))
        try:
            ogg_path, mp3_path = await loop.run_in_executor(thread_pool, extract_audio, media_file, out_dir)

            title = entry.get("title") or "صدای ریلز اینستاگرام"
            caption = f"🎙 {title[:300]}" if title else "🎙 صدای استخراج‌شده از ریلز"
            uploader = entry.get("uploader") or ""

            # 1) Send voice message
            await query.message.chat.send_action(ChatAction.RECORD_VOICE)
            with open(ogg_path, "rb") as vf:
                await query.message.reply_voice(voice=vf, caption=caption)

            # 2) Send forwardable mp3
            await query.message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            with open(mp3_path, "rb") as mf:
                await query.message.reply_audio(
                    audio=mf,
                    title="Instagram Reel Audio",
                    performer=uploader or "Instagram",
                    caption="🎵 فایل صوتی با کیفیت اصلی (قابل فوروارد)",
                )

            await status.delete()
        except Exception as e:
            logger.exception("Voice extraction error")
            await status.edit_text(f"❌ خطا در استخراج صدا: `{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

    # ------------------ Action 2: Identify music (Shazam) ------------------
    elif action == "shazam":
        status = await query.message.reply_text("🔍 در حال شناسایی موزیک در Shazam...")
        out_dir = Path(tempfile.mkdtemp(prefix="shazam_", dir=CACHE_DIR))
        try:
            # Need an audio file for recognition
            if media_file.suffix.lower() in VIDEO_EXTS:
                _, mp3_path = await loop.run_in_executor(thread_pool, extract_audio, media_file, out_dir)
                recog_file = mp3_path
            else:
                recog_file = media_file

            result = await recognize_music(recog_file)

            if result["recognized"]:
                title = result["title"]
                artist = result["artist"]
                shazam_url = result.get("shazam_url")
                cover = result.get("cover_url")

                lines = [
                    "🎵 **موزیک شناسایی شد!**",
                    f"• **عنوان:** {title}",
                    f"• **خواننده:** {artist}",
                ]
                if shazam_url:
                    lines.append(f"• [صفحه Shazam]({shazam_url})")

                kbd = make_music_bot_links_keyboard(f"{artist} {title}")
                if cover:
                    try:
                        await query.message.reply_photo(
                            photo=cover,
                            caption="\n".join(lines),
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=kbd,
                        )
                        await status.delete()
                        return
                    except Exception:
                        pass
                await status.edit_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kbd)
            else:
                # Not recognized -> offer extracted audio + bot links
                await status.edit_text(
                    "⚠️ موزیک این ویدیو در Shazam شناسایی نشد (احتمالاً موزیک دست‌ساز، ریمیکس غیررسمی یا افکت صوتی است).\n\n"
                    "فایل صوتی استخراج‌شده را زیر ارسال می‌کنم؛ می‌توانید آن را به بات‌های موزیک فوروارد کنید: 👇"
                )
                try:
                    with open(recog_file, "rb") as mf:
                        kbd = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("🤖 آهنگیفای (@ahangifybot)", url="https://t.me/ahangifybot"),
                                InlineKeyboardButton("🤖 ملوبات (@melobot)", url="https://t.me/melobot"),
                            ],
                            [InlineKeyboardButton("🤖 بات auddbot", url="https://t.me/auddbot")],
                        ])
                        await query.message.reply_audio(
                            audio=mf,
                            title="Reel Audio",
                            caption="🎵 این فایل را برای بات موزیک مورد نظرتان فوروارد کنید.",
                            reply_markup=kbd,
                        )
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Shazam error")
            await status.edit_text(f"❌ خطا در شناسایی موزیک: `{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)

    # ------------------ Action 3: Speech to text ------------------
    elif action == "stt":
        status = await query.message.reply_text("📝 در حال تبدیل گفتار به متن با هوش مصنوعی...")
        out_dir = Path(tempfile.mkdtemp(prefix="stt_", dir=CACHE_DIR))
        try:
            # Need an audio file
            if media_file.suffix.lower() in VIDEO_EXTS:
                _, mp3_path = await loop.run_in_executor(thread_pool, extract_audio, media_file, out_dir)
                stt_file = mp3_path
            else:
                stt_file = media_file

            res = await transcribe_audio(stt_file)

            if res["success"]:
                text = res["text"]
                lang = res.get("lang", "auto")
                engine = res.get("engine", "Whisper")
                body = (
                    "📝 **متن گفتار شناسایی‌شده:**\n\n"
                    f"`{text[:3500]}`\n\n"
                    f"🌐 زبان: `{lang}`  |  ⚙️ موتور: `{engine}`"
                )
                await status.edit_text(body, parse_mode=ParseMode.MARKDOWN)
            else:
                await status.edit_text(
                    f"⚠️ {res.get('error', 'متنی تشخیص داده نشد.')}\n(ممکن است ویدیو فقط موزیک بدون کلام داشته باشد)",
                    parse_mode=ParseMode.MARKDOWN,
                )
        except Exception as e:
            logger.exception("STT error")
            await status.edit_text(f"❌ خطا در متن‌نویسی: `{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN is not set! Please create a .env file with your Telegram Bot Token.\n"
            "Example: BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
        )
        print("\n❌ خطای مهم: توکن ربات تنظیم نشده است!")
        print("لطفاً توکن دریافتی از @BotFather را در فایل .env قرار دهید:")
        print("BOT_TOKEN=YOUR_BOT_TOKEN_HERE\n")
        raise SystemExit(1)

    builder = Application.builder().token(BOT_TOKEN)
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
        logger.info("Using Telegram proxy: %s", TELEGRAM_PROXY)
    app = builder.build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myid", myid_command))
    app.add_handler(CommandHandler("stats", stats_command))

    # Messages
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio_message))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & ~filters.COMMAND, handle_message))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Background cache cleaner
    async def post_init(_app):
        asyncio.create_task(cleanup_cache_loop())

    app.post_init = post_init

    logger.info("Bot is starting polling...")
    print("=" * 60)
    print("🤖 ربات دانلودر اینستاگرام (v2.0) آماده و در حال گوش دادن است...")
    print("اگر فایل .env را با توکن پر کرده‌اید، به ربات در تلگرام پیام بدهید!")
    print("=" * 60)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
