"""
ربات تلگرامی لاگین شخصی روبیکا (نسخه rubpy) — single-account
--------------------------------------------------------------
این نسخه عمداً محدود شده به «فقط یک اکانت، فقط لاگین با شماره خودت»:
  - هیچ ورودی برای لیست auth وجود ندارد.
  - هیچ حلقه‌ی چک گروهی / join گروهی وجود ندارد.
  - در هر لحظه فقط یک فرآیند لاگین (برای یک شماره) در جریان است.
  - هیچ auth/session ای غیر از همانی که خودِ کاربر همین الان با کد
    تایید خودش دریافت می‌کند، ساخته یا ذخیره نمی‌شود.
  - کد تایید و مقدار auth هرگز در لاگ سرور چاپ نمی‌شوند؛ فقط در پیام
    خصوصی تلگرام به همان کاربر برگردانده می‌شوند.

نیازمندی‌ها (requirements.txt):
    pyTelegramBotAPI
    rubpy
    pycryptodome

متغیرهای محیطی:
    BOT_TOKEN   (اجباری) توکن ربات تلگرام
    OWNER_ID    (اختیاری) اگر ست بشه، فقط همین chat_id اجازه‌ی استفاده از
                /login رو داره — برای اینکه ربات فقط ابزار شخصی خودت بمونه.
"""

import os
import re
import asyncio
import inspect
import logging
import telebot

try:
    from rubpy import Client
except ImportError:
    Client = None

try:
    from Crypto.PublicKey import RSA
    from Crypto.Util.asn1 import DerSequence
    import base64
except ImportError:
    RSA = None
    DerSequence = None

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("personal-login-rubpy")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
OWNER_ID = os.environ.get("OWNER_ID")  # اختیاری: اگر ست بشه، فقط این chat_id اجازه داره
bot = telebot.TeleBot(BOT_TOKEN)

SESSIONS_DIR = "login_sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# فقط یک فرآیند لاگین در هر لحظه، برای هر chat — نه لیست، نه دیکشنری از چند اکانت
_pending_login = {}  # chat_id -> {"session_name", "phone", "send_code_result", "public_pem", "private_pem"}


def _authorized(chat_id) -> bool:
    if not OWNER_ID:
        return True
    return str(chat_id) == str(OWNER_ID)


def run_async(coro):
    return asyncio.run(coro)


def strip_pem_headers(pem: str) -> str:
    lines = [
        line.strip() for line in pem.strip().splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    return "".join(lines)


def generate_rsa_keypair() -> tuple[str, str]:
    """
    جفت‌کلید RSA (۱۰۲۴ بیتی) برای همین یک لاگین می‌سازه.
    public_key به فرمت خام PKCS#1 (SEQUENCE{n, e}) بدون هدر PEM، چون
    پروتکل روبیکا این فرمت رو برای sign_in می‌خواد.
    """
    if RSA is None:
        raise RuntimeError("pycryptodome نصب نیست")
    key = RSA.generate(1024)
    private_pem = key.export_key().decode()
    pub = key.publickey()
    der_pkcs1 = DerSequence([pub.n, pub.e]).encode()
    public_key_pkcs1_b64 = base64.b64encode(der_pkcs1).decode()
    return private_pem, public_key_pkcs1_b64


def extract_field(obj, candidates: list[str]):
    for name in candidates:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


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


def start_login(phone: str):
    """مرحله اول: ساخت کلید RSA و ارسال کد تایید برای همین یک شماره."""
    if Client is None:
        return False, None, None, None, None, "کتابخونه rubpy نصب نیست"

    phone = phone.strip()
    session_name = re.sub(r"[^0-9]", "", phone) or "unknown"

    try:
        private_pem, public_pem = generate_rsa_keypair()
    except Exception as e:
        return False, None, None, None, None, f"ساخت کلید RSA شکست خورد: {type(e).__name__}: {e}"

    async def _start():
        client = await _make_client(f"login_{session_name}")
        try:
            result = await client.send_code(phone_number=phone)
            return result
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_start())
        # لاگ فقط وضعیت رو ثبت می‌کنه، نه محتوای خام (که ممکنه هش/کد داشته باشه)
        log.info("send_code برای شماره ختم‌شده به %s با موفقیت اجرا شد", phone[-4:])
        return True, session_name, result, private_pem, public_pem, "کد تایید ارسال شد"
    except Exception as e:
        return False, None, None, None, None, f"{type(e).__name__}: {e}"


def submit_login_code(session_name: str, phone: str, code: str, send_code_result, public_key_pem: str):
    """مرحله دوم: تایید کد برای همون یک شماره، روی یه کلاینت تازه."""
    phone_code_hash = extract_field(
        send_code_result, ["phone_code_hash", "code_hash", "hash"]
    )
    if not phone_code_hash:
        return False, None, (
            "phone_code_hash رو از خروجی send_code پیدا نکردم.\n"
            "برای دیباگ، از /debugsource برای دیدن سورس واقعی send_code/sign_in "
            "و ساختار دقیق فیلدهای خروجی استفاده کن (این دستور کد/سشن رو چاپ نمی‌کنه)."
        )

    async def _submit():
        client = await _make_client(f"login_{session_name}")
        try:
            result = await client.sign_in(
                phone_code=code,
                phone_number=phone,
                phone_code_hash=phone_code_hash,
                public_key=public_key_pem,
            )
            return result
        finally:
            await _disconnect_client(client)

    try:
        result = run_async(_submit())
        auth_val = extract_field(result, ["auth", "auth_key", "key"])
        # هرگز کد تایید یا auth رو لاگ نکن — فقط وضعیت موفقیت رو
        log.info("sign_in اجرا شد؛ auth %s.", "پیدا شد" if auth_val else "پیدا نشد")
        if auth_val:
            return True, str(auth_val), "ورود موفق"
        return True, None, (
            "ورود ظاهراً موفق بود ولی فیلد auth رو خودکار پیدا نکردم.\n"
            "با /debugsource ساختار دقیق خروجی sign_in رو (بدون افشای خودِ کد/سشن) بررسی کن."
        )
    except Exception as e:
        log.warning("sign_in شکست خورد: %s", type(e).__name__)
        return False, None, f"{type(e).__name__}: {e}"


def debug_rubpy_signatures() -> str:
    """امضای متدهای لاگین رو از کتابخونه‌ی نصب‌شده چاپ می‌کنه (بدون داده‌ی حساس)."""
    if Client is None:
        return "کتابخونه rubpy نصب نیست."
    try:
        client = Client(name=os.path.join(SESSIONS_DIR, "debug_probe"))
    except Exception as e:
        return f"ساخت Client شکست خورد: {type(e).__name__}: {e}"

    interesting = [n for n in dir(client) if any(k in n.lower() for k in
                   ("send_code", "sign_in", "login", "connect", "get_me", "key", "rsa"))]
    lines = []
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
                lines.append(f"• {name}(...)  [متد]")
        else:
            lines.append(f"• {name} = {attr!r}  [اتریبیوت]")
    return "امضای متدهای مرتبط با لاگین:\n" + "\n".join(lines)


@bot.message_handler(commands=["debugrubpy"])
def debugrubpy_cmd(message):
    if not _authorized(message.chat.id):
        return
    bot.reply_to(message, debug_rubpy_signatures())


@bot.message_handler(commands=["debugsource"])
def debugsource_cmd(message):
    """سورس واقعی send_code/sign_in رو چاپ می‌کنه (فقط کد کتابخونه، نه داده‌ی کاربر)."""
    if not _authorized(message.chat.id):
        return
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
        if fn is None:
            report.append(f"--- {name}: پیدا نشد ---")
            continue
        try:
            src = inspect.getsource(fn)
        except Exception as e:
            src = f"(سورس در دسترس نبود: {type(e).__name__}: {e})"
        report.append(f"--- {name} ---\n{src}")

    full = "\n\n".join(report)
    max_len = 3500
    for i in range(0, len(full), max_len):
        bot.send_message(message.chat.id, full[i:i + max_len])


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "سلام 👋\n"
        "این ربات فقط برای لاگین به یک اکانت شخصی روبیکا (با شماره و کد خودت) است.\n"
        "با /login شروع کن.\n\n"
        "اگه خطا خوردی: /debugrubpy یا /debugsource (این دو فقط ساختار "
        "کتابخونه رو نشون می‌دن، هیچ داده‌ی حساسی چاپ نمی‌کنن).",
    )


@bot.message_handler(commands=["login"])
def login_start(message):
    chat_id = message.chat.id
    if not _authorized(chat_id):
        bot.reply_to(message, "این ربات فقط برای مالک آن قابل استفاده است.")
        return
    if chat_id in _pending_login:
        bot.reply_to(message, "یه فرآیند لاگین قبلاً در جریانه. اول کدش رو بفرست یا /cancel بزن.")
        return

    _pending_login[chat_id] = {"stage": "await_phone"}
    bot.reply_to(
        message,
        "شماره اکانت خودت رو با کد کشور بفرست (مثلاً 989123456789).\n"
        "این فقط برای لاگین به همین یک اکانتِ خودت استفاده می‌شه.",
    )


@bot.message_handler(commands=["cancel"])
def login_cancel(message):
    chat_id = message.chat.id
    if _pending_login.pop(chat_id, None) is not None:
        bot.reply_to(message, "فرآیند لاگین لغو شد.")
    else:
        bot.reply_to(message, "چیزی برای لغو کردن نیست.")


@bot.message_handler(
    func=lambda m: _pending_login.get(m.chat.id, {}).get("stage") == "await_phone",
    content_types=["text"],
)
def login_process_phone(message):
    chat_id = message.chat.id
    phone = message.text.strip()
    status_msg = bot.reply_to(message, "در حال ارسال درخواست کد تایید...")

    ok, session_name, send_code_result, private_pem, public_pem, detail = start_login(phone)
    if not ok:
        _pending_login.pop(chat_id, None)
        bot.edit_message_text(
            f"ارسال کد ناموفق بود: {detail}",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return

    _pending_login[chat_id] = {
        "stage": "await_code",
        "session_name": session_name,
        "phone": phone,
        "send_code_result": send_code_result,
        "public_pem": public_pem,
    }
    bot.edit_message_text(
        f"{detail}. حالا کدی که برای همین اکانت اومد رو بفرست.\n"
        "(اگه کار نکرد، /cancel بزن و دوباره امتحان کن.)",
        chat_id=status_msg.chat.id,
        message_id=status_msg.message_id,
    )


@bot.message_handler(
    func=lambda m: _pending_login.get(m.chat.id, {}).get("stage") == "await_code",
    content_types=["text"],
)
def login_process_code(message):
    chat_id = message.chat.id
    pending = _pending_login.pop(chat_id, None)
    if pending is None:
        return

    code = message.text.strip()
    status_msg = bot.reply_to(message, "در حال بررسی کد...")

    ok, auth, detail = submit_login_code(
        pending["session_name"], pending["phone"], code,
        pending["send_code_result"], pending["public_pem"],
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
            f"{detail} ✅\n\nauth اکانت شما:\n`{auth}`\n\n"
            "این پیام را در جای امنی ذخیره کن و بعد از کپی، همین پیام را از چت پاک کن.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown",
        )
    else:
        bot.edit_message_text(detail, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


if __name__ == "__main__":
    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        log.warning("BOT_TOKEN تنظیم نشده! متغیر محیطی BOT_TOKEN رو ست کن.")
    if not OWNER_ID:
        log.warning("OWNER_ID ست نشده — هرکسی که چت رو استارت کنه می‌تونه از /login استفاده کنه.")
    log.info("ربات در حال اجراست...")
    bot.infinity_polling()
  
