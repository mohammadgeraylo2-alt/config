# -*- coding: utf-8 -*-
"""
ربات دانلودر اینستاگرام/پینترست + جستجوی آهنگ برای روبیکا
ساخته‌شده با کتابخانه rubka
نسخه ۱: پست/ریل اینستاگرام، پین پینترست، جستجو و دانلود آهنگ
"""

import os
import re
import sys
import random
import string
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
from requests.adapters import HTTPAdapter
import yt_dlp

# ─── سشن مشترک HTTP با connection pooling ────────────────────────────────────
# قبلاً هر درخواست (requestSendFile / آپلود / sendFile و ...) با requests.post
# خام می‌رفت که یعنی هر بار یک اتصال TCP+TLS جدید باز می‌شد. با یک Session
# مشترک و سایز pool بزرگ‌تر، اتصال‌ها به همون هاست (keep-alive) دوباره استفاده
# میشن و سرعت ارسال فایل/ویدیو (به‌خصوص چون هر ارسال چند round-trip به همون
# هاست داره) بدون تغییر منطق برنامه بهتر میشه. عمداً retry خودکار اضافه نشده:
# چون sendFile/sendMessage عملیات idempotent نیستن، retry روی POST می‌تونست
# باعث ارسال پیام تکراری بشه - این تغییر فقط سرعته، هیچ رفتاری عوض نمیشه.
HTTP_SESSION = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
HTTP_SESSION.mount("https://", _http_adapter)
HTTP_SESSION.mount("http://", _http_adapter)

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
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")          # برای چت با AI (رایگان)
INSTAGRAM_COOKIES = os.environ.get("INSTAGRAM_COOKIES", "")  # کوکی اینستاگرام برای yt-dlp (اختیاری)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")   # chat_id عددی/GUID خودت توی روبیکا (برای دسترسی به پنل ادمین)
BOT_USERNAME = os.environ.get("BOT_USERNAME", "instasavexx")  # یوزرنیم ربات بدون @ (برای ساخت لینک دعوت دونفره)

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
        client = Client(PYRUBI_SESSION_NAME)
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
                glob.glob(PYRUBI_SESSION_NAME + "*") + glob.glob("**/pyrubi_acc*", recursive=True),
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
    for fname in sorted(set(glob.glob(PYRUBI_SESSION_NAME + "*") + glob.glob("**/pyrubi_acc*", recursive=True))):
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
    client = Client(PYRUBI_SESSION_NAME)

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
    client = Client(PYRUBI_SESSION_NAME)
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
    """دستور دیباگ - قدیمی، نگه‌داشته شده صرفاً برای مستندسازی: بعد از تست
    ۷ ساختار مختلف مشخص شد دکمه‌ی شیشه‌ای نوع Link توی این نسخه کار نمی‌کنه،
    برای همین این قابلیت از build_invite_message حذف شد (فقط لینک متنی مونده)."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    await _rx(message.reply(
        "این دستور غیرفعال شده. بعد از تست، مشخص شد دکمه‌ی شیشه‌ای Link کار "
        "نمی‌کنه، پس دیگه استفاده نمی‌شه - فقط لینک متنی توی پیام دعوت باقی مونده."
    ))


@bot.on_message(commands=["find_guid"])
async def find_guid_cmd(bot: Robot, message: Message):
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await _rx(message.reply("❌ یوزرنیم رو هم بفرست، مثلاً:\n/find_guid instasavexx"))
        return
    username = parts[1].strip().lstrip("@")
    if not (os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi") or glob.glob(PYRUBI_SESSION_NAME + "*")):
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
# نکته‌ی مهم: اگه یه Railway Volume به این سرویس وصل کرده باشی، Railway خودش
# متغیر محیطی RAILWAY_VOLUME_MOUNT_PATH رو ست می‌کنه (مثلاً /data). با این
# خط، bot.db روی همون Volume پایدار نوشته می‌شه و بین دیپلوی‌ها ریست نمی‌شه -
# قبلاً چون مسیر نسبی "bot.db" روی دیسک غیرپایدار Railway بود، هر دیپلوی
# جدید کل تنظیمات (channel_guid, welcome, VIPها, بن‌ها, ...) رو صفر می‌کرد.
# اگه هنوز Volume وصل نکردی، این خط دقیقاً همون رفتار قبلی رو داره (فقط
# "bot.db" توی دایرکتوری فعلی).
DB_PATH = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "bot.db")
# مسیر session پیرابی رو هم روی همون Volume پایدار می‌ذاریم (نه توی دیسک
# موقتِ کنار کد)، تا بعد از هر Redeploy نیازی به کپی‌پیست دستی base64
# نباشه - همون مشکلی که باعث خراب‌شدن فایل موقع کپی از گوشی شد.
PYRUBI_SESSION_NAME = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "pyrubi_acc")


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
            dl_date     TEXT,
            chat_today  INTEGER DEFAULT 0,
            chat_date   TEXT
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
            tiktok      INTEGER DEFAULT 0,
            music       INTEGER DEFAULT 0,
            today_total INTEGER DEFAULT 0,
            today_date  TEXT
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS joined_members (
            user_guid TEXT PRIMARY KEY,
            joined_at TEXT
        )
        """)
        con.execute("INSERT OR IGNORE INTO stats (id, today_date) VALUES (1, ?)", (datetime.now().date().isoformat(),))
        # اگه دیتابیس قبلاً وجود داشته و ستون rubino رو نداره (آپگرید از نسخه‌ی قبلی)، اضافه‌اش کن
        try:
            con.execute("ALTER TABLE stats ADD COLUMN rubino INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("ALTER TABLE stats ADD COLUMN tiktok INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        # اگه دیتابیس قبلاً وجود داشته و ستون‌های چت رو نداره (آپگرید از نسخه‌ی قبلی)، اضافه‌شون کن
        try:
            con.execute("ALTER TABLE users ADD COLUMN chat_today INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("ALTER TABLE users ADD COLUMN chat_date TEXT")
        except Exception:
            pass  # ستون از قبل وجود داره
        # ─── سیستم دعوت دونفره: ستون‌های جدید کاربران ───
        try:
            con.execute("ALTER TABLE users ADD COLUMN invited_by TEXT")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("ALTER TABLE users ADD COLUMN referral_count INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("ALTER TABLE users ADD COLUMN referral_rewarded INTEGER DEFAULT 0")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        except Exception:
            pass  # ستون از قبل وجود داره
        try:
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code ON users(referral_code)")
        except Exception:
            pass  # ایندکس از قبل وجود داره
        defaults = {
            "maintenance": "0",
            "limit_enabled": "0",
            "free_limit": "10",
            "chat_free_limit": "5",
            "caption": CAPTION,
            "welcome": "",
            "channel_guid": "",
            "channel_link": "",
            "channel_tag": "@InstaSaveXX",
            "referral_target": "2",
            "referral_reward_days": "30",
            "bot_username": BOT_USERNAME,
            "nsfw_filter_enabled": "1",
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


# ─── فیلتر محتوای نامناسب (NSFW) با مدل Falconsai (لوکال، رایگان) ───────────
# NudeNet امتحان شد ولی چون فقط دنبال باکس دور اندام می‌گرده (نه کانتکست کل
# عکس)، نمی‌تونست فرق «رکابی خونگی عادی» رو از «محتوای واقعاً حساس» تشخیص
# بده - یا خیلی سخت‌گیر می‌شد یا خیلی ساده‌گیر. به‌جاش از یه مدل طبقه‌بندی
# تصویر (Falconsai/nsfw_image_detection روی HuggingFace) استفاده می‌کنیم که
# کل تصویر رو با کانتکست می‌بینه و یه امتیاز nsfw/normal برمی‌گردونه - دقیقاً
# شبیه قضاوت یه آدم، نه فقط تشخیص اندام. کاملاً لوکاله (بدون API/اینترنت بعد
# از دانلود اولیه‌ی مدل)، رایگانه، و rate limit نداره.
#
# نکته: باید این خط‌ها به requirements.txt اضافه بشن:
#   --extra-index-url https://download.pytorch.org/whl/cpu
#   torch
#   transformers
#   pillow
_NSFW_CLASSIFIER = None
_nsfw_classifier_init_lock = threading.Lock()

# امتیاز nsfw (بین ۰ تا ۱) که از این آستانه بیشتر باشه، بلاک میشه. این مدل
# برخلاف NudeNet یه امتیاز واحد برای کل عکس میده (نه چند لیبل جدا)، برای
# همین فقط یه آستانه لازمه. بعد از دیدن چند نمونه‌ی واقعی می‌تونیم دقیق‌ترش
# کنیم.
_NSFW_THRESHOLD = 0.75


def _get_nsfw_classifier():
    """کلاسیفایر رو فقط یه‌بار می‌سازه (lazy singleton) چون لود مدل کمی طول
    می‌کشه. thread-safe هست تا دو ریکوئست هم‌زمان دوبار نسازنش."""
    global _NSFW_CLASSIFIER
    if _NSFW_CLASSIFIER is None:
        with _nsfw_classifier_init_lock:
            if _NSFW_CLASSIFIER is None:
                from transformers import pipeline
                _NSFW_CLASSIFIER = pipeline(
                    "image-classification", model="Falconsai/nsfw_image_detection"
                )
    return _NSFW_CLASSIFIER


def _get_video_duration_blocking(video_path: str) -> float:
    """به‌جای ffprobe (که روی سرور نصب نیست) از خود ffmpeg استفاده می‌کنه؛
    ffmpeg موقع باز کردن فایل، مدت‌زمانش رو توی stderr چاپ می‌کنه، مثلاً:
    'Duration: 00:00:18.42, start: ...' که با regex از توش می‌کشیمش بیرون."""
    try:
        result = subprocess.run(
            [_FFMPEG_EXE, "-i", video_path],
            capture_output=True, timeout=10, text=True,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if not m:
            return 0.0
        hours, minutes, seconds = m.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except Exception:
        return 0.0


def _extract_video_frame_blocking(video_path: str, out_path: str, at_seconds: float = 1.0) -> bool:
    """یه فریم از ویدیو (پیش‌فرض ثانیه‌ی اول) رو با ffmpeg به‌صورت jpg
    استخراج می‌کنه تا بشه همون فریم رو برای چک NSFW استفاده کرد."""
    try:
        cmd = [_FFMPEG_EXE, "-y", "-ss", str(at_seconds), "-i", video_path,
               "-frames:v", "1", "-q:v", "3", out_path]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=True)
        return os.path.exists(out_path)
    except Exception as e:
        logger.warning(f"استخراج فریم ویدیو برای چک NSFW خطا داد: {e}")
        return False


def _check_images_nsfw_blocking(image_paths: list) -> bool:
    """یک یا چند عکس (مثلاً چند فریم از یک ویدیو) رو با مدل Falconsai چک
    می‌کنه و True برمی‌گردونه اگه توی هر کدوم از تصویرها امتیاز nsfw از
    آستانه بیشتر باشه. چون کاملاً لوکاله، نه rate limit داره نه هزینه‌ای."""
    if not image_paths:
        return False
    try:
        classifier = _get_nsfw_classifier()
    except Exception as e:
        logger.warning(f"مدل NSFW لود نشد (fail-open، اجازه‌ی ارسال داده میشه): {e}")
        return False

    for image_path in image_paths:
        try:
            results = classifier(image_path)
        except Exception as e:
            logger.warning(f"چک NSFW روی {image_path} خطا داد، این فریم رد میشه: {e}")
            continue
        # results یه لیست از {"label": "nsfw"/"normal", "score": float} هست.
        nsfw_score = next((r["score"] for r in results if r.get("label") == "nsfw"), 0.0)
        logger.info(f"NSFW debug [{os.path.basename(image_path)}]: nsfw={nsfw_score:.3f}")
        if get_setting("nsfw_filter_enabled", "1") == "1" and nsfw_score >= _NSFW_THRESHOLD:
            logger.warning(f"NSFW: امتیاز {nsfw_score:.3f} برای {image_path} - بلاک شد")
            return True
    return False


def check_media_nsfw_blocking(file_path: str) -> bool:
    """ورودی می‌تونه عکس یا ویدیو باشه. اگه فیلتر NSFW از پنل ادمین خاموش
    باشه، همیشه False (یعنی مجاز) برمی‌گردونه. برای ویدیو، به‌جای یه فریم
    ثابت (که ممکنه لحظه‌ی نامناسب رو جا بندازه)، چند فریم از نقاط مختلف طول
    ویدیو استخراج می‌کنه و همه رو با مدل NSFW چک می‌کنه."""
    if get_setting("nsfw_filter_enabled", "1") != "1":
        return False
    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext in ("mp4", "mov", "webm", "mkv"):
        duration = _get_video_duration_blocking(file_path)
        if duration <= 0:
            # ffprobe نتونست مدت‌زمان رو تشخیص بده (duration=0.0 یعنی خطا،
            # نه لزوماً ویدیوی خیلی کوتاه). قبلاً این حالت با duration<=4
            # قاطی می‌شد و فقط یه فریم توی ثانیه‌ی ۰.۳ چک می‌شد که ممکنه
            # محتوای اصلی رو جا بندازه. حالا چند نقطه‌ی ثابت رو امتحان
            # می‌کنیم تا حداقل چند فریم از طول ویدیو بررسی بشه.
            logger.warning(f"NSFW: مدت‌زمان ویدیو {file_path} تشخیص داده نشد (ffprobe خطا داد)، از افست‌های ثابت استفاده میشه")
            offsets = [0.5, 3.0, 7.0]
        elif duration <= 4:
            offsets = [max(duration * 0.3, 0.3)]
        else:
            offsets = [1.0, duration * 0.5, max(duration - 1.0, 1.0)]
        logger.info(f"NSFW debug: duration={duration:.2f}s offsets={[round(o, 2) for o in offsets]}")

        frame_paths = []
        try:
            for i, ss in enumerate(offsets):
                frame_path = f"{file_path}.nsfwframe{i}.jpg"
                if _extract_video_frame_blocking(file_path, frame_path, at_seconds=ss):
                    frame_paths.append(frame_path)
                else:
                    logger.warning(f"NSFW: استخراج فریم در ثانیه {ss:.2f} از {file_path} شکست خورد")
            if not frame_paths:
                logger.warning(f"NSFW: هیچ فریمی از {file_path} قابل‌استخراج نبود - fail-open")
                return False  # هیچ فریمی قابل‌استخراج نبود - fail-open
            return _check_images_nsfw_blocking(frame_paths)
        finally:
            for frame_path in frame_paths:
                if os.path.exists(frame_path):
                    try:
                        os.remove(frame_path)
                    except Exception:
                        pass
    else:
        return _check_images_nsfw_blocking([file_path])


def mark_joined(user_guid: str) -> bool:
    """عضویت رو ثبت می‌کنه و True برمی‌گردونه اگه این اولین‌بار بود که این
    کاربر تایید عضویت شد (برای جلوگیری از شمارش تکراری توی سیستم دعوت)."""
    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO joined_members (user_guid, joined_at) VALUES (?, ?)",
            (user_guid, datetime.now().isoformat()),
        )
    return cur.rowcount > 0


def has_joined(user_guid: str) -> bool:
    with db() as con:
        row = con.execute("SELECT 1 FROM joined_members WHERE user_guid=?", (user_guid,)).fetchone()
    return row is not None


# ─── سیستم دعوت دونفره ───────────────────────────────────────────────────────
# هر کاربر یه لینک دعوت شخصی داره (بر اساس chat_id خودش). وقتی یه کاربر جدید
# از طریق اون لینک وارد ربات میشه، دعوت‌کننده‌ش توی ستون invited_by ثبت میشه.
# فقط وقتی عضویت اون کاربر جدید واقعاً تایید بشه (فوروارد پست یا رویداد جوین)
# به‌عنوان یه دعوت موفق حساب میشه و به دعوت‌کننده اطلاع داده میشه. با رسیدن
# به سقف تنظیم‌شده (پیش‌فرض ۲ نفر)، دعوت‌کننده یه پاداش VIP یک‌باره می‌گیره.

def set_invited_by(user_id: str, referrer_id: str):
    """اگه کاربر (user_id) قبلاً دعوت‌کننده ثبت‌شده نداشته باشه و دعوت‌کننده
    خودش نباشه، referrer_id رو به‌عنوان دعوت‌کننده‌اش ثبت می‌کنه."""
    if not referrer_id or not user_id or str(referrer_id) == str(user_id):
        return
    with db() as con:
        row = con.execute("SELECT invited_by FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None or row[0]:
            return
        con.execute("UPDATE users SET invited_by=? WHERE user_id=?", (referrer_id, user_id))


def get_invited_by(user_id: str):
    with db() as con:
        row = con.execute("SELECT invited_by FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row and row[0] else None


def increment_referral_count(referrer_id: str) -> int:
    """شمارنده‌ی دعوت موفق دعوت‌کننده رو یکی زیاد می‌کنه و مقدار جدید رو برمی‌گردونه."""
    with db() as con:
        con.execute(
            "UPDATE users SET referral_count = COALESCE(referral_count, 0) + 1 WHERE user_id=?",
            (referrer_id,),
        )
        row = con.execute("SELECT referral_count FROM users WHERE user_id=?", (referrer_id,)).fetchone()
    return row[0] if row else 0


def is_referral_rewarded(referrer_id: str) -> bool:
    with db() as con:
        row = con.execute("SELECT referral_rewarded FROM users WHERE user_id=?", (referrer_id,)).fetchone()
    return bool(row and row[0])


def mark_referral_rewarded(referrer_id: str):
    with db() as con:
        con.execute("UPDATE users SET referral_rewarded=1 WHERE user_id=?", (referrer_id,))


def get_referral_count(user_id: str) -> int:
    with db() as con:
        row = con.execute("SELECT referral_count FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row and row[0] else 0


_REFERRAL_CODE_CHARS = string.ascii_lowercase + string.digits


def _generate_referral_code(length: int = 6) -> str:
    return "".join(random.choice(_REFERRAL_CODE_CHARS) for _ in range(length))


def get_or_create_referral_code(chat_id) -> str:
    """کد کوتاه دعوت (مثلاً 6 کاراکتری) هر کاربر رو برمی‌گردونه. اگه قبلاً
    نداشته، یه کد جدید و یکتا می‌سازه، ذخیره‌ش می‌کنه و برمی‌گردونه - این باعث
    میشه لینک دعوت به‌جای chat_id بلند (GUID روبیکا)، خیلی کوتاه‌تر بشه."""
    with db() as con:
        row = con.execute("SELECT referral_code FROM users WHERE user_id=?", (chat_id,)).fetchone()
        if row and row[0]:
            return row[0]
        for _ in range(10):
            code = _generate_referral_code()
            try:
                con.execute("UPDATE users SET referral_code=? WHERE user_id=?", (code, chat_id))
                return code
            except sqlite3.IntegrityError:
                continue  # تصادفاً تکراری بود، دوباره امتحان کن
    return str(chat_id)  # فال‌بک خیلی نادر (اگه هیچ کد یکتایی پیدا نشد)


def resolve_referral_code(code: str):
    """کد کوتاه دعوت رو به chat_id واقعی صاحبش تبدیل می‌کنه. اگه کد پیدا نشد
    (مثلاً لینک‌های قدیمی که مستقیم chat_id توشون بود)، None برمی‌گردونه تا
    فراخوان بتونه به رفتار قدیمی فال‌بک بزنه."""
    with db() as con:
        row = con.execute("SELECT user_id FROM users WHERE referral_code=?", (code,)).fetchone()
    return row[0] if row else None


def get_referral_link(chat_id) -> str:
    """لینک اختصاصی و کوتاه دعوت کاربر (https://rubika.ir/<username>?start=<کد کوتاه>)
    رو برمی‌گردونه. چون این یه لینک ساده‌ی متنیه (نه دکمه‌ی شیشه‌ای)، روبیکا خودش
    زیرش خط می‌کشه و لمس‌پذیرش می‌کنه؛ حتی بعد از فوروارد شدن پیام هم کار می‌کنه.
    اگه یوزرنیم ربات تنظیم نشده باشه، رشته‌ی خالی برمی‌گرده."""
    bot_username = get_setting("bot_username") or BOT_USERNAME
    if not bot_username:
        return ""
    code = get_or_create_referral_code(chat_id)
    return f"https://rubika.ir/{bot_username}?start={code}"


def make_link_button(button_id: str, text: str, url: str) -> dict:
    """دکمه‌ی شیشه‌ایِ نوع Link رو دستی و مطابق فرمت رسمی API روبیکا می‌سازه.
    این دور زدنِ باگ متد button_link توی کتابخونه‌ی rubka‌ست: اون متد یه فیلد
    تخت "url" می‌سازه، ولی روبیکا انتظار یه فیلد تو‌درتو به اسم "button_link"
    با کلیدهای "type" و "link_url" داره. بدون این ساختار دقیق، دکمه نمایش داده
    می‌شه ولی تپ‌کردنش هیچ اتفاقی نمی‌افته (دقیقاً همون چیزی که /test_link_button
    نشون داد)."""
    return {
        "id": button_id,
        "type": "Link",
        "button_text": text,
        "button_link": {"type": "url", "link_url": url},
    }


def make_link_keypad(*buttons: dict) -> dict:
    """یه کی‌پد اینلاین تک‌ستونه از چند دکمه‌ی Link (هرکدوم توی ردیف خودش) می‌سازه."""
    return {"rows": [{"buttons": [b]} for b in buttons]}


def build_invite_message(chat_id):
    """متن کوتاه دعوت دوستان - هم توی پیام اتمام سقف رایگان چت هوش مصنوعی
    استفاده میشه، هم توی /invite. لینک به‌صورت متن خام میاد چون روبیکا خودش
    زیرش خط می‌کشه و لمس‌پذیرش می‌کنه.
    نکته: قبلاً یه دکمه‌ی شیشه‌ای (Link) هم کنارش بود، ولی بعد از تست‌های
    مفصل (/test_link_button) مشخص شد این نوع دکمه توی نسخه‌ی فعلی کتابخونه/API
    اصلاً کار نمی‌کنه (نه مشکلی از تنظیمات ما) - برای همین حذف شد و فقط همون
    لینک متنیِ کاملاً کاربردی باقی موند."""
    target = int(get_setting("referral_target", "2") or "2")
    reward_days = int(get_setting("referral_reward_days", "30") or "30")
    link = get_referral_link(chat_id)
    text = f"💎 دوستاتو دعوت کن، {reward_days} روز VIP رایگان بگیر 🎁"
    keypad = None
    if link:
        text += f"\n\n🔗 {link}"
    else:
        text += f"\n\nکد دعوت: `{chat_id}`\n(دوستت باید بعد از /start این کد رو بفرسته)"
    return text, keypad


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
        if kind in ("instagram", "pinterest", "rubino", "tiktok", "music"):
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


def add_chat_usage(chat_id):
    """یه پیام چت با هوش مصنوعی رو برای کاربر می‌شماره (برای سقف رایگان روزانه)."""
    today = datetime.now().date().isoformat()
    with db() as con:
        row = con.execute("SELECT chat_date, chat_today FROM users WHERE user_id=?", (chat_id,)).fetchone()
        if row:
            chat_date, chat_today = row
            chat_today = (chat_today or 0) + 1 if chat_date == today else 1
            con.execute("UPDATE users SET chat_today=?, chat_date=? WHERE user_id=?",
                         (chat_today, today, chat_id))


async def check_chat_limit(message) -> bool:
    """سقف رایگان چت با هوش مصنوعی: هر کاربر روزی ۵ پیام رایگان (قابل تنظیم با
    chat_free_limit توی settings). کاربر VIP فعال از این سقف معافه."""
    u = get_user(message.chat_id)
    if not u:
        return True
    is_vip_active = u.get("is_vip") and (not u.get("vip_until") or u["vip_until"] >= datetime.now().isoformat())
    if is_vip_active:
        return True
    chat_free_limit = int(get_setting("chat_free_limit") or "5")
    today = datetime.now().date().isoformat()
    used_today = u.get("chat_today", 0) if u.get("chat_date") == today else 0
    if used_today >= chat_free_limit:
        target = int(get_setting("referral_target", "2") or "2")
        header = (
            f"⛔️ سقف رایگان چت با هوش مصنوعی امروزت ({chat_free_limit} پیام) تموم شده. فردا دوباره امتحان کن.\n\n"
            f"👥 با دعوت {target} نفر:\n"
        )
        invite_text_part, keypad = build_invite_message(message.chat_id)
        text = header + invite_text_part
        if keypad:
            await _rx(message.reply_inline(text, keypad))
        else:
            await _rx(message.reply(text))
        return False
    return True


# ─── جوین اجباری کانال (نسخه‌ی ساده‌شده، بدون چک واقعی) ──────────────────────
# روبیکا (rubka/pyrubi) راه قابل‌اعتمادی برای چک واقعی عضویت کاربر توی کانال
# در اختیار نمی‌ذاره (چک‌های قبلی با pyrubi هم با InvalidInput خراب می‌شدن،
# چون خودِ سشن اجازه‌ی دسترسی به متدهای کانال رو نداشت - نه یه باگ پارامتری).
# به‌جاش از یه روش قابل‌اعتماد و بدون نیاز به API جدا استفاده می‌کنیم: از
# کاربر می‌خوایم یه پست از کانال رو برای ربات فوروارد کنه. توی پیام
# فوروارد‌شده، روبیکا خودش GUID کانال مبدا رو می‌فرسته (forwarded_from) که
# با _extract_forwarded_channel_guid می‌گیریمش و با channel_guid تنظیم‌شده
# مقایسه می‌کنیم. اگه یکی بود، یعنی کاربر واقعاً عضو کانال بوده (چون فقط
# اعضا به پست‌های کانال دسترسی دارن که بتونن فورواردش کنن). نتیجه توی
# جدول joined_members (تابع‌های mark_joined/has_joined که از قبل توی دیتابیس
# هست) دائمی ذخیره می‌شه.


async def is_member(user_guid, force_refresh: bool = False) -> bool:
    """چک واقعیه: از دیتابیس (جدول joined_members) می‌خونه که این کاربر
    قبلاً با فوروارد کردن یه پست از کانال، عضویتش تایید شده یا نه.
    force_refresh فقط برای سازگاری با بقیه‌ی کد نگه داشته شده و تاثیری نداره."""
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        return True  # اگه ادمین کانال تنظیم نکرده، چک نمی‌کنیم
    return has_joined(user_guid)


def verify_join_from_forward(message) -> bool:
    """اگه پیام دریافتی یه پست فوروارد‌شده از همون کانالِ تنظیم‌شده باشه،
    عضویت کاربر رو دائمی تایید می‌کنه و True برمی‌گردونه. در غیر این صورت
    False برمی‌گردونه (بدون تغییر چیزی)."""
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        return False
    fwd_guid = _extract_forwarded_channel_guid(message)
    if fwd_guid and str(fwd_guid) == str(channel_guid):
        if mark_joined(message.sender_id):
            _spawn(notify_referrer_on_join(message.sender_id))
        return True
    return False


def _notify_referrer_on_join_sync(new_user_id: str):
    """نسخه‌ی sync/blocking - قابل صدا زدن هم از ترد جدا (لیسنر رویداد pyrubi
    که event loop نداره) و هم از ترد اصلی (از طریق asyncio.to_thread). وقتی
    عضویت یه کاربر جدید برای اولین‌بار تایید میشه، اگه از طریق لینک دعوت اومده
    باشه، به دعوت‌کننده‌ش خبر میده و در صورت رسیدن به سقف دعوت (پیش‌فرض ۲ نفر)،
    یک‌بار پاداش VIP بهش میده."""
    try:
        referrer_id = get_invited_by(new_user_id)
        if not referrer_id:
            return
        new_count = increment_referral_count(referrer_id)
        target = int(get_setting("referral_target", "2") or "2")
        try:
            _rubika_send_message_blocking(
                referrer_id,
                f"🎉 یکی از دوستات با لینک دعوت تو عضو ربات شد!\n👥 دعوت‌های موفق: {new_count}/{target}",
            )
        except Exception:
            logger.exception("خطا در اطلاع‌رسانی به دعوت‌کننده")
        if new_count >= target and not is_referral_rewarded(referrer_id):
            reward_days = int(get_setting("referral_reward_days", "30") or "30")
            try:
                set_vip(referrer_id, reward_days)
            except Exception:
                logger.exception("خطا در اعطای پاداش VIP دعوت")
            mark_referral_rewarded(referrer_id)
            try:
                _rubika_send_message_blocking(
                    referrer_id,
                    f"🏆 تبریک! با دعوت {target} نفر، {reward_days} روز VIP رایگان گرفتی 💎",
                )
            except Exception:
                logger.exception("خطا در ارسال پیام تبریک پاداش دعوت")
    except Exception:
        logger.exception("خطا در پردازش سیستم دعوت دونفره")


async def notify_referrer_on_join(new_user_id: str):
    """نسخه‌ی async - از داخل هندلرهای عادی (توی event loop اصلی) صدا زده میشه."""
    await asyncio.to_thread(_notify_referrer_on_join_sync, new_user_id)


async def send_not_joined(message):
    link = get_setting("channel_link")
    text = "🔒 دسترسی قفله\n\n"
    if link:
        text += f"عضو کانال شو:\n🔗 {link}\n\n"
    text += (
        "بعدش یکی از پست‌هاش رو برام فوروارد کن،\n"
        "همه‌چی برات باز میشه ✨"
    )
    await _rx(message.reply(text))


async def handle_check_join_callback(message):
    """این دکمه دیگه به‌تنهایی عضویت رو تایید نمی‌کنه (چون فقط زدنِ دکمه
    قابل جعله). فقط دوباره راهنمایی می‌کنه که باید یه پست از کانال رو
    فوروارد کنه."""
    channel_guid = get_setting("channel_guid")
    if not channel_guid:
        welcome = get_setting("welcome")
        await _rx(message.reply(welcome or "✅ خوش اومدی!"))
        return
    if has_joined(message.sender_id):
        welcome = get_setting("welcome")
        await _rx(message.reply(
            welcome or (
                "✅ عضویت شما تایید شد!\n\n"
                "🔗 لینک پست/ریل اینستاگرام، تیک‌تاک، پینترست یا پست روبینو بفرست تا دانلودش کنم.\n"
                "🎵 یا اسم یک آهنگ بنویس تا برات پیدا و دانلودش کنم.\n\n"
                "/help — راهنما"
            )
        ))
        return
    await send_not_joined(message)


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
        r = HTTP_SESSION.get(
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


def _rubika_request_upload_url_blocking(file_type: str = "Video") -> str:
    """فقط مرحله‌ی ۱ (گرفتن upload_url) - جدا شده تا بشه همزمان با دانلود
    ویدیو صدا زده بشه، چون این درخواست هیچ وابستگی‌ای به فایل دانلودشده نداره
    و می‌تونه زودتر (موازی با دانلود) اجرا بشه تا وقتی دانلود تموم شد، دیگه
    منتظر این round-trip نمونیم."""
    r1 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": file_type}, timeout=30)
    r1.raise_for_status()
    j1 = r1.json()
    upload_url = (j1.get("data") or j1).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile بدون upload_url برگشت: {j1}")
    return upload_url


def _rubika_send_video_blocking(chat_id: str, file_path: str, caption: str = "", file_name: str = None, inline_keypad: dict = None, upload_url: str = None) -> dict:
    # ۱) گرفتن آدرس آپلود مخصوص نوع Video - اگه از قبل (موازی با دانلود) گرفته
    # شده باشه (upload_url پاس داده شده)، این round-trip اصلاً تکرار نمیشه.
    if not upload_url:
        upload_url = _rubika_request_upload_url_blocking("Video")

    # ۲) آپلود خودِ فایل ویدیو به اون آدرس
    name = file_name or os.path.basename(file_path)
    try:
        with open(file_path, "rb") as f:
            r2 = HTTP_SESSION.post(upload_url, files={"file": (name, f, "video/mp4")}, timeout=180)
        r2.raise_for_status()
    except Exception:
        # اگه upload_url از قبل گرفته‌شده منقضی شده باشه (مثلاً دانلود خیلی طول
        # کشیده)، یه بار با یه upload_url تازه دوباره امتحان می‌کنیم تا کل
        # ارسال fail نشه.
        upload_url = _rubika_request_upload_url_blocking("Video")
        with open(file_path, "rb") as f:
            r2 = HTTP_SESSION.post(upload_url, files={"file": (name, f, "video/mp4")}, timeout=180)
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
    r3 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
    r3.raise_for_status()
    j3 = r3.json()
    return (j3.get("data") or j3)


async def send_video_native(message: "Message", file_path: str, caption: str = "", file_name: str = None, upload_url_task: "asyncio.Task" = None):
    """نسخه‌ی async - توی asyncio.to_thread اجرا میشه که بلاک نکنه. اگه به هر
    دلیلی درخواست خام به API روبیکا شکست بخوره (تغییر endpoint، قطعی و ...)،
    به‌صورت fallback از reply_document خودِ rubka استفاده می‌کنه تا حداقل فایل
    (even if not typed as Video) برای کاربر ارسال بشه و کل فرآیند fail نشه.

    upload_url_task (اختیاری): تسکی که موازی با دانلود ویدیو، upload_url رو
    از قبل گرفته - اگه پاس داده بشه و موفق شده باشه، دیگه لازم نیست منتظر
    round-trip اول (requestSendFile) بمونیم."""
    keypad = build_channel_keypad()
    upload_url = None
    if upload_url_task is not None:
        try:
            upload_url = await upload_url_task
        except Exception as e:
            logger.warning(f"پیش‌گرفتن upload_url موازی با دانلود شکست خورد، به روش عادی ادامه میدیم: {e}")
            upload_url = None
    try:
        return await asyncio.to_thread(
            _rubika_send_video_blocking, message.chat_id, file_path, caption, file_name, keypad, upload_url
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
            data = HTTP_SESSION.get(video_url, timeout=45).content
            with open(video_path, "wb") as f:
                f.write(data)
            downloaded = True

    # روش ۲: RapidAPI (چند هاست fallback - قبل از yt-dlp امتحان میشه)
    if not downloaded and RAPIDAPI_KEY:
        rapidapi_hosts = [
            "instagram-downloader-scraper-reels-igtv-posts-stories.p.rapidapi.com",
            "instagram-downloader-v2-scraper-reels-igtv-posts-stories.p.rapidapi.com",
            "all-in-one-social-media-downloader.p.rapidapi.com",
        ]
        video_url = None
        for host in rapidapi_hosts:
            if video_url:
                break
            try:
                endpoint = "get-media" if "social-media" not in host else "v1/download"
                r_api = requests.get(
                    f"https://{host}/{endpoint}",
                    headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host},
                    params={"url": clean_url},
                    timeout=25,
                )
                if r_api.status_code != 200:
                    continue
                d = r_api.json()
                if isinstance(d, dict):
                    media = d.get("media", []) or d.get("medias", [])
                    if media:
                        for m in media:
                            if isinstance(m, dict) and (m.get("type", "") == "video" or m.get("video_url")):
                                video_url = m.get("video_url") or m.get("url")
                                break
                        if not video_url and media:
                            video_url = media[0].get("url") or media[0].get("video_url")
                    if not video_url:
                        video_url = d.get("url") or d.get("video_url") or d.get("download_url")
                elif isinstance(d, list) and d:
                    video_url = d[0].get("url") or d[0].get("video_url")
            except Exception as e:
                logger.warning(f"Instagram RapidAPI ({host}) error: {e}")

        if video_url:
            try:
                video_data = HTTP_SESSION.get(video_url, timeout=45).content
                with open(video_path, "wb") as f:
                    f.write(video_data)
                downloaded = True
            except Exception as e:
                logger.warning(f"Instagram RapidAPI download error: {e}")

    # روش ۳: yt-dlp (fallback نهایی)
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
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([clean_url])
            if os.path.exists(video_path):
                downloaded = True
        except Exception as e:
            logger.warning(f"yt-dlp instagram error: {e}")

    return downloaded and os.path.exists(video_path)


async def handle_instagram(message: Message, text: str):
    chat_id = message.chat_id
    clean_url = re.sub(r"\?.*$", "", text.strip())
    video_path = f"insta_{chat_id}.mp4"
    status = await _rx(message.reply("⬇️ دارم از اینستاگرام دانلود میکنم..."))

    try:
        # requestSendFile (گرفتن upload_url) به فایل دانلودشده هیچ وابستگی‌ای
        # نداره، برای همین موازی با خودِ دانلود شروعش می‌کنیم تا وقتی دانلود
        # تموم شد، دیگه معطل این round-trip اول (که طبق تست خودت کندترین جای
        # کاره) نمونیم.
        upload_url_task = asyncio.create_task(
            asyncio.to_thread(_rubika_request_upload_url_blocking, "Video")
        )
        downloaded = await asyncio.to_thread(_download_instagram_blocking, clean_url, video_path)

        if downloaded:
            await _rx(status.edit("🔎 دارم محتوا رو برای مناسب‌بودن چک میکنم..."))
            is_nsfw = await asyncio.to_thread(check_media_nsfw_blocking, video_path)
            if is_nsfw:
                upload_url_task.cancel()
                await _rx(status.edit("🚫 این پست با قوانین ارسال محتوا مطابقت نداشت، ارسال نشد."))
                return
            await _rx(status.edit("⬆️ دارم ویدیو رو میفرستم و آهنگش رو شناسایی میکنم..."))
            # این دوتا کار به هم ربطی ندارن (یکی آپلوده، یکی تحلیل صوتیه)، برای
            # همین به‌جای سریالی (اول صبر کن آپلود تموم بشه، بعد شزم)، هم‌زمان
            # اجراشون میکنیم تا زمان کل کوتاه‌تر بشه.
            send_task = asyncio.create_task(
                send_video_native(message, video_path, build_caption(), upload_url_task=upload_url_task)
            )
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
            upload_url_task.cancel()
            await _rx(status.edit("❌ دانلود ممکن نشد. ممکنه پست خصوصی باشه یا لینک اشتباه باشه."))
    except Exception as e:
        logger.warning(f"instagram error: {e}")
        try:
            if not upload_url_task.done():
                upload_url_task.cancel()
        except Exception:
            pass
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


# ─── دانلود تیک‌تاک ───────────────────────────────────────────────────────────
def _download_tiktok_blocking(text: str, video_path: str) -> bool:
    """دانلود ویدیوی تیک‌تاک (بدون واترمارک) با چند API fallback. فقط کار
    سنگین/بلاک‌کننده رو انجام میده - توی asyncio.to_thread اجرا میشه."""
    video_url = None

    # روش ۱: tikwm (بدون نیاز به کلید)
    try:
        r = requests.post("https://www.tikwm.com/api/", data={"url": text, "hd": 1}, timeout=20)
        d = r.json()
        if d.get("code") == 0:
            video_url = d.get("data", {}).get("hdplay") or d.get("data", {}).get("play")
    except Exception as e:
        logger.warning(f"tiktok tikwm error: {e}")

    # روش ۲: RapidAPI (fallback اول)
    if not video_url and RAPIDAPI_KEY:
        try:
            host = "tiktok-downloader-download-tiktok-videos-without-watermark.p.rapidapi.com"
            r2 = requests.get(
                f"https://{host}/vid/index",
                headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host},
                params={"url": text}, timeout=20,
            )
            d2 = r2.json()
            video_url = (d2.get("video") or [None])[0] if isinstance(d2.get("video"), list) else d2.get("video")
        except Exception as e:
            logger.warning(f"tiktok rapidapi1 error: {e}")

    # روش ۳: RapidAPI (fallback دوم)
    if not video_url and RAPIDAPI_KEY:
        try:
            host2 = "tiktok-video-no-watermark2.p.rapidapi.com"
            r3 = requests.get(
                f"https://{host2}/",
                headers={"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host2},
                params={"url": text}, timeout=20,
            )
            d3 = r3.json()
            video_url = d3.get("video") or d3.get("nwm_video_url") or d3.get("video_no_watermark")
        except Exception as e:
            logger.warning(f"tiktok rapidapi2 error: {e}")

    if not video_url:
        return False

    try:
        content = HTTP_SESSION.get(video_url, timeout=40).content
        with open(video_path, "wb") as f:
            f.write(content)
    except Exception as e:
        logger.warning(f"tiktok download content error: {e}")
        return False

    return os.path.exists(video_path)


async def handle_tiktok(message: Message, text: str):
    chat_id = message.chat_id
    clean_url = text.strip()
    video_path = f"tiktok_{chat_id}.mp4"
    status = await _rx(message.reply("⬇️ دارم از تیک‌تاک دانلود میکنم..."))

    try:
        upload_url_task = asyncio.create_task(
            asyncio.to_thread(_rubika_request_upload_url_blocking, "Video")
        )
        downloaded = await asyncio.to_thread(_download_tiktok_blocking, clean_url, video_path)

        if downloaded:
            await _rx(status.edit("🔎 دارم محتوا رو برای مناسب‌بودن چک میکنم..."))
            is_nsfw = await asyncio.to_thread(check_media_nsfw_blocking, video_path)
            if is_nsfw:
                upload_url_task.cancel()
                await _rx(status.edit("🚫 این پست با قوانین ارسال محتوا مطابقت نداشت، ارسال نشد."))
                return
            await _rx(status.edit("⬆️ دارم ویدیو رو میفرستم و آهنگش رو شناسایی میکنم..."))
            send_task = asyncio.create_task(
                send_video_native(message, video_path, build_caption(), upload_url_task=upload_url_task)
            )
            detect_task = asyncio.create_task(asyncio.to_thread(detect_song_sync, video_path))
            await send_task
            add_download(chat_id, "tiktok")
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
                logger.warning(f"shazam on tiktok error: {se}")
                await _rx(status.edit("✅ ویدیو ارسال شد."))
        else:
            upload_url_task.cancel()
            await _rx(status.edit("❌ دانلود از تیک‌تاک ممکن نشد. لینک رو چک کن یا بعداً امتحان کن."))
    except Exception as e:
        logger.warning(f"tiktok error: {e}")
        try:
            if not upload_url_task.done():
                upload_url_task.cancel()
        except Exception:
            pass
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
            redir = HTTP_SESSION.get(pin_url, headers=headers_scrape, timeout=15, allow_redirects=True)
            pin_url = redir.url
        except Exception:
            pass

    media_url = None
    try:
        html = HTTP_SESSION.get(pin_url, headers=headers_scrape, timeout=20).text
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
        content = HTTP_SESSION.get(media_url, headers=headers_scrape, timeout=40).content
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
            await _rx(status.edit("🔎 دارم محتوا رو برای مناسب‌بودن چک میکنم..."))
            is_nsfw = await asyncio.to_thread(check_media_nsfw_blocking, file_path)
            if is_nsfw:
                await _rx(status.edit("🚫 این پست با قوانین ارسال محتوا مطابقت نداشت، ارسال نشد."))
                return
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
# کنسول). برای همین این کد توی یه subprocess جدا اجرا میشه که این باگ اصلاً
# پیش نمیاد.
#
# قبلاً هر پست = یه پروسه‌ی پایتون تازه (استارت + importهای سنگین rubpy +
# لاگین از نو) که همین کندیِ روبینو نسبت به اینستا رو توضیح می‌داد. حالا یه
# پروسه‌ی «دائمی» پشت صحنه باز می‌مونه: فقط یه بار استارت می‌خوره و لاگین
# می‌مونه، و برای هر پست فقط یه خط JSON از طریق stdin/stdout رد و بدل میشه -
# دیگه نه استارت پایتون تکرار میشه نه لاگین. اگه این پروسه به هر دلیلی
# کرش کنه یا قطع بشه، خودکار یه بار دیگه استارتش می‌کنیم و درخواست رو
# دوباره امتحان می‌کنیم؛ اگه بازم نشد، به همون روش قدیمی (یه پروسه‌ی
# یک‌بارمصرف) fallback می‌کنیم تا این قابلیت کلاً از کار نیفته.
_LAST_RUBINO_ERROR = {}

_RUBINO_PERSISTENT_WORKER_SRC = r'''
import sys, json, traceback

def _json_default(o):
    d = getattr(o, "__dict__", None)
    if d is not None:
        return d
    return str(o)

def main():
    from rubpy import Client, Rubino
    with Client("rubino_acc") as c:
        rubino = Rubino(c)
        # سیگنال آماده بودن، تا سمت بات بدونه لاگین/کانکت تموم شده
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                post_url = req.get("post_url", "")
                result = rubino.get_post_by_share_link(post_url)
                print(json.dumps({"ok": True, "result": result}, default=_json_default), flush=True)
            except Exception as e:
                print(json.dumps({
                    "ok": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "traceback": traceback.format_exc(),
                }), flush=True)

if __name__ == "__main__":
    main()
'''

_RUBINO_ONESHOT_WORKER_SRC = r'''
import sys, json, traceback

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
        print(json.dumps({
            "ok": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(),
        }))

if __name__ == "__main__":
    main()
'''


def _ensure_rubino_worker_script(src: str, filename: str) -> str:
    # همیشه بازنویسی می‌کنیم (نه فقط وقتی وجود نداره)، وگرنه اگه یه نسخه‌ی
    # قدیمی از این فایل روی دیسک/ولوم بمونه، تغییرات کد جدید هیچوقت اعمال
    # نمیشه و ساعت‌ها دیباگ روی نسخه‌ی کهنه انجام میشه.
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(src)
    return script_path


class RubinoWorkerManager:
    """یه پروسه‌ی روبینوی «دائمی» پشت صحنه نگه می‌داره تا لاگین/کانکت فقط
    یه بار انجام بشه، نه هر پست. درخواست‌ها سریالی (با لاک) پردازش میشن چون
    فقط یه پروسه داریم؛ اگه بخوای چند درخواست هم‌زمان سریع‌تر پردازش بشه،
    باید چند پروسه (pool) اضافه کرد که فعلاً برای سادگی نذاشتم."""

    def __init__(self):
        self._proc = None
        self._lock = asyncio.Lock()

    async def _start(self):
        script_path = _ensure_rubino_worker_script(_RUBINO_PERSISTENT_WORKER_SRC, "_rubino_persistent_worker.py")
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # منتظر سیگنال آماده‌باش (یعنی لاگین/کانکت تموم شده) می‌مونیم، وگرنه
        # ممکنه اولین درخواست واقعی قاطی پیام ready بشه.
        ready_line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=45)
        if not ready_line:
            raise RuntimeError("پروسه‌ی دائمی روبینو بدون پیام آماده‌باش قطع شد (لاگین/کانکت ناموفق؟)")
        # هر stderr احتمالی رو (بدون بلاک کردن پروسه‌ی اصلی) لاگ می‌کنیم تا
        # اگه پایپش پر شد قفل نکنه و برای دیباگ هم در دسترس بمونه.
        asyncio.create_task(self._drain_stderr(self._proc))
        logger.info("پروسه‌ی دائمی روبینو استارت شد و آماده‌ست.")

    async def _drain_stderr(self, proc):
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.warning(f"rubino persistent worker stderr: {line.decode(errors='replace').strip()}")
        except Exception:
            pass

    async def _ensure_started(self):
        if self._proc is not None and self._proc.returncode is None:
            return
        await self._start()

    async def request(self, post_url: str, timeout: float = 30.0) -> dict:
        async with self._lock:
            last_err = None
            for attempt in range(2):  # یه بار retry با ری‌استارت، اگه پروسه کرش کرده باشه
                try:
                    await self._ensure_started()
                    line = json.dumps({"post_url": post_url}) + "\n"
                    self._proc.stdin.write(line.encode("utf-8"))
                    await self._proc.stdin.drain()
                    out_line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
                    if not out_line:
                        raise RuntimeError("پروسه‌ی دائمی روبینو هیچ جوابی نداد (احتمالاً کرش کرده)")
                    return json.loads(out_line.decode("utf-8").strip())
                except Exception as e:
                    last_err = e
                    logger.warning(f"rubino persistent worker error (تلاش {attempt + 1}): {e}")
                    try:
                        if self._proc and self._proc.returncode is None:
                            self._proc.kill()
                    except Exception:
                        pass
                    self._proc = None
            raise RuntimeError(f"پروسه‌ی دائمی روبینو بعد از retry هم شکست خورد: {last_err}")


_RUBINO_WORKER = None
# تعداد پروسه‌ی روبینوی هم‌زمان - هر پروسه با همون اکانت rubino_acc لاگین
# میشه؛ طبق درخواست خودت از ۲ به ۳ رفت. یادت باشه همون ریسکی که قبلاً گفتم
# (لاگین هم‌زمان چند پروسه با یه session) با هر پروسه‌ی اضافه یه‌کم بیشتر
# میشه، ولی ۳ تا هنوز عدد معقولیه.
RUBINO_WORKER_POOL_SIZE = 3


class RubinoWorkerPool:
    """چند پروسه‌ی دائمی روبینو (به‌جای فقط یکی) نگه می‌داره تا چند
    درخواست هم‌زمان واقعاً موازی پردازش بشن، نه صف‌شده. هر درخواست از یه
    صف (Queue) یه پروسه‌ی آزاد می‌گیره؛ اگه هر ۲ پروسه مشغول باشن، درخواست
    سوم منتظر می‌مونه تا یکی آزاد بشه - دقیقاً مثل قبل ولی حالا با ظرفیت ۲
    هم‌زمان به‌جای ۱."""

    def __init__(self, size: int = RUBINO_WORKER_POOL_SIZE):
        self._workers = [RubinoWorkerManager() for _ in range(size)]
        self._available: "asyncio.Queue" = None

    def _ensure_queue(self):
        if self._available is None:
            self._available = asyncio.Queue()
            for w in self._workers:
                self._available.put_nowait(w)

    async def request(self, post_url: str, timeout: float = 30.0) -> dict:
        self._ensure_queue()
        worker = await self._available.get()
        try:
            return await worker.request(post_url, timeout=timeout)
        finally:
            self._available.put_nowait(worker)


def _get_rubino_worker() -> "RubinoWorkerPool":
    global _RUBINO_WORKER
    if _RUBINO_WORKER is None:
        _RUBINO_WORKER = RubinoWorkerPool()
    return _RUBINO_WORKER


def _download_rubino_oneshot_blocking(post_url: str) -> dict:
    """روش قدیمی (یه پروسه‌ی یک‌بارمصرف) - فقط به‌عنوان fallback نگه داشته
    شده، برای وقتی که پروسه‌ی دائمی به هر دلیلی اصلاً بالا نمیاد (مثلاً
    مشکل لاگین)، تا این قابلیت کلاً از کار نیفته."""
    script_path = _ensure_rubino_worker_script(_RUBINO_ONESHOT_WORKER_SRC, "_rubino_fetch_worker.py")
    proc = subprocess.run(
        ["python3", script_path, post_url],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"rubino oneshot subprocess exited with {proc.returncode}: {proc.stderr[-2000:]}")
    out_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    return json.loads(out_line)


async def _download_rubino_async(chat_id, text: str):
    post_url = text.strip()
    media_url = None
    ext_out = "mp4"

    try:
        try:
            payload = await _get_rubino_worker().request(post_url)
        except Exception as e:
            logger.warning(f"پروسه‌ی دائمی روبینو شکست خورد، fallback به روش قدیمی (کندتر ولی مطمئن): {e}")
            payload = await asyncio.to_thread(_download_rubino_oneshot_blocking, post_url)
        if not payload.get("ok"):
            tb = payload.get("traceback") or "(بدون traceback - نسخه‌ی قدیمی worker؟)"
            logger.warning(
                f"rubino API error (subprocess): {payload.get('error_type')}: {payload.get('error')}\n{tb}"
            )
            _LAST_RUBINO_ERROR["error"] = payload.get("error")
            _LAST_RUBINO_ERROR["error_type"] = payload.get("error_type")
            _LAST_RUBINO_ERROR["traceback"] = tb
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
        content = await asyncio.to_thread(lambda: HTTP_SESSION.get(media_url, timeout=40).content)
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
# امتیاز شباهت متنی: ترجیحاً با rapidfuzz (تحمل بالا نسبت به غلط تایپی، جابجایی
# کلمات و اسم‌های ناقص - چیزی که پیاده‌سازی قبلی که فقط overlap حروف رو حساب
# می‌کرد، اصلاً پوشش نمی‌داد). اگه rapidfuzz نصب نبود (هنوز به requirements
# اضافه نشده)، یه fallback ساده با difflib جای اون رو می‌گیره تا ربات نخوابه؛
# ولی برای کیفیت واقعی حتماً باید rapidfuzz نصب بشه.
try:
    from rapidfuzz import fuzz as _fuzz

    def _text_score(query, target):
        if not query or not target:
            return 0.0
        return (_fuzz.token_set_ratio(query, target) * 0.6
                + _fuzz.partial_ratio(query, target) * 0.4)
except Exception as _e:
    import difflib
    logger.warning(f"rapidfuzz نصب نیست، از difflib fallback استفاده میشه: {_e}")

    def _text_score(query, target):
        if not query or not target:
            return 0.0
        return difflib.SequenceMatcher(None, query.lower(), target.lower()).ratio() * 100


def fuzzy_score(query, title):
    """امتیاز شباهت متنی بین عبارت جستجو و یه عنوان/آرتیست (۰ تا ~۱۰۰)."""
    return _text_score(query, title)


# محتوایی که هیچ‌وقت «آهنگ» نیست (ری‌اکشن/پرانک/مصاحبه/آموزش و ...) - این‌ها
# کاملاً حذف میشن، نه فقط جریمه امتیاز.
_JUNK_TITLE_PATTERNS = [
    r"\breaction\b", r"ری\s*اکشن", r"\bprank\b", r"پرانک",
    r"\binterview\b", r"مصاحبه", r"\btutorial\b", r"آموزش\s*آهنگ",
    r"\bteaser\b", r"\btrailer\b", r"تیزر", r"behind[\s_-]*the[\s_-]*scenes",
    r"\bpodcast\b", r"\bcommentary\b", r"\bkaraoke\b", r"کارائوکه",
    r"\bmashup\b", r"\bhow\s*to\b", r"unboxing", r"\bvlog\b", r"\breview\b",
]

# نسخه‌های جایگزین (لایو/ریمیکس/کاور/اسپید‌آپ و ...) که معمولاً «بهترین نسخه»
# محسوب نمیشن - مگر اینکه خودِ کاربر دقیقاً دنبال همون نسخه بوده باشه.
_ALT_VERSION_PATTERNS = [
    r"\blive\b", r"زنده", r"\bremix\b", r"ریمیکس", r"\bacoustic\b",
    r"\bcover\b", r"کاور", r"\binstrumental\b", r"بی\s*کلام",
    r"\bsped\s*up\b", r"\bslowed\b", r"\breverb\b", r"nightcore",
    r"\b8d\s*audio\b", r"\btype\s*beat\b", r"\bfan\s*made\b", r"\bparody\b",
]


def _title_has_any(text, patterns):
    t = (text or "").lower()
    return any(re.search(p, t) for p in patterns)


def _norm_key(title, artist=""):
    """کلید نرمال‌شده برای تشخیص نسخه‌های تکراری یه آهنگ بین منابع مختلف -
    پرانتز/براکت، «feat/ft» و علائم نگارشی رو نادیده می‌گیره."""
    t = re.sub(r"[\(\[].*?[\)\]]", " ", f"{title} {artist}".lower())
    t = re.sub(r"\bfeat\.?\b|\bft\.?\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


_YTMUSIC = None


def _get_ytmusic():
    """نمونه‌ی مشترک YTMusic - جستجوش نیاز به لاگین نداره (فقط برای قابلیت‌های
    مربوط به کتابخونه‌ی شخصی کاربر نیاز به auth هست)."""
    global _YTMUSIC
    if _YTMUSIC is None:
        from ytmusicapi import YTMusic
        _YTMUSIC = YTMusic()
    return _YTMUSIC


def search_songs(query):
    """جستجوی حرفه‌ای آهنگ از سه منبع:
      ۱) Deezer - کاتالوگ رسمی، برای امتیاز محبوبیت (rank) و تشخیص آرتیست
      ۲) YouTube Music - بانکِ «آهنگ» نه ویدیوی خام، پوشش خیلی بهتر برای
         آهنگ‌های فارسی/کمترشناخته‌شده و به‌طور طبیعی غلط تایپی و فارسی/
         فینگلیش رو خودش تشخیص میده؛ ری‌اکشن/کاور/مصاحبه و امثالش توی این
         دسته اصلاً برنمی‌گرده چون فقط تِرک‌های واقعیِ کاتالوگ‌شده‌ن
      ۳) جستجوی خام یوتیوب - فقط وقتی دو منبع بالا هیچی پیدا نکردن (آخرین راه)
    نتیجه‌ها بر اساس (norm_key) بین منابع dedupe میشن، امتیاز شباهت متنی +
    محبوبیت می‌گیرن، و نسخه‌های جایگزین (لایو/ریمیکس/...) جریمه میشن تا
    نسخه‌ی اصلی/رسمی بالاتر از آب دربیاد. علاوه بر لیست آهنگ‌ها، آرتیستی که
    بیشترین تطابق اسمی رو با عبارت جستجو داره هم برمی‌گرده (از هر دو منبع)."""
    query = (query or "").strip()
    if not query:
        return [], None, None, None

    query_wants_alt = _title_has_any(query, _ALT_VERSION_PATTERNS)
    candidates = {}

    def _consider(title, artist, base_score, source, extra=None):
        if not title or _title_has_any(title, _JUNK_TITLE_PATTERNS):
            return
        score = base_score
        if not query_wants_alt and _title_has_any(title, _ALT_VERSION_PATTERNS):
            score -= 25
        key = _norm_key(title, artist)
        prev = candidates.get(key)
        if prev is not None and prev["score"] >= score:
            return
        item = {"title": title, "artist": artist, "score": score, "source": source}
        if extra:
            item.update(extra)
        candidates[key] = item

    # ۱) Deezer
    deezer_artist_scores = {}
    try:
        r = HTTP_SESSION.get("https://api.deezer.com/search", params={"q": query, "limit": 15}, timeout=8)
        tracks = r.json().get("data", [])
        for track in tracks:
            a_id = track.get("artist", {}).get("id")
            a_name = track.get("artist", {}).get("name", "")
            if a_id and a_id not in deezer_artist_scores:
                deezer_artist_scores[a_id] = {"name": a_name, "score": _text_score(query, a_name)}
        for track in tracks:
            title = track.get("title", "")
            artist = track.get("artist", {}).get("name", "")
            text_score = _text_score(query, f"{title} {artist}")
            pop_bonus = min((track.get("rank") or 0) / 15000, 12)
            _consider(title, artist, text_score + pop_bonus, "deezer")
    except Exception as e:
        logger.warning(f"deezer search error: {e}")

    # ۲) YouTube Music - منبع اصلی برای پوشش آهنگ‌های فارسی/زیرزمینی
    yt_artist_scores = {}
    try:
        yt = _get_ytmusic()
        yt_results = yt.search(query, filter="songs", limit=15) or []
        for item in yt_results:
            title = item.get("title", "")
            artist_list = item.get("artists") or []
            artists = ", ".join(a.get("name", "") for a in artist_list if a.get("name"))
            for a in artist_list:
                a_name = a.get("name", "")
                if a_name and a_name not in yt_artist_scores:
                    yt_artist_scores[a_name] = _text_score(query, a_name)
            text_score = _text_score(query, f"{title} {artists}")
            _consider(title, artists, text_score, "ytmusic", {"video_id": item.get("videoId")})
    except Exception as e:
        logger.warning(f"ytmusic search error: {e}")

    # ۳) fallback آخر: فقط اگه دو منبع بالا هیچی ندادن
    if not candidates:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(f"ytsearch15:{query}", download=False)
                for entry in (info.get("entries") or []):
                    title = entry.get("title", "")
                    text_score = _text_score(query, title)
                    _consider(title, entry.get("uploader", ""), text_score, "youtube")
        except Exception as e:
            logger.warning(f"youtube fallback search error: {e}")

    results = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)

    # تشخیص بهترین آرتیست مطابق، بین Deezer و YouTube Music - هرکدوم امتیاز
    # بالاتری داشت انتخاب میشه (حداقل ۴۵ امتیاز، وگرنه یعنی تطابق واقعی نیست).
    top_artist_id, top_artist_name, top_artist_source, best_score = None, None, None, 44
    if deezer_artist_scores:
        best_id = max(deezer_artist_scores, key=lambda x: deezer_artist_scores[x]["score"])
        s = deezer_artist_scores[best_id]["score"]
        if s > best_score:
            top_artist_id, top_artist_name, top_artist_source, best_score = (
                best_id, deezer_artist_scores[best_id]["name"], "deezer", s)
    if yt_artist_scores:
        best_name = max(yt_artist_scores, key=lambda x: yt_artist_scores[x])
        s = yt_artist_scores[best_name]
        if s > best_score:
            top_artist_id, top_artist_name, top_artist_source, best_score = (
                best_name, best_name, "ytmusic", s)

    return results[:10], top_artist_id, top_artist_name, top_artist_source


def get_artist_tracks(artist_id, source="deezer"):
    """۵۰ تا از پرطرفدارترین آهنگ‌های یه آرتیست خاص. برای Deezer از endpoint
    رسمی top-tracks استفاده میشه؛ برای آرتیست‌هایی که فقط توی YouTube Music
    پیدا شدن (مثلاً چون روی Deezer موجود نیستن)، جستجوی «فقط آهنگ» با اسم
    آرتیست به‌عنوان جایگزین top-tracks استفاده میشه."""
    if source == "ytmusic":
        try:
            yt = _get_ytmusic()
            yt_results = yt.search(artist_id, filter="songs", limit=50) or []
            out = []
            for t in yt_results:
                title = t.get("title", "")
                if _title_has_any(title, _JUNK_TITLE_PATTERNS):
                    continue
                artists = ", ".join(a.get("name", "") for a in (t.get("artists") or []) if a.get("name"))
                out.append({"title": title, "artist": artists, "video_id": t.get("videoId")})
            return out
        except Exception as e:
            logger.warning(f"ytmusic artist tracks error: {e}")
            return []
    try:
        r = HTTP_SESSION.get(f"https://api.deezer.com/artist/{artist_id}/top", params={"limit": 50}, timeout=8)
        return [{"title": t.get("title", ""), "artist": t.get("artist", {}).get("name", "")}
                for t in r.json().get("data", [])]
    except Exception:
        return []


def download_song_file(title, artist, chat_id, video_id=None):
    mp3_path = f"song_{chat_id}.mp3"
    ydl_opts = {
        "format": "bestaudio[ext=mp3]/bestaudio[ext=m4a]/bestaudio[ext=opus]/bestaudio/best",
        "outtmpl": f"song_{chat_id}.%(ext)s",
        "quiet": True, "noplaylist": True,
        # پست‌پروسسور ffmpeg: هر فرمتی که yt-dlp گرفت (حتی m4a خام/DASH که
        # روبیکا با Invalid_format ردش می‌کنه، مخصوصاً وقتی روی سرور JS
        # runtime نصب نیست و یوتیوب فرمت‌های محدودتری برمی‌گردونه) رو به یه
        # mp3 استاندارد و سالم تبدیل می‌کنه، تا همیشه چیزی که برای ارسال به
        # روبیکا می‌فرستیم قابل قبول باشه.
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "ffmpeg_location": _FFMPEG_EXE,
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
    # اگه video_id از خودِ نتیجه‌ی جستجو (YouTube Music) موجود باشه، اول
    # مستقیم همون رو دانلود می‌کنیم - یعنی دقیقاً همون نسخه‌ای که به کاربر
    # نشون دادیم دانلود میشه، نه اینکه یه سرچ متنیِ جدید یه ویدیوی متفاوت
    # (و شاید بی‌کیفیت‌تر) رو بیاره.
    searches = []
    if video_id:
        searches.append(f"https://www.youtube.com/watch?v={video_id}")
    searches += [
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
    results, artist_id, artist_name, artist_source = await asyncio.to_thread(search_songs, query)
    if not results:
        await _rx(status.edit("❌ آهنگی پیدا نشد، اسم دقیق‌تری بنویس."))
        return
    user_search_results[chat_id] = results
    if artist_id:
        user_artist_data[chat_id] = {"id": artist_id, "name": artist_name, "source": artist_source}
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
    tracks = await asyncio.to_thread(get_artist_tracks, artist_data["id"], artist_data.get("source", "deezer"))
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
    path = await asyncio.to_thread(download_song_file, title, artist, chat_id, track.get("video_id"))
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
    r = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/getFile", json={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    j = r.json()
    data = j.get("data") or j
    url = data.get("download_url")
    if not url:
        raise RuntimeError(f"getFile بدون download_url برگشت: {j}")
    return url


def _download_rubika_file_blocking(file_id: str, dest_path: str) -> bool:
    url = _rubika_get_file_download_url_blocking(file_id)
    resp = HTTP_SESSION.get(url, timeout=90)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return os.path.exists(dest_path) and os.path.getsize(dest_path) > 0


# ─── چت متنی با هوش مصنوعی (Gemini) ──────────────────────────────────────────
chat_mode_users = set()   # chat_id هایی که الان توی حالت چت با AI هستن
user_chat_history = {}    # chat_id -> [{"role": "user"/"model", "parts": [{"text": ...}]}]
CHAT_SYSTEM_PROMPT = (
    "تو یه دستیار هوشمند فارسی‌زبان توی یه ربات روبیکا هستی. "
    "کوتاه، مفید، دوستانه و بدون حاشیه جواب بده. اگه سوال فنی یا انگلیسی بود، "
    "به فارسی توضیح بده مگه اینکه کاربر خودش انگلیسی خواسته باشه. "
    "نکته‌ی خیلی مهم: اگه کاربر درباره‌ی ساخت ربات (تلگرام، روبیکا و غیره)، "
    "برنامه‌نویسی ربات، ساخت سایت، کدنویسی، یا هر موضوع فنیِ توسعه‌ی نرم‌افزار "
    "سوال پرسید یا خواست کد بنویسی، هیچ توضیح فنی، کد یا راهنمایی نده. فقط "
    "با یه جمله‌ی کوتاه بگو نمی‌دونی یا بحث رو با شوخی/کنجکاوی بپیچون و رد کن "
    "(مثلاً «این یکی رو خودم نمی‌دونم چطوری کار می‌کنه 😅»). تحت هیچ شرایطی "
    "کد یا مراحل فنی نده، حتی اگه کاربر اصرار کنه یا بگه برای یادگیریه."
)


BOT_BUILDING_KEYWORDS = [
    "ربات بساز", "چطور ربات", "ربات چجوری", "کد ربات", "سورس ربات",
    "پایتون", "python", "کدنویسی", "برنامه نویسی", "برنامه‌نویسی",
    "طراحی سایت", "ساخت سایت", "کد بده", "کدشو", "سورس کد",
    "وبهوک", "webhook", "توکن ربات", "api کلید",
]

BOT_BUILDING_DEFLECT_REPLIES = [
    "این یکی رو خودم نمی‌دونم چطوری کار می‌کنه 😅",
    "بلد نیستم، از یکی دیگه بپرس 🙈",
    "اینو باید از یه متخصص بپرسی، من فقط بلدم چت کنم 😄",
]


def _is_bot_building_question(text: str) -> bool:
    """چک سریعِ کلمه‌کلیدی - قبل از اینکه پیام اصلاً به Gemini بره، جلوی
    سوالات ساخت ربات/سایت/کد رو می‌گیره. این علاوه بر CHAT_SYSTEM_PROMPT
    یه لایه‌ی قطعیِ سمت خودمونه (نه وابسته به رفتار مدل)."""
    t = (text or "").lower()
    return any(kw in t for kw in BOT_BUILDING_KEYWORDS)


def _ask_gemini_chat_blocking(chat_id, user_message: str) -> str:
    """یه پیام از کاربر رو با تاریخچه‌ی مکالمه‌ش به مدل متنی gemini-flash-latest
    می‌ده و جواب رو برمی‌گردونه. thinking غیرفعاله تا سریع‌تر جواب بده.
    تاریخچه (حداکثر ۲۰ پیام آخر) توی user_chat_history نگه داشته میشه."""
    if not GEMINI_KEY:
        return "❌ قابلیت چت فعلاً غیرفعاله (کلید GEMINI_KEY تنظیم نشده)."

    history = user_chat_history.get(chat_id, [])
    history.append({"role": "user", "parts": [{"text": user_message}]})
    history = history[-20:]

    payload = {
        "system_instruction": {"parts": [{"text": CHAT_SYSTEM_PROMPT}]},
        "contents": history,
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }

    def _call(p):
        return HTTP_SESSION.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}",
            json=p, timeout=25,
        )

    try:
        r = _call(payload)
        if r.status_code == 400:
            # احتمالاً تاریخچه خراب/خیلی طولانی شده - پاکش کن و فقط با همین پیام دوباره امتحان کن
            user_chat_history.pop(chat_id, None)
            history = [{"role": "user", "parts": [{"text": user_message}]}]
            payload["contents"] = history
            r = _call(payload)
        if r.status_code in (503, 429):
            # سرور موقتاً شلوغه - یه بار دیگه با تاخیر کوتاه امتحان کن
            time.sleep(2)
            r = _call(payload)
        if r.status_code == 429:
            return "❌ سقف رایگان Gemini پر شده، چند دقیقه دیگه دوباره امتحان کن."
        if r.status_code == 503:
            return "❌ سرور Gemini موقتاً شلوغه، چند لحظه دیگه دوباره امتحان کن."
        r.raise_for_status()
        data = r.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return "❌ جوابی از هوش مصنوعی نگرفتم، دوباره امتحان کن."
        parts = candidates[0].get("content", {}).get("parts", [])
        reply = "".join(p.get("text", "") for p in parts).strip()
        if not reply:
            return "❌ جوابی از هوش مصنوعی نگرفتم، دوباره امتحان کن."
        history.append({"role": "model", "parts": [{"text": reply}]})
        user_chat_history[chat_id] = history
        return reply
    except requests.exceptions.HTTPError as e:
        logger.warning(f"gemini chat خطا داد: {e}")
        return f"❌ خطا از Gemini: {e}"
    except Exception as e:
        logger.warning(f"gemini chat خطا داد: {e}")
        return f"❌ خطا در اتصال به هوش مصنوعی: {e}"


async def handle_chat_message(message: "Message", text: str):
    """یه پیام کاربر توی حالت چت رو می‌گیره، به Gemini می‌ده و جواب رو
    ادیت می‌کنه روی همون پیام «دارم فکر می‌کنم...» تا سریع‌تر و تمیزتر باشه."""
    try:
        if _is_bot_building_question(text):
            await _rx(message.reply(random.choice(BOT_BUILDING_DEFLECT_REPLIES)))
            return
        if not await check_chat_limit(message):
            return
        status = await _rx(message.reply("⏳ دارم فکر می‌کنم..."))
        reply = await asyncio.to_thread(_ask_gemini_chat_blocking, message.chat_id, text)
        add_chat_usage(message.chat_id)
        if status is not None and hasattr(status, "edit"):
            await _rx(status.edit(reply))
        else:
            await _rx(message.reply(reply))
    except Exception as e:
        logger.warning(f"handle_chat_message خطا داد: {e}")
        try:
            await _rx(message.reply(f"❌ خطا: {e}"))
        except Exception:
            pass


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
    nsfw_st = "🟢 روشن" if get_setting("nsfw_filter_enabled", "1") == "1" else "🔴 خاموش"
    return (
        f"🔧 پنل ادمین\n\nحالت تعمیر: {maintenance}\nمحدودیت دانلود: {limit_st}\n"
        f"فیلتر NSFW: {nsfw_st}"
    )


def admin_main_keypad():
    b = InlineBuilder()
    b = b.row(b.button_simple("adm_stats", "📊 آمار"))
    b = b.row(b.button_simple("adm_users", "👥 کاربران"), b.button_simple("adm_vip", "💎 VIP"))
    b = b.row(b.button_simple("adm_banned", "🚫 بن‌شده‌ها"))
    b = b.row(b.button_simple("adm_settings", "⚙️ تنظیمات"))
    b = b.row(b.button_simple("adm_broadcast", "📢 پیام همگانی"))
    b = b.row(b.button_simple("adm_maintenance", "🔧 حالت تعمیر (روشن/خاموش)"))
    b = b.row(b.button_simple("adm_limit_toggle", "🔢 محدودیت دانلود (روشن/خاموش)"))
    b = b.row(b.button_simple("adm_nsfw_toggle", "🔞 فیلتر NSFW (روشن/خاموش)"))
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

    elif btn_id == "adm_nsfw_toggle":
        new_val = "0" if get_setting("nsfw_filter_enabled", "1") == "1" else "1"
        set_setting("nsfw_filter_enabled", new_val)
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
            f"🎶 تیک‌تاک: {s.get('tiktok', 0)}\n"
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
            f"🤖 یوزرنیم ربات: {get_setting('bot_username') or BOT_USERNAME or '-'}\n"
            f"👥 تعداد دعوت لازم: {get_setting('referral_target', '2')}\n"
        )
        await _rx(message.reply_inline(text, settings_keypad()))

    elif btn_id in (
        "adm_give_vip", "adm_remove_vip", "adm_ban_user", "adm_unban_user", "adm_search_user",
        "adm_set_channel_guid", "adm_set_channel_link", "adm_set_channel_tag", "adm_set_limit", "adm_set_welcome",
        "adm_set_caption", "adm_set_bot_username", "adm_set_referral_target",
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
            "adm_set_bot_username": "🤖 یوزرنیم ربات رو بدون @ بفرست (برای ساخت لینک دعوت):",
            "adm_set_referral_target": "👥 تعداد دعوت لازم برای گرفتن پاداش رو بفرست (عدد):",
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
    b = b.row(b.button_simple("adm_set_bot_username", "🤖 یوزرنیم ربات"))
    b = b.row(b.button_simple("adm_set_referral_target", "👥 تعداد دعوت لازم"))
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

    elif action == "adm_set_bot_username":
        username = text.strip().lstrip("@")
        set_setting("bot_username", username)
        await _rx(message.reply(f"✅ یوزرنیم ربات تنظیم شد: {username}"))

    elif action == "adm_set_referral_target":
        try:
            set_setting("referral_target", str(int(text.strip())))
            await _rx(message.reply(f"✅ تعداد دعوت لازم به {text.strip()} نفر تغییر کرد."))
        except Exception:
            await _rx(message.reply("❌ یک عدد وارد کن."))

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
@bot.on_message(commands=["chat"])
async def chat_cmd(bot: Robot, message: Message):
    try:
        register_user(message.chat_id)
        if is_banned(message.chat_id):
            await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
            return
        if not await is_member(message.sender_id):
            await send_not_joined(message)
            return
        if not GEMINI_KEY:
            await _rx(message.reply("❌ قابلیت چت فعلاً غیرفعاله (کلید GEMINI_KEY تنظیم نشده)."))
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            chat_mode_users.add(message.chat_id)
            _spawn(handle_chat_message(message, parts[1].strip()))
            return
        chat_mode_users.add(message.chat_id)
        builder = InlineBuilder()
        builder = builder.row(builder.button_simple("chat_exit", "❌ خروج از حالت چت"))
        await _rx(message.reply_inline(
            "🤖 چت با هوش مصنوعی\n\nالان توی حالت چتی، هر چی بنویسی جواب می‌دم.\n"
            "برای خروج، دکمه‌ی زیر رو بزن یا /clearchat بزن.",
            builder.build(),
        ))
    except Exception:
        logger.exception("خطا در /chat")


@bot.on_message(commands=["clearchat"])
async def clearchat_cmd(bot: Robot, message: Message):
    try:
        user_chat_history.pop(message.chat_id, None)
        chat_mode_users.discard(message.chat_id)
        await _rx(message.reply("🗑 تاریخچه‌ی چت پاک شد و از حالت چت خارج شدی."))
    except Exception:
        logger.exception("خطا در /clearchat")


async def handle_chat_exit_callback(message):
    chat_mode_users.discard(message.chat_id)
    await _rx(message.reply("✅ از حالت چت خارج شدی."))


@bot.on_message(commands=["whoami"])
async def whoami_cmd(bot: Robot, message: Message):
    """دستور عمومی (برای همه، نه فقط ادمین): GUID واقعی خودِ فرستنده رو نشون
    می‌ده و می‌گه از نظر منطق جوین اجباری (کلیک روی «عضو شدم») تاییدشده
    حساب می‌شه یا نه. توجه: این یه چک واقعی نیست، فقط وضعیت دیکشنری کلیک‌هاست."""
    channel_guid = get_setting("channel_guid")
    text = f"🆔 GUID شما: {message.sender_id}"
    if channel_guid:
        found = await is_member(message.sender_id)
        text += f"\n📣 وضعیت جوین اجباری: {'✅ تاییدشده' if found else '❌ هنوز تاییدنشده'}"
    await _rx(message.reply(text))


def start_menu_keypad():
    """کیبورد شیشه‌ای زیر پیام /start - فقط قابلیت هوش مصنوعی، بدون
    کانفیگ رایگان و بدون دکمه‌ی «چقدر منو می‌شناسن»."""
    b = InlineBuilder()
    b = b.row(b.button_simple("start_download_info", "⬇️ دانلود اینستا/تیک‌تاک/پینترست/روبینو + موزیک"))
    b = b.row(b.button_simple("start_chat", "🤖 چت هوش مصنوعی"))
    b = b.row(b.button_simple("start_invite", "🤝 دعوت دوستان"))
    return b.build()


def invite_text(chat_id):
    """متن + دکمه‌ی شیشه‌ای مشترک /invite برای استفاده هم توی دستور و هم توی
    دکمه‌ی منو. یه تاپل (text, keypad) برمی‌گردونه؛ keypad ممکنه None باشه
    (وقتی یوزرنیم ربات تنظیم نشده و باید از کد دعوت متنی استفاده بشه)."""
    target = int(get_setting("referral_target", "2") or "2")
    reward_days = int(get_setting("referral_reward_days", "30") or "30")
    count = get_referral_count(chat_id)
    rewarded = "✅ گرفتی" if is_referral_rewarded(chat_id) else "⏳ نگرفتی"
    _, keypad = build_invite_message(chat_id)
    text = (
        "🤝 دعوت دوستان\n\n"
        f"👥 دعوت‌های موفق: {count}/{target}\n"
        f"🎁 پاداش: {reward_days} روز VIP رایگان (وقتی {target} نفر عضو بشن)\n"
        f"💎 وضعیت پاداش: {rewarded}"
    )
    if not keypad:
        link = get_referral_link(chat_id)
        link_line = f"کد دعوت: `{chat_id}`\n(دوستت باید بعد از /start این کد رو بفرسته)" if not link else f"🔗 {link}"
        text = f"🤝 دعوت دوستان\n\n{link_line}\n\n" + text.split("\n\n", 1)[1]
    return text, keypad


async def start_menu_download_info_callback(message):
    """کلیک روی دکمه‌ی «دانلود اینستا/تیک‌تاک/...» توی منوی /start. این فقط
    توضیح میده، دستور جدیدی نمی‌سازه - لینک دادن همیشه بدون دستور هم کار
    می‌کرده، این دکمه فقط برای کسایی که تازه استارت زدن و فکر می‌کنن فقط
    قابلیت چت هوش مصنوعی هست."""
    register_user(message.chat_id)
    if is_banned(message.chat_id):
        await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
        return
    await _rx(message.reply(
        "⬇️ دانلود پست/ریل/موزیک\n\n"
        "کافیه لینک رو مستقیم بفرستی، نیازی به دستور خاصی نیست:\n\n"
        "🔗 لینک پست/ریل اینستاگرام\n"
        "🔗 لینک ویدیوی تیک‌تاک\n"
        "🔗 لینک پین پینترست\n"
        "🔗 لینک پست روبینو\n"
        "🎵 یا فقط اسم یه آهنگ بنویس\n\n"
        "همین که بفرستی، خودم تشخیص می‌دم و دانلودش می‌کنم."
    ))


async def start_menu_invite_callback(message):
    """کلیک روی دکمه‌ی «دعوت دوستان» توی منوی /start."""
    register_user(message.chat_id)
    if is_banned(message.chat_id):
        await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
        return
    text, keypad = invite_text(message.chat_id)
    if keypad:
        await _rx(message.reply_inline(text, keypad))
    else:
        await _rx(message.reply(text))


async def start_menu_chat_callback(message):
    """کلیک روی دکمه‌ی «چت با هوش مصنوعی» توی منوی /start."""
    register_user(message.chat_id)
    if is_banned(message.chat_id):
        await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
        return
    if not await is_member(message.sender_id):
        await send_not_joined(message)
        return
    if not GEMINI_KEY:
        await _rx(message.reply("❌ قابلیت چت فعلاً غیرفعاله (کلید GEMINI_KEY تنظیم نشده)."))
        return
    chat_mode_users.add(message.chat_id)
    builder = InlineBuilder()
    builder = builder.row(builder.button_simple("chat_exit", "❌ خروج از حالت چت"))
    await _rx(message.reply_inline(
        "🤖 چت با هوش مصنوعی\n\nالان توی حالت چتی، هر چی بنویسی جواب می‌دم.\n"
        "برای خروج، دکمه‌ی زیر رو بزن یا /clearchat بزن.",
        builder.build(),
    ))


@bot.on_message(commands=["start"])
async def start(bot: Robot, message: Message):
    logger.info(f"📩 /start از {message.sender_id} (chat_id={message.chat_id})")
    try:
        register_user(message.chat_id)
        # ─── دریافت کد دعوت از /start <کد کوتاه یا chat_id قدیمی> (سیستم دعوت دونفره) ───
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            payload = parts[1].strip()
            referrer_id = resolve_referral_code(payload) or payload  # فال‌بک به لینک‌های قدیمی
            set_invited_by(message.chat_id, referrer_id)
        if is_banned(message.chat_id):
            await _rx(message.reply("⛔️ شما مسدود شده‌اید."))
            return
        if not await is_member(message.sender_id):
            await send_not_joined(message)
            return
        welcome = get_setting("welcome")
        await _rx(message.reply_inline(
            welcome or (
                "سلام 👋\n\n"
                "لینک اینستاگرام، تیک‌تاک، پینترست یا روبینو رو بفرست — دانلودش می‌کنم.\n"
                "اسم یه آهنگ هم بفرستی، پیداش می‌کنم.\n\n"
                "راهنما: /help"
            ),
            start_menu_keypad(),
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
            "• اسم آهنگ یا خواننده بنویس\n"
            "• /chat — چت با هوش مصنوعی\n"
            "• /clearchat — پاک کردن تاریخچه‌ی چت و خروج از حالت چت\n"
            "• /invite — دریافت لینک دعوت و دریافت پاداش VIP"
        ))
    except Exception:
        logger.exception("خطا در هندلر /help")


@bot.on_message(commands=["invite"])
async def invite_cmd(bot: Robot, message: Message):
    """لینک/کد دعوت شخصی کاربر + وضعیت پیشرفت سیستم دعوت دونفره."""
    logger.info(f"📩 /invite از {message.sender_id}")
    try:
        register_user(message.chat_id)
        text, keypad = invite_text(message.chat_id)
        if keypad:
            await _rx(message.reply_inline(text, keypad))
        else:
            await _rx(message.reply(text))
    except Exception:
        logger.exception("خطا در هندلر /invite")


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
        client = Client(PYRUBI_SESSION_NAME)
        lines = []
        for method_name, tag in (("get_all_members", "عضو عادی"), ("get_admin_members", "ادمین/سازنده")):
            fn = getattr(client, method_name)
            start_id = None
            pages = 0
            while True:
                pages += 1
                result = fn(channel_guid, start_id=start_id)
                if isinstance(result, list):
                    members = result
                    has_continue = False
                    next_start_id = None
                else:
                    members = result.get("in_chat_members") or []
                    has_continue = result.get("has_continue")
                    next_start_id = result.get("next_start_id")
                for item in members:
                    if isinstance(item, dict):
                        g = item.get("member_guid")
                        name = item.get("first_name") or item.get("title") or ""
                        username = item.get("username")
                        jt = item.get("join_type") or tag
                    else:
                        g, name, username, jt = item, "", None, tag
                    label = f"{g}  ({name}"
                    if username:
                        label += f" @{username}"
                    label += f", {jt})"
                    lines.append(label)
                if not has_continue or not next_start_id or pages > 20:
                    break
                start_id = next_start_id
        return lines

    try:
        guids = await asyncio.to_thread(_blocking)
        channel_link = get_setting("channel_link") or "-"
        header = f"📣 channel_guid فعلی: {channel_guid}\n🔗 channel_link فعلی: {channel_link}\n👥 تعداد کل (عضو عادی + ادمین/سازنده): {len(guids)}\n\n"
        text = header + "\n".join(guids)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        await _rx(message.reply(f"❌ خطا: {type(e).__name__}: {e}"))
        logger.error("❌ list_members خطا داد", exc_info=True)


def _pyrubi_list_handlers_blocking() -> str:
    """دنبال متدهایی می‌گرده که شبیه handler/event decorator باشن (نه فقط
    on_message) - چون on_message ظاهراً فقط پیام‌های متنی رو می‌گیره، نه
    رویدادهای عضویت/چت. اسم متد دقیق برای رویدادهای چت رو از همینجا پیدا
    می‌کنیم."""
    from pyrubi import Client
    client = Client(PYRUBI_SESSION_NAME)
    out = []

    all_names = [n for n in dir(client) if not n.startswith("_")]
    keywords = ["on_", "handler", "event", "update", "chat", "member", "join"]
    candidates = sorted(set(n for n in all_names if any(k in n.lower() for k in keywords)))
    out.append("متدهای مرتبط پیدا‌شده روی client:\n" + ", ".join(candidates))

    for name in candidates:
        try:
            attr = getattr(client, name)
            if callable(attr):
                sig = str(inspect.signature(attr))
                out.append(f"• {name}{sig}")
        except Exception as e:
            out.append(f"• {name}: نتونستم امضا رو بگیرم ({e})")

    return "\n".join(out)


@bot.on_message(commands=["pyrubi_handlers"])
async def pyrubi_handlers_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: لیست همه‌ی handlerها/decoratorهای موجود
    روی Client پیرابی، برای پیدا کردن اسم درست رویداد عضویت/چت."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    if not os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi"):
        await _rx(message.reply("❌ سشن پیرابی موجود نیست."))
        return
    await _rx(message.reply("🔎 دارم handlerهای موجود رو لیست می‌کنم..."))
    try:
        text = await asyncio.to_thread(_pyrubi_list_handlers_blocking)
        for i in range(0, len(text), 3500):
            await _rx(message.reply(text[i:i + 3500]))
    except Exception as e:
        logger.exception("خطا در pyrubi_handlers")
        await _rx(message.reply(f"❌ خطای کلی: {type(e).__name__}: {e}"))


def _try_extract_join_event(update, channel_guid: str):
    """سعی می‌کنه از یه آپدیتِ ناشناخته‌ی pyrubi، GUID کانال و GUID کاربرِ
    تازه‌جوین‌شده رو در بیاره. چون فرمت دقیق مستند نیست، چند تا حدسِ منطقی
    رو امتحان می‌کنه. اگه چیزی پیدا نشد، (None, None) برمی‌گردونه."""
    def _get(obj, *names):
        for name in names:
            if isinstance(obj, dict) and name in obj:
                return obj[name]
            val = getattr(obj, name, None)
            if val is not None:
                return val
        return None

    obj_guid = _get(update, "object_guid", "objectGuid", "chat_guid", "channel_guid")
    if obj_guid != channel_guid:
        return None, None

    action = _get(update, "action", "type", "chat_action", "update_type")
    action_str = str(action).lower() if action else ""
    if action and "join" not in action_str and "member" not in action_str and "new" not in action_str:
        return None, None

    user_guid = _get(update, "new_member_guid", "member_guid", "author_guid", "user_guid", "actor_guid")
    if not user_guid or user_guid == channel_guid:
        return None, None

    return channel_guid, user_guid


def _run_pyrubi_event_logger():
    import builtins

    def _blocked_input(prompt=""):
        # اگه pyrubi توی این ترد بخواد بره سراغ لاگین اینتراکتیو (یعنی
        # session رو نامعتبر تشخیص داده)، به‌جای کرش با EOFError یا فرستادن
        # SMS واقعی، یه خطای واضح می‌ندازیم.
        raise RuntimeError(
            f"pyrubi خواست وارد فرآیند لاگین اینتراکتیو بشه (prompt={prompt!r}) - "
            "یعنی session نامعتبره یا خراب شده."
        )

    original_input = builtins.input
    builtins.input = _blocked_input
    try:
        from pyrubi import Client
        client = Client(PYRUBI_SESSION_NAME)
    except Exception as e:
        logger.warning(f"⚠️ لیسنر pyrubi استارت نخورد (session نامعتبر؟): {type(e).__name__}: {e}")
        return
    finally:
        builtins.input = original_input

    @client.on_message()
    def _on_any_update(update):
        try:
            logger.info(f"📡 PYRUBI UPDATE ({type(update).__name__}): {update!r}"[:1500])
        except Exception:
            logger.exception("خطا در لاگ رویداد pyrubi")
        try:
            channel_guid = get_setting("channel_guid")
            if not channel_guid:
                return
            ch_guid, user_guid = _try_extract_join_event(update, channel_guid)
            if user_guid and not has_joined(user_guid):
                if mark_joined(user_guid):
                    # این callback توی ترد جدای pyrubi اجراست (بدون event loop)،
                    # برای همین از نسخه‌ی sync مستقیم استفاده می‌کنیم نه _spawn.
                    _notify_referrer_on_join_sync(user_guid)
                logger.info(f"✅ رویداد جوین شناسایی شد و عضویت ثبت شد: {user_guid}")
        except Exception:
            logger.exception("خطا در تشخیص رویداد جوین")

    client.run()


def start_pyrubi_event_logger_thread():
    if not (os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi") or glob.glob(PYRUBI_SESSION_NAME + "*")):
        logger.warning("⚠️ سشن pyrubi موجود نیست، لیسنر رویداد اجرا نشد.")
        return
    t = threading.Thread(target=_run_pyrubi_event_logger, daemon=True, name="pyrubi-event-logger")
    t.start()
    logger.info("📡 لیسنر رویدادهای خام pyrubi استارت شد.")


@bot.on_message(commands=["rubino_filecheck"])
async def rubino_filecheck_cmd(bot: Robot, message: Message):
    """دستور دیباگ - فقط ادمین: خودِ فایل rubino_acc.rp رو مستقیم (بدون rubpy) چک می‌کنه."""
    if not ADMIN_CHAT_ID or message.sender_id != ADMIN_CHAT_ID:
        return
    path = "rubino_acc.rp"
    lines = []
    if not os.path.exists(path):
        await _rx(message.reply(f"❌ فایل {path} اصلاً روی دیسک وجود نداره."))
        return
    size = os.path.getsize(path)
    lines.append(f"📦 مسیر: {os.path.abspath(path)}")
    lines.append(f"📏 حجم: {size} بایت")
    with open(path, "rb") as f:
        head = f.read(16)
    lines.append(f"🔍 ۱۶ بایت اول (hex): {head.hex()}")
    lines.append(f"🔍 باید با این شروع بشه اگه SQLite باشه: 53514c69746520666f726d6174203300")
    try:
        conn = sqlite3.connect(path)
        cur = conn.execute("PRAGMA integrity_check;")
        result = cur.fetchall()
        lines.append(f"✅ sqlite integrity_check: {result}")
        cur2 = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        lines.append(f"📋 جدول‌ها: {cur2.fetchall()}")
        conn.close()
    except Exception as e:
        lines.append(f"❌ sqlite3 خطا داد: {type(e).__name__}: {e}")
    await _rx(message.reply("\n".join(lines)))


@bot.on_message()
async def handle_text(bot: Robot, message: Message):
    text = (message.text or "").strip()
    file_meta = _extract_incoming_file(message)
    logger.info(f"📩 پیام از {message.sender_id}: text={text[:60]!r} file={bool(file_meta)}")
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
            # شاید همین پیام، همون پستِ فوروارد‌شده از کانال باشه که کاربر
            # به‌عنوان اثبات عضویت فرستاده - اول اینو چک می‌کنیم.
            if verify_join_from_forward(message):
                await _rx(message.reply(
                    "✅ عضویت شما تایید شد!\n\n"
                    "🔗 حالا لینک یا اسم آهنگ رو بفرست."
                ))
                return
            await send_not_joined(message)
            return
        if not await check_limit(message):
            return

        # ─── چت با هوش مصنوعی: اگه کاربر توی حالت چته، فایلی نفرستاده، و
        # متنش هم یه لینک دانلودی (اینستا/تیک‌تاک/پینترست/روبینو) نیست ───
        # قبلاً این چک قبل از تشخیص لینک بود، برای همین اگه کسی توی حالت
        # چت بود و لینک اینستا می‌فرستاد، به‌جای دانلود، هوش مصنوعی جوابِ
        # بی‌ربط می‌داد. حالا اول چک می‌کنیم لینک دانلودیه یا نه.
        is_download_link = (
            "instagram.com" in text
            or "tiktok.com" in text or "vm.tiktok.com" in text
            or "pinterest.com" in text or "pin.it" in text
            or "rubika.ir/post" in text or "rubino.ir" in text
        )
        if message.chat_id in chat_mode_users and text and not file_meta and not is_download_link:
            _spawn(handle_chat_message(message, text))
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
        elif "tiktok.com" in text or "vm.tiktok.com" in text:
            _spawn(handle_tiktok(message, text))
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
        elif btn_id == "chat_exit":
            _spawn(handle_chat_exit_callback(message))
        elif btn_id == "start_chat":
            _spawn(start_menu_chat_callback(message))
        elif btn_id == "start_invite":
            _spawn(start_menu_invite_callback(message))
        elif btn_id == "start_download_info":
            _spawn(start_menu_download_info_callback(message))
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
    r = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/sendMessage", json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("data") or j


def _rubika_edit_message_blocking(chat_id: str, message_id: str, text: str) -> dict:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    r = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/editMessageText", json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("data") or j


def _rubika_send_music_blocking(chat_id: str, file_path: str, caption: str = "") -> dict:
    r1 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": "Music"}, timeout=30)
    r1.raise_for_status()
    upload_url = (r1.json().get("data") or r1.json()).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile(Music) بدون upload_url: {r1.json()}")

    with open(file_path, "rb") as f:
        r2 = HTTP_SESSION.post(upload_url, files={"file": (os.path.basename(file_path), f, "audio/mpeg")}, timeout=180)
    r2.raise_for_status()
    file_id = (r2.json().get("data") or r2.json()).get("file_id")
    if not file_id:
        raise RuntimeError(f"آپلود آهنگ بدون file_id: {r2.json()}")

    payload = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        payload["text"] = caption
    r3 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
    r3.raise_for_status()
    j3 = r3.json()
    return j3.get("data") or j3


def _rubika_send_document_blocking(chat_id: str, file_path: str, caption: str = "") -> dict:
    """فایل‌های عمومی (مثل .txt) رو به‌عنوان File (نه Music) آپلود و ارسال می‌کنه -
    برای فرستادن یه مقدار طولانی (مثل session base64) توی یه فایل واحد، به‌جای
    چند پیام تکه‌تکه."""
    r1 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/requestSendFile", json={"type": "File"}, timeout=30)
    r1.raise_for_status()
    upload_url = (r1.json().get("data") or r1.json()).get("upload_url")
    if not upload_url:
        raise RuntimeError(f"requestSendFile(File) بدون upload_url: {r1.json()}")

    with open(file_path, "rb") as f:
        r2 = HTTP_SESSION.post(upload_url, files={"file": (os.path.basename(file_path), f, "text/plain")}, timeout=180)
    r2.raise_for_status()
    file_id = (r2.json().get("data") or r2.json()).get("file_id")
    if not file_id:
        raise RuntimeError(f"آپلود فایل بدون file_id: {r2.json()}")

    payload = {"chat_id": chat_id, "file_id": file_id}
    if caption:
        payload["text"] = caption
    r3 = HTTP_SESSION.post(f"{RUBIKA_API_BASE}/sendFile", json=payload, timeout=30)
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
        elif btn_id == "chat_exit":
            await handle_chat_exit_callback(msg)
        elif btn_id == "start_chat":
            await start_menu_chat_callback(msg)
        elif btn_id == "start_invite":
            await start_menu_invite_callback(msg)
        elif btn_id == "start_download_info":
            await start_menu_download_info_callback(msg)
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
    r = HTTP_SESSION.post(
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
    session_path = PYRUBI_SESSION_NAME + ".pyrubi"
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
    start_pyrubi_event_logger_thread()
    start_inline_webhook_thread()
    try:
        bot.set_commands([
            {"command": "start", "description": "شروع و منوی اصلی"},
            {"command": "chat", "description": "🤖 چت با هوش مصنوعی"},
            {"command": "clearchat", "description": "🗑 پاک‌کردن تاریخچه چت"},
            {"command": "invite", "description": "🤝 دعوت دوستان"},
            {"command": "help", "description": "📖 راهنمای ربات"},
        ])
        logger.info("✅ لیست دستورات ربات (منوی پایین چت) تنظیم شد")
    except Exception as e:
        logger.warning(f"⚠️ تنظیم لیست دستورات ربات ناموفق بود: {e}")
    bot.run()
