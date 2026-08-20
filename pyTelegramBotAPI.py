import os
import re
import json
import logging
from typing import Optional

import telebot
from telebot import types

try:
    from pyrubi import Client
except ImportError:
    Client = None


# ============================================================
# تنظیمات
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN تنظیم نشده است. "
        "در Railway متغیر محیطی BOT_TOKEN را تنظیم کن."
    )

bot = telebot.TeleBot(BOT_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("rubika-session-checker")


# ============================================================
# State
# ============================================================

waiting_for_sessions = set()
waiting_for_channel = set()

# chat_id -> list[dict]
# هر مورد:
# {
#   "auth": "...",
#   "private": "..."
# }
healthy_sessions_by_chat = {}


# ============================================================
# ابزارهای عمومی
# ============================================================

def safe_error(exc: Exception) -> str:
    """
    خطا را کوتاه و قابل نمایش می‌کند.
    برای جلوگیری از چاپ اطلاعات حساس، خود auth/private را نمایش نمی‌دهیم.
    """
    text = str(exc).strip()

    if not text:
        return type(exc).__name__

    if len(text) > 500:
        text = text[:500] + "..."

    return f"{type(exc).__name__}: {text}"


def make_client(auth: str, private: str):
    """
    ساخت Client با روش مستند Pyrubi 3.6.0.
    """

    if Client is None:
        raise RuntimeError(
            "کتابخانه pyrubi نصب نیست. "
            "در requirements.txt آن را اضافه کن."
        )

    auth = auth.strip()
    private = private.strip()

    if not auth:
        raise ValueError("auth خالی است.")

    if not private:
        raise ValueError("private key خالی است.")

    # روش مستند Pyrubi برای session دستی
    return Client(
        auth=auth,
        private=private,
    )


# ============================================================
# Parse session
# ============================================================

def parse_sessions(raw: str):
    """
    ورودی‌های قابل قبول:

    حالت خطی:
        auth1|private1
        auth2|private2

    JSON:
        [
            {
                "auth": "AUTH1",
                "private": "PRIVATE1"
            },
            {
                "auth": "AUTH2",
                "private": "PRIVATE2"
            }
        ]

    همچنین JSON ساده:
        [
            ["AUTH1", "PRIVATE1"],
            ["AUTH2", "PRIVATE2"]
        ]
    """

    raw = raw.strip()

    if not raw:
        return []

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    if raw.startswith("[") and raw.endswith("]"):
        try:
            data = json.loads(raw)

            sessions = []

            for item in data:

                if isinstance(item, dict):
                    auth = str(item.get("auth", "")).strip()
                    private = str(item.get("private", "")).strip()

                elif isinstance(item, list) and len(item) >= 2:
                    auth = str(item[0]).strip()
                    private = str(item[1]).strip()

                else:
                    continue

                if auth and private:
                    sessions.append(
                        {
                            "auth": auth,
                            "private": private,
                        }
                    )

            return sessions

        except json.JSONDecodeError:
            pass

    # --------------------------------------------------------
    # حالت خط به خط
    # auth|private
    # --------------------------------------------------------

    sessions = []

    for line in raw.splitlines():

        line = line.strip()

        if not line:
            continue

        if "|" not in line:
            continue

        auth, private = line.split("|", 1)

        auth = auth.strip()
        private = private.strip()

        if auth and private:
            sessions.append(
                {
                    "auth": auth,
                    "private": private,
                }
            )

    return sessions


# ============================================================
# Check session
# ============================================================

def check_single_session(auth: str, private: str):
    """
    بررسی می‌کند session قابل استفاده است یا نه.

    اول get_me را امتحان می‌کند.
    اگر نسخه نصب‌شده get_me نداشت، get_chats را امتحان می‌کند.
    """

    try:

        client = make_client(auth, private)

        # روش ترجیحی
        get_me = getattr(client, "get_me", None)

        if callable(get_me):
            result = get_me()

            if result is not None:
                return True, "سالم"

            return False, "get_me پاسخ خالی داد"

        # روش جایگزین
        get_chats = getattr(client, "get_chats", None)

        if callable(get_chats):
            try:
                result = get_chats(limit=1)
            except TypeError:
                result = get_chats()

            if result is not None:
                return True, "سالم"

            return False, "get_chats پاسخ خالی داد"

        return False, "متد get_me یا get_chats در این نسخه پیدا نشد"

    except Exception as exc:

        log.exception("Session check failed")

        return False, safe_error(exc)


# ============================================================
# Join
# ============================================================

def normalize_channel_input(value: str) -> str:
    """
    لینک/شناسه کانال را کمی تمیز می‌کند.
    """

    value = value.strip()

    value = value.replace(
        "https://rubika.ir/",
        ""
    )

    value = value.replace(
        "http://rubika.ir/",
        ""
    )

    value = value.replace(
        "rubika.ir/",
        ""
    )

    return value.strip()


def join_channel_with_session(
    auth: str,
    private: str,
    channel: str,
):
    """
    تلاش برای join کردن با session.

    فقط متدی را اجرا می‌کنیم که واقعاً روی Client وجود داشته باشد.
    """

    try:

        client = make_client(auth, private)

        channel = normalize_channel_input(channel)

        if not channel:
            return False, "شناسه/لینک کانال خالی است"

        # ----------------------------------------------------
        # 1) اگر شناسه GUID داده شده باشد
        # ----------------------------------------------------

        method = getattr(
            client,
            "join_channel_by_guid",
            None,
        )

        if callable(method):

            result = method(channel)

            return True, "عضویت انجام شد"

        # ----------------------------------------------------
        # 2) اگر لینک دعوت باشد
        # ----------------------------------------------------

        method = getattr(
            client,
            "join_channel_by_link",
            None,
        )

        if callable(method):

            # اگر کاربر لینک کامل فرستاده باشد،
            # نسخه اصلی را دوباره استفاده می‌کنیم.
            result = method(channel)

            return True, "عضویت انجام شد"

        # ----------------------------------------------------
        # 3) متد عمومی
        # ----------------------------------------------------

        method = getattr(
            client,
            "join_channel",
            None,
        )

        if callable(method):

            result = method(channel)

            return True, "عضویت انجام شد"

        return (
            False,
            "هیچ‌کدام از متدهای join در نسخه نصب‌شده Pyrubi پیدا نشد.",
        )

    except Exception as exc:

        log.exception("Join failed")

        return False, safe_error(exc)


# ============================================================
# /start
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def start(message):

    text = (
        "سلام 👋\n\n"

        "این ربات sessionهای Pyrubi را بررسی می‌کند.\n\n"

        "برای شروع:\n"
        "/check\n\n"

        "بعد sessionها را هر کدام در یک خط بفرست:\n\n"

        "auth1|private1\n"
        "auth2|private2\n\n"

        "یا به صورت JSON:\n"
        '[{"auth":"AUTH","private":"PRIVATE"}]\n\n'

        "بعد از بررسی، اگر session سالم وجود داشته باشد، "
        "می‌توانی شناسه یا لینک کانال را برای عملیات join ارسال کنی.\n\n"

        "⚠️ auth و private اطلاعات حساس حساب هستند. "
        "آن‌ها را برای افراد دیگر ارسال نکن."
    )

    bot.reply_to(
        message,
        text,
    )


# ============================================================
# /check
# ============================================================

@bot.message_handler(
    commands=["check"]
)
def check_start(message):

    chat_id = message.chat.id

    waiting_for_sessions.add(chat_id)

    bot.reply_to(
        message,
        "sessionها را بفرست.\n\n"
        "فرمت:\n"
        "auth|private\n"
        "auth|private\n\n"
        "هر session در یک خط.",
    )


# ============================================================
# دریافت sessionها
# ============================================================

@bot.message_handler(
    func=lambda m: (
        m.chat.id in waiting_for_sessions
    ),
    content_types=["text"],
)
def process_sessions(message):

    chat_id = message.chat.id

    waiting_for_sessions.discard(chat_id)

    sessions = parse_sessions(
        message.text
    )

    if not sessions:

        bot.reply_to(
            message,
            "هیچ session معتبری پیدا نشد.\n\n"
            "فرمت صحیح:\n"
            "auth|private",
        )

        return

    if len(sessions) > 50:

        bot.reply_to(
            message,
            "حداکثر ۵۰ session در هر بار بررسی مجاز است.",
        )

        return

    status = bot.reply_to(
        message,
        f"در حال بررسی {len(sessions)} session...",
    )

    healthy_sessions = []

    result_lines = []

    for index, session in enumerate(
        sessions,
        start=1,
    ):

        ok, detail = check_single_session(
            session["auth"],
            session["private"],
        )

        if ok:

            healthy_sessions.append(
                session
            )

            result_lines.append(
                f"✅ session {index}: سالم"
            )

        else:

            result_lines.append(
                f"❌ session {index}: {detail}"
            )

    summary = (
        "نتیجه بررسی:\n\n"
        + "\n".join(result_lines)
        + "\n\n"
        + f"📊 {len(healthy_sessions)} "
          f"از {len(sessions)} session سالم است."
    )

    try:

        bot.edit_message_text(
            summary,
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

    except Exception:

        bot.send_message(
            chat_id,
            summary,
        )

    # --------------------------------------------------------
    # اگر session سالم داریم، مرحله join
    # --------------------------------------------------------

    if healthy_sessions:

        healthy_sessions_by_chat[
            chat_id
        ] = healthy_sessions

        markup = types.InlineKeyboardMarkup()

        markup.row(
            types.InlineKeyboardButton(
                "عضویت در کانال ✅",
                callback_data="join_yes",
            ),
            types.InlineKeyboardButton(
                "لغو",
                callback_data="join_no",
            ),
        )

        bot.send_message(
            chat_id,
            (
                f"{len(healthy_sessions)} session سالم پیدا شد.\n\n"
                "می‌خواهی با این sessionها وارد کانال شوند؟"
            ),
            reply_markup=markup,
        )


# ============================================================
# انتخاب Join
# ============================================================

@bot.callback_query_handler(
    func=lambda call: call.data in (
        "join_yes",
        "join_no",
    )
)
def handle_join_choice(call):

    chat_id = call.message.chat.id

    try:
        bot.answer_callback_query(
            call.id
        )
    except Exception:
        pass

    if call.data == "join_no":

        healthy_sessions_by_chat.pop(
            chat_id,
            None,
        )

        bot.edit_message_text(
            "باشه، عملیات join لغو شد.",
            chat_id=chat_id,
            message_id=call.message.message_id,
        )

        return

    if chat_id not in healthy_sessions_by_chat:

        bot.send_message(
            chat_id,
            "session سالمی برای این عملیات پیدا نشد. دوباره /check را بزن.",
        )

        return

    waiting_for_channel.add(
        chat_id
    )

    bot.edit_message_text(
        "حالا لینک یا شناسه کانال روبیکا را بفرست.",
        chat_id=chat_id,
        message_id=call.message.message_id,
    )


# ============================================================
# دریافت کانال
# ============================================================

@bot.message_handler(
    func=lambda m: (
        m.chat.id in waiting_for_channel
    ),
    content_types=["text"],
)
def process_channel(message):

    chat_id = message.chat.id

    waiting_for_channel.discard(
        chat_id
    )

    channel = message.text.strip()

    sessions = healthy_sessions_by_chat.pop(
        chat_id,
        [],
    )

    if not sessions:

        bot.reply_to(
            message,
            "session سالمی پیدا نشد. دوباره /check را بزن.",
        )

        return

    if not channel:

        bot.reply_to(
            message,
            "لینک یا شناسه کانال خالی است.",
        )

        return

    status = bot.reply_to(
        message,
        f"در حال بررسی {len(sessions)} session برای join...",
    )

    joined = 0

    result_lines = []

    for index, session in enumerate(
        sessions,
        start=1,
    ):

        ok, detail = join_channel_with_session(
            session["auth"],
            session["private"],
            channel,
        )

        if ok:

            joined += 1

            result_lines.append(
                f"✅ session {index}: {detail}"
            )

        else:

            result_lines.append(
                f"❌ session {index}: {detail}"
            )

    summary = (
        "نتیجه عملیات:\n\n"
        + "\n".join(result_lines)
        + "\n\n"
        + f"📊 موفق: {joined} از {len(sessions)}"
    )

    try:

        bot.edit_message_text(
            summary,
            chat_id=status.chat.id,
            message_id=status.message_id,
        )

    except Exception:

        bot.send_message(
            chat_id,
            summary,
        )


# ============================================================
# مدیریت پیام‌های اشتباه
# ============================================================

@bot.message_handler(
    content_types=["text"]
)
def fallback(message):

    chat_id = message.chat.id

    # اگر پیام مربوط به یکی از stateها نیست
    if chat_id in waiting_for_sessions:
        return

    if chat_id in waiting_for_channel:
        return

    bot.reply_to(
        message,
        "برای شروع /check را بزن.",
    )


# ============================================================
# اجرا
# ============================================================

if __name__ == "__main__":

    log.info(
        "Rubika session checker started."
    )

    log.info(
        "Pyrubi installed: %s",
        Client is not None,
    )

    try:

        bot.infinity_polling(
            skip_pending=True,
            timeout=30,
            long_polling_timeout=30,
        )

    except KeyboardInterrupt:

        log.info(
            "Bot stopped by user."
        )

    except Exception:

        log.exception(
            "Bot crashed."
        )

        raise
