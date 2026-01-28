# app/handlers/start.py
from telegram import Update
from telegram.ext import ContextTypes

from ..keyboards import main_menu_kb


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Assalomu alaykum.\n"
        "Buyruqlar:\n"
        "/register - Ro'yxatdan o'tish\n"
        "/login - Tizimga kirish\n"
        "/kiritish - Chek yuborish (login kerak)\n"
        "/cancel - Jarayonni bekor qilish",
        reply_markup=main_menu_kb(),
    )
