"""
ربات تلگرامی چک‌کننده اعتبار اکانت‌های روبیکا
----------------------------------------------
کاربرد: چند تا Auth روبیکا رو می‌فرستی، ربات با هر کدوم یه عملیات ساده
(مثل خوندن پروفایل خودت) امتحان می‌کنه و بهت میگه چندتاش سالم و قابل‌استفاده‌ست.

نیازمندی‌ها (requirements.txt):
    pyTelegramBotAPI
    pyrubi

بعد از بررسی، اگه حداقل یه اکانت سالم بود، ربات می‌پرسه می‌خوای اکانت‌های
سالم رو عضو یه کانال کنه یا نه. اگه آره رو زدی، آیدی/لینک کانال رو می‌فرستی
و ربات با همون اکانت‌های سالم توش join می‌کنه.

نکته مهم: pyrubi مستندسازی ضعیفی داره (طبق تجربه قبلیت با این کتابخونه‌ها).
اگه get_chats یا get_me یا join_channel متد درستی نبود، توی خط‌های
CHECK_METHOD و JOIN_METHOD پایین باید با متد درست جایگزینش کنی.
ساختار try/except طوری نوشته شده که فقط همون یه خط رو عوض کنی،
بقیه کد دست‌نخورده می‌مونه.
"""

import os
import re
import json
import logging
import telebot
from telebot import types

try:
    from pyrubi import Client
except ImportError:
    Client = None  # اگه ایمپورت نشد، پیام واضح می‌دیم

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("auth-checker")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
bot = telebot.TeleBot(BOT_TOKEN)

# state ساده در حافظه: منتظر لیست auth از کاربر هستیم یا نه
waiting_for_auths = set()

# منتظر آیدی کانال هستیم یا نه (بعد از تایید join)
waiting_for_channel = set()

# آخرین لیست authهای سالم هر چت، برای استفاده در مرحله join
healthy_auths_by_chat = {}


def check_single_auth(auth: str) -> tuple[bool, str]:
    """
    یه auth رو با یه عملیات سبک تست می‌کنه.
    خروجی: (سالم بود یا نه, توضیح کوتاه خطا در صورت مشکل)
    """
    auth = auth.strip()
    if not auth:
        return False, "خالی بود"

    if Client is None:
        return False, "کتابخونه pyrubi نصب نیست"

    try:
        client = Client(auth)

        # ==== CHECK_METHOD: همین خط رو در صورت نیاز عوض کن ====
        # گزینه‌های رایج pyrubi برای تست سبک بودن سشن:
        #   client.get_chats(limit=1)
        #   client.get_me()
        # اگه هرکدوم AttributeError داد، متد دیگه رو جایگزین کن.
        result = client.get_chats(limit=1)
        # ======================================================

        if result is not None:
            return True, "سالم"
        return False, "پاسخ خالی از سرور"

    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def join_channel_with_auth(auth: str, channel_id: str) -> tuple[bool, str]:
    """
    با یه auth سالم، سعی می‌کنه عضو کانال داده‌شده بشه.
    خروجی: (موفق بود یا نه, توضیح کوتاه)
    """
    try:
        client = Client(auth.strip())

        # ==== JOIN_METHOD: همین خط رو در صورت نیاز عوض کن ====
        # گزینه‌های رایج pyrubi برای join کردن به کانال:
        #   client.join_channel_by_guid(channel_id)
        #   client.join_channel(channel_id)          # اگه لینک/یوزرنیم بگیره
        #   client.join_channel_by_link(channel_id)  # اگه لینک دعوت بگیره
        # بسته به نسخه‌ی pyrubi که نصب داری، AttributeError می‌ده و باید
        # متد درست رو جایگزین کنی.
        client.join_channel_by_guid(channel_id)
        # ======================================================

        return True, "عضو شد"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def debug_join_methods(auth: str, channel_id: str) -> str:
    """
    همه‌ی متدهای مرتبط با join روی Client رو پیدا می‌کنه، هر کدوم رو با
    channel_id امتحان می‌کنه و نتیجه/خطای خام هر کدوم رو برمی‌گردونه.
    فقط برای دیباگ استفاده می‌شه، نه برای join واقعی توی فلو اصلی.
    """
    if Client is None:
        return "کتابخونه pyrubi نصب نیست."

    try:
        client = Client(auth.strip())
    except Exception as e:
        return f"ساخت Client شکست خورد: {type(e).__name__}: {e}"

    candidate_names = [name for name in dir(client) if "join" in name.lower()]
    if not candidate_names:
        return "هیچ متدی با اسم شامل 'join' روی Client پیدا نشد."

    report_lines = [f"متدهای پیدا‌شده: {', '.join(candidate_names)}", ""]

    for name in candidate_names:
        method = getattr(client, name)
        if not callable(method):
            report_lines.append(f"• {name}: قابل فراخوانی نیست (متد نیست)")
            continue
        try:
            result = method(channel_id)
            report_lines.append(f"• {name}({channel_id!r}) → موفق ✅\n  خروجی خام: {result!r}")
        except TypeError as e:
            # احتمالاً امضای متد فرق داره (پارامتر دیگه‌ای می‌خواد)
            report_lines.append(f"• {name}({channel_id!r}) → خطای امضا (TypeError): {e}")
        except Exception as e:
            report_lines.append(f"• {name}({channel_id!r}) → خطا: {type(e).__name__}: {e}")

    return "\n".join(report_lines)


@bot.message_handler(commands=["debugjoin"])
def debugjoin_start(message):
    msg = bot.reply_to(
        message,
        "برای دیباگ، یه auth و آیدی/لینک کانال رو توی دو خط جدا بفرست:\n"
        "auth\n"
        "channel_id",
    )
    bot.register_next_step_handler(msg, debugjoin_process)


def debugjoin_process(message):
    parts = [line.strip() for line in message.text.splitlines() if line.strip()]
    if len(parts) < 2:
        bot.reply_to(message, "باید دو خط بفرستی: auth و بعد آیدی/لینک کانال.")
        return

    auth, channel_id = parts[0], parts[1]
    status_msg = bot.reply_to(message, "در حال تست متدهای join روی این auth...")

    report = debug_join_methods(auth, channel_id)

    # پیام تلگرام محدودیت طول داره، برای گزارش طولانی تقسیمش می‌کنیم
    max_len = 3500
    for chunk_start in range(0, len(report), max_len):
        chunk = report[chunk_start:chunk_start + max_len]
        if chunk_start == 0:
            bot.edit_message_text(chunk, chat_id=status_msg.chat.id, message_id=status_msg.message_id)
        else:
            bot.send_message(status_msg.chat.id, chunk)


def parse_auth_list(raw: str) -> list[str]:
    """
    ورودی رو به لیست auth تبدیل می‌کنه. هم فرمت خط‌به‌خط رو قبول می‌کنه
    و هم فرمت آرایه‌ی JSON مثل: ["auth1","auth2","auth3"]
    """
    raw = raw.strip()

    if raw.startswith("[") and raw.endswith("]"):
        try:
            data = json.loads(raw)
            return [str(item).strip() for item in data if str(item).strip()]
        except json.JSONDecodeError:
            pass  # اگه JSON معتبر نبود، می‌ریم سراغ روش دستی زیر

    # حذف براکت اضافی (اگه بود) و جدا کردن با خط جدید یا کاما
    cleaned = raw.replace("[", "").replace("]", "")
    parts = re.split(r"[\n,]+", cleaned)
    return [p.strip().strip('"').strip("'") for p in parts if p.strip().strip('"').strip("'")]


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "سلام 👋\n"
        "با /check شروع کن، بعد Authهات رو بفرست — هر کدوم توی یه خط جدا، "
        "یا به شکل آرایه مثل:\n"
        '["auth۱","auth۲","auth۳"]\n\n'
        "اگه join کردن به کانال خطا داد، از /debugjoin برای پیدا کردن "
        "متد درست pyrubi استفاده کن."
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
        f"نتیجه بررسی:\n\n"
        + "\n".join(lines)
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
        f"نتیجه اد کردن به کانال:\n\n"
        + "\n".join(lines)
        + f"\n\n📊 جمع‌بندی: {joined} از {len(healthy_auths)} اکانت با موفقیت اد شدن."
    )

    bot.edit_message_text(summary, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


if __name__ == "__main__":
    if BOT_TOKEN == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        log.warning("BOT_TOKEN تنظیم نشده! توی Railway، متغیر محیطی BOT_TOKEN رو ست کن.")
    log.info("ربات در حال اجراست...")
    bot.infinity_polling()
    
