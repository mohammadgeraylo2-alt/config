# -*- coding: utf-8 -*-
"""
ربات دانلودر اینستاگرام/پینترست + جستجوی آهنگ برای روبیکا
ساخته‌شده با کتابخانه rubka
نسخه ۱: پست/ریل اینستاگرام، پین پینترست، جستجو و دانلود آهنگ
"""

import os
import re
import glob
import time
import logging
import threading
import subprocess
import asyncio
import sqlite3
from datetime import datetime, timedelta

import requests
import yt_dlp

from rubka import Robot
from rubka.context import Message
from rubka.button import InlineBuilder

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── تنظیم مسیر ffmpeg (لازم برای تشخیص آهنگ از ویدیو) ──────────────────────
try:
    import imageio_ffmpeg
    _FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
except Exception as _e:
    import shutil
    _FFMPEG_EXE = shutil.which("ffmpeg") or "ffmpeg"
    logger.warning(f"imageio_ffmpeg لود نشد، از ffmpeg سیستم استفاده میشه: {_e}")

# ─── تنظیمات ────────────────────────────────────────────────────────────────
TOKEN = os.environ["BOT_TOKEN"]                       # توکن ربات روبیکا
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")     # برای Pro Social API (اختیاری)
INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES", "")  # کوکی اینستاگرام برای yt-dlp (اختیاری)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")   # chat_id عددی/GUID خودت توی روبیکا (برای دسترسی به پنل ادمین)

CAPTION = "🎵 ربات دانلودر و موزیک‌یاب"

bot = Robot(token=TOKEN)

# ─── دیتابیس (کاربران، تنظیمات، آمار) ────────────────────────────────────────
DB_PATH = "bot.db"


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     TEXT PRIMARY KEY,
            first_name  TEXT,
            joined_at   TEXT,
            is_vip      INTEGER DEFAULT 0,
            vip_until   TEXT,
            is_banned   INTEGER DEFAULT 0,
            downloads   INTEGER DEFAULT 0,
            dl_today    INTEGER DEFAULT 0,
            dl_date     TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS stats (
            id          INTEGER PRIMARY KEY CHECK (id=1),
            total       INTEGER DEFAULT 0,
            instagram   INTEGER DEFAULT 0,
            pinterest   INTEGER DEFAULT 0,
            music       INTEGER DEFAULT 0,
            today_total INTEGER DEFAULT 0,
            today_date  TEXT
        );
        """)
        con.execute("INSERT OR IGNORE INTO stats (id, today_date) VALUES (1, ?)", (datetime.now().date().isoformat(),))
        defaults = {
            "maintenance": "0",
            "limit_enabled": "0",
            "free_limit": "10",
            "caption": CAPTION,
            "welcome": "",
            "channel_guid": "",
            "channel_link": "",
        }
        for k, v in defaults.items():
            con.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def get_setting(key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with db() as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
                     (key, value, value))


def register_user(chat_id, first_name=""):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, joined_at) VALUES (?, ?, ?)",
            (chat_id, first_name, datetime.now().isoformat()),
        )


def is_banned(chat_id):
    with db() as con:
        row = con.execute("SELECT is_banned FROM users WHERE user_id=?", (chat_id,)).fetchone()
    return bool(row and row[0])


def get_user(chat_id):
    with db() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM users WHERE user_id=?", (chat_id,)).fetchone()
    return dict(row) if row else None


def ban_user(chat_id):
    with db() as con:
        con.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (chat_id,))


def unban_user(chat_id):
    with db() as con:
        con.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (chat_id,))


def set_vip(chat_id, days):
    until = (datetime.now() + timedelta(days=days)).isoformat()
    with db() as con:
        con.execute("UPDATE users SET is_vip=1, vip_until=? WHERE user_id=?", (until, chat_id))


def remove_vip(chat_id):
    with db() as con:
        con.execute("UPDATE users SET is_vip=0, vip_until=NULL WHERE user_id=?", (chat_id,))


def add_download(chat_id, kind):
    today = datetime.now().date().isoformat()
    with db() as con:
        row = con.execute("SELECT dl_date, dl_today, downloads FROM users WHERE user_id=?", (chat_id,)).fetchone()
        if row:
            dl_date, dl_today, downloads = row
            dl_today = dl_today + 1 if dl_date == today else 1
            con.execute("UPDATE users SET downloads=?, dl_today=?, dl_date=? WHERE user_id=?",
                         (downloads + 1, dl_today, today, chat_id))
        con.execute("UPDATE stats SET total=total+1, today_total = CASE WHEN today_date=? THEN today_total+1 ELSE 1 END, today_date=? WHERE id=1",
                     (today, today))
        if kind in ("instagram", "pinterest", "music"):
            con.execute(f"UPDATE stats SET {kind} = {kind} + 1 WHERE id=1")


def get_stats():
    with db() as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM stats WHERE id=1").fetchone()
        total_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        vip_count = con.execute("SELECT COUNT(*) FROM users WHERE is_vip=1").fetchone()[0]
        banned_count = con.execute("SELECT COUNT(*) FROM users WHERE is_banned=1").fetchone()[0]
    d = dict(row) if row else {}
    d["total_users"], d["vip_count"], d["banned_count"] = total_users, vip_count, banned_count
    return d


def get_all_users():
    with db() as con:
        return [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]


def get_vip_users():
    with db() as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM users WHERE is_vip=1").fetchall()]


def get_banned_users():
    with db() as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM users WHERE is_banned=1").fetchall()]


def check_limit(message):
    """محدودیت دانلود رایگان روزانه - اگه فعال باشه و کاربر VIP نباشه چک میشه."""
    if get_setting("limit_enabled") != "1":
        return True
    u = get_user(message.chat_id)
    if not u:
        return True
    is_vip_active = u.get("is_vip") and (not u.get("vip_until") or u["vip_until"] >= datetime.now().isoformat())
    if is_vip_active:
        return True
    free_limit = int(get_setting("free_limit") or "10")
    today = datetime.now().date().isoformat()
    used_today = u.get("dl_today", 0) if u.get("dl_date") == today else 0
    if used_today >= free_limit:
        message.reply(f"⛔️ سقف دانلود رایگان امروزت ({free_limit} تا) تموم شده. فردا دوباره امتحان کن.")
        return False
    return True


# ─── جوین اجباری کانال ────────────────────────────────────────────────────────
def is_member(chat_id):
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        return True  # اگه ادمین کانال تنظیم نکرده، چک نمی‌کنیم
    try:
        return bool(bot.check_join(channel_guid, chat_id))
    except Exception as e:
        logger.warning(f"check_join error: {e}")
        return True  # اگه خطا داد (مثلاً ربات ادمین کانال نیست)، جلوی کاربر رو نگیر


def send_not_joined(message):
    link = get_setting("channel_link")
    builder = InlineBuilder()
    if link:
        builder = builder.row(builder.button_link("join_ch", "📢 عضویت در کانال", link))
    text = "⛔️ برای استفاده از ربات، اول باید عضو کانال ما بشی، بعد دوباره پیام بده."
    if link:
        message.reply_inline(text, builder.build())
    else:
        message.reply(text)


# چون get_updates به‌صورت پیوسته و تک‌رشته‌ای اجرا میشه، هر پردازش سنگین (دانلود)
# رو توی یک Thread جدا اجرا می‌کنیم تا ربات موقع دانلود یک نفر، بلاک نشه برای بقیه.
def run_in_thread(fn):
    def wrapper(*args, **kwargs):
        threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()
    return wrapper


def normalize_cookie_text(raw: str) -> str:
    if not raw:
        return raw
    text = raw.strip()
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


# ─── تشخیص آهنگ از داخل ویدیو با Shazam (رایگان، بدون کلید) ─────────────────
async def _shazam_detect_async(input_path):
    try:
        from shazamio import Shazam
        shazam = Shazam()
    except ImportError:
        logger.error("shazamio نصب نیست! به requirements.txt اضافه کن.")
        return None

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, timeout=10, text=True,
        )
        duration = float(probe.stdout.strip() or "0")
    except Exception:
        duration = 30

    if duration <= 15:
        offsets = [0]
    elif duration <= 30:
        offsets = [0, int(duration / 2)]
    else:
        offsets = [0, 10, 20, 30]

    for ss in offsets:
        tmp_audio = f"{input_path}_shazam_{ss}.wav"
        try:
            subprocess.run(
                [_FFMPEG_EXE, "-y", "-i", input_path, "-ss", str(ss), "-t", "12",
                 "-vn", "-ar", "44100", "-ac", "2", "-f", "wav", tmp_audio],
                capture_output=True, timeout=30,
            )
            if os.path.exists(tmp_audio) and os.path.getsize(tmp_audio) > 1000:
                out = await shazam.recognize(tmp_audio)
                track = out.get("track")
                if track:
                    return {"title": track.get("title", ""), "artist": track.get("subtitle", "")}
        except Exception as e:
            logger.warning(f"shazam offset ss={ss} error: {e}")
        finally:
            if os.path.exists(tmp_audio):
                try:
                    os.remove(tmp_audio)
                except Exception:
                    pass
    return None


def detect_song_sync(input_path):
    """اجرای همزمان (sync) تابع async شزم — هر thread حلقه‌ی asyncio خودش رو می‌سازه."""
    try:
        return asyncio.run(_shazam_detect_async(input_path))
    except Exception as e:
        logger.warning(f"detect_song_sync error: {e}")
        return None


# ─── Pro Social API (RapidAPI) - برای اینستاگرام ────────────────────────────
PRO_SOCIAL_HOST = "pro-social.p.rapidapi.com"


def _extract_first_video_url(d):
    if isinstance(d, str):
        if d.startswith("http") and (".mp4" in d or "video" in d.lower()):
            return d
        return None
    if isinstance(d, dict):
        for key in ("video_url", "download_url", "video", "url", "media_url", "hd_url", "sd_url"):
            v = d.get(key)
            if isinstance(v, str) and v.startswith("http") and (".mp4" in v or "video" in key):
                return v
        for v in d.values():
            found = _extract_first_video_url(v)
            if found:
                return found
    elif isinstance(d, list):
        for item in d:
            found = _extract_first_video_url(item)
            if found:
                return found
    return None


def pro_social_post_detail(shortcode: str):
    if not RAPIDAPI_KEY:
        return None
    try:
        r = requests.get(
            f"https://{PRO_SOCIAL_HOST}/postdetail/",
            headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": PRO_SOCIAL_HOST},
            params={"shortcode": shortcode, "safe_url": "false"},
            timeout=25,
        )
        d = r.json()
        return _extract_first_video_url(d)
    except Exception as e:
        logger.warning(f"Pro Social post error: {e}")
    return None


# ─── دانلود اینستاگرام (پست/ریل) ─────────────────────────────────────────────
@run_in_thread
def handle_instagram(message: Message, text: str):
    chat_id = message.chat_id
    clean_url = re.sub(r"\?.*$", "", text.strip())
    video_path = f"insta_{chat_id}.mp4"
    status = message.reply("⬇️ دارم از اینستاگرام دانلود میکنم...")

    downloaded = False
    try:
        # روش ۱: Pro Social API
        sc_m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", clean_url)
        if sc_m:
            video_url = pro_social_post_detail(sc_m.group(1))
            if video_url:
                data = requests.get(video_url, timeout=45).content
                with open(video_path, "wb") as f:
                    f.write(data)
                downloaded = True

        # روش ۲: yt-dlp (fallback)
        if not downloaded:
            ydl_opts = {
                "outtmpl": video_path,
                "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
                "noplaylist": True,
                "socket_timeout": 45,
                "retries": 5,
                "quiet": True,
                "http_headers": {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"},
            }
            if INSTAGRAM_COOKIES:
                cookie_file = "instagram_cookies.txt"
                with open(cookie_file, "w") as cf:
                    cf.write(normalize_cookie_text(INSTAGRAM_COOKIES))
                ydl_opts["cookiefile"] = cookie_file
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
            if os.path.exists(video_path):
                downloaded = True

        if downloaded and os.path.exists(video_path):
            message.reply_document(path=video_path, text=CAPTION)
            add_download(chat_id, "instagram")
            status.edit("🎵 دارم آهنگش رو شناسایی میکنم...")
            try:
                result = detect_song_sync(video_path)
                if result and result.get("title"):
                    title, artist = result["title"], result.get("artist", "")
                    status.edit(f"✅ آهنگ پیدا شد!\n🎵 {title}\n🎤 {artist}\n\nدارم دانلود میکنم...")
                    song_path = download_song_file(title, artist, chat_id)
                    if song_path and os.path.exists(song_path):
                        message.reply_music(path=song_path, text=CAPTION, file_name=f"{title} - {artist}.mp3")
                        status.edit("✅ دانلود شد.")
                        for f in glob.glob(f"song_{chat_id}.*"):
                            try:
                                os.remove(f)
                            except Exception:
                                pass
                    else:
                        status.edit("🎵 آهنگ شناسایی شد ولی دانلودش ممکن نشد.")
                else:
                    status.edit("✅ ویدیو ارسال شد.\n🎵 آهنگی شناسایی نشد.")
            except Exception as se:
                logger.warning(f"shazam on instagram error: {se}")
                status.edit("✅ ویدیو ارسال شد.")
        else:
            status.edit("❌ دانلود ممکن نشد. ممکنه پست خصوصی باشه یا لینک اشتباه باشه.")
    except Exception as e:
        logger.warning(f"instagram error: {e}")
        try:
            status.edit(f"❌ خطا: {e}")
        except Exception:
            pass
    finally:
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception:
                pass


# ─── دانلود پینترست ───────────────────────────────────────────────────────────
@run_in_thread
def handle_pinterest(message: Message, text: str):
    chat_id = message.chat_id
    status = message.reply("⬇️ دارم از پینترست دانلود میکنم...")
    headers_scrape = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    file_path = None
    try:
        pin_url = text.strip()
        if "pin.it" in pin_url:
            try:
                redir = requests.get(pin_url, headers=headers_scrape, timeout=15, allow_redirects=True)
                pin_url = redir.url
            except Exception:
                pass

        media_url, is_video = None, False
        try:
            html = requests.get(pin_url, headers=headers_scrape, timeout=20).text
            video_patterns = [
                r'"V_720p"[^}]*"url":"([^"]+)"',
                r'"V_480p"[^}]*"url":"([^"]+)"',
                r'"url":"(https://v1\.pinimg\.com/videos/[^"]+\.mp4[^"]*)"',
            ]
            for pat in video_patterns:
                m = re.search(pat, html)
                if m:
                    media_url = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
                    is_video = True
                    break
            if not media_url:
                img_patterns = [
                    r'"url":"(https://i\.pinimg\.com/originals/[^"]+)"',
                    r'(https://i\.pinimg\.com/originals/[^\s"\'\\]+)',
                ]
                for pat in img_patterns:
                    m = re.search(pat, html)
                    if m:
                        media_url = m.group(1).replace("\\u002F", "/").replace("\\/", "/")
                        break
        except Exception as e:
            logger.warning(f"pinterest scrape error: {e}")

        if not media_url:
            try:
                ydl_opts = {
                    "outtmpl": f"pinterest_{chat_id}.%(ext)s",
                    "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "merge_output_format": "mp4",
                    "noplaylist": True,
                    "quiet": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([pin_url])
                matches = glob.glob(f"pinterest_{chat_id}.*")
                if matches:
                    file_path = matches[0]
            except Exception as e:
                logger.warning(f"pinterest yt-dlp error: {e}")

        if not file_path and media_url:
            clean_media_url = media_url.split("?")[0]
            ext_out = clean_media_url.rsplit(".", 1)[-1].lower() if "." in clean_media_url else "jpg"
            if ext_out in ("heic", "heif", "avif", "webp"):
                ext_out = "jpg"
                media_url = re.sub(r"/originals/", "/736x/", media_url)
                media_url = re.sub(r"\.(heic|heif|avif|webp)(\?.*)?$", ".jpg", media_url)
            content = requests.get(media_url, headers=headers_scrape, timeout=40).content
            file_path = f"pinterest_{chat_id}.{ext_out}"
            with open(file_path, "wb") as f:
                f.write(content)

        if file_path and os.path.exists(file_path):
            ext = file_path.rsplit(".", 1)[-1].lower()
            if ext in ("mp4", "mov", "webm"):
                message.reply_document(path=file_path, text=CAPTION)
            else:
                message.reply_image(path=file_path, text=CAPTION)
            add_download(chat_id, "pinterest")
            status.edit("✅ دانلود شد.")
        else:
            status.edit("❌ دانلود پینترست ممکن نشد، دوباره امتحان کن.")
    except Exception as e:
        logger.warning(f"pinterest error: {e}")
        try:
            status.edit(f"❌ خطا: {e}")
        except Exception:
            pass
    finally:
        for f in glob.glob(f"pinterest_{chat_id}.*"):
            try:
                os.remove(f)
            except Exception:
                pass


# ─── جستجو و دانلود آهنگ ──────────────────────────────────────────────────────
def fuzzy_score(query, title):
    q, t = query.lower(), title.lower()
    if q == t:
        return 100
    score = 0
    if t.startswith(q) or q.startswith(t):
        score += 50
    score += len(set(q.split()) & set(t.split())) * 20
    q_chars, t_chars = set(q.replace(" ", "")), set(t.replace(" ", ""))
    score += int(len(q_chars & t_chars) / max(len(q_chars), len(t_chars), 1) * 30)
    return score


def search_songs(query):
    results, seen = [], set()
    try:
        r = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 8}, timeout=8)
        for track in r.json().get("data", []):
            title = track.get("title", "")
            artist = track.get("artist", {}).get("name", "")
            key = f"{title} {artist}".lower().strip()
            if key not in seen:
                seen.add(key)
                results.append({"title": title, "artist": artist, "score": fuzzy_score(query, f"{title} {artist}")})
    except Exception:
        pass
    if not results:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(f"ytsearch8:{query} song", download=False)
                for entry in info.get("entries", []):
                    title = entry.get("title", "")
                    key = title.lower().strip()
                    if key not in seen:
                        seen.add(key)
                        results.append({"title": title, "artist": entry.get("uploader", ""),
                                         "score": fuzzy_score(query, title)})
        except Exception:
            pass
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:6]


def download_song_file(title, artist, chat_id):
    mp3_path = f"song_{chat_id}.mp3"
    ydl_opts = {
        "format": "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "outtmpl": f"song_{chat_id}.%(ext)s",
        "quiet": True, "noplaylist": True, "socket_timeout": 30,
    }
    searches = [
        f"scsearch1:{title} {artist}",
        f"scsearch1:{title}",
        f"ytsearch1:{title} {artist} official audio",
        f"ytsearch1:{title} {artist}",
        f"ytsearch1:{title} audio",
    ]
    for search in searches:
        for f in glob.glob(f"song_{chat_id}.*"):
            try:
                os.remove(f)
            except Exception:
                pass
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([search])
            found = glob.glob(f"song_{chat_id}.*")
            if found:
                if found[0] != mp3_path:
                    os.rename(found[0], mp3_path)
                return mp3_path
        except Exception as e:
            logger.warning(f"song dl attempt failed ({search}): {e}")
            continue
    return None


user_search_results = {}  # chat_id -> list[{"title","artist"}]


@run_in_thread
def handle_music_search(message: Message, query: str):
    chat_id = message.chat_id
    status = message.reply("🔎 دارم آهنگ رو جستجو میکنم...")
    results = search_songs(query)
    if not results:
        status.edit("❌ آهنگی پیدا نشد، اسم دقیق‌تری بنویس.")
        return
    user_search_results[chat_id] = results
    builder = InlineBuilder()
    for i, t in enumerate(results):
        label = f"{t['title'][:28]} - {t['artist'][:15]}"
        builder = builder.row(builder.button_simple(f"dl_{i}", label))
    inline_keypad = builder.build()
    status.edit("🎵 نتایج جستجو، یکی رو انتخاب کن:")
    message.reply_inline("🎵 نتایج جستجو:", inline_keypad)


@run_in_thread
def handle_download_callback(message: Message, index: int):
    chat_id = message.chat_id
    results = user_search_results.get(chat_id, [])
    if not results or index >= len(results):
        message.reply("خطا، دوباره اسم آهنگ رو بفرست.")
        return
    track = results[index]
    title, artist = track["title"], track["artist"]
    status = message.reply(f"⬇️ دارم دانلود میکنم...\n{title} - {artist}")
    path = download_song_file(title, artist, chat_id)
    if not path or not os.path.exists(path):
        status.edit("❌ آهنگ پیدا نشد.")
        return
    try:
        message.reply_music(path=path, text=CAPTION, file_name=f"{title} - {artist}.mp3")
        add_download(chat_id, "music")
        status.edit("✅ ارسال شد.")
    except Exception as e:
        status.edit(f"❌ خطا در ارسال: {e}")
    finally:
        for f in glob.glob(f"song_{chat_id}.*"):
            try:
                os.remove(f)
            except Exception:
                pass


# ─── پنل ادمین ────────────────────────────────────────────────────────────────
admin_pending = {}  # chat_id -> اکشن در حال انتظار برای ورودی متنی


def admin_main_text():
    maintenance = "🟡 روشن" if get_setting("maintenance") == "1" else "🟢 خاموش"
    limit_st = "🟡 روشن" if get_setting("limit_enabled") == "1" else "🟢 خاموش"
    return f"🔧 پنل ادمین\n\nحالت تعمیر: {maintenance}\nمحدودیت دانلود: {limit_st}"


def admin_main_keypad():
    b = InlineBuilder()
    b = b.row(b.button_simple("adm_stats", "📊 آمار"))
    b = b.row(b.button_simple("adm_users", "👥 کاربران"), b.button_simple("adm_vip", "💎 VIP"))
    b = b.row(b.button_simple("adm_banned", "🚫 بن‌شده‌ها"))
    b = b.row(b.button_simple("adm_settings", "⚙️ تنظیمات"))
    b = b.row(b.button_simple("adm_broadcast", "📢 پیام همگانی"))
    b = b.row(b.button_simple("adm_maintenance", "🔧 حالت تعمیر (روشن/خاموش)"))
    b = b.row(b.button_simple("adm_limit_toggle", "🔢 محدودیت دانلود (روشن/خاموش)"))
    return b.build()


def back_keypad():
    b = InlineBuilder()
    return b.row(b.button_simple("adm_back", "🔙 برگشت")).build()


@bot.on_message(commands=["admin"])
def admin_panel(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    message.reply_inline(admin_main_text(), admin_main_keypad())


def handle_admin_callback(message: Message, btn_id: str):
    chat_id = message.chat_id

    if btn_id == "adm_back":
        message.reply_inline(admin_main_text(), admin_main_keypad())

    elif btn_id == "adm_maintenance":
        new_val = "0" if get_setting("maintenance") == "1" else "1"
        set_setting("maintenance", new_val)
        message.reply_inline(admin_main_text(), admin_main_keypad())

    elif btn_id == "adm_limit_toggle":
        new_val = "0" if get_setting("limit_enabled") == "1" else "1"
        set_setting("limit_enabled", new_val)
        message.reply_inline(admin_main_text(), admin_main_keypad())

    elif btn_id == "adm_stats":
        s = get_stats()
        text = (
            f"📊 آمار ربات\n\n"
            f"👥 کل کاربران: {s.get('total_users', 0)}\n"
            f"💎 VIP: {s.get('vip_count', 0)}\n"
            f"🚫 بن‌شده: {s.get('banned_count', 0)}\n\n"
            f"🔢 کل دانلودها: {s.get('total', 0)}\n"
            f"📸 اینستاگرام: {s.get('instagram', 0)}\n"
            f"📌 پینترست: {s.get('pinterest', 0)}\n"
            f"🎵 موزیک: {s.get('music', 0)}"
        )
        message.reply_inline(text, back_keypad())

    elif btn_id == "adm_users":
        total = len(get_all_users())
        message.reply_inline(f"👥 مدیریت کاربران\n\nکل: {total} کاربر\n\nبرای بن/آنبن یا دیدن اطلاعات یک کاربر:", user_actions_keypad())

    elif btn_id == "adm_vip":
        vips = get_vip_users()
        message.reply_inline(f"💎 مدیریت VIP\n\nتعداد فعال: {len(vips)}", vip_actions_keypad())

    elif btn_id == "adm_vip_list":
        vips = get_vip_users()
        if not vips:
            message.reply_inline("هیچ کاربر VIP فعالی وجود ندارد.", back_keypad())
            return
        lines = [f"👤 {v.get('user_id')}\n📅 تا: {(v.get('vip_until') or '')[:10] or 'نامحدود'}" for v in vips]
        message.reply_inline("💎 کاربران VIP:\n\n" + "\n\n".join(lines), back_keypad())

    elif btn_id == "adm_banned":
        banned = get_banned_users()
        if not banned:
            message.reply_inline("هیچ کاربر بن‌شده‌ای وجود ندارد.", back_keypad())
            return
        lines = [f"🚫 {b.get('user_id')}" for b in banned]
        message.reply_inline("🚫 کاربران بن‌شده:\n\n" + "\n".join(lines), back_keypad())

    elif btn_id == "adm_settings":
        text = (
            f"⚙️ تنظیمات\n\n"
            f"📣 GUID کانال: {get_setting('channel_guid') or '-'}\n"
            f"🔗 لینک کانال: {get_setting('channel_link') or '-'}\n"
            f"🔢 سقف رایگان روزانه: {get_setting('free_limit')}\n"
        )
        message.reply_inline(text, settings_keypad())

    elif btn_id in (
        "adm_give_vip", "adm_remove_vip", "adm_ban_user", "adm_unban_user", "adm_search_user",
        "adm_set_channel_guid", "adm_set_channel_link", "adm_set_limit", "adm_set_welcome",
        "adm_set_caption",
    ):
        prompts = {
            "adm_give_vip": "💎 chat_id کاربر رو بفرست (تعداد روز هم بعدش، مثلاً: 12345 30):",
            "adm_remove_vip": "💎 chat_id کاربری که میخوای VIP رو ازش بگیری بفرست:",
            "adm_ban_user": "🚫 chat_id کاربری که میخوای بن کنی بفرست:",
            "adm_unban_user": "✅ chat_id کاربری که میخوای آنبن کنی بفرست:",
            "adm_search_user": "🔍 chat_id کاربر رو بفرست:",
            "adm_set_channel_guid": "📣 GUID کانال رو بفرست (مثلاً c0xABCDEF...) — ربات باید ادمین کانال باشه:",
            "adm_set_channel_link": "🔗 لینک عضویت کانال رو بفرست (مثلاً https://rubika.ir/mychannel):",
            "adm_set_limit": "🔢 سقف دانلود رایگان روزانه رو بفرست (عدد):",
            "adm_set_welcome": "👋 متن پیام خوش‌آمدگویی جدید رو بفرست:",
            "adm_set_caption": "✏️ متن کپشن فایل‌ها رو بفرست:",
        }
        admin_pending[chat_id] = btn_id
        message.reply_inline(prompts[btn_id], back_keypad())

    elif btn_id == "adm_broadcast":
        admin_pending[chat_id] = "adm_broadcast"
        message.reply_inline("📢 متن پیامی که میخوای به همه کاربران ارسال بشه رو بفرست:", back_keypad())


def user_actions_keypad():
    b = InlineBuilder()
    b = b.row(b.button_simple("adm_search_user", "🔍 اطلاعات کاربر"))
    b = b.row(b.button_simple("adm_ban_user", "🚫 بن کاربر"), b.button_simple("adm_unban_user", "✅ آنبن کاربر"))
    b = b.row(b.button_simple("adm_back", "🔙 برگشت"))
    return b.build()


def vip_actions_keypad():
    b = InlineBuilder()
    b = b.row(b.button_simple("adm_give_vip", "➕ دادن VIP"), b.button_simple("adm_remove_vip", "➖ گرفتن VIP"))
    b = b.row(b.button_simple("adm_vip_list", "📋 لیست VIPها"))
    b = b.row(b.button_simple("adm_back", "🔙 برگشت"))
    return b.build()


def settings_keypad():
    b = InlineBuilder()
    b = b.row(b.button_simple("adm_set_channel_guid", "📣 تنظیم GUID کانال"))
    b = b.row(b.button_simple("adm_set_channel_link", "🔗 تنظیم لینک کانال"))
    b = b.row(b.button_simple("adm_set_limit", "🔢 محدودیت رایگان"))
    b = b.row(b.button_simple("adm_set_welcome", "👋 پیام خوش‌آمد"))
    b = b.row(b.button_simple("adm_set_caption", "✏️ کپشن فایل‌ها"))
    b = b.row(b.button_simple("adm_back", "🔙 برگشت"))
    return b.build()


def handle_admin_text(message: Message, text: str):
    chat_id = message.chat_id
    action = admin_pending.pop(chat_id, None)
    if not action:
        return

    if action == "adm_give_vip":
        try:
            parts = text.split()
            uid, days = parts[0], int(parts[1]) if len(parts) > 1 else 30
            set_vip(uid, days)
            message.reply(f"✅ کاربر {uid} برای {days} روز VIP شد.")
        except Exception:
            message.reply("❌ فرمت اشتباه. مثال: 12345 30")

    elif action == "adm_remove_vip":
        remove_vip(text.strip())
        message.reply(f"✅ VIP کاربر {text.strip()} حذف شد.")

    elif action == "adm_ban_user":
        register_user(text.strip())
        ban_user(text.strip())
        message.reply(f"🚫 کاربر {text.strip()} بن شد.")

    elif action == "adm_unban_user":
        unban_user(text.strip())
        message.reply(f"✅ کاربر {text.strip()} آنبن شد.")

    elif action == "adm_search_user":
        u = get_user(text.strip())
        if not u:
            message.reply("کاربر پیدا نشد.")
        else:
            vip_until = (u.get("vip_until") or "")[:10] or "-"
            message.reply(
                f"👤 اطلاعات کاربر\n\n"
                f"🆔 chat_id: {u['user_id']}\n"
                f"📅 عضویت: {(u.get('joined_at') or '')[:10]}\n"
                f"💎 VIP: {'بله تا ' + vip_until if u.get('is_vip') else 'خیر'}\n"
                f"🚫 بن: {'بله' if u.get('is_banned') else 'خیر'}\n"
                f"📥 کل دانلود: {u.get('downloads', 0)}"
            )

    elif action == "adm_set_channel_guid":
        set_setting("channel_guid", text.strip())
        message.reply(f"✅ GUID کانال تنظیم شد: {text.strip()}")

    elif action == "adm_set_channel_link":
        set_setting("channel_link", text.strip())
        message.reply(f"✅ لینک کانال تنظیم شد: {text.strip()}")

    elif action == "adm_set_limit":
        try:
            set_setting("free_limit", str(int(text.strip())))
            message.reply(f"✅ سقف رایگان به {text.strip()} در روز تغییر کرد.")
        except Exception:
            message.reply("❌ یک عدد وارد کن.")

    elif action == "adm_set_welcome":
        set_setting("welcome", text)
        message.reply("✅ پیام خوش‌آمدگویی آپدیت شد.")

    elif action == "adm_set_caption":
        set_setting("caption", text)
        message.reply("✅ کپشن فایل‌ها آپدیت شد.")

    elif action == "adm_broadcast":
        targets = get_all_users()
        status = message.reply(f"📢 در حال ارسال به {len(targets)} کاربر...")
        success, fail = 0, 0
        for uid in targets:
            try:
                bot.send_message(uid, text)
                success += 1
            except Exception:
                fail += 1
        status.edit(f"✅ ارسال شد: {success}\n❌ ناموفق: {fail}")


# ─── هندلرهای اصلی ────────────────────────────────────────────────────────────
@bot.on_message(commands=["start"])
def start(bot: Robot, message: Message):
    register_user(message.chat_id)
    if is_banned(message.chat_id):
        message.reply("⛔️ شما مسدود شده‌اید.")
        return
    if not is_member(message.chat_id):
        send_not_joined(message)
        return
    welcome = get_setting("welcome")
    message.reply(
        welcome or (
            "سلام! 👋\n\n"
            "🔗 لینک پست/ریل اینستاگرام یا لینک پینترست بفرست تا دانلودش کنم.\n"
            "🎵 یا اسم یک آهنگ بنویس تا برات پیدا و دانلودش کنم.\n\n"
            "/help — راهنما"
        )
    )


@bot.on_message(commands=["help"])
def help_cmd(bot: Robot, message: Message):
    message.reply(
        "📖 راهنما:\n\n"
        "• لینک اینستاگرام (پست/ریل) بفرست\n"
        "• لینک پینترست بفرست\n"
        "• اسم آهنگ یا خواننده بنویس"
    )


@bot.on_message()
def handle_text(bot: Robot, message: Message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    # ─── اگه ادمینه و منتظر جواب یه اکشن پنل ادمینه ───
    if ADMIN_CHAT_ID and message.sender_id == ADMIN_CHAT_ID and message.chat_id in admin_pending:
        handle_admin_text(message, text)
        return

    register_user(message.chat_id)
    if is_banned(message.chat_id):
        message.reply("⛔️ شما مسدود شده‌اید.")
        return
    if get_setting("maintenance") == "1" and message.sender_id != ADMIN_CHAT_ID:
        message.reply("🔧 ربات در حال تعمیر است.")
        return
    if not is_member(message.chat_id):
        send_not_joined(message)
        return
    if not check_limit(message):
        return

    if "instagram.com" in text:
        handle_instagram(message, text)
    elif "pinterest.com" in text or "pin.it" in text:
        handle_pinterest(message, text)
    else:
        handle_music_search(message, text)


@bot.on_callback()
def on_callback(bot: Robot, message: Message):
    btn_id = message.aux_data.button_id if message.aux_data else None
    if not btn_id:
        return
    if btn_id.startswith("dl_"):
        try:
            index = int(btn_id.split("_")[1])
        except Exception:
            return
        handle_download_callback(message, index)
    elif btn_id.startswith("adm_"):
        if message.sender_id != ADMIN_CHAT_ID or not ADMIN_CHAT_ID:
            return
        handle_admin_callback(message, btn_id)


if __name__ == "__main__":
    init_db()
    logger.info("Rubika bot starting...")
    bot.run()
