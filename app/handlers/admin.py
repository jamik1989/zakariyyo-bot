# app/handlers/admin.py
import re
import secrets
import string
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from ..config import ADMIN_IDS
from ..db import create_operator, list_operators, count_operators, delete_operator_by_phone

AD_MENU, AD_ADD_PHONE, AD_ADD_NAME, AD_ADD_PASS, AD_DEL_PHONE = range(5)


def _is_admin(update: Update) -> bool:
    uid = getattr(update.effective_user, "id", None)
    return uid in ADMIN_IDS


def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Operator qo‘shish", callback_data="adm:add")],
        [InlineKeyboardButton("📋 Operatorlar ro‘yxati", callback_data="adm:list")],
        [InlineKeyboardButton("🗑 Operator o‘chirish", callback_data="adm:del")],
        [InlineKeyboardButton("⬅️ Yopish", callback_data="adm:close")],
    ])


def _gen_password(length: int = 6) -> str:
    # 6 xonali raqamli parol (operatorlar uchun qulay)
    digits = string.digits
    return "".join(secrets.choice(digits) for _ in range(int(length)))


async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    total = count_operators()
    await update.message.reply_text(
        f"🛠 Admin panel\n\n👥 Operatorlar soni: {total}",
        reply_markup=_admin_menu_kb(),
    )
    return AD_MENU


async def admin_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not _is_admin(update):
        await q.edit_message_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    data = (q.data or "").strip()

    if data == "adm:close":
        await q.edit_message_text("✅ Yopildi.")
        return ConversationHandler.END

    if data == "adm:list":
        ops = list_operators(limit=200)
        if not ops:
            await q.edit_message_text("Hozircha operator yo‘q.", reply_markup=_admin_menu_kb())
            return AD_MENU

        lines = ["📋 Operatorlar ro‘yxati (oxirgilari yuqorida):", ""]
        for o in ops[:50]:
            lines.append(f"• {o['name']} — {o['phone']} (id:{o['id']})")
        if len(ops) > 50:
            lines.append(f"\n… yana {len(ops)-50} ta operator bor.")
        await q.edit_message_text("\n".join(lines), reply_markup=_admin_menu_kb())
        return AD_MENU

    if data == "adm:add":
        context.user_data.pop("adm_new", None)
        await q.edit_message_text("📌 Operator telefon raqamini kiriting (namuna: 901234567):")
        return AD_ADD_PHONE

    if data == "adm:del":
        await q.edit_message_text("📌 O‘chiriladigan operator telefon raqamini kiriting (namuna: 901234567):")
        return AD_DEL_PHONE

    return AD_MENU


async def admin_add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    phone = (update.message.text or "").strip()
    phone = re.sub(r"\D+", "", phone)
    if len(phone) < 9:
        await update.message.reply_text("❌ Telefon noto‘g‘ri. Namuna: 901234567")
        return AD_ADD_PHONE

    context.user_data["adm_new"] = {"phone": phone}
    await update.message.reply_text("✍️ Operator ismini kiriting:")
    return AD_ADD_NAME


async def admin_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("❌ Ism bo‘sh bo‘lmasin.")
        return AD_ADD_NAME

    d = context.user_data.get("adm_new") or {}
    d["name"] = name
    context.user_data["adm_new"] = d

    # parolni admin xohlasa o‘zi kiritsin, xohlamasa "auto" deb yozsin
    await update.message.reply_text("🔐 Operator parolini kiriting yoki AUTO deb yozing (tavsiya):")
    return AD_ADD_PASS


async def admin_add_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    pwd = (update.message.text or "").strip()
    if not pwd:
        await update.message.reply_text("❌ Parol bo‘sh bo‘lmasin. AUTO deb yozishingiz ham mumkin.")
        return AD_ADD_PASS

    if pwd.lower() == "auto":
        pwd = _gen_password(6)

    d = context.user_data.get("adm_new") or {}
    phone = (d.get("phone") or "").strip()
    name = (d.get("name") or "").strip()

    ok = create_operator(phone, name, pwd)
    if not ok:
        await update.message.reply_text("❌ Bu telefon raqam allaqachon ro‘yxatda. Boshqa raqam kiriting.")
        return AD_ADD_PHONE

    context.user_data.pop("adm_new", None)

    await update.message.reply_text(
        "✅ Operator qo‘shildi!\n\n"
        f"📌 Login (telefon): {phone}\n"
        f"🔐 Parol: {pwd}\n\n"
        "Operator botga kirib /login qiladi. Keyin faqat /kiritish va /tasdiq ishlaydi."
    )
    # qaytadan panel
    total = count_operators()
    await update.message.reply_text(
        f"🛠 Admin panel\n\n👥 Operatorlar soni: {total}",
        reply_markup=_admin_menu_kb(),
    )
    return AD_MENU


async def admin_del_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        await update.message.reply_text("❌ Sizda admin huquqi yo‘q.")
        return ConversationHandler.END

    phone = (update.message.text or "").strip()
    phone = re.sub(r"\D+", "", phone)

    ok = delete_operator_by_phone(phone)
    if ok:
        await update.message.reply_text(f"✅ Operator o‘chirildi: {phone}")
    else:
        await update.message.reply_text("❌ Operator topilmadi.")

    total = count_operators()
    await update.message.reply_text(
        f"🛠 Admin panel\n\n👥 Operatorlar soni: {total}",
        reply_markup=_admin_menu_kb(),
    )
    return AD_MENU


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.")
    return ConversationHandler.END
