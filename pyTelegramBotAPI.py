"""
ربات تلگرامی بررسی سلامت اکانت روبیکا
----------------------------------------
کاربردش: کاربر Auth (سشن) روبیکا رو می‌فرسته، ربات چک می‌کنه
اکانت سالمه (لاگین معتبره) یا ساسپند/نامعتبر شده.

نصب پیش‌نیازها روی Railway (requirements.txt):
    python-telegram-bot==21.*
    rubka

متغیر محیطی لازم:
    BOT_TOKEN  -> توکن ربات تلگرام (از BotFather)
"""

import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from rubka import Client  # کتابخانه rubka

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")


def check_rubika_auth(auth: str) -> tuple[bool, str]:
    """
    با auth داده‌شده وصل می‌شه و یه متد سبک (getUserInfo) صدا می‌زنه.
    اگه موفق بود یعنی سشن سالمه.
    برمی‌گردونه: (is_healthy, detail_message)
    """
    try:
        client = Client(auth=auth)  # اگه امضای سازنده rubka فرق داره بگو تا اصلاح کنم
        me = client.get_me()  # یا client.get_user_info() بسته به نسخه rubka

        # بعضی نسخه‌ها dict برمی‌گردونن، بعضی object
        if isinstance(me, dict):
            success = me.get("status", "").upper() == "OK" or "user" in me
        else:
            success = bool(me)

        if success:
            return True, "✅ اکانت سالمه و لاگین معتبره."
        return False, "⚠️ پاسخ نامعتبر از سرور روبیکا."

    except Exception as e:
        err = str(e).lower()
        if "invalid" in err or "auth" in err or "unauthorized" in err:
            return False, f"❌ اکانت نامعتبر/ساسپند شده.\nجزئیات: {e}"
        return False, f"❌ خطا در بررسی: {e}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! برای بررسی سلامت اکانت روبیکا:\n"
        "دستور /check رو بزن و Auth رو بعدش بفرست.\n"
        "مثال:\n/check YOUR_AUTH_STRING"
    )


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗️لطفاً auth رو بعد از دستور بفرست.\nمثال: /check abc123...")
        return

    auth = context.args[0].strip()
    await update.message.reply_text("در حال بررسی... ⏳")

    is_healthy, detail = check_rubika_auth(auth)
    await update.message.reply_text(detail)


async def plain_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # اگه کاربر مستقیم auth رو بدون دستور فرستاد
    auth = update.message.text.strip()
    if len(auth) < 10:
        return
    await update.message.reply_text("در حال بررسی... ⏳")
    is_healthy, detail = check_rubika_auth(auth)
    await update.message.reply_text(detail)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("متغیر محیطی BOT_TOKEN تنظیم نشده.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_text_handler))

    logger.info("Bot started.")
    app.run_polling()


if __name__ =
        = "__main__":
    main()
