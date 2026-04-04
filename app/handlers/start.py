from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from ..config import ADMIN_IDS, APP_MODE


def _menu_keyboard(is_logged: bool, is_admin: bool) -> ReplyKeyboardMarkup:
    # Operator bo'lsa — bot turiga qarab menyu
    if is_logged:
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

        # fallback
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    # Admin bo'lsa
    if is_admin:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/admin")], [KeyboardButton("/login")], [KeyboardButton("/start")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    # Login qilmagan oddiy user
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
        if APP_MODE == "order_bot":
            text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang: /kiritish."
        elif APP_MODE == "confirm_bot":
            text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang: /tasdiq yoki /takror."
        else:
            text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang."
    elif is_admin:
        text = "🛠 Admin. /admin orqali operatorlarni boshqarasiz. Operator sifatida ishlash uchun /login ham bor."
    else:
        text = "Assalomu alaykum. Botdan foydalanish uchun avval /login qiling."

    await update.message.reply_text(text, reply_markup=_menu_keyboard(is_logged, is_admin))