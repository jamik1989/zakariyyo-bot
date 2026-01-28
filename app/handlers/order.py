# app/handlers/order.py
import re
import os
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from dateutil import parser as du_parser

from ..config import GROUP_CHAT_ID
from ..services.moysklad import (
    get_sales_channels,
    get_default_organization,
    get_or_create_counterparty,
    create_paymentin,
    create_cashin,
    attach_file_to_paymentin,
    attach_file_to_cashin,
)

# STATES
STEP_TEXT, STEP_CHECK, STEP_AMOUNT_DATE, STEP_CHANNEL, STEP_PAYTYPE = range(5)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------- phone helpers ----------------

def _normalize_phone_uz(phone_raw: str) -> str:
    """
    Qabul qiladi:
      - 919915252
      - +998919915252
      - 998919915252
      - 91 991 52 52
    Natija: +998XXXXXXXXX
    """
    digits = re.sub(r"\D", "", phone_raw or "")
    if not digits:
        return ""

    if len(digits) == 9:
        return "+998" + digits

    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits

    if len(digits) > 12:
        return "+998" + digits[-9:]

    if 9 < len(digits) < 12:
        return "+998" + digits[-9:]

    return "+" + digits


def _digits_only_phone(phone_plus: str) -> str:
    """MoySklad uchun: faqat raqam."""
    return re.sub(r"\D", "", phone_plus or "")


def _normalize_month_words(s: str) -> str:
    x = s or ""
    repl = {
        "yan": "jan", "fev": "feb", "mart": "mar", "apr": "apr", "may": "may",
        "iyun": "jun", "iyul": "jul", "avg": "aug", "sen": "sep", "okt": "oct",
        "noy": "nov", "dek": "dec",
    }
    for k, v in repl.items():
        x = re.sub(rf"\b{k}\b", v, x, flags=re.IGNORECASE)
    return x


def _parse_amount_date_one_line(text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Kutiladigan format:
      600000-12.01.2026
      600000 - 12.01.2026
      600000/12.01.2026
    Natija:
      (600000, "2026-01-12")
    """
    s = (text or "").strip()
    m = re.match(r"^\s*([0-9][0-9\s.,]{2,20})\s*[-/,]\s*(.+?)\s*$", s)
    if not m:
        return None, None

    amount_raw = m.group(1)
    date_raw = m.group(2)

    amount_digits = re.sub(r"\D", "", amount_raw)
    if not amount_digits:
        return None, None

    amount = int(amount_digits)
    if amount <= 0:
        return None, None

    try:
        dt = du_parser.parse(_normalize_month_words(date_raw), dayfirst=True, fuzzy=True)
        date_iso = dt.date().isoformat()
    except Exception:
        return None, None

    return amount, date_iso


def _norm_brand(brand_raw: str) -> str:
    """✅ Project umuman ishlatmaymiz. Faqat brend nomini tozalaymiz."""
    return " ".join((brand_raw or "").strip().upper().split())


# ---------------- Conversation flow ----------------

async def kiritish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.")
        return ConversationHandler.END

    await update.message.reply_text(
        "✍️ Ma'lumotni kiriting:\n"
        "BREND-Mijoz Ismi-Telefon\n\n"
        "Misol:\n"
        "NIKE-Azamat-+998919915252\n"
        "yoki:\n"
        "NIKE-Azamat-919915252"
    )
    return STEP_TEXT


async def step_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    parts = [p.strip() for p in text.split("-", maxsplit=2)]
    if len(parts) != 3:
        await update.message.reply_text("❌ Format xato.\nTo'g'ri format:\nBREND-Mijoz Ismi-Telefon")
        return STEP_TEXT

    brand_raw, client_name, phone_raw = parts

    brand_norm = _norm_brand(brand_raw)
    if not brand_norm:
        await update.message.reply_text("❌ Brend bo‘sh bo‘lmasligi kerak.")
        return STEP_TEXT

    phone_plus = _normalize_phone_uz(phone_raw)
    if not phone_plus:
        await update.message.reply_text("❌ Telefon noto‘g‘ri. Masalan: +998901234567 yoki 901234567")
        return STEP_TEXT

    context.user_data["order"] = {
        "brand": brand_norm,
        "client_name": client_name,
        "phone_plus": phone_plus,
        "phone_digits": _digits_only_phone(phone_plus),
    }

    await update.message.reply_text(
        f"✅ Qabul qilindi:\n"
        f"🏷 Brend: {brand_norm}\n"
        f"👤 Mijoz: {client_name}\n"
        f"📞 Tel: {phone_plus}\n\n"
        "📎 Endi chekni rasm (foto) ko‘rinishida yuboring."
    )
    return STEP_CHECK


async def handle_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    if msg.document and (
        msg.document.mime_type == "application/pdf"
        or (msg.document.file_name or "").lower().endswith(".pdf")
    ):
        await msg.reply_text("📄 Hozircha PDF qabul qilmaymiz. Iltimos, chekni foto qilib yuboring.")
        return STEP_CHECK

    if not msg.photo:
        await msg.reply_text("❌ Iltimos, chekni rasm (foto) sifatida yuboring.")
        return STEP_CHECK

    file = await msg.photo[-1].get_file()
    img_path = TMP_DIR / f"check_{msg.message_id}.jpg"
    await file.download_to_drive(str(img_path))
    context.user_data["check_path"] = str(img_path)

    await msg.reply_text(
        "✅ Chek qabul qilindi.\n"
        "💰 Summani va 📅 sanani bitta xabarda kiriting:\n"
        "Masalan: 600000-28.01.2026"
    )
    return STEP_AMOUNT_DATE


async def handle_manual_amount_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    amount, date_iso = _parse_amount_date_one_line(text)

    if amount is None or date_iso is None:
        await update.message.reply_text(
            "❌ Format noto‘g‘ri.\n"
            "Iltimos bitta xabarda shunday yozing:\n"
            "600000-28.01.2026"
        )
        return STEP_AMOUNT_DATE

    context.user_data["amount_uzs"] = int(amount)
    context.user_data["date_iso"] = str(date_iso)

    return await ask_sales_channel(update.message, context)


async def ask_sales_channel(message, context: ContextTypes.DEFAULT_TYPE):
    try:
        channels = get_sales_channels(limit=50)
    except Exception as e:
        await message.reply_text(f"❌ Kanal olishda xatolik: {e}")
        return ConversationHandler.END

    if not channels:
        await message.reply_text("❌ MoySklad’da 'канал продаж' topilmadi. Avval sales channel yarating.")
        return ConversationHandler.END

    channels = channels[:10]
    context.user_data["channels_map"] = {c["id"]: c["meta"] for c in channels}

    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"sc:{c['id']}")] for c in channels]
    await message.reply_text("📊 Kanal prodaj (канал продаж) ni tanlang:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STEP_CHANNEL


async def on_sales_channel_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sc_id = (query.data or "").split("sc:", 1)[-1]
    sc_meta = (context.user_data.get("channels_map") or {}).get(sc_id)
    if not sc_meta:
        await query.edit_message_text("❌ Kanal topilmadi. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    context.user_data["sales_channel_meta"] = sc_meta

    kb = [
        [InlineKeyboardButton("💵 Naqt", callback_data="pt:cash")],
        [InlineKeyboardButton("💳 Karta", callback_data="pt:card")],
    ]
    await query.edit_message_text("To‘lov turini tanlang:", reply_markup=InlineKeyboardMarkup(kb))
    return STEP_PAYTYPE


async def on_paytype_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pt = (query.data or "").split("pt:", 1)[-1]
    if pt not in ("cash", "card"):
        return STEP_PAYTYPE

    order = context.user_data.get("order", {})
    operator = context.user_data.get("operator", {})
    amount = int(context.user_data.get("amount_uzs") or 0)
    date_iso = str(context.user_data.get("date_iso") or "")
    sc_meta = context.user_data.get("sales_channel_meta")
    check_path = context.user_data.get("check_path")

    if amount <= 0 or not date_iso or not sc_meta:
        await query.edit_message_text("❌ Ma’lumot yetarli emas. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    try:
        org = get_default_organization()

        # ✅ Контрагент nomi: "BRAND Mijoz"
        cp_name = f"{order.get('brand')} {order.get('client_name')}".strip()
        cp_phone_digits = str(order.get("phone_digits") or "").strip()
        cp = get_or_create_counterparty(cp_name, phone=cp_phone_digits)

        desc = (
            f"{order.get('brand')} | {order.get('client_name')} | {order.get('phone_plus')} | "
            f"Operator: {operator.get('name')} ({operator.get('phone')})"
        )

        # ✅ MUHIM: PROJECT umuman yuborilmaydi (Проект bo‘sh)
        if pt == "card":
            created = create_paymentin(
                organization_meta=org["meta"],
                agent_meta=cp["meta"],
                sales_channel_meta=sc_meta,
                sum_uzs=amount,
                date_iso=date_iso,
                description=desc,
            )
            doc_kind = "Входящий платёж"
            if created.get("id") and check_path:
                attach_file_to_paymentin(str(created["id"]), str(check_path))
        else:
            created = create_cashin(
                organization_meta=org["meta"],
                agent_meta=cp["meta"],
                sales_channel_meta=sc_meta,
                sum_uzs=amount,
                date_iso=date_iso,
                description=desc,
            )
            doc_kind = "Приходный ордер"
            if created.get("id") and check_path:
                attach_file_to_cashin(str(created["id"]), str(check_path))

        await query.edit_message_text(
            f"✅ MoySklad’ga {doc_kind} yuborildi.\n"
            f"📄 Doc: {created.get('name','N/A')}\n"
            f"🆔 ID: {created.get('id','N/A')}"
        )

        if GROUP_CHAT_ID:
            caption = (
                f"✅ {doc_kind}\n\n"
                f"🏷 Brend: {order.get('brand')}\n"
                f"👤 Mijoz: {order.get('client_name')}\n"
                f"📞 Tel: {order.get('phone_plus')}\n"
                f"💵 Summa: {amount:,} UZS\n"
                f"📅 Sana: {date_iso}\n"
                f"👨‍💼 Operator: {operator.get('name')} ({operator.get('phone')})\n"
                f"🧾 MoySklad: {created.get('name','N/A')}"
            )
            if check_path and os.path.exists(check_path):
                with open(check_path, "rb") as f:
                    await context.bot.send_photo(chat_id=GROUP_CHAT_ID, photo=f, caption=caption)
            else:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=caption)

    except Exception as e:
        await query.edit_message_text(f"❌ MoySklad yuborishda xatolik: {e}")
        return ConversationHandler.END

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.")
    return ConversationHandler.END
