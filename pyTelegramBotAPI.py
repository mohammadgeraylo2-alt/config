import os
import json
import logging
from typing import List, Dict, Tuple

import telebot
from telebot import types

from pyrubi import Client


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN تنظیم نشده است.")

bot = telebot.TeleBot(BOT_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("rubika-bot")


# ============================================================
# MEMORY STATE
# ============================================================

waiting_sessions = set()
waiting_channel = set()

healthy_sessions = {}


# ============================================================
# CLIENT
# ============================================================

def create_client(auth: str, private: str):
    """
    Pyrubi 3.6.0:
        Client(auth=..., private=...)
    """

    return Client(
        auth=auth.strip(),
        private=private.strip()
    )


# ============================================================
# PARSE
# ============================================================

def parse_sessions(text: str) -> List[Dict[str, str]]:

    text = text.strip()

    if not text:
        return []

    # JSON
    if text.startswith("["):

        try:
            data = json.loads(text)

            result = []

            for item in data:

                if not isinstance(item, dict):
                    continue

                auth = str(
                    item.get("auth", "")
                ).strip()

                private = str(
                    item.get("private", "")
                ).strip()

                if auth and private:
                    result.append({
                        "auth": auth,
                        "private": private
                    })

            return result

        except Exception:
            pass

    # auth|private
    result = []

    for line in text.splitlines():

        line = line.strip()

        if not line or "|" not in line:
            continue

        auth, private = line.split("|", 1)

        auth = auth.strip()
        private = private.strip()

        if auth and private:
            result.append({
                "auth": auth,
                "private": private
            })

    return result


# ============================================================
# CHECK SESSION
# ============================================================

def check_session(
    auth: str,
    private: str
) -> Tuple[bool, str]:

    try:

        client = create_client(
            auth,
            private
        )

        # get_me اگر موجود باشد
        method = getattr(
            client,
            "get_me",
            None
        )

        if callable(method):

            result = method()

            if result is not None:
                return True, "سالم"

        # fallback
        method = getattr(
            client,
            "get_chats",
            None
        )

        if callable(method):

            try:
                result = method(limit=1)
            except TypeError:
                result = method()

            if result is not None:
                return True, "سالم"

        return False, "متد بررسی حساب پیدا نشد"

    except Exception as e:

        log.exception(
            "check_session failed"
        )

        return False, (
            f"{type(e).__name__}: "
            f"{str(e)[:300]}"
        )


# ============================================================
# JOIN
# ============================================================

def join_channel(
    auth: str,
    private: str,
    channel: str
) -> Tuple[bool, str]:

    try:

        client = create_client(
            auth,
            private
        )

        channel = channel.strip()

        # ----------------------------------------------------
        # لینک دعوت
        # ----------------------------------------------------

        if (
            "joinc/" in channel.lower()
            or "/join/" in channel.lower()
        ):

            method = getattr(
                client,
                "join_channel_by_link",
                None
            )

            if callable(method):

                method(channel)

                return True, "عضو شد"

        # ----------------------------------------------------
        # GUID
        # ----------------------------------------------------

        method = getattr(
            client,
            "join_channel_by_guid",
            None
        )

        if callable(method):

            method(channel)

            return True, "عضو شد"

        # ----------------------------------------------------
        # generic
        # ----------------------------------------------------

        method = getattr(
            client,
            "join_channel",
            None
        )

        if callable(method):

            method(channel)

            return True, "عضو شد"

        return False, (
            "متد مناسب Join در نسخه "
            "نصب‌شده Pyrubi پیدا نشد."
        )

    except Exception as e:

        log.exception(
            "join_channel failed"
        )

        return False, (
            f"{type(e).__name__}: "
            f"{str(e)[:300]}"
        )


# ============================================================
# START
# ============================================================

@bot.message_handler(
    commands=["start", "help"]
)
def start(message):

    bot.reply_to(
        message,
        "سلام 👋\n\n"
        "برای بررسی session:\n"
        "/check\n\n"
        "فرمت:\n"
        "auth|private\n"
        "auth|private\n\n"
        "⚠️ شماره، کد ورود یا private key را "
        "در چت عمومی ارسال نکن."
    )


# ============================================================
# CHECK
# ============================================================

@bot.message_handler(
    commands=["check"]
)
def check_command(message):

    waiting_sessions.add(
        message.chat.id
    )

    bot.reply_to(
        message,
        "sessionها را بفرست.\n\n"
        "هر اکانت در یک خط:\n\n"
        "auth|private"
    )


@bot.message_handler(
    func=lambda m:
        m.chat.id in waiting_sessions,
    content_types=["text"]
)
def receive_sessions(message):

    chat_id = message.chat.id

    waiting_sessions.discard(
        chat_id
    )

    sessions = parse_sessions(
        message.text
    )

    if not sessions:

        bot.reply_to(
            message,
            "فرمت session درست نیست.\n\n"
            "باید این‌طور باشد:\n"
            "auth|private"
        )

        return

    if len(sessions) > 50:

        bot.reply_to(
            message,
            "حداکثر ۵۰ session در هر مرحله."
        )

        return

    status = bot.reply_to(
        message,
        f"در حال بررسی {len(sessions)} اکانت..."
    )

    healthy = []

    lines = []

    for i, session in enumerate(
        sessions,
        1
    ):

        ok, detail = check_session(
            session["auth"],
            session["private"]
        )

        if ok:

            healthy.append(
                session
            )

            lines.append(
                f"✅ اکانت {i}: سالم"
            )

        else:

            lines.append(
                f"❌ اکانت {i}: {detail}"
            )

    result = (
        "نتیجه بررسی:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"📊 سالم: {len(healthy)} "
          f"از {len(sessions)}"
    )

    bot.edit_message_text(
        result,
        chat_id=chat_id,
        message_id=status.message_id
    )

    if not healthy:
        return

    healthy_sessions[
        chat_id
    ] = healthy

    keyboard = types.InlineKeyboardMarkup()

    keyboard.row(
        types.InlineKeyboardButton(
            "Join کانال ✅",
            callback_data="join_yes"
        ),
        types.InlineKeyboardButton(
            "لغو ❌",
            callback_data="join_no"
        )
    )

    bot.send_message(
        chat_id,
        f"{len(healthy)} اکانت سالم است.\n"
        "می‌خواهی وارد کانال شوند؟",
        reply_markup=keyboard
    )


# ============================================================
# JOIN CONFIRMATION
# ============================================================

@bot.callback_query_handler(
    func=lambda c:
        c.data in (
            "join_yes",
            "join_no"
        )
)
def join_confirmation(call):

    chat_id = call.message.chat.id

    bot.answer_callback_query(
        call.id
    )

    if call.data == "join_no":

        healthy_sessions.pop(
            chat_id,
            None
        )

        bot.edit_message_text(
            "عملیات لغو شد.",
            chat_id=chat_id,
            message_id=call.message.message_id
        )

        return

    if chat_id not in healthy_sessions:

        bot.send_message(
            chat_id,
            "session سالمی وجود ندارد."
        )

        return

    waiting_channel.add(
        chat_id
    )

    bot.edit_message_text(
        "لینک دعوت یا GUID کانال را بفرست.",
        chat_id=chat_id,
        message_id=call.message.message_id
    )


# ============================================================
# RECEIVE CHANNEL
# ============================================================

@bot.message_handler(
    func=lambda m:
        m.chat.id in waiting_channel,
    content_types=["text"]
)
def receive_channel(message):

    chat_id = message.chat.id

    waiting_channel.discard(
        chat_id
    )

    channel = message.text.strip()

    sessions = healthy_sessions.pop(
        chat_id,
        []
    )

    if not sessions:

        bot.reply_to(
            message,
            "session سالمی پیدا نشد."
        )

        return

    status = bot.reply_to(
        message,
        f"در حال Join کردن "
        f"{len(sessions)} اکانت..."
    )

    success = 0
    lines = []

    for i, session in enumerate(
        sessions,
        1
    ):

        ok, detail = join_channel(
            session["auth"],
            session["private"],
            channel
        )

        if ok:

            success += 1

            lines.append(
                f"✅ اکانت {i}: {detail}"
            )

        else:

            lines.append(
                f"❌ اکانت {i}: {detail}"
            )

    result = (
        "نتیجه Join:\n\n"
        + "\n".join(lines)
        + "\n\n"
        + f"📊 موفق: {success} "
          f"از {len(sessions)}"
    )

    bot.edit_message_text(
        result,
        chat_id=chat_id,
        message_id=status.message_id
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    log.info(
        "Rubika Telegram bot started."
    )

    bot.infinity_polling(
        skip_pending=True,
        timeout=30,
        long_polling_timeout=30
    )
