from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from ..config import ADMIN_IDS


def _menu_keyboard(is_logged: bool, is_admin: bool) -> ReplyKeyboardMarkup:
    # Operator: faqat 2 ta tugma
    if is_logged:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    # Admin: admin panel + login
    if is_admin:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/admin")], [KeyboardButton("/login")], [KeyboardButton("/start")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    # Mehmon: faqat login
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/login")], [KeyboardButton("/start")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = getattr(update.effective_user, "id", None)
    is_admin = uid in ADMIN_IDS
    is_logged = bool(context.user_data.get("operator"))

    if is_logged:
        text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang: /kiritish yoki /tasdiq."
    elif is_admin:
        text = "🛠 Admin. /admin orqali operatorlarni boshqarasiz. Operator sifatida ishlash uchun /login ham bor."
    else:
        text = "Assalomu alaykum. Botdan foydalanish uchun avval /login qiling."

    await update.message.reply_text(text, reply_markup=_menu_keyboard(is_logged, is_admin))
