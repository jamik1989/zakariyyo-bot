from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from ..config import APP_MODE


def _menu_keyboard() -> ReplyKeyboardMarkup:
    if APP_MODE == "order_bot":
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/kiritish")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    if APP_MODE == "confirm_bot":
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if APP_MODE == "order_bot":
        text = "✅ Xush kelibsiz. Kerakli bo‘lim: /kiritish."
    elif APP_MODE == "confirm_bot":
        text = "✅ Xush kelibsiz. Kerakli bo‘limlar: /tasdiq yoki /takror."
    else:
        text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang."

    await update.message.reply_text(text, reply_markup=_menu_keyboard())