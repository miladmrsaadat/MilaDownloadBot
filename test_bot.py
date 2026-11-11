"""
Test suite for Instagram Telegram Bot v2
Verifies URL regex extraction, access control, media cache,
audio conversion, keyboard builders, and offline behavior.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import (
    MEDIA_CACHE,
    extract_audio,
    extract_instagram_url,
    get_cached_media,
    is_user_allowed,
    make_media_keyboard,
    make_music_bot_links_keyboard,
    register_cache_media,
    to_stt_wav,
)


class TestInstagramBot(unittest.TestCase):
    def test_extract_url_direct_link(self):
        url = "https://www.instagram.com/reel/DC_test123/"
        self.assertEqual(extract_instagram_url(url), url)

    def test_extract_url_without_www(self):
        url = "https://instagram.com/reel/DC_test123"
        self.assertEqual(extract_instagram_url(url), "https://instagram.com/reel/DC_test123/")

    def test_extract_url_m_subdomain(self):
        url = "https://m.instagram.com/reel/DC_test123/?igsh=123"
        self.assertEqual(extract_instagram_url(url), "https://m.instagram.com/reel/DC_test123/")

    def test_extract_url_with_query_params(self):
        url = "https://www.instagram.com/reel/DC_test123/?igsh=abcdefgh123&utm_source=ig_web_copy_link"
        self.assertEqual(extract_instagram_url(url), "https://www.instagram.com/reel/DC_test123/")

    def test_extract_url_from_share_text(self):
        text = "Check out this reel from Milad! https://www.instagram.com/reel/DC_test123/ #reels #ai"
        self.assertEqual(extract_instagram_url(text), "https://www.instagram.com/reel/DC_test123/")

    def test_extract_url_post(self):
        text = "Look at this https://instagram.com/p/DBxyz789/?img_index=1"
        self.assertEqual(extract_instagram_url(text), "https://instagram.com/p/DBxyz789/")

    def test_extract_url_tv(self):
        url = "https://instagram.com/tv/CB1234567/"
        self.assertEqual(extract_instagram_url(url), url)

    def test_extract_url_stories(self):
        url = "https://instagram.com/stories/username/1234567890123456789/"
        self.assertEqual(extract_instagram_url(url), url)

    def test_extract_url_share_links(self):
        url = "https://www.instagram.com/share/r/ABC123xyz/?s=12"
        self.assertEqual(extract_instagram_url(url), "https://www.instagram.com/share/r/ABC123xyz/")
        url2 = "https://instagram.com/share/p/XYZ789abc/"
        self.assertEqual(extract_instagram_url(url2), "https://instagram.com/share/p/XYZ789abc/")

    def test_extract_url_no_match(self):
        self.assertIsNone(extract_instagram_url("Hello bot, how are you?"))
        self.assertIsNone(extract_instagram_url("https://youtube.com/watch?v=12345"))
        self.assertIsNone(extract_instagram_url(""))
        self.assertIsNone(extract_instagram_url(None))

    def test_access_control(self):
        import bot

        old = bot.ALLOWED_USERS
        bot.ALLOWED_USERS = set()
        self.assertTrue(is_user_allowed(999999999))
        bot.ALLOWED_USERS = {111111, 222222}
        self.assertTrue(is_user_allowed(111111))
        self.assertFalse(is_user_allowed(333333))
        bot.ALLOWED_USERS = old

    def test_cache_register_and_get(self):
        tmp = Path(tempfile.mkdtemp(prefix="cachetest_"))
        f = tmp / "media.mp4"
        f.write_bytes(b"dummy")
        key = register_cache_media(f, "title", "uploader", "http://url")
        self.assertIn(key, MEDIA_CACHE)
        self.assertEqual(get_cached_media(key), f)
        self.assertEqual(MEDIA_CACHE[key]["media_type"], "video")

    def test_make_media_keyboard(self):
        kbd = make_media_keyboard("abc123456789", "video")
        callbacks = [b.callback_data for row in kbd.inline_keyboard for b in row]
        self.assertIn("voice:abc123456789", callbacks)
        self.assertIn("shazam:abc123456789", callbacks)
        self.assertIn("stt:abc123456789", callbacks)

    def test_make_music_bot_links_keyboard(self):
        kbd = make_music_bot_links_keyboard("Coldplay Yellow")
        urls = [b.url for row in kbd.inline_keyboard for b in row]
        self.assertTrue(any("ahangify" in u for u in urls))
        self.assertTrue(any("melobot" in u for u in urls))
        self.assertTrue(any("youtube.com" in u for u in urls))

    def test_extract_audio_ffmpeg(self):
        tmp = Path(tempfile.mkdtemp(prefix="audiotest_"))
        src = tmp / "src.mp3"
        subprocess.run(
            ["ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "mp3", "-y", str(src), "-loglevel", "error"],
            check=True,
        )
        ogg, mp3 = extract_audio(src, tmp / "out")
        self.assertTrue(ogg.exists() and ogg.stat().st_size > 0)
        self.assertTrue(mp3.exists() and mp3.stat().st_size > 0)
        self.assertEqual(ogg.suffix, ".ogg")
        self.assertEqual(mp3.suffix, ".mp3")
        wav = to_stt_wav(src, tmp / "out")
        self.assertTrue(wav.exists() and wav.suffix == ".wav")


if __name__ == "__main__":
    unittest.main()
