# -*- coding: utf-8 -*-
"""
ربات دانلودر اینستاگرام/پینترست + جستجوی آهنگ برای روبیکا
ساخته‌شده با کتابخانه rubka
نسخه ۱: پست/ریل اینستاگرام، پین پینترست، جستجو و دانلود آهنگ
"""

import os
import re
import json
import glob
import time
import logging
import inspect
import traceback
import subprocess
import asyncio
import sqlite3
import threading
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

CAPTION = "📥 دانلود شد با ربات دانلودر اینستاگرام، پینترست و موزیک‌یاب\n✨ برای دانلود پست/ریل/پینترست بعدیت، فقط لینکشو بفرست"

bot = Robot(token=TOKEN)

# ─── لاگین اکانت جدا برای Rubino (فقط ادمین) ────────────────────────────────
import queue
import base64

_rubino_login_state = {}


def _rubino_login_worker(phone: str, code_queue: "queue.Queue", result_queue: "queue.Queue"):
    import builtins
    original_input = builtins.input

    def fake_input(prompt=""):
        logger.info(f"🔑 rubino login prompt: {prompt}")
        p = (prompt or "").lower()
        if "phone" in p or "شماره" in p:
            return phone
        if "correct" in p or "[y or n]" in p or "y/n" in p:
            return "y"
        return code_queue.get()

    builtins.input = fake_input
    try:
        from rubpy import Client
        with Client("rubino_acc") as client:
            client.start(phone_number=phone)
        result_queue.put(("ok", None))
    except Exception as e:
        result_queue.put(("error", str(e)))
    finally:
        builtins.input = original_input
        _rubino_login_state["in_progress"] = False


async def _watch_rubino_login(chat_id, result_queue):
    status, err = await asyncio.to_thread(result_queue.get)
    _rubino_login_state["in_progress"] = False
    if status == "ok":
        try:
            # اسم دقیق و مسیر فایل session بسته به نسخه‌ی rubpy فرق می‌کنه، برای
            # همین به‌جای فرض کردن یه اسم ثابت، کل پروژه رو برای هر فایلی که
            # اسمش با rubino_acc شروع بشه می‌گردیم (هم پسوند .session، هم .db، هم
            # بدون پسوند، هم داخل زیرپوشه‌ها).
            candidates = sorted(
                glob.glob("rubino_acc*") + glob.glob("**/rubino_acc*", recursive=True),
                key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                reverse=True,
            )
            candidates = [p for p in dict.fromkeys(candidates) if os.path.isfile(p)]
            if not candidates:
                raise FileNotFoundError("هیچ فایل session‌ای با اسم rubino_acc پیدا نشد")
            session_path = candidates[0]
            with open(session_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            b64_file_path = "rubino_session_b64.txt"
            with open(b64_file_path, "w") as out:
                out.write(b64)
            await asyncio.to_thread(
                _rubika_send_document_blocking,
                chat_id,
                b64_file_path,
                f"✅ لاگین موفق شد (فایل: {session_path}).\nاین فایل رو دانلود و بازش کن، کل متنش رو کپی کن و توی Railway به‌عنوان RUBINO_SESSION_B64 ذخیره کن. بعد این فایل و پیام‌های قبلی رو از چت پاک کن.",
            )
            try:
                os.remove(b64_file_path)
            except Exception:
                pass
        except Exception as e:
            await _rx(bot.send_message(chat_id, f"⚠️ لاگین شد ولی خوندن session خطا داد: {e}"))
    else:
        await _rx(bot.send_message(chat_id, f"❌ لاگین ناموفق: {err}"))

@bot.on_message(commands=["rubino_login"])
async def rubino_login_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    logger.info(f"DEBUG rubino_login text={message.text!r}")
    if _rubino_login_state.get("in_progress"):
        await _rx(message.reply(
            "⚠️ یه لاگین دیگه در حال انجامه. اگه گیر کرده، اول بزن:\n/rubino_reset"
        ))
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _rx(message.reply("فرمت: /rubino_login 989123456789"))
        return
    phone = parts[1]
    code_queue = queue.Queue()
    result_queue = queue.Queue()
    _rubino_login_state["code_queue"] = code_queue
    _rubino_login_state["in_progress"] = True
    t = threading.Thread(target=_rubino_login_worker, args=(phone, code_queue, result_queue), daemon=True)
    t.start()
    await _rx(message.reply("📲 منتظر کد تایید هستم. وقتی رسید بفرست:\n/rubino_code 12345"))
    _spawn(_watch_rubino_login(message.chat_id, result_queue))


@bot.on_message(commands=["rubino_code"])
async def rubino_code_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _rx(message.reply("فرمت: /rubino_code 12345"))
        return
    q = _rubino_login_state.get("code_queue")
    if not q:
        await _rx(message.reply("❌ اول /rubino_login رو بزن."))
        return
    q.put(parts[1])
    await _rx(message.reply("⏳ در حال بررسی کد..."))


@bot.on_message(commands=["rubino_reset"])
async def rubino_reset_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    _rubino_login_state.clear()
    removed = []
    for fname in sorted(set(glob.glob("rubino_acc*") + glob.glob("**/rubino_acc*", recursive=True))):
        if not os.path.isfile(fname):
            continue
        try:
            os.remove(fname)
            removed.append(fname)
        except Exception as e:
            logger.warning(f"⚠️ حذف {fname} ناموفق بود: {e}")
    if removed:
        await _rx(message.reply(f"✅ ریست شد. فایل‌های حذف‌شده: {', '.join(removed)}\nحالا دوباره /rubino_login بزن."))
    else:
        await _rx(message.reply("✅ ریست شد (فایل session‌ای پیدا نشد).\nحالا دوباره /rubino_login بزن."))


# ─── لاگین جدا با pyrubi (تلاش دوم برای چک عضویت کانال، چون rubpy با وجود
# سشن سالم هم روی get_channel_info/get_channel_all_members با INVALID_INPUT
# گیر کرد) ───────────────────────────────────────────────────────────────
_pyrubi_login_state = {}


def _pyrubi_login_worker(phone: str, code_queue: "queue.Queue", result_queue: "queue.Queue"):
    import builtins
    original_input = builtins.input

    def fake_input(prompt=""):
        logger.info(f"🔑 pyrubi login prompt: {prompt}")
        p = (prompt or "").lower()
        if "phone" in p or "شماره" in p:
            return phone
        if "correct" in p or "[y or n]" in p or "y/n" in p:
            return "y"
        return code_queue.get()

    builtins.input = fake_input
    try:
        from pyrubi import Client
        client = Client("pyrubi_acc")
        # پیرابی خودش موقع اولین ساخت Client، اگه سشن نباشه، وارد فرآیند
        # لاگین اینتراکتیو میشه (همون‌جایی که fake_input جواب میده).
        try:
            client.get_me()
        except Exception:
            pass
        result_queue.put(("ok", None))
    except Exception as e:
        result_queue.put(("error", str(e)))
    finally:
        builtins.input = original_input
        _pyrubi_login_state["in_progress"] = False


async def _watch_pyrubi_login(chat_id, result_queue):
    status, err = await asyncio.to_thread(result_queue.get)
    _pyrubi_login_state["in_progress"] = False
    if status == "ok":
        try:
            candidates = sorted(
                glob.glob("pyrubi_acc*") + glob.glob("**/pyrubi_acc*", recursive=True),
                key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                reverse=True,
            )
            candidates = [p for p in dict.fromkeys(candidates) if os.path.isfile(p)]
            if not candidates:
                raise FileNotFoundError("هیچ فایل session‌ای با اسم pyrubi_acc پیدا نشد")
            session_path = candidates[0]
            with open(session_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            b64_file_path = "pyrubi_session_b64.txt"
            with open(b64_file_path, "w") as out:
                out.write(b64)
            await asyncio.to_thread(
                _rubika_send_document_blocking,
                chat_id,
                b64_file_path,
                f"✅ لاگین pyrubi موفق شد (فایل: {session_path}).\nاین فایل رو دانلود و بازش کن، کل متنش رو کپی کن و توی Railway به‌عنوان PYRUBI_SESSION_B64 ذخیره کن. بعد این فایل و پیام‌های قبلی رو از چت پاک کن.",
            )
            try:
                os.remove(b64_file_path)
            except Exception:
                pass
        except Exception as e:
            await _rx(bot.send_message(chat_id, f"⚠️ لاگین pyrubi شد ولی خوندن session خطا داد: {e}"))
    else:
        await _rx(bot.send_message(chat_id, f"❌ لاگین pyrubi ناموفق: {err}"))


@bot.on_message(commands=["pyrubi_login"])
async def pyrubi_login_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    if _pyrubi_login_state.get("in_progress"):
        await _rx(message.reply("⚠️ یه لاگین pyrubi دیگه در حال انجامه. اگه گیر کرده: /pyrubi_reset"))
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _rx(message.reply("فرمت: /pyrubi_login 989123456789"))
        return
    phone = parts[1]
    code_queue = queue.Queue()
    result_queue = queue.Queue()
    _pyrubi_login_state["code_queue"] = code_queue
    _pyrubi_login_state["in_progress"] = True
    t = threading.Thread(target=_pyrubi_login_worker, args=(phone, code_queue, result_queue), daemon=True)
    t.start()
    await _rx(message.reply("📲 منتظر کد تایید pyrubi هستم. وقتی رسید بفرست:\n/pyrubi_code 12345"))
    _spawn(_watch_pyrubi_login(message.chat_id, result_queue))


@bot.on_message(commands=["pyrubi_code"])
async def pyrubi_code_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await _rx(message.reply("فرمت: /pyrubi_code 12345"))
        return
    q = _pyrubi_login_state.get("code_queue")
    if not q:
        await _rx(message.reply("❌ اول /pyrubi_login رو بزن."))
        return
    q.put(parts[1])
    await _rx(message.reply("⏳ در حال بررسی کد pyrubi..."))


@bot.on_message(commands=["pyrubi_reset"])
async def pyrubi_reset_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    _pyrubi_login_state.clear()
    removed = []
    for fname in sorted(set(glob.glob("pyrubi_acc*") + glob.glob("**/pyrubi_acc*", recursive=True))):
        if not os.path.isfile(fname):
            continue
        try:
            os.remove(fname)
            removed.append(fname)
        except Exception as e:
            logger.warning(f"⚠️ حذف {fname} ناموفق بود: {e}")
    if removed:
        await _rx(message.reply(f"✅ ریست شد. فایل‌های حذف‌شده: {', '.join(removed)}\nحالا دوباره /pyrubi_login بزن."))
    else:
        await _rx(message.reply("✅ ریست شد (فایل session‌ای پیدا نشد).\nحالا دوباره /pyrubi_login بزن."))


def _pyrubi_debug_blocking(channel_guid: str) -> str:
    """قبل از حدس زدن اسم/امضای متد، اول دنبال متدهایی توی خودِ کلاینت
    pyrubi می‌گردیم که اسمشون channel یا member داره، امضای واقعیشون رو در
    میاریم، و بعد با GUID درست کانال صداشون می‌زنیم - همون روشی که برای
    rubpy جواب داد."""
    from pyrubi import Client
    out = []
    client = Client("pyrubi_acc")

    candidates = [
        name for name in dir(client)
        if not name.startswith("_")
        and ("channel" in name.lower() or "member" in name.lower())
        and callable(getattr(client, name, None))
    ]
    out.append("متدهای پیدا‌شده: " + ", ".join(candidates))

    for name in candidates:
        fn = getattr(client, name)
        try:
            sig = str(inspect.signature(fn))
        except Exception:
            sig = "(نامشخص)"
        out.append(f"• {name} - امضا: {sig}")
        try:
            result = fn(channel_guid)
            out.append(f"• {name}(positional): ✅ {result!r}"[:600])
        except Exception as e:
            out.append(f"• {name}(positional): ❌ {type(e).__name__}: {e}")

    return "\n\n".join(out)


def _pyrubi_find_guid_blocking(username: str) -> str:
    """یوزرنیم کانال رو می‌گیره و GUID واقعیش رو با pyrubi پیدا می‌کنه."""
    from pyrubi import Client
    client = Client("pyrubi_acc")
    out = []
    try:
        result = client.check_channel_username(username)
        out.append(f"check_channel_username('{username}'):\n{result!r}")
    except Exception as e:
        out.append(f"check_channel_username خطا داد: {type(e).__name__}: {e}")

    candidates = [
        name for name in dir(client)
        if not name.startswith("_")
        and any(k in name.lower() for k in ("object", "username", "info", "chat", "search"))
        and callable(getattr(client, name, None))
    ]
    out.append("متدهای کاندید: " + ", ".join(candidates))

    for name in candidates:
        fn = getattr(client, name)
        try:
            sig = str(inspect.signature(fn))
        except Exception:
            sig = "(نامشخص)"
        try:
            result = fn(username)
            out.append(f"• {name}{sig}('{username}'): ✅\n{result!r}"[:1200])
        except Exception as e:
            out.append(f"• {name}{sig}('{username}'): ❌ {type(e).__name__}: {e}")

    return "\n\n".join(out)


@bot.on_message(commands=["test_link_button"])
async def test_link_button_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: چک می‌کنه اصلاً دکمه‌ی نوع Link کار می‌کنه یا
    نه، با یه لینک خارجی (گوگل) و یه لینک rubika.ir - تا مشخص بشه مشکل از
    خودِ مکانیزم دکمه‌ست یا مخصوص لینک‌های rubika.ir."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    builder = InlineBuilder()
    builder = builder.row(builder.button_link("test_google", "🔗 تست گوگل", "https://google.com"))
    builder = builder.row(builder.button_link("test_rubika", "🔗 تست کانال", "https://rubika.ir/instasavexx"))
    await _rx(message.reply_inline("این دو تا دکمه رو بزن، ببین کدوم باز میشه:", builder.build()))


@bot.on_message(commands=["find_guid"])
async def find_guid_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await _rx(message.reply("❌ یوزرنیم رو هم بفرست، مثلاً:\n/find_guid instasavexx"))
        return
    username = parts[1].strip().lstrip("@")
    if not (os.path.exists("pyrubi_acc.db") or glob.glob("pyrubi_acc*")):
        await _rx(message.reply("❌ اکانت pyrubi هنوز لاگین نشده. اول بزن:\n/pyrubi_login 989xxxxxxxxx"))
        return
    await _rx(message.reply(f"🔎 دارم GUID یوزرنیم '{username}' رو پیدا می‌کنم..."))
    try:
        text = await asyncio.to_thread(_pyrubi_find_guid_blocking, username)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        logger.exception("خطا در find_guid")
        await _rx(message.reply(f"❌ خطای کلی: {type(e).__name__}: {e}"))


@bot.on_message(commands=["test_pyrubi"])
async def test_pyrubi_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        await _rx(message.reply("❌ هنوز GUID کانال توی تنظیمات ست نشده."))
        return
    if not (os.path.exists("pyrubi_acc.db") or glob.glob("pyrubi_acc*")):
        await _rx(message.reply("❌ اکانت pyrubi هنوز لاگین نشده. اول بزن:\n/pyrubi_login 989xxxxxxxxx"))
        return
    await _rx(message.reply("🔎 دارم متدهای channel/member رو توی pyrubi پیدا و تست می‌کنم..."))
    try:
        text = await asyncio.to_thread(_pyrubi_debug_blocking, channel_guid)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        logger.exception("خطا در test_pyrubi")
        await _rx(message.reply(f"❌ خطای کلی: {type(e).__name__}: {e}"))


def _spawn(coro):
    """کوروتین رو به‌صورت یک asyncio Task جدا اجرا می‌کنه (نه await مستقیم) تا
    وقتی یک نفر داره دانلود می‌کنه، ربات بتونه همزمان به بقیه هم جواب بده -
    این جایگزین درست و امن همون کاری‌ه که قبلاً threading.Thread قرار بود انجام
    بده، ولی چون همه چیز توی همون event loop اصلی می‌مونه، به مشکل قبلی
    (RuntimeError/قطع SSL) برنمی‌خوریم."""
    task = asyncio.create_task(coro)

    def _log_exc(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.exception("خطای مدیریت‌نشده در تسک پس‌زمینه", exc_info=exc)

    task.add_done_callback(_log_exc)
    return task


async def _rx(value):
    """کتابخونه‌ی rubka (نسخه‌ی نصب‌شده روی Railway) بعضی متدهاش مثل
    message.reply(...) به‌جای برگردوندن مستقیم نتیجه، یه asyncio.Task
    برمی‌گردونن (که خودش باید await بشه تا پیام واقعی به‌دست بیاد) - این دقیقاً
    همون چیزی بود که توی لاگ باعث خطای «'_asyncio.Task' object has no
    attribute 'edit'» می‌شد. این تابع هر چیزی که await‌پذیر باشه (چه coroutine
    خام، چه Task، چه Future) رو پشت‌سرهم await می‌کنه تا به نتیجه‌ی نهایی برسه."""
    for _ in range(5):
        if inspect.isawaitable(value):
            value = await value
        else:
            break
    return value

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
            rubino      INTEGER DEFAULT 0,
            music       INTEGER DEFAULT 0,
            today_total INTEGER DEFAULT 0,
            today_date  TEXT
        );
        """)
        con.execute("INSERT OR IGNORE INTO stats (id, today_date) VALUES (1, ?)", (datetime.now().date().isoformat(),))
        # اگه دیتابیس قبلاً وجود داشته و ستون rubino رو نداره (آپگرید از نسخه‌ی قبلی)، اضافه‌اش کن
        try:
            con.execute("ALTER TABLE stats ADD COLUMN rubino INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        defaults = {
            "maintenance": "0",
            "limit_enabled": "0",
            "free_limit": "10",
            "caption": CAPTION,
            "welcome": "",
            "channel_guid": "",
            "channel_link": "",
            "channel_tag": "@InstaSaveXX",
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
        if kind in ("instagram", "pinterest", "rubino", "music"):
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


async def check_limit(message):
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
        await _rx(message.reply(f"⛔️ سقف دانلود رایگان امروزت ({free_limit} تا) تموم شده. فردا دوباره امتحان کن."))
        return False
    return True


# ─── جوین اجباری کانال ────────────────────────────────────────────────────────
def _pyrubi_is_member_blocking(channel_guid: str, user_guid: str) -> bool:
    """bot.check_join (rubka) با EOFError کرش می‌کنه (ظاهراً می‌خواد لاگین
    تعاملی بزنه). به‌جاش با pyrubi (که مطمئنیم کار می‌کنه) لیست اعضای کانال
    رو صفحه‌به‌صفحه می‌گیریم و چک می‌کنیم user_guid توشه یا نه.
    توجه: just_get_guids=True فرمت خروجی رو عوض می‌کنه (لیست ساده به‌جای
    دیکشنری با in_chat_members)، برای همین حذفش کردیم و خودمون از روی
    دیکشنری کامل GUID رو استخراج می‌کنیم - همون فرمتی که قبلاً تست و تایید شد."""
    from pyrubi import Client
    client = Client("pyrubi_acc")
    start_id = None
    while True:
        result = client.get_all_members(channel_guid, start_id=start_id)
        if isinstance(result, list):
            members = result
            has_continue = False
            next_start_id = None
        else:
            members = result.get("in_chat_members") or []
            has_continue = result.get("has_continue")
            next_start_id = result.get("next_start_id")
        for item in members:
            g = item.get("member_guid") if isinstance(item, dict) else item
            if g == user_guid:
                return True
        if not has_continue or not next_start_id:
            return False
        start_id = next_start_id


async def is_member(user_guid):
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        return True  # اگه ادمین کانال تنظیم نکرده، چک نمی‌کنیم
    if not (os.path.exists("pyrubi_acc.db") or glob.glob("pyrubi_acc*")):
        logger.error("❌ چک عضویت رد شد: اکانت pyrubi لاگین نیست.")
        return True  # اگه اکانت pyrubi لاگین نیست، جلوی کاربر رو نگیر
    try:
        return await asyncio.to_thread(_pyrubi_is_member_blocking, channel_guid, user_guid)
    except Exception as e:
        logger.error(f"❌ چک عضویت با pyrubi خطا داد (GUID={channel_guid}, user_guid={user_guid}): {e}", exc_info=True)
        return True  # اگه خطا داد، جلوی کاربر رو نگیر



async def send_not_joined(message):
    link = get_setting("channel_link")
    builder = InlineBuilder()
    builder = builder.row(builder.button_simple("check_join", "✅ عضو شدم"))
    keypad = builder.build()
    text = "⛔️ برای استفاده از ربات، اول باید عضو کانال ما بشی."
    if link:
        # دکمه‌ی نوع Link توی کیپد شیشه‌ای کار نمی‌کنه (تست شد، هیچ واکنشی
        # نداره)، برای همین لینک رو مستقیم توی متن می‌ذاریم - لینک‌های متنی
        # توی روبیکا خودکار قابل کلیک میشن.
        text += f"\n\n🔗 {link}"
    text += "\nبعد از عضویت روی دکمه‌ی «عضو شدم» بزن."
    await _rx(message.reply_inline(text, keypad))


async def handle_check_join_callback(message):
    # نکته‌ی مهم: chat_id (شناسه‌ی PV با کاربر، پیشوند b0) با GUID واقعی
    # کاربر (پیشوند u0، همون چیزی که توی لیست اعضای کانال میاد) کاملاً
    # فرق داره. برای چک عضویت باید از sender_id استفاده کنیم، نه chat_id.
    if await is_member(message.sender_id):
        welcome = get_setting("welcome")
        await _rx(message.reply(
            welcome or (
                "✅ عضویت شما تایید شد!\n\n"
                "🔗 لینک پست/ریل اینستاگرام، پینترست یا پست روبینو بفرست تا دانلودش کنم.\n"
                "🎵 یا اسم یک آهنگ بنویس تا برات پیدا و دانلودش کنم.\n\n"
                "/help — راهنما"
            )
        ))
    else:
        await _rx(message.reply("❌ هنوز عضو کانال نشدی! اول عضو شو، بعد دوباره روی دکمه بزن."))


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
    except Exception as import_err:
        # قبلاً اینجا فقط یه پیام کلی («shazamio نصب نیست») چاپ میشد که مشکل
        # واقعی رو مخفی می‌کرد. الان کل خطا (اسم exception + متن کامل + traceback)
        # رو توی لاگ می‌نویسیم تا اگه یکی از زیرماژول‌های shazamio (مثل
        # shazamio_core، pydub، numpy یا pydantic) گیر داشته باشه، دقیقاً معلوم بشه.
        logger.error(
            f"import شزم شکست خورد ({type(import_err).__name__}): {import_err}",
            exc_info=True,
        )
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


# ─── ارسال ویدیو با type واقعی "Video" (مستقیم به API خودِ روبیکا) ──────────
# کتابخونه‌ی rubka توی متدهای reply_* گزینه‌ی type="Video" رو باز نذاشته (فقط
# File/Image/Music و امثالش رو پوشش داده)، در حالی که خودِ سرور روبیکا طبق
# مستندات رسمی (requestSendFile -> FileTypeEnum) از نوع Video پشتیبانی می‌کنه.
# این سه تابع دقیقاً همون فلوی رسمی رو با requests پیاده می‌کنن، کاملاً مستقل
# از rubka و فقط با توکن خودِ ربات:
#   ۱) requestSendFile با type=Video  -> upload_url
#   ۲) آپلود فایل (multipart/form-data) به upload_url -> file_id
#   ۳) sendFile با file_id به chat_id -> ارسال واقعی پیام (به‌صورت پلیر ویدیو)
RUBIKA_API_BASE = f"https://botapi.rubika.ir/v3/{TOKEN}"


def build_caption(with_tag: bool = True) -> str:
    """کپشن اصلی فایل‌ها (ویدیو/آهنگ) - همیشه تگ کانال و لینک کانال (اگه ست
    شده باشن) رو هم زیرش اضافه می‌کنه، چون لینک به‌عنوان دکمه کار نمی‌کرد و
    باید به‌صورت متن باشه تا قابل کلیک بمونه."""
    cap = get_setting("caption") or CAPTION
    parts = [cap]
    if with_tag:
        tag = get_setting("channel_tag") or "@InstaSaveXX"
        if tag:
            parts.append(tag)
        link = get_setting("channel_link")
        if link:
            parts.append(f"🔗 {link}")
    return "\n\n".join(parts)


def build_channel_keypad():
    """دکمه‌ی شیشه‌ای «کانال ما» که زیر ویدیوها می‌ذاشتیم - غیرفعال شد چون
    دکمه‌ی نوع Link توی این نسخه اصلاً کار نمی‌کنه (تست شد: نه لینک گوگل نه
    لینک کانال، هیچکدوم با این نوع دکمه باز نمیشن). اگه لازم شد، لینک کانال
    رو باید به‌صورت متن توی caption اضافه کرد، نه دکمه."""
    return None


def _rubika_send_video_blocking(chat_id: str, file_path: str, caption: str = "", file_name: str = None, inline_keypad: dict = None) -> dict:
    # ۱) گرفتن آدرس آپلود مخصوص نوع Video
    r1 = requests.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": "Video"}, timeout=30)
    r1.raise_for_status()
    j1 = r1.json()
    upload_url = (j1.get("data") or j1).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile بدون upload_url برگشت: {j1}")

    # ۲) آپلود خودِ فایل ویدیو به اون آدرس
    name = file_name or os.path.basename(file_path)
    with open(file_path, "rb") as f:
        r2 = requests.post(upload_url, files={"file": (name, f, "video/mp4")}, timeout=180)
    r2.raise_for_status()
    j2 = r2.json()
    file_id = (j2.get("data") or j2).get("file_id")
    if not file_id:
        raise RuntimeError(f"آپلود فایل بدون file_id برگشت: {j2}")

    # ۳) ارسال واقعیِ فایل (با file_id) به چت - به همراه دکمه‌ی شیشه‌ای کانال (در صورت وجود)
    payload = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        payload["text"] = caption
    if inline_keypad:
        payload["inline_keypad"] = inline_keypad
    r3 = requests.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
    r3.raise_for_status()
    j3 = r3.json()
    return (j3.get("data") or j3)


async def send_video_native(message: "Message", file_path: str, caption: str = "", file_name: str = None):
    """نسخه‌ی async - توی asyncio.to_thread اجرا میشه که بلاک نکنه. اگه به هر
    دلیلی درخواست خام به API روبیکا شکست بخوره (تغییر endpoint، قطعی و ...)،
    به‌صورت fallback از reply_document خودِ rubka استفاده می‌کنه تا حداقل فایل
    (even if not typed as Video) برای کاربر ارسال بشه و کل فرآیند fail نشه."""
    keypad = build_channel_keypad()
    try:
        return await asyncio.to_thread(
            _rubika_send_video_blocking, message.chat_id, file_path, caption, file_name, keypad
        )
    except Exception as e:
        logger.warning(f"ارسال ویدیو با type=Video خام شکست خورد، fallback به reply_document: {e}")
        result = await _rx(message.reply_document(path=file_path, text=caption))
        if keypad:
            try:
                await _rx(message.reply_inline("📢 کانال ما:", keypad))
            except Exception:
                pass
        return result


async def send_music_native(message: "Message", file_path: str, caption: str = ""):
    """مثل send_video_native ولی برای آهنگ - مستقیم با HTTP خام به API روبیکا
    آپلود می‌کنه، چون متد reply_music خودِ کتابخونه‌ی rubka یه باگ داره و
    فاصله‌های توی اسم فایل رو با %20 جایگزین می‌کنه (اسم فایل توی چت خراب
    نشون داده میشه). این تابع اون باگ رو کاملاً دور می‌زنه.
    کپشن همیشه با تگ کانال (@InstaSaveXX) همراه ارسال میشه."""
    tag = get_setting("channel_tag") or "@InstaSaveXX"
    if tag and tag not in (caption or ""):
        caption = f"{caption}\n\n{tag}" if caption else tag
    try:
        return await asyncio.to_thread(
            _rubika_send_music_blocking, message.chat_id, file_path, caption
        )
    except Exception as e:
        logger.warning(f"ارسال آهنگ خام شکست خورد، fallback به reply_music: {e}")
        return await _rx(message.reply_music(path=file_path, text=caption))


# ─── دانلود اینستاگرام (پست/ریل) ─────────────────────────────────────────────
def _download_instagram_blocking(clean_url: str, video_path: str) -> bool:
    """این تابع فقط کار سنگین/بلاک‌کننده (دانلود) رو انجام میده و هیچ تماسی با
    rubka نداره؛ توی یک ترد جدا (asyncio.to_thread) اجرا میشه تا موقع دانلود
    یک نفر، ربات برای بقیه هنگ نکنه - بدون اینکه به مشکل event loop قبلی بربخوریم."""
    downloaded = False
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

    return downloaded and os.path.exists(video_path)


async def handle_instagram(message: Message, text: str):
    chat_id = message.chat_id
    clean_url = re.sub(r"\?.*$", "", text.strip())
    video_path = f"insta_{chat_id}.mp4"
    status = await _rx(message.reply("⬇️ دارم از اینستاگرام دانلود میکنم..."))

    try:
        downloaded = await asyncio.to_thread(_download_instagram_blocking, clean_url, video_path)

        if downloaded:
            await _rx(status.edit("⬆️ دارم ویدیو رو میفرستم و آهنگش رو شناسایی میکنم..."))
            # این دوتا کار به هم ربطی ندارن (یکی آپلوده، یکی تحلیل صوتیه)، برای
            # همین به‌جای سریالی (اول صبر کن آپلود تموم بشه، بعد شزم)، هم‌زمان
            # اجراشون میکنیم تا زمان کل کوتاه‌تر بشه.
            send_task = asyncio.create_task(send_video_native(message, video_path, build_caption()))
            detect_task = asyncio.create_task(asyncio.to_thread(detect_song_sync, video_path))
            await send_task
            add_download(chat_id, "instagram")
            try:
                result = await detect_task
                if result and result.get("title"):
                    title, artist = result["title"], result.get("artist", "")
                    await _rx(status.edit(f"✅ آهنگ پیدا شد!\n🎵 {title}\n🎤 {artist}\n\nدارم دانلود میکنم..."))
                    song_path = await asyncio.to_thread(download_song_file, title, artist, chat_id)
                    if song_path and os.path.exists(song_path):
                        song_path = _rename_for_send(song_path, title, artist)
                        await send_music_native(message, song_path, build_caption())
                        await _rx(status.edit("✅ دانلود شد."))
                        for f in glob.glob(f"song_{chat_id}.*") + [song_path]:
                            try:
                                os.remove(f)
                            except Exception:
                                pass
                    else:
                        await _rx(status.edit("🎵 آهنگ شناسایی شد ولی دانلودش ممکن نشد."))
                else:
                    await _rx(status.edit("✅ ویدیو ارسال شد.\n🎵 آهنگی شناسایی نشد."))
            except Exception as se:
                logger.warning(f"shazam on instagram error: {se}")
                await _rx(status.edit("✅ ویدیو ارسال شد."))
        else:
            await _rx(status.edit("❌ دانلود ممکن نشد. ممکنه پست خصوصی باشه یا لینک اشتباه باشه."))
    except Exception as e:
        logger.warning(f"instagram error: {e}")
        try:
            await _rx(status.edit(f"❌ خطا: {e}"))
        except Exception:
            pass
    finally:
        if os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception:
                pass


# ─── دانلود پینترست ───────────────────────────────────────────────────────────
def _download_pinterest_blocking(chat_id, text: str):
    """کار سنگین/بلاک‌کننده‌ی دانلود پینترست؛ توی asyncio.to_thread اجرا میشه
    و فقط مسیر فایل دانلودشده (یا None) رو برمی‌گردونه."""
    headers_scrape = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    file_path = None
    pin_url = text.strip()
    if "pin.it" in pin_url:
        try:
            redir = requests.get(pin_url, headers=headers_scrape, timeout=15, allow_redirects=True)
            pin_url = redir.url
        except Exception:
            pass

    media_url = None
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

    return file_path if file_path and os.path.exists(file_path) else None


async def handle_pinterest(message: Message, text: str):
    chat_id = message.chat_id
    status = await _rx(message.reply("⬇️ دارم از پینترست دانلود میکنم..."))
    try:
        file_path = await asyncio.to_thread(_download_pinterest_blocking, chat_id, text)
        if file_path:
            ext = file_path.rsplit(".", 1)[-1].lower()
            if ext in ("mp4", "mov", "webm"):
                await send_video_native(message, file_path, build_caption())
            else:
                await _rx(message.reply_image(path=file_path, text=build_caption()))
            add_download(chat_id, "pinterest")
            await _rx(status.edit("✅ دانلود شد."))
        else:
            await _rx(status.edit("❌ دانلود پینترست ممکن نشد، دوباره امتحان کن."))
    except Exception as e:
        logger.warning(f"pinterest error: {e}")
        try:
            await _rx(status.edit(f"❌ خطا: {e}"))
        except Exception:
            pass
    finally:
        for f in glob.glob(f"pinterest_{chat_id}.*"):
            try:
                os.remove(f)
            except Exception:
                pass


# ─── دانلود روبینو (پست/کلیپ) ────────────────────────────────────────────────
# قبلاً این تابع صفحه‌ی عمومی پست رو اسکرپ می‌کرد (og:video/mp4 توی HTML) که
# کار نمی‌کرد چون صفحه‌ی پست سمت کلاینت (JS) رندر میشه و HTML خام فقط پوسته‌ی
# سایته. حالا مستقیم از API روبینو (از طریق rubpy، با همون اکانت لاگین‌شده‌ی
# rubino_acc.rp) اطلاعات پست رو می‌گیریم که هم قابل‌اعتمادتره هم به تغییرات
# ظاهری سایت وابسته نیست.
# توی تست‌های قبلی مشخص شد که فراخوانی مستقیم rubpy/Rubino داخل پروسه‌ی
# ربات (چه sync، چه async) با یه باگ داخلیِ خودِ کتابخونه برخورد می‌کنه:
# موقع disconnect دنبال یه event loop در حال اجرا می‌گرده و پیدا نمی‌کنه
# (RuntimeError: no running event loop توی rubka/asynco.py) که باعث میشه
# اتصال با خطای NoneType قطع بشه. تنها حالتی که همیشه درست کار کرد، اجرای
# کد توی یه پروسه‌ی پایتون کاملاً تازه و مجزا بود (دقیقاً مثل تست دستی توی
# کنسول). برای همین این تابع رو توی یه subprocess جدا اجرا می‌کنیم که این
# باگ اصلاً پیش نمیاد.
_RUBINO_FETCH_WORKER_SRC = r'''
import sys, json

def _json_default(o):
    d = getattr(o, "__dict__", None)
    if d is not None:
        return d
    return str(o)

def main():
    post_url = sys.argv[1]
    try:
        from rubpy import Client, Rubino
        with Client("rubino_acc") as c:
            rubino = Rubino(c)
            result = rubino.get_post_by_share_link(post_url)
        print(json.dumps({"ok": True, "result": result}, default=_json_default))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))

if __name__ == "__main__":
    main()
'''


def _ensure_rubino_fetch_worker() -> str:
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_rubino_fetch_worker.py")
    if not os.path.exists(script_path):
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(_RUBINO_FETCH_WORKER_SRC)
    return script_path


async def _download_rubino_async(chat_id, text: str):
    post_url = text.strip()
    media_url = None
    ext_out = "mp4"

    try:
        script_path = _ensure_rubino_fetch_worker()
        proc = await asyncio.to_thread(
            subprocess.run,
            ["python3", script_path, post_url],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            logger.warning(f"rubino subprocess exited with {proc.returncode}: {proc.stderr[-2000:]}")
            return None

        out_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        payload = json.loads(out_line)
        if not payload.get("ok"):
            logger.warning(f"rubino API error (subprocess): {payload.get('error')}")
            return None

        result = payload.get("result") or {}
        data = result.get("original_update") or result

        post = (data or {}).get("post") or {}
        if not post:
            logger.warning(f"rubino: پاسخ API فیلد post نداشت: {result}")
            return None

        # پست‌های چندتایی (کروسل/آلبوم) لیست جدا دارن؛ فعلاً اولین آیتم رو
        # می‌گیریم.
        if post.get("is_multi_file") and post.get("file_list"):
            target = post["file_list"][0]
        else:
            target = post

        file_type = str(target.get("file_type") or post.get("file_type") or "").lower()
        media_url = target.get("full_file_url") or post.get("full_file_url")

        if not media_url:
            # فال‌بک: اگه full_file_url نبود (مثلاً پست فقط عکسه)، از
            # thumbnail/snapshot استفاده کن.
            media_url = (
                target.get("full_thumbnail_url")
                or post.get("full_thumbnail_url")
                or post.get("full_snapshot_url")
            )
            ext_out = "jpg"
        else:
            ext_out = "mp4" if file_type == "video" else "jpg"

        if not media_url:
            logger.warning(f"rubino: هیچ لینک مدیایی توی پاسخ API پیدا نشد: {post}")
            return None
    except Exception as e:
        logger.warning(f"rubino API error: {e}\n{traceback.format_exc()}")
        return None

    try:
        content = await asyncio.to_thread(lambda: requests.get(media_url, timeout=40).content)
        file_path = f"rubino_{chat_id}.{ext_out}"
        with open(file_path, "wb") as f:
            f.write(content)
        return file_path if os.path.exists(file_path) else None
    except Exception as e:
        logger.warning(f"rubino media download error: {e}")
        return None


async def handle_rubino(message: Message, text: str):
    chat_id = message.chat_id
    status = await _rx(message.reply("⬇️ دارم از روبینو دانلود میکنم..."))
    try:
        file_path = await _download_rubino_async(chat_id, text)
        if file_path:
            ext = file_path.rsplit(".", 1)[-1].lower()
            is_video = ext in ("mp4", "mov", "webm")

            if is_video:
                await _rx(status.edit("⬆️ دارم ویدیو رو میفرستم و آهنگش رو شناسایی میکنم..."))
                # هم‌زمان اجرا میشن: آپلود ویدیو (که کاره سنگین شبکه‌ست) و
                # تحلیل صوتی شزم (که کار پردازشیه) - این دو تا به هم ربطی
                # ندارن، برای همین سریالی اجراشون نمی‌کنیم.
                send_task = asyncio.create_task(send_video_native(message, file_path, build_caption()))
                detect_task = asyncio.create_task(asyncio.to_thread(detect_song_sync, file_path))
                await send_task
            else:
                await _rx(message.reply_image(path=file_path, text=build_caption()))
                detect_task = None
            add_download(chat_id, "rubino")

            if is_video:
                try:
                    result = await detect_task
                    if result and result.get("title"):
                        title, artist = result["title"], result.get("artist", "")
                        await _rx(status.edit(f"✅ آهنگ پیدا شد!\n🎵 {title}\n🎤 {artist}\n\nدارم دانلود میکنم..."))
                        song_path = await asyncio.to_thread(download_song_file, title, artist, chat_id)
                        if song_path and os.path.exists(song_path):
                            song_path = _rename_for_send(song_path, title, artist)
                            await send_music_native(message, song_path, build_caption())
                            await _rx(status.edit("✅ دانلود شد."))
                            for f in glob.glob(f"song_{chat_id}.*") + [song_path]:
                                try:
                                    os.remove(f)
                                except Exception:
                                    pass
                        else:
                            await _rx(status.edit("🎵 آهنگ شناسایی شد ولی دانلودش ممکن نشد."))
                    else:
                        await _rx(status.edit("✅ ویدیو ارسال شد.\n🎵 آهنگی شناسایی نشد."))
                except Exception as se:
                    logger.warning(f"shazam on rubino error: {se}")
                    await _rx(status.edit("✅ ویدیو ارسال شد."))
            else:
                await _rx(status.edit("✅ دانلود شد."))
        else:
            await _rx(status.edit("❌ دانلود از روبینو ممکن نشد. شاید پست خصوصیه یا ساختار صفحه فرق داره."))
    except Exception as e:
        logger.warning(f"rubino error: {e}")
        try:
            await _rx(status.edit(f"❌ خطا: {e}"))
        except Exception:
            pass
    finally:
        for f in glob.glob(f"rubino_{chat_id}.*"):
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
    """جستجوی آهنگ - اول Deezer (نتایج مرتب‌تر و تشخیص آرتیست دقیق‌تر)، بعد
    یوتیوب به‌عنوان fallback. علاوه بر لیست آهنگ‌ها، آی‌دی آرتیستی که بیشترین
    تطابق اسمی رو با عبارت جستجو داره هم برمی‌گردونه، تا بشه دکمه‌ی «همه
    آهنگ‌های فلان آرتیست» رو اضافه کرد."""
    results, seen_titles, top_artist_id, top_artist_name = [], set(), None, None
    try:
        r = requests.get("https://api.deezer.com/search", params={"q": query, "limit": 10}, timeout=8)
        tracks = r.json().get("data", [])
        artist_scores = {}
        for track in tracks:
            a_id = track.get("artist", {}).get("id")
            a_name = track.get("artist", {}).get("name", "")
            if a_id and a_id not in artist_scores:
                artist_scores[a_id] = {"name": a_name, "score": fuzzy_score(query, a_name)}
        if artist_scores:
            best_id = max(artist_scores, key=lambda x: artist_scores[x]["score"])
            top_artist_id, top_artist_name = best_id, artist_scores[best_id]["name"]
        for track in tracks:
            title = track.get("title", "")
            artist = track.get("artist", {}).get("name", "")
            key = f"{title} {artist}".lower().strip()
            if key not in seen_titles:
                seen_titles.add(key)
                results.append({"title": title, "artist": artist, "score": fuzzy_score(query, f"{title} {artist}")})
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query} song", download=False)
            for entry in info.get("entries", []):
                title = entry.get("title", "")
                key = title.lower().strip()
                if key not in seen_titles:
                    seen_titles.add(key)
                    results.append({"title": title, "artist": entry.get("uploader", ""),
                                     "score": fuzzy_score(query, title)})
    except Exception:
        pass
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:10], top_artist_id, top_artist_name


def get_artist_tracks(artist_id):
    """۵۰ تا از پرطرفدارترین آهنگ‌های یه آرتیست خاص، از Deezer."""
    try:
        r = requests.get(f"https://api.deezer.com/artist/{artist_id}/top", params={"limit": 50}, timeout=8)
        return [{"title": t.get("title", ""), "artist": t.get("artist", {}).get("name", "")}
                for t in r.json().get("data", [])]
    except Exception:
        return []


def download_song_file(title, artist, chat_id):
    mp3_path = f"song_{chat_id}.mp3"
    ydl_opts = {
        "format": "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "outtmpl": f"song_{chat_id}.%(ext)s",
        "quiet": True, "noplaylist": True,
        # قبلاً socket_timeout=30 بود و هیچ سقفی برای retries نداشت (پیش‌فرض
        # خودِ yt-dlp تا ۱۰ بار retry می‌کنه) - همین باعث می‌شد اگه یه منبع
        # کند/بی‌جواب بود، چند ده ثانیه قبل از رفتن سراغ منبع بعدی هدر بره.
        # بعداً برای افزایش سرعت این مقادیر خیلی تهاجمی شدن (12/1/1/1) و همین
        # باعث می‌شد سر اولین لکنت کوچیک شبکه (که روی IP سرورهای Railway موقع
        # صحبت با یوتیوب/ساندکلاود رایجه) کل تلاش fail بخوره و آهنگ اصلاً
        # دانلود نشه. این مقادیر همچنان خیلی سریع‌تر از حالت اولیه‌ن، ولی یه
        # کم تحمل بیشتر دارن تا سر هر لکنت جزئی تسلیم نشن.
        "socket_timeout": 18,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 2,
    }
    # لیست تلاش‌ها رو از ۵ به ۳ تا کم کردیم؛ یوتیوب رو اول امتحان می‌کنیم چون
    # معمولاً سریع‌تر و مطمئن‌تر از ساندکلاده (که گاهی سرچش کند/بی‌نتیجه‌ست).
    searches = [
        f"ytsearch1:{title} {artist} audio",
        f"ytsearch1:{title} {artist}",
        f"scsearch1:{title} {artist}",
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
user_artist_data = {}  # chat_id -> {"id": artist_id, "name": artist_name}


async def handle_music_search(message: Message, query: str):
    chat_id = message.chat_id
    status = await _rx(message.reply("🔎 دارم آهنگ رو جستجو میکنم..."))
    results, artist_id, artist_name = await asyncio.to_thread(search_songs, query)
    if not results:
        await _rx(status.edit("❌ آهنگی پیدا نشد، اسم دقیق‌تری بنویس."))
        return
    user_search_results[chat_id] = results
    if artist_id:
        user_artist_data[chat_id] = {"id": artist_id, "name": artist_name}
    builder = InlineBuilder()
    for i, t in enumerate(results):
        label = f"{t['title'][:28]} - {t['artist'][:15]}"
        builder = builder.row(builder.button_simple(f"dl_{i}", label))
    if artist_id and artist_name:
        builder = builder.row(builder.button_simple("all_songs", f"همه آهنگهای {artist_name}"))
    inline_keypad = builder.build()
    await _rx(status.edit("🎵 نتایج جستجو، یکی رو انتخاب کن:"))
    await _rx(message.reply_inline("🎵 نتایج جستجو:", inline_keypad))


async def handle_all_songs_callback(message: Message):
    chat_id = message.chat_id
    artist_data = user_artist_data.get(chat_id)
    if not artist_data:
        await _rx(message.reply("خطا، دوباره سرچ کن."))
        return
    status = await _rx(message.reply(f"دارم آهنگهای {artist_data['name']} رو میگیرم..."))
    tracks = await asyncio.to_thread(get_artist_tracks, artist_data["id"])
    if not tracks:
        await _rx(status.edit("آهنگی پیدا نشد"))
        return
    user_search_results[chat_id] = tracks
    builder = InlineBuilder()
    for i, t in enumerate(tracks):
        label = f"{t['title'][:28]} - {t['artist'][:15]}"
        builder = builder.row(builder.button_simple(f"dl_{i}", label))
    inline_keypad = builder.build()
    await _rx(status.edit(f"🎵 همه آهنگهای {artist_data['name']}:"))
    await _rx(message.reply_inline(f"🎵 همه آهنگهای {artist_data['name']}:", inline_keypad))


# ─── ارسال آهنگ با نام درست ────────────────────────────────────────────────
# متد reply_music توی rubka پارامتر file_name رو قبول نمی‌کنه (برخلاف چیزی که
# قبلاً فرض شده بود) - اسم فایلی که کاربر توی چت می‌بینه از روی خودِ نام فایل
# روی دیسک تعیین میشه. برای همین قبل از ارسال، فایل رو با اسم «آهنگ - خواننده»
# rename می‌کنیم و بعد بدون آرگومان اضافه می‌فرستیم.
def _safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name[:120] or "song"


def _rename_for_send(src_path: str, title: str, artist: str) -> str:
    ext = src_path.rsplit(".", 1)[-1] if "." in src_path else "mp3"
    display_name = _safe_filename(f"{title} - {artist}" if artist else title)
    new_path = os.path.join(os.path.dirname(src_path) or ".", f"{display_name}.{ext}")
    try:
        if new_path != src_path:
            if os.path.exists(new_path):
                os.remove(new_path)
            os.rename(src_path, new_path)
        return new_path
    except Exception as e:
        logger.warning(f"rename فایل آهنگ شکست خورد، از اسم قبلی استفاده میشه: {e}")
        return src_path


async def handle_download_callback(message: Message, index: int):
    chat_id = message.chat_id
    results = user_search_results.get(chat_id, [])
    if not results or index >= len(results):
        await _rx(message.reply("خطا، دوباره اسم آهنگ رو بفرست."))
        return
    track = results[index]
    title, artist = track["title"], track["artist"]
    status = await _rx(message.reply(f"⬇️ دارم دانلود میکنم...\n{title} - {artist}"))
    path = await asyncio.to_thread(download_song_file, title, artist, chat_id)
    if not path or not os.path.exists(path):
        await _rx(status.edit("❌ آهنگ پیدا نشد."))
        return
    try:
        path = _rename_for_send(path, title, artist)
        await send_music_native(message, path, build_caption())
        add_download(chat_id, "music")
        await _rx(status.edit("✅ ارسال شد."))
    except Exception as e:
        await _rx(status.edit(f"❌ خطا در ارسال: {e}"))
    finally:
        for f in glob.glob(f"song_{chat_id}.*") + [path]:
            try:
                os.remove(f)
            except Exception:
                pass


# ─── تشخیص آهنگ از ویدیو/ویسی که خودِ کاربر می‌فرسته یا فوروارد می‌کنه ────────
# برخلاف حالت اینستاگرام/روبینو (که ما خودمون فایل رو دانلود می‌کنیم)، اینجا
# فایل از قبل روی سرورهای روبیکا هست و فقط باید با file_id، از طریق endpoint
# رسمی getFile آدرس دانلودش رو بگیریم و خودمون دانلودش کنیم.
AUDIO_VIDEO_EXTS = {
    "mp4", "mov", "webm", "mkv", "avi", "3gp", "3gpp",
    "ogg", "oga", "opus", "mp3", "wav", "m4a", "aac", "flac",
}


def _guess_media_ext(file_name: str, hint: str = "") -> str:
    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[-1].lower().strip()
        if ext and len(ext) <= 5:
            return ext
    h = (hint or "").lower()
    if "voice" in h:
        return "ogg"
    if "video" in h:
        return "mp4"
    if "music" in h or "audio" in h:
        return "mp3"
    return "mp4"


def _rubika_get_file_download_url_blocking(file_id: str) -> str:
    r = requests.post(f"{RUBIKA_API_BASE}/getFile", json={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    j = r.json()
    data = j.get("data") or j
    url = data.get("download_url")
    if not url:
        raise RuntimeError(f"getFile بدون download_url برگشت: {j}")
    return url


def _download_rubika_file_blocking(file_id: str, dest_path: str) -> bool:
    url = _rubika_get_file_download_url_blocking(file_id)
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0


def _debug_log_message_shape(message: "Message"):
    """این تابع فقط برای دیباگ موقته - ساختار واقعی آبجکت message رو (اسم
    اتریبیوت‌ها و مقادیرشون) توی لاگ چاپ می‌کنه، تا بفهمیم کتابخونه‌ی rubka
    نصب‌شده روی Railway اطلاعات فایل (ویدیو/ویس/موزیک) رو با چه اسمی
    برمی‌گردونه. بعد از اینکه اسم درست پیدا شد، این تابع و صداکردنش حذف میشه."""
    try:
        keys = None
        for getter in (lambda: vars(message), lambda: message.__dict__):
            try:
                d = getter()
                keys = list(d.keys())
                break
            except Exception:
                continue
        if keys is None:
            keys = [a for a in dir(message) if not a.startswith("_") and not callable(getattr(message, a, None))]
        logger.info(f"🔍 DEBUG اتریبیوت‌های پیام: {keys}")
        for k in keys:
            try:
                v = getattr(message, k)
            except Exception:
                continue
            if v in (None, "", [], {}) or callable(v):
                continue
            logger.info(f"    · {k} = {v!r}"[:500])
    except Exception as e:
        logger.warning(f"debug dump message error: {e}")


def _extract_forwarded_channel_guid(message: "Message"):
    """اگه ادمین یه پست از یه کانال رو برای ربات فوروارد کرده باشه، این تابع
    سعی می‌کنه GUID اون کانال رو از اطلاعات فوروارد (forwarded_from) پیام
    دربیاره - چون بسته به نسخه‌ی rubka اسمش می‌تونه فرق کنه، چند حالت رایج رو
    امتحان می‌کنیم."""
    candidate_attrs = ("forwarded_from", "forward_from", "forwardFrom", "forwarded", "forward")
    for attr in candidate_attrs:
        f = getattr(message, attr, None)
        if not f:
            continue
        if isinstance(f, dict):
            guid = (
                f.get("from_chat_id") or f.get("fromChatId") or f.get("chat_id")
                or f.get("from_sender_id") or f.get("fromSenderId")
            )
        else:
            guid = (
                getattr(f, "from_chat_id", None) or getattr(f, "fromChatId", None)
                or getattr(f, "chat_id", None) or getattr(f, "from_sender_id", None)
                or getattr(f, "fromSenderId", None)
            )
        if guid:
            return guid
    return None


def _extract_incoming_file(message: "Message"):
    """سعی می‌کنه از پیام دریافتی (ویدیو/ویس/فایل صوتی که کاربر فرستاده یا\n    فوروارد کرده) اطلاعات فایل رو دربیاره - چون بسته به نسخه‌ی rubka، این\n    اطلاعات می‌تونه اسمش فرق کنه (message.file، message.video، ...)، چند تا\n    اسم رایج رو امتحان می‌کنیم."""
    candidate_attrs = (
        "file", "media", "video", "voice", "music", "audio", "gif",
        "document", "attachment", "file_data", "file_inline",
    )
    for attr in candidate_attrs:
        f = getattr(message, attr, None)
        if not f:
            continue
        if isinstance(f, dict):
            file_id = f.get("file_id") or f.get("fileId") or f.get("id")
            file_name = f.get("file_name") or f.get("fileName") or f.get("name") or ""
        else:
            file_id = getattr(f, "file_id", None) or getattr(f, "fileId", None) or getattr(f, "id", None)
            file_name = getattr(f, "file_name", None) or getattr(f, "fileName", None) or getattr(f, "name", "") or ""
        if file_id:
            return {"file_id": file_id, "file_name": file_name or "", "kind": attr}
    # اگه هیچ‌کدوم از اسم‌های رایج جواب نداد، دنبال هر اتریبیوتی می‌گردیم که
    # خودش یه دیکشنری/شیء داشته باشه با کلید file_id توش (fallback عمومی)
    try:
        for attr in [a for a in dir(message) if not a.startswith("_")]:
            try:
                f = getattr(message, attr)
            except Exception:
                continue
            if callable(f) or f in (None, "", [], {}):
                continue
            if isinstance(f, dict) and ("file_id" in f or "fileId" in f):
                file_id = f.get("file_id") or f.get("fileId")
                file_name = f.get("file_name") or f.get("fileName") or f.get("name") or ""
                if file_id:
                    return {"file_id": file_id, "file_name": file_name or "", "kind": attr}
            elif hasattr(f, "file_id") and getattr(f, "file_id", None):
                file_name = getattr(f, "file_name", None) or getattr(f, "name", "") or ""
                return {"file_id": f.file_id, "file_name": file_name or "", "kind": attr}
    except Exception:
        pass
    return None


def _looks_like_own_caption(text: str) -> bool:
    """اگه متن پیام دقیقاً همون کپشن/تگی باشه که خودِ ربات روی فایل‌هایی که
    می‌فرسته می‌ذاره (یعنی احتمالاً کاربر یکی از فایل‌های خودِ رباتو فوروارد
    کرده ولی ما نتونستیم فایلش رو تشخیص بدیم)، این تابع True برمی‌گردونه."""
    if not text:
        return False
    t = text.strip()
    own_cap = (get_setting("caption") or CAPTION).strip()
    tag = (get_setting("channel_tag") or "@InstaSaveXX").strip()
    if t == own_cap or t == tag:
        return True
    if own_cap and own_cap in t and len(t) <= len(own_cap) + 60:
        return True
    return False


async def handle_media_song_detect(message: "Message", file_meta: dict):
    chat_id = message.chat_id
    ext = _guess_media_ext(file_meta.get("file_name"), file_meta.get("kind") or getattr(message, "type", ""))
    media_path = f"media_{chat_id}.{ext}"
    status = await _rx(message.reply("🎧 دارم آهنگ رو از فایلت شناسایی میکنم..."))
    try:
        ok = await asyncio.to_thread(_download_rubika_file_blocking, file_meta["file_id"], media_path)
        if not ok:
            await _rx(status.edit("❌ نتونستم فایل رو دانلود کنم."))
            return
        result = await asyncio.to_thread(detect_song_sync, media_path)
        if result and result.get("title"):
            title, artist = result["title"], result.get("artist", "")
            await _rx(status.edit(f"✅ آهنگ پیدا شد!\n🎵 {title}\n🎤 {artist}\n\nدارم دانلود میکنم..."))
            song_path = await asyncio.to_thread(download_song_file, title, artist, chat_id)
            if song_path and os.path.exists(song_path):
                song_path = _rename_for_send(song_path, title, artist)
                await send_music_native(message, song_path, build_caption())
                add_download(chat_id, "music")
                await _rx(status.edit("✅ دانلود شد."))
                for f in glob.glob(f"song_{chat_id}.*") + [song_path]:
                    try:
                        os.remove(f)
                    except Exception:
                        pass
            else:
                await _rx(status.edit("🎵 آهنگ شناسایی شد ولی دانلودش ممکن نشد."))
        else:
            await _rx(status.edit("❌ آهنگی توی این فایل شناسایی نشد."))
    except Exception as e:
        logger.warning(f"media song detect error: {e}")
        try:
            await _rx(status.edit(f"❌ خطا: {e}"))
        except Exception:
            pass
    finally:
        if os.path.exists(media_path):
            try:
                os.remove(media_path)
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
async def admin_panel(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    await _rx(message.reply_inline(admin_main_text(), admin_main_keypad()))


async def handle_admin_callback(message: Message, btn_id: str):
    chat_id = message.chat_id

    if btn_id == "adm_back":
        await _rx(message.reply_inline(admin_main_text(), admin_main_keypad()))

    elif btn_id == "adm_maintenance":
        new_val = "0" if get_setting("maintenance") == "1" else "1"
        set_setting("maintenance", new_val)
        await _rx(message.reply_inline(admin_main_text(), admin_main_keypad()))

    elif btn_id == "adm_limit_toggle":
        new_val = "0" if get_setting("limit_enabled") == "1" else "1"
        set_setting("limit_enabled", new_val)
        await _rx(message.reply_inline(admin_main_text(), admin_main_keypad()))

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
            f"🎬 روبینو: {s.get('rubino', 0)}\n"
            f"🎵 موزیک: {s.get('music', 0)}"
        )
        await _rx(message.reply_inline(text, back_keypad()))

    elif btn_id == "adm_users":
        total = len(get_all_users())
        await _rx(message.reply_inline(f"👥 مدیریت کاربران\n\nکل: {total} کاربر\n\nبرای بن/آنبن یا دیدن اطلاعات یک کاربر:", user_actions_keypad()))

    elif btn_id == "adm_vip":
        vips = get_vip_users()
        await _rx(message.reply_inline(f"💎 مدیریت VIP\n\nتعداد فعال: {len(vips)}", vip_actions_keypad()))

    elif btn_id == "adm_vip_list":
        vips = get_vip_users()
        if not vips:
            await _rx(message.reply_inline("هیچ کاربر VIP فعالی وجود ندارد.", back_keypad()))
            return
        lines = [f"👤 {v.get('user_id')}\n📅 تا: {(v.get('vip_until') or '')[:10] or 'نامحدود'}" for v in vips]
        await _rx(message.reply_inline("💎 کاربران VIP:\n\n" + "\n\n".join(lines), back_keypad()))

    elif btn_id == "adm_banned":
        banned = get_banned_users()
        if not banned:
            await _rx(message.reply_inline("هیچ کاربر بن‌شده‌ای وجود ندارد.", back_keypad()))
            return
        lines = [f"🚫 {b.get('user_id')}" for b in banned]
        await _rx(message.reply_inline("🚫 کاربران بن‌شده:\n\n" + "\n".join(lines), back_keypad()))

    elif btn_id == "adm_settings":
        text = (
            f"⚙️ تنظیمات\n\n"
            f"📣 GUID کانال: {get_setting('channel_guid') or '-'}\n"
            f"🔗 لینک کانال: {get_setting('channel_link') or '-'}\n"
            f"🏷 تگ کانال: {get_setting('channel_tag') or '-'}\n"
            f"🔢 سقف رایگان روزانه: {get_setting('free_limit')}\n"
        )
        await _rx(message.reply_inline(text, settings_keypad()))

    elif btn_id in (
        "adm_give_vip", "adm_remove_vip", "adm_ban_user", "adm_unban_user", "adm_search_user",
        "adm_set_channel_guid", "adm_set_channel_link", "adm_set_channel_tag", "adm_set_limit", "adm_set_welcome",
        "adm_set_caption",
    ):
        prompts = {
            "adm_give_vip": "💎 chat_id کاربر رو بفرست (تعداد روز هم بعدش، مثلاً: 12345 30):",
            "adm_remove_vip": "💎 chat_id کاربری که میخوای VIP رو ازش بگیری بفرست:",
            "adm_ban_user": "🚫 chat_id کاربری که میخوای بن کنی بفرست:",
            "adm_unban_user": "✅ chat_id کاربری که میخوای آنبن کنی بفرست:",
            "adm_search_user": "🔍 chat_id کاربر رو بفرست:",
            "adm_set_channel_guid": "📣 یه پست از کانال رو برام فوروارد کن (خودم GUID رو پیدا میکنم)، یا اگه GUID رو داری مستقیم بفرست (مثلاً c0xABCDEF...):",
            "adm_set_channel_link": "🔗 لینک عضویت کانال رو بفرست (مثلاً https://rubika.ir/mychannel):",
            "adm_set_channel_tag": "🏷 تگ کانال رو بفرست (مثلاً @InstaSaveXX) - زیر آهنگ‌ها گذاشته میشه:",
            "adm_set_limit": "🔢 سقف دانلود رایگان روزانه رو بفرست (عدد):",
            "adm_set_welcome": "👋 متن پیام خوش‌آمدگویی جدید رو بفرست:",
            "adm_set_caption": "✏️ متن کپشن فایل‌ها رو بفرست:",
        }
        admin_pending[chat_id] = btn_id
        await _rx(message.reply_inline(prompts[btn_id], back_keypad()))

    elif btn_id == "adm_broadcast":
        admin_pending[chat_id] = "adm_broadcast"
        await _rx(message.reply_inline("📢 متن پیامی که میخوای به همه کاربران ارسال بشه رو بفرست:", back_keypad()))


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
    b = b.row(b.button_simple("adm_set_channel_tag", "🏷 تنظیم تگ کانال"))
    b = b.row(b.button_simple("adm_set_limit", "🔢 محدودیت رایگان"))
    b = b.row(b.button_simple("adm_set_welcome", "👋 پیام خوش‌آمد"))
    b = b.row(b.button_simple("adm_set_caption", "✏️ کپشن فایل‌ها"))
    b = b.row(b.button_simple("adm_back", "🔙 برگشت"))
    return b.build()


async def handle_admin_text(message: Message, text: str):
    chat_id = message.chat_id
    action = admin_pending.pop(chat_id, None)
    if not action:
        return

    if action == "adm_give_vip":
        try:
            parts = text.split()
            uid, days = parts[0], int(parts[1]) if len(parts) > 1 else 30
            set_vip(uid, days)
            await _rx(message.reply(f"✅ کاربر {uid} برای {days} روز VIP شد."))
        except Exception:
            await _rx(message.reply("❌ فرمت اشتباه. مثال: 12345 30"))

    elif action == "adm_remove_vip":
        remove_vip(text.strip())
        await _rx(message.reply(f"✅ VIP کاربر {text.strip()} حذف شد."))

    elif action == "adm_ban_user":
        register_user(text.strip())
        ban_user(text.strip())
        await _rx(message.reply(f"🚫 کاربر {text.strip()} بن شد."))

    elif action == "adm_unban_user":
        unban_user(text.strip())
        await _rx(message.reply(f"✅ کاربر {text.strip()} آنبن شد."))

    elif action == "adm_search_user":
        u = get_user(text.strip())
        if not u:
            await _rx(message.reply("کاربر پیدا نشد."))
        else:
            vip_until = (u.get("vip_until") or "")[:10] or "-"
            await _rx(message.reply(
                f"👤 اطلاعات کاربر\n\n"
                f"🆔 chat_id: {u['user_id']}\n"
                f"📅 عضویت: {(u.get('joined_at') or '')[:10]}\n"
                f"💎 VIP: {'بله تا ' + vip_until if u.get('is_vip') else 'خیر'}\n"
                f"🚫 بن: {'بله' if u.get('is_banned') else 'خیر'}\n"
                f"📥 کل دانلود: {u.get('downloads', 0)}"
            ))

    elif action == "adm_set_channel_guid":
        fwd_guid = _extract_forwarded_channel_guid(message)
        guid = fwd_guid or text.strip()
        if not guid:
            _debug_log_message_shape(message)
            await _rx(message.reply(
                "❌ چیزی پیدا نشد. یا GUID رو دستی بفرست، یا یه پست از کانال رو فوروارد کن.\n"
                "اگه فوروارد کردی و بازم نشد، لاگ Railway رو برام بفرست تا بررسی کنم."
            ))
            return
        set_setting("channel_guid", guid)
        source = "از پیام فوروارد‌شده" if fwd_guid else "دستی"
        await _rx(message.reply(f"✅ GUID کانال تنظیم شد ({source}): {guid}"))

    elif action == "adm_set_channel_link":
        set_setting("channel_link", text.strip())
        await _rx(message.reply(f"✅ لینک کانال تنظیم شد: {text.strip()}"))

    elif action == "adm_set_channel_tag":
        set_setting("channel_tag", text.strip())
        await _rx(message.reply(f"✅ تگ کانال تنظیم شد: {text.strip()}"))

    elif action == "adm_set_limit":
        try:
            set_setting("free_limit", str(int(text.strip())))
            await _rx(message.reply(f"✅ سقف رایگان به {text.strip()} در روز تغییر کرد."))
        except Exception:
            await _rx(message.reply("❌ یک عدد وارد کن."))

    elif action == "adm_set_welcome":
        set_setting("welcome", text)
        await _rx(message.reply("✅ پیام خوش‌آمدگویی آپدیت شد."))

    elif action == "adm_set_caption":
        set_setting("caption", text)
        await _rx(message.reply("✅ کپشن فایل‌ها آپدیت شد."))

    elif action == "adm_broadcast":
        targets = get_all_users()
        status = await _rx(message.reply(f"📢 در حال ارسال به {len(targets)} کاربر..."))
        success, fail = 0, 0
        for uid in targets:
            try:
                await _rx(bot.send_message(uid, text))
                success += 1
            except Exception:
                fail += 1
        await _rx(status.edit(f"✅ ارسال شد: {success}\n❌ ناموفق: {fail}"))


# ─── هندلرهای اصلی ────────────────────────────────────────────────────────────
@bot.on_message(commands=["start"])
async def start(bot: Robot, message: Message):
    logger.info(f"📩 /start از {message.sender_id} (chat_id={message.chat_id})")
    try:
        register_user(message.chat_id)
        if is_banned(message.chat_id):
            await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
            return
        if not await is_member(message.sender_id):
            await send_not_joined(message)
            return
        welcome = get_setting("welcome")
        await _rx(message.reply(
            welcome or (
                "سلام! 👋\n\n"
                "🔗 لینک پست/ریل اینستاگرام، پینترست یا پست روبینو بفرست تا دانلودش کنم.\n"
                "🎵 یا اسم یک آهنگ بنویس تا برات پیدا و دانلودش کنم.\n\n"
                "/help — راهنما"
            )
        ))
    except Exception:
        logger.exception("خطا در هندلر /start")


@bot.on_message(commands=["help"])
async def help_cmd(bot: Robot, message: Message):
    logger.info(f"📩 /help از {message.sender_id}")
    try:
        await _rx(message.reply(
            "📖 راهنما:\n\n"
            "• لینک اینستاگرام (پست/ریل) بفرست\n"
            "• لینک پینترست بفرست\n"
            "• لینک پست روبینو (rubika.ir/post/...) بفرست\n"
            "• اسم آهنگ یا خواننده بنویس"
        ))
    except Exception:
        logger.exception("خطا در هندلر /help")


@bot.on_message(commands=["list_members"])
async def list_members_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: کل لیست GUID اعضای کانال رو (بدون کات شدن)
    برمی‌گردونه تا مستقیم با چشم چک کنیم یه GUID خاص توشه یا نه."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        await _rx(message.reply("❌ هنوز GUID کانال توی تنظیمات ست نشده."))
        return

    def _blocking():
        from pyrubi import Client
        client = Client("pyrubi_acc")
        all_guids = []
        start_id = None
        pages = 0
        while True:
            pages += 1
            result = client.get_all_members(channel_guid, start_id=start_id)
            if isinstance(result, list):
                members = result
                has_continue = False
                next_start_id = None
            else:
                members = result.get("in_chat_members") or []
                has_continue = result.get("has_continue")
                next_start_id = result.get("next_start_id")
            for item in members:
                g = item.get("member_guid") if isinstance(item, dict) else item
                name = item.get("first_name") if isinstance(item, dict) else ""
                all_guids.append(f"{g}  ({name})")
            if not has_continue or not next_start_id or pages > 20:
                break
            start_id = next_start_id
        return all_guids

    try:
        guids = await asyncio.to_thread(_blocking)
        text = f"👥 تعداد کل: {len(guids)}\n\n" + "\n".join(guids)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        await _rx(message.reply(f"❌ خطا: {type(e).__name__}: {e}"))
        logger.error("❌ list_members خطا داد", exc_info=True)


@bot.on_message(commands=["test_member"])
async def test_member_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: مستقیم منطق جدید is_member (pyrubi-based) رو
    تست می‌کنه و هر خطایی رو کامل نشون میده، به‌جای اینکه بی‌صدا True برگردونه."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        await _rx(message.reply("❌ هنوز GUID کانال توی تنظیمات ست نشده."))
        return
    has_session = os.path.exists("pyrubi_acc.db") or bool(glob.glob("pyrubi_acc*"))
    await _rx(message.reply(
        f"🔎 channel_guid={channel_guid}\n"
        f"🔎 chat_id (شناسه‌ی PV)={message.chat_id}\n"
        f"🔎 sender_id (GUID واقعی کاربر - همینو چک می‌کنیم)={message.sender_id}\n"
        f"🔎 فایل سشن pyrubi روی دیسک هست؟ {has_session}\n"
        f"🔎 متغیر PYRUBI_SESSION_B64 ست شده؟ {bool(os.environ.get('PYRUBI_SESSION_B64'))}"
    ))
    if not has_session:
        await _rx(message.reply("❌ فایل pyrubi_acc.db روی دیسک نیست - یعنی _restore_pyrubi_session کار نکرده یا لاگین اصلاً انجام نشده."))
        return
    try:
        result = await asyncio.to_thread(_pyrubi_is_member_blocking, channel_guid, message.sender_id)
        await _rx(message.reply(f"✅ نتیجه‌ی is_member (روی sender_id): {result}"))
    except Exception as e:
        await _rx(message.reply(f"❌ is_member خطا داد:\n{type(e).__name__}: {e}"))
        logger.error("❌ test_member خطا داد", exc_info=True)


@bot.on_message(commands=["test_join"])
async def test_join_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: مستقیم توی چت نشون میده که check_join داره
    خطا میده یا نه، بدون نیاز به سرک کشیدن توی لاگ‌های Railway."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        await _rx(message.reply("❌ هنوز GUID کانال توی تنظیمات ست نشده."))
        return
    await _rx(message.reply(f"🔎 دارم check_join رو با GUID={channel_guid} تست میکنم..."))
    try:
        result = await _rx(bot.check_join(channel_guid, message.chat_id))
        await _rx(message.reply(f"✅ check_join بدون خطا اجرا شد.\nنتیجه‌ی خام: {result!r}"))
    except AttributeError as e:
        await _rx(message.reply(
            f"❌ متد check_join روی این نسخه از rubka اصلاً وجود نداره:\n{e}\n\n"
            "یعنی باید یه راه دیگه برای چک عضویت پیاده کنیم."
        ))
    except Exception as e:
        await _rx(message.reply(f"❌ check_join خطا داد:\n{type(e).__name__}: {e}"))


def _rubpy_check_join_blocking(channel_guid: str, user_guid: str) -> list:
    """دستور check_join خودِ rubka کار نمی‌کنه چون ظاهراً یه سشن کاربری واقعی
    لازم داره (و روی Railway بدون ترمینال، سر گرفتن شماره/کد با EOFError
    می‌ترکه). این تابع به‌جاش از همون اکانت روبینوی لاگین‌شده (rubino_acc که
    با /rubino_login ساختیم) استفاده می‌کنه - چون سشنش از قبل روی دیسک هست،
    نیازی به input() نداره.
    اولین دور تست نشون داد get_channel_all_members و get_channel_info واقعاً
    وجود دارن و اجرا میشن ولی با آرگومان پوزیشنال INVALID_INPUT میدن - یعنی
    احتمالاً اسم پارامتر یا شکل فراخوانی درست نیست، نه اینکه اکانت عضو کانال
    نباشه. برای همین این نسخه اول امضای واقعی متد رو از خودِ کد استخراج
    می‌کنه (inspect.signature) و بعد چند شکلِ مختلفِ فراخوانی (کیورد آرگومان‌
    های رایج) رو امتحان می‌کنه، تا از روی نتیجه‌ی واقعی مشخص بشه کدوم درسته."""
    from rubpy import Client
    attempts = []
    with Client("rubino_acc") as client:
        candidates = ["get_channel_all_members", "get_channel_info"]
        kwarg_names = ["object_guid", "channel_guid", "guid"]
        for name in candidates:
            fn = getattr(client, name, None)
            if fn is None or not callable(fn):
                attempts.append((name, "⚪️ متد وجود نداره"))
                continue
            try:
                sig = str(inspect.signature(fn))
            except Exception:
                sig = "(نامشخص)"
            attempts.append((f"{name} - امضا", sig))
            # پوزیشنال (همون تست دور اول، برای مقایسه)
            try:
                result = fn(channel_guid)
                attempts.append((f"{name}(positional)", f"✅ {result!r}"[:600]))
            except Exception as e:
                attempts.append((f"{name}(positional)", f"❌ {type(e).__name__}: {e}"))
            # چند شکل کیورد رایج
            for kw in kwarg_names:
                try:
                    result = fn(**{kw: channel_guid})
                    attempts.append((f"{name}({kw}=...)", f"✅ {result!r}"[:600]))
                except TypeError as e:
                    attempts.append((f"{name}({kw}=...)", f"⚪️ پارامتر نامعتبره: {e}"))
                except Exception as e:
                    attempts.append((f"{name}({kw}=...)", f"❌ {type(e).__name__}: {e}"))
    return attempts


@bot.on_message(commands=["test_join2"])
async def test_join2_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: امضای واقعی get_channel_all_members/
    get_channel_info رو نشون میده و چند شکل فراخوانی رو امتحان می‌کنه."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        await _rx(message.reply("❌ هنوز GUID کانال توی تنظیمات ست نشده."))
        return
    if not (os.path.exists("rubino_acc.rp") or glob.glob("rubino_acc*")):
        await _rx(message.reply("❌ اکانت روبینو هنوز لاگین نشده. اول بزن:\n/rubino_login 989xxxxxxxxx"))
        return
    await _rx(message.reply(
        f"📎 مقدار دقیق ذخیره‌شده:\nrepr = {channel_guid!r}\nlen = {len(channel_guid)}"
    ))
    await _rx(message.reply("🔎 دارم امضای واقعی متدها رو در میارم و چند شکل فراخوانی رو امتحان میکنم..."))
    try:
        attempts = await asyncio.to_thread(_rubpy_check_join_blocking, channel_guid, message.sender_id)
        # پیام طولانیه، ممکنه لازم بشه چند تیکه بفرستیم
        chunk = ""
        for n, r in attempts:
            line = f"• {n}:\n{r}\n\n"
            if len(chunk) + len(line) > 3500:
                await _rx(message.reply(chunk))
                chunk = ""
            chunk += line
        if chunk:
            await _rx(message.reply(chunk))
    except Exception as e:
        logger.exception("خطا در test_join2")
        await _rx(message.reply(f"❌ خطای کلی: {type(e).__name__}: {e}"))


def _rubpy_deep_dump_blocking() -> str:
    """چون هم پارامتر هم فرمت GUID درسته ولی سرور همچنان INVALID_INPUT
    میده، دو تا فرضیه‌ی باقی‌مونده رو چک می‌کنیم:
    ۱) شاید سشن rubino_acc.rp در واقع مال یه اکانت دیگه‌ست (نه اون شماره‌ای
       که فکر می‌کنیم)، پس اول خودِ اکانتِ لاگین‌شده رو شناسایی می‌کنیم.
    ۲) شاید get_channel_info خودش داخلش یه باگ داره (اسم فیلد اشتباه به
       سرور می‌فرسته) - برای همین سورس واقعیش رو در میاریم."""
    from rubpy import Client
    out = []
    with Client("rubino_acc") as client:
        # فرضیه ۱: خودِ اکانت لاگین‌شده کیه؟
        for attr in ("guid", "auth", "user_guid", "me"):
            val = getattr(client, attr, None)
            out.append(f"client.{attr} = {val!r}")
        for name in ("get_me", "get_self_info", "get_my_profile"):
            fn = getattr(client, name, None)
            if callable(fn):
                try:
                    out.append(f"{name}() => {fn()!r}"[:600])
                except Exception as e:
                    out.append(f"{name}() => خطا: {type(e).__name__}: {e}")
        # فرضیه ۲: سورس واقعی get_channel_info چیه؟
        try:
            src = inspect.getsource(client.get_channel_info)
        except Exception as e:
            src = f"(نتونستم سورس رو بگیرم: {e})"
        out.append("--- سورس get_channel_info ---\n" + src)
    return "\n\n".join(out)


@bot.on_message(commands=["test_join3"])
async def test_join3_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    if not (os.path.exists("rubino_acc.rp") or glob.glob("rubino_acc*")):
        await _rx(message.reply("❌ اکانت روبینو هنوز لاگین نشده."))
        return
    await _rx(message.reply("🔎 دارم هویت سشن و سورس واقعی get_channel_info رو در میارم..."))
    try:
        text = await asyncio.to_thread(_rubpy_deep_dump_blocking)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        logger.exception("خطا در test_join3")
        await _rx(message.reply(f"❌ خطای کلی: {type(e).__name__}: {e}"))


@bot.on_message()
async def handle_text(bot: Robot, message: Message):
    text = (message.text or "").strip()
    file_meta = _extract_incoming_file(message)
    logger.info(f"📩 پیام از {message.sender_id}: text={text[:60]!r} file={bool(file_meta)}")
    if not file_meta and not (text.startswith("/") and text.strip() in ("/", "")):
        # فقط وقتی فایل تشخیص داده نشد، ساختار پیام رو لاگ می‌کنیم (برای دیباگ
        # موقتِ اسم فیلد فایل - بعداً این خط حذف میشه)
        _debug_log_message_shape(message)
    if not text and not file_meta:
        return
    if text.startswith("/"):
        return
    try:
        # ─── اگه ادمینه و منتظر جواب یه اکشن پنل ادمینه ───
        if ADMIN_CHAT_ID and message.sender_id == ADMIN_CHAT_ID and message.chat_id in admin_pending:
            await handle_admin_text(message, text)
            return

        register_user(message.chat_id)
        if is_banned(message.chat_id):
            await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
            return
        if get_setting("maintenance") == "1" and message.sender_id != ADMIN_CHAT_ID:
            await _rx(message.reply("🔧 ربات در حال تعمیر است."))
            return
        if not await is_member(message.sender_id):
            await send_not_joined(message)
            return
        if not await check_limit(message):
            return

        # ─── ویدیو/ویس/فایل صوتی که کاربر مستقیم فرستاده یا فوروارد کرده ───
        if file_meta:
            _spawn(handle_media_song_detect(message, file_meta))
            return

        # اگه فایل تشخیص داده نشد ولی متن پیام دقیقاً همون کپشن/تگ خودِ رباته،
        # یعنی احتمالاً کاربر یه فایل (که خودمون فرستاده بودیم) رو فوروارد
        # کرده و ما نتونستیم فایلش رو بگیریم - به‌جای جستجوی بی‌فایده، بهش
        # بگیم مستقیم خودِ فایل رو بفرسته.
        if _looks_like_own_caption(text):
            await _rx(message.reply(
                "🎬 اگه می‌خوای آهنگِ یه ویدیو یا ویس رو پیدا کنم، لطفاً خودِ فایل ویدیو/ویس رو برام بفرست یا فوروارد کن (نه فقط کپشنش)."
            ))
            return

        # این سه‌تا با _spawn (نه await مستقیم) اجرا میشن تا دانلود یک نفر
        # جلوی جواب دادن به بقیه‌ی کاربرا رو نگیره.
        if "instagram.com" in text:
            _spawn(handle_instagram(message, text))
        elif "pinterest.com" in text or "pin.it" in text:
            _spawn(handle_pinterest(message, text))
        elif "rubika.ir/post" in text or "rubino.ir" in text:
            _spawn(handle_rubino(message, text))
        else:
            _spawn(handle_music_search(message, text))
    except Exception:
        logger.exception("خطا در هندلر handle_text")
        try:
            await _rx(message.reply(f"❌ یه خطای غیرمنتظره پیش اومد."))
        except Exception:
            pass



@bot.on_callback()
async def on_callback(bot: Robot, message: Message):
    btn_id = message.aux_data.button_id if message.aux_data else None
    logger.info(f"📩 کال‌بک از {message.sender_id}: {btn_id}")
    if not btn_id:
        return
    try:
        if btn_id.startswith("dl_"):
            try:
                index = int(btn_id.split("_")[1])
            except Exception:
                return
            _spawn(handle_download_callback(message, index))
        elif btn_id == "all_songs":
            _spawn(handle_all_songs_callback(message))
        elif btn_id == "check_join":
            _spawn(handle_check_join_callback(message))
        elif btn_id.startswith("adm_"):
            if message.sender_id != ADMIN_CHAT_ID or not ADMIN_CHAT_ID:
                return
            await handle_admin_callback(message, btn_id)
    except Exception:
        logger.exception("خطا در هندلر on_callback")


# ═══════════════════════════════════════════════════════════════════════════
# سیستم دریافت کلیک دکمه‌های شیشه‌ای (InlineKeypad) از طریق وبهوک
# ═══════════════════════════════════════════════════════════════════════════
# طبق مستندات رسمی روبیکا، دو نوع endpoint کاملاً جدا وجود داره:
#   Endpoint/receiveUpdate         -> پیام‌های معمولی
#   Endpoint/receiveInlineMessage  -> فقط و فقط کلیک دکمه‌های شیشه‌ای (inline)
# و کلیک دکمه‌های شیشه‌ای هیچ‌وقت از طریق getUpdates (polling) قابل دریافت
# نیست - فقط با ثبت یه وبهوک واقعی برای نوع receiveInlineMessage میاد. برای
# همین یه سرور وب کوچیک (aiohttp) بالا می‌آریم که آدرس عمومی Railway بهش وصله،
# و اون آدرس رو با متد updateBotEndpoints به روبیکا معرفی می‌کنیم. پیام‌های
# معمولی همچنان از همون مسیر polling قبلیِ خودِ rubka میان و دست‌نخورده می‌مونن.
from aiohttp import web

INLINE_WEBHOOK_PATH = "/rubika/inline"


def _rubika_send_message_blocking(chat_id: str, text: str, inline_keypad: dict = None) -> dict:
    payload = {"chat_id": chat_id, "text": text}
    if inline_keypad:
        payload["inline_keypad"] = inline_keypad
    r = requests.post(f"{RUBIKA_API_BASE}/sendMessage", json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("data") or j


def _rubika_edit_message_blocking(chat_id: str, message_id: str, text: str) -> dict:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    r = requests.post(f"{RUBIKA_API_BASE}/editMessageText", json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("data") or j


def _rubika_send_music_blocking(chat_id: str, file_path: str, caption: str = "") -> dict:
    r1 = requests.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": "Music"}, timeout=30)
    r1.raise_for_status()
    upload_url = (r1.json().get("data") or r1.json()).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile(Music) بدون upload_url: {r1.json()}")

    with open(file_path, "rb") as f:
        r2 = requests.post(upload_url, files={"file": (os.path.basename(file_path), f, "audio/mpeg")}, timeout=180)
    r2.raise_for_status()
    file_id = (r2.json().get("data") or r2.json()).get("file_id")
    if not file_id:
        raise RuntimeError(f"آپلود آهنگ بدون file_id: {r2.json()}")

    payload = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        payload["text"] = caption
    r3 = requests.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
    r3.raise_for_status()
    j3 = r3.json()
    return j3.get("data") or j3


def _rubika_send_document_blocking(chat_id: str, file_path: str, caption: str = "") -> dict:
    """فایل‌های عمومی (مثل .txt) رو به‌عنوان File (نه Music) آپلود و ارسال می‌کنه -
    برای فرستادن یه مقدار طولانی (مثل session base64) توی یه فایل واحد، به‌جای
    چند پیام تکه‌تکه."""
    r1 = requests.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": "File"}, timeout=30)
    r1.raise_for_status()
    upload_url = (r1.json().get("data") or r1.json()).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile(File) بدون upload_url: {r1.json()}")

    with open(file_path, "rb") as f:
        r2 = requests.post(upload_url, files={"file": (os.path.basename(file_path), f, "text/plain")}, timeout=180)
    r2.raise_for_status()
    file_id = (r2.json().get("data") or r2.json()).get("file_id")
    if not file_id:
        raise RuntimeError(f"آپلود فایل بدون file_id: {r2.json()}")

    payload = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        payload["text"] = caption
    r3 = requests.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
    r3.raise_for_status()
    j3 = r3.json()
    return j3.get("data") or j3


class _RawSentMessage:
    """نسخه‌ی سبک همون شیئی که reply() توی rubka برمی‌گردونه - فقط برای edit()."""
    def __init__(self, chat_id: str, message_id: str):
        self.chat_id = chat_id
        self.message_id = message_id

    async def edit(self, new_text: str):
        return await asyncio.to_thread(_rubika_edit_message_blocking, self.chat_id, self.message_id, new_text)


class RawMessage:
    """شبیه‌سازِ سبکِ کلاس Message از rubka، ولی مستقیم با HTTP خام کار می‌کنه.
    فقط همون متدهایی که توی handle_download_callback و handle_admin_callback
    استفاده میشن (reply, reply_inline, reply_music) پیاده‌سازی شدن، تا بشه
    همون تابع‌های موجود رو بدون تغییر، هم برای پیام‌های عادی (از مسیر rubka)
    و هم برای کلیک دکمه‌های شیشه‌ای (از مسیر وبهوک) استفاده کرد."""
    def __init__(self, chat_id: str, sender_id: str, message_id: str, button_id: str):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.message_id = message_id
        self.aux_data = type("AuxData", (), {"button_id": button_id})()

    async def reply(self, text: str, *args, **kwargs):
        data = await asyncio.to_thread(_rubika_send_message_blocking, self.chat_id, text)
        return _RawSentMessage(self.chat_id, data.get("message_id"))

    async def reply_inline(self, text: str, inline_keypad: dict = None, *args, **kwargs):
        data = await asyncio.to_thread(_rubika_send_message_blocking, self.chat_id, text, inline_keypad)
        return _RawSentMessage(self.chat_id, data.get("message_id"))

    async def reply_music(self, path: str, text: str = "", *args, **kwargs):
        return await asyncio.to_thread(_rubika_send_music_blocking, self.chat_id, path, text)


async def _handle_inline_click(inline: dict):
    chat_id = inline.get("chat_id")
    sender_id = inline.get("sender_id")
    message_id = inline.get("message_id")
    aux = inline.get("aux_data") or {}
    btn_id = aux.get("button_id")
    if not btn_id or not chat_id:
        return

    logger.info(f"📩 کلیک دکمه‌ی شیشه‌ای (وبهوک) از {sender_id}: {btn_id}")
    msg = RawMessage(chat_id, sender_id, message_id, btn_id)
    try:
        if btn_id.startswith("dl_"):
            try:
                index = int(btn_id.split("_")[1])
            except Exception:
                return
            await handle_download_callback(msg, index)
        elif btn_id == "all_songs":
            await handle_all_songs_callback(msg)
        elif btn_id == "check_join":
            await handle_check_join_callback(msg)
        elif btn_id.startswith("adm_"):
            if not ADMIN_CHAT_ID or sender_id != ADMIN_CHAT_ID:
                return
            await handle_admin_callback(msg, btn_id)
    except Exception:
        logger.exception("خطا در پردازش کلیک دکمه‌ی شیشه‌ای (وبهوک)")


async def _inline_webhook_handler(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)
    inline = body.get("inline_message") if isinstance(body, dict) else None
    if inline:
        try:
            await _handle_inline_click(inline)
        except Exception:
            logger.exception("خطا در پردازش وبهوک دکمه‌ی شیشه‌ای")
    return web.json_response({"ok": True})


def _register_inline_webhook_blocking(public_base_url: str):
    full_url = public_base_url.rstrip("/") + INLINE_WEBHOOK_PATH
    r = requests.post(
        f"{RUBIKA_API_BASE}/updateBotEndpoints",
        json={"url": full_url, "type": "ReceiveInlineMessage"},
        timeout=30,
    )
    r.raise_for_status()
    logger.info(f"✅ وبهوک دکمه‌های شیشه‌ای ثبت شد: {full_url} -> پاسخ: {r.json()}")


async def _run_inline_webhook_server():
    app = web.Application()
    app.router.add_post(INLINE_WEBHOOK_PATH, _inline_webhook_handler)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 سرور وبهوک دکمه‌های شیشه‌ای روی پورت {port} بالا اومد (مسیر: {INLINE_WEBHOOK_PATH}).")
    while True:
        await asyncio.sleep(3600)


def start_inline_webhook_thread():
    """سرور وبهوک رو توی یه ترد و event loop کاملاً جدا اجرا می‌کنه، مستقل از
    حلقه‌ی داخلی خودِ rubka، تا هیچ تداخلی با پردازش پیام‌های عادی نداشته باشه."""
    def _runner():
        asyncio.run(_run_inline_webhook_server())
    t = threading.Thread(target=_runner, daemon=True, name="inline-webhook")
    t.start()

    public_domain = os.environ.get("PUBLIC_URL") or os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if public_domain:
        if not public_domain.startswith("http"):
            public_domain = f"https://{public_domain}"
        try:
            _register_inline_webhook_blocking(public_domain)
        except Exception as e:
            logger.warning(f"⚠️ ثبت وبهوک دکمه‌های شیشه‌ای ناموفق بود: {e}")
    else:
        logger.warning(
            "⚠️ متغیر محیطی PUBLIC_URL یا RAILWAY_PUBLIC_DOMAIN ست نشده - "
            "وبهوک دکمه‌های شیشه‌ای ثبت نشد و دکمه‌ها کار نخواهند کرد."
        )


def _restore_rubino_session():
    """اگه متغیر محیطی RUBINO_SESSION_B64 توی Railway ست شده باشه، فایل
    session روبینو (که موقع /rubino_login ساخته و ازش base64 گرفته شده بود)
    رو دوباره روی دیسک می‌سازه، تا بعد از هر Redeploy لازم نباشه دوباره لاگین
    بشیم."""
    b64 = os.environ.get("RUBINO_SESSION_B64", "")
    if not b64:
        return
    session_path = "rubino_acc.rp"
    if os.path.exists(session_path):
        logger.info("ℹ️ فایل session روبینو از قبل روی دیسک هست، از RUBINO_SESSION_B64 بازسازی نمی‌کنیم.")
        return
    try:
        raw = base64.b64decode(b64)
        with open(session_path, "wb") as f:
            f.write(raw)
        logger.info(f"✅ فایل session روبینو از روی RUBINO_SESSION_B64 بازسازی شد: {session_path}")
    except Exception as e:
        logger.warning(f"⚠️ بازسازی فایل session روبینو از RUBINO_SESSION_B64 ناموفق بود: {e}")


def _restore_pyrubi_session():
    """اگه متغیر محیطی PYRUBI_SESSION_B64 توی Railway ست شده باشه، فایل
    session پیرابی (که موقع /pyrubi_login ساخته و ازش base64 گرفته شده بود)
    رو دوباره روی دیسک می‌سازه، تا بعد از هر Redeploy یا ری‌استارت لازم نباشه
    دوباره لاگین بشیم (چون جوین اجباری الان به همین سشن وابسته‌ست)."""
    b64 = os.environ.get("PYRUBI_SESSION_B64", "")
    if not b64:
        return
    session_path = "pyrubi_acc.db"
    if os.path.exists(session_path):
        logger.info("ℹ️ فایل session پیرابی از قبل روی دیسک هست، از PYRUBI_SESSION_B64 بازسازی نمی‌کنیم.")
        return
    try:
        raw = base64.b64decode(b64)
        with open(session_path, "wb") as f:
            f.write(raw)
        logger.info(f"✅ فایل session پیرابی از روی PYRUBI_SESSION_B64 بازسازی شد: {session_path}")
    except Exception as e:
        logger.warning(f"⚠️ بازسازی فایل session پیرابی از PYRUBI_SESSION_B64 ناموفق بود: {e}")


if __name__ == "__main__":
    init_db()
    logger.info("Rubika bot starting...")
    _restore_rubino_session()
    _restore_pyrubi_session()
    start_inline_webhook_thread()
    bot.run()
