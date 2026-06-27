import logging
import random
import socket
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# منابع مرتب‌شده از بهترین به بدترین برای ایران
SOURCES = [
    # اولویت اول - بهینه برای ایران، هر ۱۵ دقیقه آپدیت
    "https://raw.githubusercontent.com/ebrasha/free-v2ray-public-list/refs/heads/main/all_extracted_configs.txt",
    # اولویت دوم - هر ۱۵ دقیقه آپدیت، حجم زیاد
    "https://raw.githubusercontent.com/MatinGhanbari/v2ray-configs/main/subscriptions/filtered/subs/vless.txt",
    # اولویت سوم - هر ۵ دقیقه آپدیت
    "https://github.com/Epodonios/v2ray-configs/raw/main/Splitted-By-Protocol/vless.txt",
    # اولویت چهارم
    "https://raw.githubusercontent.com/barry-far/V2ray-config/main/Splitted-By-Protocol/vless.txt",
    # اولویت پنجم
    "https://raw.githubusercontent.com/sevcator/5ubscrpt10n/main/protocols/vl.txt",
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def parse_vless(config: str):
    """آدرس و پورت رو از کانفیگ VLESS در میاره"""
    try:
        without_scheme = config[len("vless://"):]
        at_index = without_scheme.index("@")
        host_part = without_scheme[at_index + 1:]
        host_port = host_part.split("?")[0].split("#")[0]

        if host_port.startswith("["):
            bracket_end = host_port.index("]")
            host = host_port[1:bracket_end]
            port = int(host_port[bracket_end + 2:])
        else:
            parts = host_port.rsplit(":", 1)
            host = parts[0]
            port = int(parts[1])

        return host, port
    except Exception:
        return None


def check_config(config: str, timeout: int = 3) -> bool:
    """چک می‌کنه پورت سرور باز هست یا نه"""
    parsed = parse_vless(config)
    if not parsed:
        return False

    host, port = parsed
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def fetch_vless_configs() -> list:
    """از منابع GitHub کانفیگ VLESS می‌گیره"""
    configs = []
    for url in SOURCES:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                lines = resp.text.splitlines()
                vless_lines = [l.strip() for l in lines if l.strip().startswith("vless://")]
                configs.extend(vless_lines)
                logger.info(f"✅ {len(vless_lines)} کانفیگ از {url}")
        except Exception as e:
            logger.warning(f"❌ خطا از {url}: {e}")
    return configs


def get_valid_config(max_tries: int = 20):
    """کانفیگ‌ها رو تست می‌کنه تا یه کانفیگ سالم پیدا کنه"""
    configs = fetch_vless_configs()
    if not configs:
        return None

    random.shuffle(configs)

    for config in configs[:max_tries]:
        if check_config(config):
            return config

    return None


def build_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 کانفیگ جدید", callback_data="new_config")],
        [InlineKeyboardButton("📋 راهنمای اتصال", callback_data="guide")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌐 *ربات کانفیگ رایگان VLESS*\n\n"
        "در حال دریافت و تست کانفیگ‌ها...\n"
        "⏳ لطفاً چند ثانیه صبر کنید",
        parse_mode="Markdown",
    )

    config = get_valid_config()

    if config:
        parsed = parse_vless(config)
        host_info = f"`{parsed[0]}:{parsed[1]}`" if parsed else ""

        await update.message.reply_text(
            f"✅ *کانفیگ VLESS تست‌شده:*\n\n"
            f"`{config}`\n\n"
            f"🖥 سرور: {host_info}\n"
            f"👆 روی متن بزن تا کپی بشه",
            parse_mode="Markdown",
            reply_markup=build_keyboard(),
        )
    else:
        await update.message.reply_text(
            "❌ در حال حاضر کانفیگ سالمی پیدا نشد.\n"
            "دوباره امتحان کن! /start",
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "new_config":
        await query.edit_message_text("🔄 در حال تست کانفیگ‌ها...")

        config = get_valid_config()

        if config:
            parsed = parse_vless(config)
            host_info = f"`{parsed[0]}:{parsed[1]}`" if parsed else ""

            await query.edit_message_text(
                f"✅ *کانفیگ VLESS تست‌شده:*\n\n"
                f"`{config}`\n\n"
                f"🖥 سرور: {host_info}\n"
                f"👆 روی متن بزن تا کپی بشه",
                parse_mode="Markdown",
                reply_markup=build_keyboard(),
            )
        else:
            await query.edit_message_text(
                "❌ کانفیگ سالمی پیدا نشد. دوباره امتحان کن!",
                reply_markup=build_keyboard(),
            )

    elif query.data == "guide":
        guide_text = (
            "📱 *راهنمای استفاده از کانفیگ VLESS:*\n\n"
            "1️⃣ اپ *v2rayNG* (اندروید) یا *Shadowrocket* (iOS) نصب کن\n\n"
            "2️⃣ کانفیگ دریافتی رو کپی کن\n\n"
            "3️⃣ توی اپ گزینه *Import from clipboard* رو بزن\n\n"
            "4️⃣ روی کانفیگ بزن و *Connect* کن\n\n"
            "⚡ اگه یه کانفیگ کار نکرد، کانفیگ جدید بگیر!"
        )
        await query.edit_message_text(
            guide_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 برگشت", callback_data="new_config")]
            ]),
        )


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN تنظیم نشده!")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("ربات شروع به کار کرد...")
    app.run_polling()


if __name__ == "__main__":
    main()
