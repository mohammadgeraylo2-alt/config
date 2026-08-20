"""
ربات تلگرامی چک‌کننده اعتبار اکانت‌های روبیکا + لاگین با شماره (نسخه rubpy)
--------------------------------------------------------------------------
تفاوت اصلی با نسخه‌ی pyrubi:
  - pyrubi برای لاگین، تعاملی و بلوکه‌کننده بود (input() می‌خواست) و رو سرور
    (بدون ترمینال) با EOFError کرش می‌کرد.
  - rubpy متدهای لاگین رو جدا و async ساخته (send_code / sign_in) که می‌شه
    دستی و مرحله‌به‌مرحله صداشون زد؛ دقیقاً مناسب فلوی «شماره از تلگرام بگیر،
    کد از تلگرام بگیر».

نیازمندی‌ها (requirements.txt):
    pyTelegramBotAPI
    rubpy

نکته مهم درباره‌ی این فایل:
    مستندات آنلاین rubpy برای من (مدل) قابل دسترسی کامل نبود، پس اسم دقیق
    پارامترهای send_code/sign_in تضمین‌شده نیست. برای حل این مشکل، به‌جای
    حدس زدن، کد با inspect نام واقعی پارامترهای متدِ نصب‌شده روی خود سرور
    رو می‌خونه و مقدار مناسب (شماره/کد/هش) رو بر اساس شباهت اسمی بهش پاس
    می‌ده (build_kwargs). اگه بازم جواب نداد، با /debugrubpy امضای دقیق
    متدها رو از کتابخونه‌ی واقعی نصب‌شده چاپ کن و بر اساسش CONCEPT_HINTS
    پایین رو دستی تنظیم کن.
"""

import os
import re
import json
import asyncio
import inspect
import logging
import telebot
from telebot import types

try:
    from rubpy import Client
except ImportError:
    Client = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth-checker-rubpy")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
bot = telebot.TeleBot(BOT_TOKEN)

# پوشه‌ای که سشن‌های موقت لاگین توش ذخیره می‌شن (روی Railway بین ریستارت‌ها پاک می‌شه، مشکلی نیست)
SESSIONS_DIR = "login_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

waiting_for_auths = set()
waiting_for_channel = set()
healthy_auths_by_chat = {}

waiting_for_login_phone = set()
# chat_id -> {"client": Client, "phone": str, "send_code_result": <خروجی خام send_code>}
pending_login_by_chat = {}


def run_async(coro):
    """یه coroutine رو تو یه event loop تازه اجرا می‌کنه (چون telebot sync هست)."""
    return asyncio.run(coro)


def build_kwargs(func, concept_values: dict) -> dict:
    """
    اسم پارامترهای واقعیِ func رو با inspect می‌خونه و بر اساس شباهت اسمی
    (مثلاً 'phone' تو 'phone_number') مقدار مناسب رو بهشون map می‌کنه.
    اگه هیچ پارامتری match نشد، dict خالی برمی‌گرده (یعنی باید positional
    صداش بزنیم).
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {}

    kwargs = {}
    for name, param in sig.parameters.items():
        if name in ("self", "args", "kwargs"):
            continue
        lname = name.lower()
        for concept, value in concept_values.items():
            if concept in lname:
                kwargs[name] = value
                break
    return kwargs


async def _make_client(session_name: str):
    if Client is None:
        raise RuntimeError("کتابخونه rubpy نصب نیست")
    client = Client(name=os.path.join(SESSIONS_DIR, session_name))
    connect_fn = getattr(client, "connect", None)
    if callable(connect_fn):
        await connect_fn()
    return client


async def _disconnect_client(client):
    disconnect_fn = getattr(client, "disconnect", None)
    if callable(disconnect_fn):
        try:
            await disconnect_fn()
        except Exception:
            pass


def check_single_auth(auth: str, private_key: str = None) -> tuple[bool, str]:
    """
    یه سشن روبیکا (auth [+ private]) رو با یه عملیات سبک تست می‌کنه.
    """
    auth = auth.strip()
    if not auth:
        return False, "خالی بود"
    if Client is None:
        return False, "کتابخونه rubpy نصب نیست"

    async def _check():
        client = Client(name="temp_check_session", auth=auth, private_key=private_key)
        connect_fn = getattr(client, "connect", None)
        if callable(connect_fn):
            await connect_fn()
        try:
            # ==== CHECK_METHOD: اگه get_me نبود، با /debugrubpy اسم درست رو پیدا کن ====
            get_me_fn = getattr(client, "get_me", None) or getattr(client, "get_chats", None)
            if get_me_fn is None:
                raise AttributeError("نه get_me نه get_chats روی Client پیدا شد")
            result = await get_me_fn()
            # =========================================================================
            return result
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_check())
        if result is not None:
            return True, "سالم"
        return False, "پاسخ خالی از سرور"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def debug_rubpy_signatures() -> str:
    """امضای دقیق متدهای لاگین/چک رو از کتابخونه‌ی واقعی نصب‌شده چاپ می‌کنه."""
    if Client is None:
        return "کتابخونه rubpy نصب نیست."

    lines = []
    try:
        client = Client(name=os.path.join(SESSIONS_DIR, "debug_probe"))
    except Exception as e:
        return f"ساخت Client شکست خورد: {type(e).__name__}: {e}"

    interesting = [n for n in dir(client) if any(k in n.lower() for k in
                   ("send_code", "sign_in", "login", "code", "connect", "get_me", "get_chats",
                    "key", "rsa", "crypto"))]
    if not interesting:
        return "هیچ متد مرتبطی پیدا نشد."

    for name in interesting:
        try:
            attr = getattr(client, name)
        except Exception as e:
            lines.append(f"• {name}: خطا در دسترسی ({type(e).__name__})")
            continue
        if callable(attr):
            try:
                sig = inspect.signature(attr)
                lines.append(f"• {name}{sig}  [متد]")
            except (TypeError, ValueError):
                lines.append(f"• {name}(...) — امضا قابل خوندن نبود  [متد]")
        else:
            lines.append(f"• {name} = {attr!r}  [اتریبیوت]")

    # بررسی وجود ماژول رمزنگاری pycryptodome که rubpy روش ساخته شده
    try:
        import Crypto  # noqa: F401
        lines.append("\npycryptodome نصب است (برای ساخت RSA در دسترس است).")
    except ImportError:
        lines.append("\npycryptodome نصب نیست.")

    return "امضای متدها/اتریبیوت‌های پیدا‌شده روی rubpy Client:\n" + "\n".join(lines)


@bot.message_handler(commands=["debugrubpy"])
def debugrubpy_cmd(message):
    bot.reply_to(message, debug_rubpy_signatures())


def start_login(phone: str):
    """مرحله اول: ساخت Client و ارسال کد تایید."""
    if Client is None:
        return False, None, None, "کتابخونه rubpy نصب نیست"

    phone = phone.strip()
    session_name = re.sub(r"[^0-9]", "", phone) or "unknown"

    async def _start():
        client = await _make_client(f"login_{session_name}")
        send_code_fn = getattr(client, "send_code", None)
        if send_code_fn is None:
            raise AttributeError(
                "متد send_code روی Client پیدا نشد. با /debugrubpy اسم درست رو پیدا کن."
            )
        kwargs = build_kwargs(send_code_fn, {"phone": phone})
        if kwargs:
            result = await send_code_fn(**kwargs)
        else:
            result = await send_code_fn(phone)
        return client, result

    try:
        client, result = run_async(_start())
        return True, client, result, "کد تایید ارسال شد"
    except Exception as e:
        return False, None, None, f"{type(e).__name__}: {e}"


def submit_login_code(client, phone: str, code: str, send_code_result):
    """مرحله دوم: فرستادن کد تایید و گرفتن سشن نهایی."""

    async def _submit():
        sign_in_fn = getattr(client, "sign_in", None)
        if sign_in_fn is None:
            raise AttributeError(
                "متد sign_in روی Client پیدا نشد. با /debugrubpy اسم درست رو پیدا کن."
            )

        # مقادیر ممکن برای پارامترهای sign_in: شماره، کد، و هر چیزی که از
        # send_code برگشته (معمولاً phone_code_hash یا مشابهش)
        concept_values = {"phone": phone, "code": code}
        if isinstance(send_code_result, dict):
            for k, v in send_code_result.items():
                if "hash" in k.lower():
                    concept_values["hash"] = v
        elif hasattr(send_code_result, "phone_code_hash"):
            concept_values["hash"] = getattr(send_code_result, "phone_code_hash")

        kwargs = build_kwargs(sign_in_fn, concept_values)
        if kwargs:
            result = await sign_in_fn(**kwargs)
        else:
            result = await sign_in_fn(phone, code)

        # سعی می‌کنیم auth نهایی رو از خود client یا نتیجه پیدا کنیم
        auth_val = getattr(client, "auth", None) or getattr(client, "auth_key", None)
        await _disconnect_client(client)
        return auth_val, result

    try:
        auth_val, result = run_async(_submit())
        if auth_val:
            return True, str(auth_val), "ورود موفق"
        return True, None, (
            "ورود موفق ولی auth رو خودکار پیدا نکردم — سشن تو فایل "
            f"{SESSIONS_DIR}/login_{re.sub(r'[^0-9]', '', phone)}.session ذخیره شده، "
            "با /debugrubpy بررسی کن اسم اتریبیوت auth چیه."
        )
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@bot.message_handler(commands=["login"])
def login_start(message):
    waiting_for_login_phone.add(message.chat.id)
    bot.reply_to(
        message,
        "شماره اکانتی که می‌خوای واردش بشی رو بفرست (با کد کشور، مثلاً 989123456789).\n"
        "بعدش کد تاییدی که برای همون اکانت میاد رو از من می‌خوام.\n\n"
        "اگه خطا خوردی، با /debugrubpy می‌تونی امضای دقیق متدهای لاگین رو ببینی.",
    )


@bot.message_handler(func=lambda m: m.chat.id in waiting_for_login_phone, content_types=["text"])
def login_process_phone(message):
    chat_id = message.chat.id
    waiting_for_login_phone.discard(chat_id)

    phone = message.text.strip()
    status_msg = bot.reply_to(message, "در حال ارسال درخواست کد تایید...")

    ok, client, send_code_result, detail = start_login(phone)
    if not ok:
        bot.edit_message_text(
            f"ارسال کد ناموفق بود: {detail}\nبا /debugrubpy امضای واقعی متدها رو چک کن.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return

    pending_login_by_chat[chat_id] = {
        "client": client,
        "phone": phone,
        "send_code_result": send_code_result,
    }
    bot.edit_message_text(
        f"{detail}. حالا کدی که برای اکانت {phone} اومد رو بفرست.",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id,
    )


@bot.message_handler(func=lambda m: m.chat.id in pending_login_by_chat, content_types=["text"])
def login_process_code(message):
    chat_id = message.chat.id
    pending = pending_login_by_chat.pop(chat_id, None)
    if pending is None:
        return

    code = message.text.strip()
    status_msg = bot.reply_to(message, "در حال بررسی کد...")

    ok, auth, detail = submit_login_code(
        pending["client"], pending["phone"], code, pending["send_code_result"]
    )
    if not ok:
        bot.edit_message_text(
            f"ورود ناموفق بود: {detail}",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return

    if auth:
        bot.edit_message_text(
            f"{detail} ✅\n\nauth این اکانت:\n`{auth}`\n\n"
            "این رو جایی امن نگه دار (توی پیام‌های دیگه پاکش کن).",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown",
        )
    else:
        bot.edit_message_text(detail, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


def parse_auth_list(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            data = json.loads(raw)
            return [str(item).strip() for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass
    cleaned = raw.replace("[", "").replace("]", "")
    parts = re.split(r"[\n,]+", cleaned)
    return [p.strip().strip('"').strip("'") for p in parts if p.strip().strip('"').strip("'")]


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "سلام 👋\n"
        "با /check شروع کن، بعد Authهات رو بفرست — هر کدوم توی یه خط جدا.\n\n"
        "اگه auth یه اکانت رو نداری، با /login شماره + کد تایید بده تا "
        "auth رو برات بسازم.\n\n"
        "اگه چیزی خطا داد، با /debugrubpy امضای واقعی متدهای rubpy رو ببین.",
    )


@bot.message_handler(commands=["check"])
def ask_auths(message):
    waiting_for_auths.add(message.chat.id)
    bot.reply_to(message, "باشه، حالا Authهات رو بفرست (هر کدوم توی یه خط جدا).")


@bot.message_handler(func=lambda m: m.chat.id in waiting_for_auths, content_types=["text"])
def process_auths(message):
    waiting_for_auths.discard(message.chat.id)

    auths = parse_auth_list(message.text)
    if not auths:
        bot.reply_to(message, "چیزی دریافت نشد. دوباره /check رو بزن.")
        return

    status_msg = bot.reply_to(message, f"در حال بررسی {len(auths)} اکانت...")

    healthy = 0
    healthy_auths = []
    lines = []
    for i, auth in enumerate(auths, start=1):
        ok, detail = check_single_auth(auth)
        if ok:
            healthy += 1
            healthy_auths.append(auth)
            lines.append(f"✅ اکانت {i}: سالم")
        else:
            lines.append(f"❌ اکانت {i}: مشکل دارد ({detail})")

    summary = (
        f"نتیجه بررسی:\n\n" + "\n".join(lines)
        + f"\n\n📊 جمع‌بندی: {healthy} از {len(auths)} اکانت سالم و قابل‌استفاده‌ست."
    )
    bot.edit_message_text(summary, chat_id=status_msg.chat.id, message_id=status_msg.message_id)

    if healthy_auths:
        healthy_auths_by_chat[message.chat.id] = healthy_auths
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("بله، اد کن ✅", callback_data="join_yes"),
            types.InlineKeyboardButton("نه", callback_data="join_no"),
        )
        bot.send_message(
            message.chat.id,
            f"می‌خوای {healthy} اکانت سالم رو عضو یه کانال کنم؟",
            reply_markup=markup,
        )


@bot.callback_query_handler(func=lambda c: c.data in ("join_yes", "join_no"))
def handle_join_choice(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id

    if call.data == "join_no":
        healthy_auths_by_chat.pop(chat_id, None)
        bot.edit_message_text("باشه، کاری انجام نشد.", chat_id=chat_id, message_id=call.message.message_id)
        return

    if chat_id not in healthy_auths_by_chat:
        bot.send_message(chat_id, "لیست اکانت سالمی پیدا نشد. دوباره /check رو بزن.")
        return

    waiting_for_channel.add(chat_id)
    bot.edit_message_text(
        "آیدی یا لینک کانال روبیکا رو بفرست.",
        chat_id=chat_id,
        message_id=call.message.message_id,
    )


def join_channel_with_auth(auth: str, channel_id: str) -> tuple[bool, str]:
    async def _join():
        client = Client(name="temp_join_session", auth=auth.strip())
        connect_fn = getattr(client, "connect", None)
        if callable(connect_fn):
            await connect_fn()
        try:
            join_fn = None
            for name in dir(client):
                if "join" in name.lower() and callable(getattr(client, name)):
                    join_fn = getattr(client, name)
                    break
            if join_fn is None:
                raise AttributeError("هیچ متد join روی Client پیدا نشد")
            kwargs = build_kwargs(join_fn, {"channel": channel_id, "guid": channel_id, "link": channel_id})
            if kwargs:
                await join_fn(**kwargs)
            else:
                await join_fn(channel_id)
        finally:
            await _disconnect_client(client)

    try:
        run_async(_join())
        return True, "عضو شد"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@bot.message_handler(func=lambda m: m.chat.id in waiting_for_channel, content_types=["text"])
def process_channel_join(message):
    chat_id = message.chat.id
    waiting_for_channel.discard(chat_id)

    channel_id = message.text.strip()
    healthy_auths = healthy_auths_by_chat.pop(chat_id, [])
    if not healthy_auths:
        bot.reply_to(message, "لیست اکانت سالمی پیدا نشد. دوباره /check رو بزن.")
        return

    status_msg = bot.reply_to(message, f"در حال اد کردن {len(healthy_auths)} اکانت به کانال...")

    joined = 0
    lines = []
    for i, auth in enumerate(healthy_auths, start=1):
        ok, detail = join_channel_with_auth(auth, channel_id)
        if ok:
            joined += 1
            lines.append(f"✅ اکانت {i}: {detail}")
        else:
            lines.append(f"❌ اکانت {i}: {detail}")

    summary = (
        f"نتیجه اد کردن به کانال:\n\n" + "\n".join(lines)
        + f"\n\n📊 جمع‌بندی: {joined} از {len(healthy_auths)} اکانت با موفقیت اد شدن."
    )
    bot.edit_message_text(summary, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


if __name__ == "__main__":
    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        log.warning("BOT_TOKEN تنظیم نشده! توی Railway، متغیر محیطی BOT_TOKEN رو ست کن.")
    log.info("ربات در حال اجراست...")
    bot.infinity_polling()
  
