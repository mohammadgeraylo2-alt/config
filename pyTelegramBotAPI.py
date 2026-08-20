"""
ربات تلگرامی چک سلامت اوت روبیکا
requirements.txt:
    pyTelegramBotAPI
    rubpy

Environment Variable لازم روی Railway:
    BOT_TOKEN
"""

import os
import telebot
from rubpy import Client

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
bot = telebot.TeleBot(BOT_TOKEN)


def check_auth(auth: str) -> str:
    try:
        client = Client(name="checker", auth=auth)
        me = client.get_me()
        if me:
            return "✅ اوت سالمه."
        return "⚠️ پاسخ نامعتبر بود."
    except Exception as e:
        return f"❌ اوت نامعتبره یا ساسپند شده.\n{e}"


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "اوت روبیکا رو بفرست تا چک کنم سالمه یا نه.")


@bot.message_handler(func=lambda m: True)
def handle_auth(message):
    auth = message.text.strip()
    if len(auth) < 20:
        bot.reply_to(message, "اوت معتبر بفرست.")
        return
    bot.reply_to(message, "در حال بررسی...")
    result = check_auth(auth)
    bot.reply_to(message, result)


bot.infinity
_polling()
