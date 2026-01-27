from telegram import Update
from telegram.ext import ContextTypes

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Assalomu alaykum.\n"
        "Buyruqlar:\n"
        "/register - Ro'yxatdan o'tish\n"
        "/login - Tizimga kirish\n"
        "/kiritish - Chek yuborish (login kerak)"
    )
