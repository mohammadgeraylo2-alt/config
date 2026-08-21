"""
ربات تلگرامی چک اکانت روبیکا + لاگین با شماره (rubpy)
requirements.txt: pyTelegramBotAPI, rubpy
"""

import os
import re
import json
import asyncio
import inspect
import logging
import base64
import telebot
from telebot import types

try:
    from rubpy import Client
except ImportError:
    Client = None

try:
    from Crypto.PublicKey import RSA
    from Crypto.Util.asn1 import DerSequence
except ImportError:
    RSA = None
    DerSequence = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth-checker-rubpy")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
bot = telebot.TeleBot(BOT_TOKEN)

SESSIONS_DIR = "login_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

waiting_for_auths = set()
waiting_for_channel = set()
healthy_auths_by_chat = {}
waiting_for_login_phone = set()
pending_login_by_chat = {}  # chat_id -> {session_name, phone, send_code_result, public_pem}


def run_async(coro):
    return asyncio.run(coro)


def build_kwargs(func, concept_values: dict) -> dict:
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return {}
    kwargs = {}
    for name in sig.parameters:
        if name in ("self", "args", "kwargs"):
            continue
        for concept, value in concept_values.items():
            if concept in name.lower():
                kwargs[name] = value
                break
    return kwargs


def generate_rsa_keypair() -> tuple[str, str]:
    """کلید عمومی به فرمت خام PKCS#1 base64 (بدون هدر PEM) برمی‌گردونه — فرمتی که روبیکا می‌خواد."""
    if RSA is None:
        raise RuntimeError("pycryptodome نصب نیست")
    key = RSA.generate(1024)
    private_pem = key.export_key().decode()
    pub = key.publickey()
    der = DerSequence([pub.n, pub.e]).encode()
    return private_pem, base64.b64encode(der).decode()


def extract_field(obj, candidates: list[str]):
    for name in candidates:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name) and getattr(obj, name) is not None:
            return getattr(obj, name)
    return None


def dump_fields(obj) -> str:
    """همه‌ی فیلدهای واقعی یه شیء رو برای دیباگ چاپ می‌کنه."""
    if isinstance(obj, dict):
        return repr(obj)
    d = getattr(obj, "__dict__", None)
    if d:
        return repr(d)
    return repr(obj)


async def _make_client(session_name: str):
    if Client is None:
        raise RuntimeError("کتابخونه rubpy نصب نیست")
    client = Client(name=os.path.join(SESSIONS_DIR, session_name))
    if callable(getattr(client, "connect", None)):
        await client.connect()
    return client


async def _disconnect_client(client):
    if callable(getattr(client, "disconnect", None)):
        try:
            await client.disconnect()
        except Exception:
            pass


def check_single_auth(auth: str, private_key: str = None) -> tuple[bool, str]:
    auth = auth.strip()
    if not auth:
        return False, "خالی بود"
    if Client is None:
        return False, "کتابخونه rubpy نصب نیست"

    async def _check():
        client = Client(name="temp_check_session", auth=auth, private_key=private_key)
        if callable(getattr(client, "connect", None)):
            await client.connect()
        try:
            fn = getattr(client, "get_me", None) or getattr(client, "get_chats", None)
            if fn is None:
                raise AttributeError("نه get_me نه get_chats پیدا شد")
            return await fn()
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_check())
        return (True, "سالم") if result is not None else (False, "پاسخ خالی از سرور")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def debug_rubpy_signatures() -> str:
    if Client is None:
        return "کتابخونه rubpy نصب نیست."
    try:
        client = Client(name=os.path.join(SESSIONS_DIR, "debug_probe"))
    except Exception as e:
        return f"ساخت Client شکست خورد: {type(e).__name__}: {e}"

    keys = ("send_code", "sign_in", "login", "code", "connect", "get_me", "get_chats", "key", "rsa", "crypto")
    lines = []
    for name in [n for n in dir(client) if any(k in n.lower() for k in keys)]:
        try:
            attr = getattr(client, name)
        except Exception as e:
            lines.append(f"• {name}: خطا ({type(e).__name__})")
            continue
        if callable(attr):
            try:
                lines.append(f"• {name}{inspect.signature(attr)}  [متد]")
            except (TypeError, ValueError):
                lines.append(f"• {name}(...)  [متد]")
        else:
            lines.append(f"• {name} = {attr!r}  [اتریبیوت]")
    return "امضای متدها/اتریبیوت‌ها:\n" + "\n".join(lines)


@bot.message_handler(commands=["debugrubpy"])
def debugrubpy_cmd(message):
    bot.reply_to(message, debug_rubpy_signatures())


@bot.message_handler(commands=["debugsource"])
def debugsource_cmd(message):
    if Client is None:
        bot.reply_to(message, "کتابخونه rubpy نصب نیست.")
        return
    try:
        client = Client(name=os.path.join(SESSIONS_DIR, "debug_probe2"))
    except Exception as e:
        bot.reply_to(message, f"ساخت Client شکست خورد: {type(e).__name__}: {e}")
        return

    report = []
    for name in ("send_code", "sign_in"):
        fn = getattr(client, name, None)
        try:
            src = inspect.getsource(fn) if fn else f"{name}: پیدا نشد"
        except Exception as e:
            src = f"(سورس در دسترس نبود: {e})"
        report.append(f"--- {name} ---\n{src}")

    full = "\n\n".join(report)
    for i in range(0, len(full), 3500):
        bot.send_message(message.chat.id, full[i:i + 3500])


def start_login(phone: str):
    if Client is None:
        return False, None, None, None, None, "کتابخونه rubpy نصب نیست"

    phone = phone.strip()
    session_name = re.sub(r"[^0-9]", "", phone) or "unknown"

    try:
        private_pem, public_pem = generate_rsa_keypair()
    except Exception as e:
        return False, None, None, None, None, f"ساخت کلید RSA شکست خورد: {e}"

    async def _start():
        client = await _make_client(f"login_{session_name}")
        try:
            return await client.send_code(phone_number=phone)
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_start())
        return True, session_name, result, private_pem, public_pem, "کد تایید ارسال شد"
    except Exception as e:
        return False, None, None, None, None, f"{type(e).__name__}: {e}"


def submit_login_code(session_name: str, phone: str, code: str, send_code_result, public_key_pem: str):
    phone_code_hash = extract_field(send_code_result, ["phone_code_hash", "code_hash", "hash"])
    if not phone_code_hash:
        return False, None, (
            f"phone_code_hash پیدا نشد. فیلدهای واقعی send_code:\n{dump_fields(send_code_result)}"
        )

    async def _submit():
        client = await _make_client(f"login_{session_name}")
        try:
            return await client.sign_in(
                phone_code=code,
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                public_key=public_key_pem,
            )
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_submit())
        auth_val = extract_field(result, ["auth", "auth_key", "key"])
        if auth_val:
            return True, str(auth_val), "ورود موفق"
        return True, None, f"موفق بود ولی auth پیدا نشد. فیلدهای واقعی:\n{dump_fields(result)}"
    except Exception as e:
        return False, None, (
            f"{type(e).__name__}: {e}\n\n"
            f"phone_code_hash: {phone_code_hash!r}\n"
            f"فیلدهای send_code: {dump_fields(send_code_result)}\n"
            f"طول public_key: {len(public_key_pem)}"
        )


@bot.message_handler(commands=["login"])
def login_start(message):
    waiting_for_login_phone.add(message.chat.id)
    bot.reply_to(
        message,
        "شماره اکانت رو با کد کشور بفرست (مثلاً 989123456789).\n"
        "اگه خطا خوردی، /debugrubpy یا /debugsource رو بزن.",
    )


@bot.message_handler(func=lambda m: m.chat.id in waiting_for_login_phone, content_types=["text"])
def login_process_phone(message):
    chat_id = message.chat.id
    waiting_for_login_phone.discard(chat_id)

    phone = message.text.strip()
    status_msg = bot.reply_to(message, "در حال ارسال درخواست کد تایید...")

    ok, session_name, send_code_result, private_pem, public_pem, detail = start_login(phone)
    if not ok:
        bot.edit_message_text(f"ارسال کد ناموفق بود: {detail}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    pending_login_by_chat[chat_id] = {
        "session_name": session_name,
        "phone": phone,
        "send_code_result": send_code_result,
        "public_pem": public_pem,
    }
    bot.edit_message_text(
        f"{detail}. حالا کدی که برای اکانت {phone} اومد رو بفرست.",
        chat_id=status_msg.chat.id, message_id=status_msg.message_id,
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
        pending["session_name"], pending["phone"], code,
        pending["send_code_result"], pending["public_pem"],
    )
    if not ok:
        bot.edit_message_text(f"ورود ناموفق بود: {detail}", chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        return

    if auth:
        bot.edit_message_text(
            f"{detail} ✅\n\nauth این اکانت:\n`{auth}`\n\nاین رو جایی امن نگه دار.",
            chat_id=status_msg.chat.id, message_id=status_msg.message_id, parse_mode="Markdown",
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
        "سلام 👋\n/check برای بررسی auth، /login برای ساخت auth جدید با شماره.\n"
        "برای دیباگ: /debugrubpy یا /debugsource",
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

    healthy, healthy_auths, lines = 0, [], []
    for i, auth in enumerate(auths, start=1):
        ok, detail = check_single_auth(auth)
        if ok:
            healthy += 1
            healthy_auths.append(auth)
            lines.append(f"✅ اکانت {i}: سالم")
        else:
            lines.append(f"❌ اکانت {i}: مشکل دارد ({detail})")

    summary = "نتیجه بررسی:\n\n" + "\n".join(lines) + f"\n\n📊 جمع‌بندی: {healthy} از {len(auths)} سالم."
    bot.edit_message_text(summary, chat_id=status_msg.chat.id, message_id=status_msg.message_id)

    if healthy_auths:
        healthy_auths_by_chat[message.chat.id] = healthy_auths
        markup = types.InlineKeyboardMarkup()
        markup.add(
            types.InlineKeyboardButton("بله، اد کن ✅", callback_data="join_yes"),
            types.InlineKeyboardButton("نه", callback_data="join_no"),
        )
        bot.send_message(message.chat.id, f"می‌خوای {healthy} اکانت سالم رو عضو یه کانال کنم؟", reply_markup=markup)


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
    bot.edit_message_text("آیدی یا لینک کانال روبیکا رو بفرست.", chat_id=chat_id, message_id=call.message.message_id)


def join_channel_with_auth(auth: str, channel_id: str) -> tuple[bool, str]:
    async def _join():
        client = Client(name="temp_join_session", auth=auth.strip())
        if callable(getattr(client, "connect", None)):
            await client.connect()
        try:
            join_fn = next(
                (getattr(client, n) for n in dir(client) if "join" in n.lower() and callable(getattr(client, n))),
                None,
            )
            if join_fn is None:
                raise AttributeError("هیچ متد join پیدا نشد")
            kwargs = build_kwargs(join_fn, {"channel": channel_id, "guid": channel_id, "link": channel_id})
            await (join_fn(**kwargs) if kwargs else join_fn(channel_id))
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

    joined, lines = 0, []
    for i, auth in enumerate(healthy_auths, start=1):
        ok, detail = join_channel_with_auth(auth, channel_id)
        if ok:
            joined += 1
        lines.append(f"{'✅' if ok else '❌'} اکانت {i}: {detail}")

    summary = "نتیجه اد کردن:\n\n" + "\n".join(lines) + f"\n\n📊 {joined} از {len(healthy_auths)} موفق."
    bot.edit_message_text(summary, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


if __name__ == "__main__":
    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        log.warning("BOT_TOKEN تنظیم نشده!")
    log.info("ربات در حال اجراست...")
    bot.infinity_polling()
  
