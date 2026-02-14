from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes, ConversationHandler

from ..db import create_operator, check_operator
from ..config import ADMIN_IDS

REG_PHONE, REG_NAME, REG_PASS = range(3)
LOG_PHONE, LOG_PASS = range(2)


def _menu_keyboard() -> ReplyKeyboardMarkup:
    # Pastki panel (Reply keyboard). Inline emas.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END


# ---------- REGISTER ----------
async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Faqat admin operator ro'yxatdan o'tkazadi
    uid = getattr(update.effective_user, "id", None)
    if uid not in ADMIN_IDS:
        await update.message.reply_text(
            "❌ Ro'yxatdan o'tish yopiq. Admin sizga login/parol beradi. /login qiling.",
            reply_markup=_menu_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text("📌 Operator telefon raqamini kiriting (namuna: 901234567):")
    return REG_PHONE


async def register_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.message.text or "").strip()
    context.user_data["reg_phone"] = phone
    await update.message.reply_text("✍️ Ismingizni kiriting:")
    return REG_NAME


async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    context.user_data["reg_name"] = name
    await update.message.reply_text("🔐 Parol kiriting:")
    return REG_PASS


async def register_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = (update.message.text or "").strip()
    phone = context.user_data.get("reg_phone")
    name = context.user_data.get("reg_name")

    ok = create_operator(phone, name, password)
    if not ok:
        await update.message.reply_text(
            "❌ Bu telefon raqam allaqachon ro'yxatdan o'tgan. /login qiling.",
            reply_markup=_menu_keyboard(),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Operator yaratildi.\n\n"
        "🔐 Kirish ma’lumotlari:\n"
        f"📞 Login: {phone}\n"
        f"🔑 Parol: {password}\n\n"
        "Operator /login qilib kiradi.",
        reply_markup=_menu_keyboard(),
    )
    return ConversationHandler.END


# ---------- LOGIN ----------
async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📌 Telefon raqamingizni kiriting:")
    return LOG_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = (update.message.text or "").strip()
    context.user_data["log_phone"] = phone
    await update.message.reply_text("🔐 Parolingizni kiriting:")
    return LOG_PASS


async def login_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = (update.message.text or "").strip()
    phone = context.user_data.get("log_phone")

    row = check_operator(phone, password)
    if not row:
        await update.message.reply_text(
            "❌ Noto'g'ri parol yoki operator topilmadi! Admin sizga login/parol beradi. /login qayta urinib ko‘ring.",
            reply_markup=_menu_keyboard(),
        )
        return ConversationHandler.END

    # sqlite Row bo‘lishi ham mumkin, tuple bo‘lishi ham — ikkalasini ham ushlaymiz
    try:
        op_id, op_phone, op_name = row
    except Exception:
        op_id = row["id"]
        op_phone = row["phone"]
        op_name = row["name"]

    context.user_data["operator"] = {"id": op_id, "phone": op_phone, "name": op_name}

    await update.message.reply_text(
        f"✅ Xush kelibsiz, {op_name}!\nKerakli bo‘limni tanlang:",
        reply_markup=_menu_keyboard(),
    )
    return ConversationHandler.END
