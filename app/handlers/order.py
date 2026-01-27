# app/handlers/order.py
import re
import os
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from dateutil import parser as du_parser
from PIL import Image, ImageOps, ImageEnhance
import pytesseract

from ..config import GROUP_CHAT_ID
from ..services.moysklad import (
    get_or_create_project,
    get_sales_channels,
    get_default_organization,
    get_or_create_counterparty,
    create_paymentin,
    create_cashin,
    attach_file_to_paymentin,
    attach_file_to_cashin,
)

TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# STATES
STEP_TEXT, STEP_CHECK, STEP_AMOUNT, STEP_DATE, STEP_CHANNEL, STEP_PAYTYPE = range(6)

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
    return re.sub(r"\D", "", phone_plus or "")


# ---------------- OCR helpers ----------------

def _preprocess_for_ocr(img: Image.Image) -> Image.Image:
    """
    Bank skrinlari: gradient fon + katta raqamlar.
    """
    im = img.convert("RGB")
    im = ImageOps.grayscale(im)
    im = ImageOps.autocontrast(im)

    im = ImageEnhance.Contrast(im).enhance(2.2)
    im = ImageEnhance.Sharpness(im).enhance(2.0)

    w, h = im.size
    im = im.resize((w * 2, h * 2))

    # threshold — bank skrinlarda yaxshi
    im = im.point(lambda p: 255 if p > 155 else 0)
    return im


def _ocr_image(image_path: Path) -> str:
    img = Image.open(image_path)
    img = _preprocess_for_ocr(img)

    lang = os.getenv("TESS_LANG", "rus+eng")

    # Telefon skrinlarda ba'zan psm 11 yaxshiroq: sparse text
    # fallback psm 6
    for psm in (11, 6):
        try:
            config = f"--psm {psm}"
            text = pytesseract.image_to_string(img, lang=lang, config=config)
            if text and len(text.strip()) >= 10:
                return text
        except Exception:
            continue

    return pytesseract.image_to_string(img, lang=lang, config="--psm 6")


# ---------------- parsing helpers ----------------

def _norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _parse_number_token(token: str) -> Optional[int]:
    """
    "600 000.00" -> 600000
    "1,500,000"  -> 1500000
    "400000"     -> 400000
    """
    if not token:
        return None

    t = token.strip()

    # valyuta/so‘zlarni olib tashlaymiz
    t = re.sub(r"(uzs|сум|so['`’]?m|som|sum)", "", t, flags=re.IGNORECASE).strip()

    # .00 / ,00 ni kesib tashlaymiz
    t = re.sub(r"([.,]\s*00)\b", "", t)

    digits = re.sub(r"[^\d]", "", t)
    if not digits:
        return None

    try:
        return int(digits)
    except Exception:
        return None


def _extract_candidates_amounts(text: str) -> List[Tuple[int, str, int]]:
    """
    Kandidatlar: (amount_int, raw_fragment, weight)
    weight qanchalik katta bo‘lsa, ishonch shunchalik yuqori.
    """
    t = text or ""
    candidates: List[Tuple[int, str, int]] = []

    # 1) Juda ishonchli: valyuta bilan birga kelgan raqamlar
    currency_patterns = [
        r"([0-9][0-9\s.,]{1,20})\s*(uzs|сум|so['`’]?m|som|sum)\b",
    ]
    for p in currency_patterns:
        for m in re.finditer(p, t, flags=re.IGNORECASE):
            frag = m.group(1)
            val = _parse_number_token(frag)
            if val is not None:
                candidates.append((val, frag, 90))

    # 2) Kalit so‘zlar atrofida
    key_patterns = [
        r"(оплачено|успешно|плат[её]ж|перевод|зачислено|оплата)\s*[:\-]?\s*([0-9][0-9\s.,]{1,20})",
        r"(жами|jami|summa|сумма|итого|итог|total)\s*[:\-]?\s*([0-9][0-9\s.,]{1,20})",
        r"(muvaffaqiyatli|to['`’]?langan|o['`’]?tkazma|amalga oshirildi|bajarildi|tasdiqlandi)\s*[:\-]?\s*([0-9][0-9\s.,]{1,20})",
    ]
    for p in key_patterns:
        for m in re.finditer(p, t, flags=re.IGNORECASE):
            frag = m.group(2)
            val = _parse_number_token(frag)
            if val is not None:
                candidates.append((val, frag, 70))

    # 3) Umumiy yirik raqamlar (ekrandagi katta summa)
    for m in re.finditer(r"\b[0-9][0-9\s.,]{4,20}\b", t):
        frag = m.group(0)
        val = _parse_number_token(frag)
        if val is not None:
            candidates.append((val, frag, 40))

    return candidates


def _try_fix_small_amount(amount: int) -> int:
    """
    OCR ba'zan 400 000 o‘rniga 40/60/90 chiqaradi.
    Shunda eng mantiqiy ko‘paytirishni tanlaymiz.
    """
    if amount <= 0:
        return amount

    # normal bo‘lsa tegmaymiz
    if amount >= 10_000:
        return amount

    # 40 -> 40_000? 400_000? 4_000_000?
    # bank o‘tkazmalarda odatda 50 mingdan past bo‘lmaydi
    for mult in (1_000, 10_000, 100_000):
        cand = amount * mult
        if 50_000 <= cand <= 50_000_000:
            return cand

    return amount


def _extract_amount_uzs(text: str) -> Optional[int]:
    candidates = _extract_candidates_amounts(text)
    if not candidates:
        return None

    # Avval weight bo‘yicha, keyin amount bo‘yicha
    candidates.sort(key=lambda x: (x[2], x[0]), reverse=True)

    # eng yuqori ishonchli kandidatni olamiz
    best_amount, best_raw, best_w = candidates[0]

    # mantiqiy fix
    best_amount = _try_fix_small_amount(best_amount)

    # sanity
    if best_amount <= 0:
        return None
    return int(best_amount)


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


def _extract_date_iso(text: str) -> Optional[str]:
    t = _norm_spaces(text)

    for m in re.finditer(r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b", t):
        try:
            dt = du_parser.parse(m.group(0), dayfirst=True, fuzzy=True)
            return dt.date().isoformat()
        except Exception:
            pass

    for m in re.finditer(r"\b(\d{4})[./-](\d{2})[./-](\d{2})\b", t):
        try:
            dt = du_parser.parse(m.group(0), dayfirst=False, fuzzy=True)
            return dt.date().isoformat()
        except Exception:
            pass

    try:
        dt = du_parser.parse(_normalize_month_words(t), fuzzy=True, dayfirst=True)
        return dt.date().isoformat()
    except Exception:
        return None


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
        await update.message.reply_text(
            "❌ Format xato.\nTo'g'ri format:\nBREND-Mijoz Ismi-Telefon"
        )
        return STEP_TEXT

    brand_raw, client_name, phone_raw = parts

    phone_plus = _normalize_phone_uz(phone_raw)
    if not phone_plus:
        await update.message.reply_text("❌ Telefon noto‘g‘ri. Masalan: +998901234567 yoki 901234567")
        return STEP_TEXT

    try:
        project = get_or_create_project(brand_raw)
        brand_norm = project.get("name") or " ".join(brand_raw.strip().upper().split())
    except Exception:
        brand_norm = " ".join(brand_raw.strip().upper().split())
        project = None

    context.user_data["order"] = {
        "brand": brand_norm,
        "project_meta": project.get("meta") if project else None,  # hujjatga yubormaymiz
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
        await msg.reply_text("📄 Hozircha OCR faqat rasmda ishlaydi. Iltimos, chekni foto qilib yuboring.")
        return STEP_CHECK

    if not msg.photo:
        await msg.reply_text("❌ Iltimos, chekni rasm (foto) sifatida yuboring.")
        return STEP_CHECK

    file = await msg.photo[-1].get_file()
    img_path = TMP_DIR / f"check_{msg.message_id}.jpg"
    await file.download_to_drive(str(img_path))
    context.user_data["check_path"] = str(img_path)

    try:
        ocr_text = _ocr_image(img_path)
    except Exception as e:
        await msg.reply_text(f"❌ OCR xato: {e}")
        return ConversationHandler.END

    context.user_data["ocr_text"] = ocr_text

    amount = _extract_amount_uzs(ocr_text)
    date_iso = _extract_date_iso(ocr_text)

    context.user_data["amount_uzs"] = amount
    context.user_data["date_iso"] = date_iso

    order = context.user_data.get("order", {})
    preview = (
        "✅ Chek OCR natijasi:\n"
        f"🏷 Brend: {order.get('brand')}\n"
        f"👤 Mijoz: {order.get('client_name')}\n"
        f"📞 Tel: {order.get('phone_plus')}\n"
        f"💰 Summa: {f'{amount:,} UZS' if amount else 'TOPILMADI'}\n"
        f"📅 Sana: {date_iso if date_iso else 'TOPILMADI'}\n"
    )
    await msg.reply_text(preview)

    if amount is None:
        await msg.reply_text("💰 Summani topa olmadim. Summani faqat raqam qilib kiriting (masalan: 600000):")
        return STEP_AMOUNT

    if date_iso is None:
        await msg.reply_text("📅 Sanani topa olmadim. Sana kiriting (dd.mm.yyyy):")
        return STEP_DATE

    return await ask_sales_channel(msg, context)


async def handle_manual_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = re.sub(r"\D", "", update.message.text or "")
    if not raw:
        await update.message.reply_text("❌ Faqat raqam kiriting. Masalan: 600000")
        return STEP_AMOUNT

    val = int(raw)
    if val <= 0:
        await update.message.reply_text("❌ Summa 0 dan katta bo‘lishi kerak.")
        return STEP_AMOUNT

    context.user_data["amount_uzs"] = val

    if context.user_data.get("date_iso") is None:
        await update.message.reply_text("📅 Sana kiriting (dd.mm.yyyy):")
        return STEP_DATE

    return await ask_sales_channel(update.message, context)


async def handle_manual_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    try:
        dt = du_parser.parse(_normalize_month_words(raw), dayfirst=True, fuzzy=True)
        context.user_data["date_iso"] = dt.date().isoformat()
    except Exception:
        await update.message.reply_text("❌ Sana formati noto‘g‘ri. Qayta kiriting (dd.mm.yyyy):")
        return STEP_DATE

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
    await message.reply_text(
        "📊 Kanal prodaj (канал продаж) ni tanlang:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
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
    await query.edit_message_text(
        "To‘lov turini tanlang:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
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

    if amount <= 0:
        await query.edit_message_text("❌ Summa noto‘g‘ri. Qaytadan /kiritish qiling.")
        return ConversationHandler.END
    if not date_iso:
        await query.edit_message_text("❌ Sana yo‘q. Qaytadan /kiritish qiling.")
        return ConversationHandler.END
    if not sc_meta:
        await query.edit_message_text("❌ Sales channel yo‘q. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    try:
        org = get_default_organization()

        cp_name = f"{order.get('brand')} {order.get('client_name')}".strip()
        cp_phone_digits = str(order.get("phone_digits") or "").strip()
        cp = get_or_create_counterparty(cp_name, phone=cp_phone_digits)

        desc = (
            f"{order.get('brand')} | {order.get('client_name')} | {order.get('phone_plus')} | "
            f"Operator: {operator.get('name')} ({operator.get('phone')})"
        )

        if pt == "card":
            created = create_paymentin(
                organization_meta=org["meta"],
                agent_meta=cp["meta"],
                project_meta=None,
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
                project_meta=None,
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
