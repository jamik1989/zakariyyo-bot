from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes
from ..config import ADMIN_IDS, APP_MODE


def _menu_keyboard(is_logged: bool, is_admin: bool) -> ReplyKeyboardMarkup:
    mode = (APP_MODE or "").strip().lower()

    # ===== ORDER BOT =====
    if mode == "order":
        if is_logged:
            return ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton("/kiritish")]],
                resize_keyboard=True,
                one_time_keyboard=False,
                selective=True,
            )

        if is_admin:
            return ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton("/admin")], [KeyboardButton("/login")], [KeyboardButton("/start")]],
                resize_keyboard=True,
                one_time_keyboard=False,
                selective=True,
            )

        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/login")], [KeyboardButton("/start")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    # ===== CONFIRM BOT =====
    if is_logged:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

    if is_admin:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton("/admin")], [KeyboardButton("/login")], [KeyboardButton("/start")]],
            resize_keyboard=True,
            one_time_keyboard=False,
            selective=True,
        )

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
    mode = (APP_MODE or "").strip().lower()

    if mode == "order":
        if is_logged:
            text = "✅ Xush kelibsiz. Kerakli bo‘limni tanlang: /kiritish."
        elif is_admin:
            text = "🛠 Admin. /admin orqali operatorlarni boshqarasiz. Operator sifatida ishlash uchun /login ham bor."
        else:
            text = "Assalomu alaykum. Botdan foydalanish uchun avval /login qiling."
    else:
        if is_logged:
            text = "✅ Xush kelibsiz. Kerakli bo‘limlarni tanlang: /tasdiq yoki /takror."
        elif is_admin:
            text = "🛠 Admin. /admin orqali operatorlarni boshqarasiz. Operator sifatida ishlash uchun /login ham bor."
        else:
            text = "Assalomu alaykum. Botdan foydalanish uchun avval /login qiling."

    await update.message.reply_text(text, reply_markup=_menu_keyboard(is_logged, is_admin))