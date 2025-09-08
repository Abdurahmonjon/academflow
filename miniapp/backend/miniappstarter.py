import os
import threading
import time
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


BOT_TOKEN = "8388239106:AAF7onMN3FvA8TST-bZO2FKe9yJHon6EtZE"
WEBAPP_URL = "https://abdurahmonjon.github.io/academflow/miniapp/front/index.html"  # mini-app frontend URL
RENDER_URL = os.getenv("RENDER_URL", "https://your-app.onrender.com")  # Renderdagi app URL’ingizni shu yerga yozasiz


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Dasturga kirish", url=WEBAPP_URL)]
    ]
    await update.message.reply_text(
        "Mini App`ni ochish uchun pastdagi tugmachani yoki OPEN tugmani bosing",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def keep_alive():
    """Render uxlab qolmasligi uchun self-ping"""
    while True:
        try:
            requests.get(RENDER_URL)
            print("Ping sent to keep alive...")
        except Exception as e:
            print("Ping error:", e)
        time.sleep(600)  # 10 daqiqa kutadi


def main():
    print("Starting the bot...")

    # keep_alive threadini ishga tushiramiz
    threading.Thread(target=keep_alive, daemon=True).start()

    # Telegram botni ishga tushiramiz
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()


if __name__ == '__main__':
    main()
