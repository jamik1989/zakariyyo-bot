from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from ..db import create_operator, check_operator
from ..config import ADMIN_IDS, APP_MODE

REG_PHONE, REG_NAME, REG_PASS = range(3)
LOG_PHONE, LOG_PASS = range(2)


def _clean_phone(text: str) -> str:
    return "".join(ch for ch in (text or "") if ch.isdigit())


def _menu_keyboard(is_logged: bool = False, is_admin: bool = False) -> ReplyKeyboardMarkup:
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


# ================= REGISTER =================

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = getattr(update.effective_user, "id", None)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Sizda /register huquqi yo‘q.", reply_markup=_menu_keyboard(False, uid in ADMIN_IDS))
        return ConversationHandler.END

    await update.message.reply_text("📱 Yangi operator telefon raqamini kiriting:")
    return REG_PHONE


async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = _clean_phone(update.message.text)
    if len(phone) < 7:
        await update.message.reply_text("❌ Telefon noto‘g‘ri. Qaytadan kiriting:")
        return REG_PHONE

    context.user_data["reg_phone"] = phone
    await update.message.reply_text("👤 Operator ismini kiriting:")
    return REG_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Ism bo‘sh bo‘lmasin. Qaytadan kiriting:")
        return REG_NAME

    context.user_data["reg_name"] = name
    await update.message.reply_text("🔐 Parol kiriting:")
    return REG_PASS


async def register_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = (update.message.text or "").strip()
    if not password:
        await update.message.reply_text("❌ Parol bo‘sh bo‘lmasin. Qaytadan kiriting:")
        return REG_PASS

    phone = context.user_data.get("reg_phone", "")
    name = context.user_data.get("reg_name", "")

    ok = create_operator(phone, name, password)

    uid = getattr(update.effective_user, "id", None)
    is_admin = uid in ADMIN_IDS

    if ok:
        await update.message.reply_text(
            f"✅ Operator yaratildi:\n👤 {name}\n📱 {phone}",
            reply_markup=_menu_keyboard(False, is_admin),
        )
    else:
        await update.message.reply_text(
            "❌ Bu telefon bilan operator allaqachon mavjud.",
            reply_markup=_menu_keyboard(False, is_admin),
        )

    context.user_data.pop("reg_phone", None)
    context.user_data.pop("reg_name", None)
    return ConversationHandler.END


# ================= LOGIN =================

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 Telefon raqamingizni kiriting:")
    return LOG_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = _clean_phone(update.message.text)
    if len(phone) < 7:
        await update.message.reply_text("❌ Telefon noto‘g‘ri. Qaytadan kiriting:")
        return LOG_PHONE

    context.user_data["login_phone"] = phone
    await update.message.reply_text("🔐 Parolingizni kiriting:")
    return LOG_PASS


async def login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = (update.message.text or "").strip()
    phone = context.user_data.get("login_phone", "")

    row = check_operator(phone, password)
    uid = getattr(update.effective_user, "id", None)
    is_admin = uid in ADMIN_IDS

    if not row:
        await update.message.reply_text("❌ Login yoki parol noto‘g‘ri.")
        return LOG_PASS

    context.user_data["operator"] = {
        "id": row["id"] if hasattr(row, "__getitem__") else row[0],
        "phone": row["phone"] if hasattr(row, "__getitem__") else row[1],
        "name": row["name"] if hasattr(row, "__getitem__") else row[2],
    }

    mode = (APP_MODE or "").strip().lower()
    if mode == "order":
        welcome = f"✅ Xush kelibsiz, {context.user_data['operator']['name']}!\nKerakli bo‘limni tanlang: /kiritish."
    else:
        welcome = f"✅ Xush kelibsiz, {context.user_data['operator']['name']}!\nKerakli bo‘limlarni tanlang: /tasdiq yoki /takror."

    await update.message.reply_text(
        welcome,
        reply_markup=_menu_keyboard(True, is_admin),
    )

    context.user_data.pop("login_phone", None)
    return ConversationHandler.END


# ================= CANCEL =================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = getattr(update.effective_user, "id", None)
    is_admin = uid in ADMIN_IDS
    is_logged = bool(context.user_data.get("operator"))

    await update.message.reply_text(
        "Bekor qilindi.",
        reply_markup=_menu_keyboard(is_logged, is_admin),
    )
    return ConversationHandler.END