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

علاوه بر این، دکمه‌ی «ورود با شماره» هم اضافه شده: برای اکانت‌هایی که
authشون رو نداری، با /login شماره + کد تایید رو می‌فرستی و ربات auth
رو برات می‌سازه (خودت باید کد رو از پیامک/اپ روبیکای همون اکانت بخونی).

نکته مهم: pyrubi مستندسازی ضعیفی داره (طبق تجربه قبلیت با این کتابخونه‌ها).
اگه get_chats یا get_me یا join_channel یا متدهای لاگین (send_code/
sign_in) درست نبودن، توی خط‌های CHECK_METHOD و JOIN_METHOD و
LOGIN_METHOD پایین باید با متد درست جایگزینشون کنی. برای پیدا کردن اسم
درست متدهای لاگین از /debuglogin استفاده کن (مثل /debugjoin که برای
join بود). ساختار try/except طوری نوشته شده که فقط همون یه خط رو عوض
کنی، بقیه کد دست‌نخورده می‌مونه.
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

# --- state مربوط به فلوی لاگین با شماره ---
# منتظر شماره تلفن هستیم یا نه
waiting_for_login_phone = set()
# منتظر کد تایید هستیم یا نه؛ chat_id -> {"client": Client, "phone": str, "code_info": ...}
pending_login_by_chat = {}


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


def debug_login_methods(phone: str) -> str:
    """
    یه Client خام (بدون auth قبلی) می‌سازه و متدهایی که توی اسمشون
    code / login / sign / auth هست رو لیست می‌کنه، تا اگه send_code یا
    sign_in جواب نداد، بشه اسم درست متد رو پیدا کرد.
    فقط برای دیباگ؛ چیزی رو واقعاً صدا نمی‌زنه چون خیلی از این متدها
    آرگومان‌های متفاوت می‌خوان و اجراشون بدون کنترل می‌تونه کد تایید
    اضافه بفرسته.
    """
    if Client is None:
        return "کتابخونه pyrubi نصب نیست."

    try:
        client = Client(phone.strip())
    except Exception as e:
        return f"ساخت Client شکست خورد: {type(e).__name__}: {e}"

    candidate_names = [
        name for name in dir(client)
        if any(key in name.lower() for key in ("code", "login", "sign", "auth"))
    ]
    if not candidate_names:
        return "هیچ متد مرتبطی با code/login/sign/auth روی Client پیدا نشد."

    return "متدهای پیدا‌شده (بدون اجرا، فقط اسم):\n" + "\n".join(f"• {n}" for n in candidate_names)


@bot.message_handler(commands=["debuglogin"])
def debuglogin_start(message):
    msg = bot.reply_to(
        message,
        "شماره‌ای که می‌خوای باهاش لاگین کنی رو بفرست (مثلاً 989123456789)، "
        "تا متدهای مرتبط با لاگین روی Client رو لیست کنم (بدون اجرا).",
    )
    bot.register_next_step_handler(msg, debuglogin_process)


def debuglogin_process(message):
    phone = message.text.strip()
    bot.reply_to(message, debug_login_methods(phone))


def start_login(phone: str):
    """
    مرحله اول لاگین: ساخت Client با شماره و درخواست ارسال کد تایید.
    خروجی: (موفق بود یا نه, client یا None, پیام/جزئیات)
    """
    if Client is None:
        return False, None, "کتابخونه pyrubi نصب نیست"

    phone = phone.strip()
    try:
        client = Client(phone)

        # ==== LOGIN_METHOD (ارسال کد): اگه send_code نبود عوضش کن ====
        # گزینه‌های رایج توی کتابخونه‌های مشابه:
        #   client.send_code(phone)
        #   client.sendCode(phone)
        # خروجی معمولاً یه pass_key/hash هست که برای مرحله بعد لازمه؛
        # اینجا کلاینت رو نگه می‌داریم چون معمولاً این مقدار رو خودش
        # داخل خودش ذخیره می‌کنه.
        client.send_code(phone)
        # ===============================================================

        return True, client, "کد تایید ارسال شد"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def submit_login_code(client, phone: str, code: str):
    """
    مرحله دوم لاگین: فرستادن کد تایید و گرفتن auth نهایی.
    خروجی: (موفق بود یا نه, auth یا None, پیام/جزئیات)
    """
    try:
        # ==== LOGIN_METHOD (تایید کد): اگه sign_in نبود عوضش کن ====
        # گزینه‌های رایج:
        #   client.sign_in(phone, code)
        #   client.signIn(phone, code)
        # اگه اکانت رمز دو مرحله‌ای (پسورد) داشته باشه، این متد ممکنه
        # خطای مربوط به نیاز به پسورد بده؛ در اون صورت باید یه مرحله‌ی
        # سوم (گرفتن پسورد از کاربر و صدا زدن متد تکمیل ورود با پسورد)
        # اضافه کنیم.
        client.sign_in(phone, code)
        # ==============================================================

        # ==== auth نهایی رو از کلاینت می‌گیریم؛ اسم اتریبیوت رو چک کن ====
        auth = getattr(client, "auth", None)
        if auth is None:
            auth = getattr(client, "auth_key", None)
        # ===================================================================

        if auth:
            return True, str(auth), "ورود موفق"
        return True, None, "ورود موفق ولی auth رو خودکار پیدا نکردم — با /debuglogin چک کن اسم درست اتریبیوت چیه"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


@bot.message_handler(commands=["login"])
def login_start(message):
    waiting_for_login_phone.add(message.chat.id)
    bot.reply_to(
        message,
        "شماره اکانتی که می‌خوای واردش بشی رو بفرست (با کد کشور، مثلاً 989123456789).\n"
        "بعدش کد تاییدی که برای همون اکانت میاد رو از من می‌خوام.",
    )


@bot.message_handler(func=lambda m: m.chat.id in waiting_for_login_phone, content_types=["text"])
def login_process_phone(message):
    chat_id = message.chat.id
    waiting_for_login_phone.discard(chat_id)

    phone = message.text.strip()
    status_msg = bot.reply_to(message, "در حال ارسال درخواست کد تایید...")

    ok, client, detail = start_login(phone)
    if not ok:
        bot.edit_message_text(
            f"ارسال کد ناموفق بود: {detail}\nاگه فکر می‌کنی اسم متد اشتباهه، از /debuglogin استفاده کن.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
        )
        return

    pending_login_by_chat[chat_id] = {"client": client, "phone": phone}
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

    ok, auth, detail = submit_login_code(pending["client"], pending["phone"], code)
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
            "این رو جایی امن نگه دار (توی پیام‌های دیگه پاکش کن). "
            "می‌تونی همین auth رو با /check امتحان کنی و به لیست join اضافه کنی.",
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode="Markdown",
        )
    else:
        bot.edit_message_text(detail, chat_id=status_msg.chat.id, message_id=status_msg.message_id)


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
        "اگه auth یه اکانت رو نداری، با /login شماره + کد تایید بده تا "
        "auth رو برات بسازم.\n\n"
        "اگه join کردن به کانال خطا داد، از /debugjoin و اگه لاگین خطا "
        "داد از /debuglogin برای پیدا کردن متد درست pyrubi استفاده کن."
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
    
