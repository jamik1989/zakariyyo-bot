# app/handlers/order.py
import re
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any, Set
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes, ConversationHandler
from dateutil import parser as du_parser

from ..config import GROUP_CHAT_ID
from ..services.moysklad import (
    ms_get,
    get_sales_channels,
    get_default_organization,
    get_or_create_counterparty,
    create_paymentin,
    create_cashin,
    attach_file_to_paymentin,
    attach_file_to_cashin,
)
from ..services.vision import detect_amount_date_time

# /kiritish -> paytype -> cp_search -> cp_pick/create -> (cash: fix fields) (card: check+ocr then fix missing) -> channel -> send
STEP_PAYTYPE, STEP_CP_SEARCH, STEP_CP_PICK, STEP_AMOUNT_DATE, STEP_CHECK, STEP_CHANNEL, STEP_REVIEW = range(7)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("Asia/Tashkent")


# ---------------- UI ----------------

def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("/kiritish")],
            [KeyboardButton("/start")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _paytype_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("💵 Naqt", callback_data="pt:cash")],
        [InlineKeyboardButton("💳 Karta", callback_data="pt:card")],
    ]
    return InlineKeyboardMarkup(kb)


def _review_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("✅ Tasdiq", callback_data="rv:confirm")],
        [InlineKeyboardButton("✏️ Tuzatish", callback_data="rv:edit")],
    ]
    return InlineKeyboardMarkup(kb)


# ---------------- helpers ----------------

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _norm_brand(brand: str) -> str:
    return " ".join((brand or "").strip().upper().split())


def _normalize_phone_uz(phone_raw: str) -> str:
    """
    Qabul qiladi:
      - 910175253
      - +998910175253
      - 998910175253
      - 91 017 52 53
    Natija: +998XXXXXXXXX
    """
    digits = _digits_only(phone_raw)
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


def _parse_date_only(text: str) -> Optional[str]:
    s = (text or "").strip()
    if not s:
        return None

    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", s)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return f"{y:04d}-{mo:02d}-{d:02d}"
        except Exception:
            pass

    try:
        dt = du_parser.parse(_normalize_month_words(s), dayfirst=True, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def _parse_time_only(text: str) -> Optional[str]:
    s = (text or "").strip()
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _parse_amount_only(text: str) -> Optional[int]:
    s = (text or "").strip()
    digits = _digits_only(s)
    if not digits:
        return None

    # karta raqami bo‘lishi mumkin (13+)
    if len(digits) >= 13:
        return None

    val = int(digits)
    if 1000 <= val <= 500_000_000:
        return val
    return None


def _parse_amount_date_time_flexible(text: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Qabul qiladi:
    - 400000
    - 28.01.2026
    - 14:23
    - 400000-28.01.2026
    - 400000-28.01.2026 14:23
    """
    s = (text or "").strip()
    if not s:
        return None, None, None

    time_hms = _parse_time_only(s)
    if time_hms:
        s_wo_time = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b", "", s).strip()
    else:
        s_wo_time = s

    m = re.match(r"^\s*([0-9][0-9\s.,]{2,20})\s*[-/,]\s*(.+?)\s*$", s_wo_time)
    if m:
        amount = _parse_amount_only(m.group(1))
        date_iso = _parse_date_only(m.group(2))
        return amount, date_iso, time_hms

    amount = _parse_amount_only(s_wo_time)
    date_iso = _parse_date_only(s_wo_time)

    if date_iso and amount and re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", s_wo_time):
        amount = None

    return amount, date_iso, time_hms


def _parse_brand_name_phone(text: str) -> Optional[Tuple[str, str, str]]:
    """
    "brend-mijoz-910..." formatini ushlaydi.
    Qaytaradi: (BRAND_UPPER, client_name, phone_plus)
    """
    s = (text or "").strip()
    parts = [p.strip() for p in s.split("-", maxsplit=2)]
    if len(parts) != 3:
        return None

    brand_raw, client_name, phone_raw = parts
    brand = _norm_brand(brand_raw)
    if not brand or not client_name:
        return None

    phone_plus = _normalize_phone_uz(phone_raw)
    if not phone_plus:
        return None

    return brand, client_name, phone_plus


def _cp_title(cp: Dict[str, Any]) -> str:
    name = (cp.get("name") or "").strip() or "NoName"
    phone = (cp.get("phone") or "").strip()
    return f"{name} ({phone})" if phone else name


def _search_counterparties(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    digits = _digits_only(q)
    if len(digits) >= 7:
        data = ms_get("/entity/counterparty", params={"filter": f"phone~{digits}", "limit": limit})
    else:
        data = ms_get("/entity/counterparty", params={"search": q, "limit": limit})
    return data.get("rows", []) or []


async def _ask_sales_channel(chat_update_obj, context: ContextTypes.DEFAULT_TYPE):
    try:
        channels = get_sales_channels(limit=50)
    except Exception as e:
        if hasattr(chat_update_obj, "edit_message_text"):
            await chat_update_obj.edit_message_text(f"❌ Kanal olishda xatolik: {e}")
        else:
            await chat_update_obj.reply_text(f"❌ Kanal olishda xatolik: {e}")
        return ConversationHandler.END

    if not channels:
        msg = "❌ MoySklad’da 'канал продаж' topilmadi. Avval sales channel yarating."
        if hasattr(chat_update_obj, "edit_message_text"):
            await chat_update_obj.edit_message_text(msg)
        else:
            await chat_update_obj.reply_text(msg)
        return ConversationHandler.END

    channels = channels[:10]
    context.user_data["channels_map"] = {c["id"]: c["meta"] for c in channels}

    keyboard = [[InlineKeyboardButton(c["name"], callback_data=f"sc:{c['id']}")] for c in channels]
    markup = InlineKeyboardMarkup(keyboard)

    if hasattr(chat_update_obj, "edit_message_text"):
        await chat_update_obj.edit_message_text("📊 Kanal prodaj (канал продаж) ni tanlang:", reply_markup=markup)
    else:
        await chat_update_obj.reply_text("📊 Kanal prodaj (канал продаж) ni tanlang:", reply_markup=markup)

    return STEP_CHANNEL


def _missing_fields(context: ContextTypes.DEFAULT_TYPE) -> Set[str]:
    missing: Set[str] = set()
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    time_hms = context.user_data.get("time_hms")

    if not isinstance(amount, int) or amount <= 0:
        missing.add("amount")
    if not date_iso:
        missing.add("date")
    if not time_hms:
        missing.add("time")
    return missing


def _prompt_for_missing(missing: Set[str]) -> str:
    parts = []
    if "amount" in missing:
        parts.append("💰 Summani kiriting (masalan: 400000)")
    if "date" in missing:
        parts.append("📅 Sanani kiriting (masalan: 28.01.2026)")
    if "time" in missing:
        parts.append("🕒 Vaqtni kiriting (masalan: 14:23)")

    if not parts:
        return "✅ Hammasi topildi."

    return (
        "✏️ Chekdan hammasi topilmadi.\n"
        "Faqat yetishmagan ma’lumotni kiriting.\n\n"
        + "\n".join(parts)
        + "\n\n"
        "Agar xohlasangiz hammasini bitta qatorda ham yuborishingiz mumkin:\n"
        "400000-28.01.2026 14:23"
    )


def _cleanup_after_done(context: ContextTypes.DEFAULT_TYPE):
    for k in (
        "paytype",
        "cp_query",
        "cp_candidates",
        "cp",
        "amount_uzs",
        "date_iso",
        "time_hms",
        "check_path",
        "ocr_text",
        "channels_map",
    ):
        context.user_data.pop(k, None)


# ---------------- flow ----------------

async def kiritish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.")
        return ConversationHandler.END

    await update.message.reply_text("1) To‘lov turini tanlang:", reply_markup=_paytype_keyboard())
    return STEP_PAYTYPE


async def on_paytype_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pt = (query.data or "").split("pt:", 1)[-1]
    if pt not in ("cash", "card"):
        return STEP_PAYTYPE

    context.user_data["paytype"] = pt

    await query.edit_message_text(
        "2) Kontragent qidirish:\n"
        "Brand / ism / telefon yozing.\n\n"
        "Yoki tez yaratish uchun shunday yozing:\n"
        "brendnomi-MijozNomi-910175253\n\n"
        "Misol:\n"
        "- NIKE\n"
        "- Azamat\n"
        "- 998901234567"
    )
    return STEP_CP_SEARCH


async def cp_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("❌ Qidiruv bo‘sh. Brand/ism/tel yozing.")
        return STEP_CP_SEARCH

    # ✅ FAST CREATE: brand-name-phone
    triple = _parse_brand_name_phone(q)
    if triple:
        brand, client_name, phone_plus = triple
        cp_name = f"{brand} {client_name}".strip()

        try:
            cp = get_or_create_counterparty(name=cp_name, phone=phone_plus)
        except Exception as e:
            await update.message.reply_text(f"❌ Kontragent yaratishda xatolik: {e}")
            return STEP_CP_SEARCH

        context.user_data["cp"] = {
            "id": cp.get("id"),
            "name": cp.get("name"),
            "phone": cp.get("phone"),
            "meta": cp.get("meta"),
        }

        pt = context.user_data.get("paytype")
        if pt == "card":
            await update.message.reply_text("3) Chek rasmini yuboring (foto).")
            return STEP_CHECK

        await update.message.reply_text(
            "3) Naqt uchun summa, sana, (ixtiyoriy) vaqt kiriting.\n"
            "Masalan: 600000-28.01.2026 14:23"
        )
        context.user_data.pop("amount_uzs", None)
        context.user_data.pop("date_iso", None)
        context.user_data.pop("time_hms", None)
        return STEP_AMOUNT_DATE

    # ✅ normal search flow
    context.user_data["cp_query"] = q

    try:
        rows = _search_counterparties(q, limit=10)
    except Exception as e:
        await update.message.reply_text(f"❌ Kontragent qidirishda xatolik: {e}")
        return STEP_CP_SEARCH

    context.user_data["cp_candidates"] = {r["id"]: r for r in rows if r.get("id")}

    keyboard = []
    for r in rows[:10]:
        rid = r.get("id")
        if rid:
            keyboard.append([InlineKeyboardButton(_cp_title(r), callback_data=f"cp:{rid}")])

    keyboard.append([InlineKeyboardButton("➕ Yangi kontragent yaratish", callback_data=f"cpnew:{q}")])

    await update.message.reply_text("Topilgan kontragentlar:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STEP_CP_PICK


async def on_cp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cp_id = (query.data or "").split("cp:", 1)[-1]
    cand = (context.user_data.get("cp_candidates") or {}).get(cp_id)
    if not cand:
        await query.edit_message_text("❌ Kontragent topilmadi. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    context.user_data["cp"] = {
        "id": cand.get("id"),
        "name": cand.get("name"),
        "phone": cand.get("phone"),
        "meta": cand.get("meta"),
    }

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text("3) Chek rasmini yuboring (foto).")
        return STEP_CHECK

    await query.edit_message_text(
        "3) Naqt uchun summa, sana, (ixtiyoriy) vaqt kiriting.\n"
        "Masalan:\n"
        "600000-28.01.2026 14:23\n"
        "yoki faqat:\n"
        "600000-28.01.2026"
    )
    context.user_data.pop("amount_uzs", None)
    context.user_data.pop("date_iso", None)
    context.user_data.pop("time_hms", None)
    return STEP_AMOUNT_DATE


async def on_cp_create_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = (query.data or "").split("cpnew:", 1)[-1].strip()
    name = raw or "New Counterparty"

    # Agar telefon ko‘rinishida bo‘lsa — +998 qilib yuboramiz
    phone_plus = ""
    digits = _digits_only(raw)
    if len(digits) >= 7:
        phone_plus = _normalize_phone_uz(digits)

    try:
        cp = get_or_create_counterparty(name=name, phone=(phone_plus or None))
    except Exception as e:
        await query.edit_message_text(f"❌ Yangi kontragent yaratishda xatolik: {e}")
        return ConversationHandler.END

    context.user_data["cp"] = {
        "id": cp.get("id"),
        "name": cp.get("name"),
        "phone": cp.get("phone"),
        "meta": cp.get("meta"),
    }

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text("3) Chek rasmini yuboring (foto).")
        return STEP_CHECK

    await query.edit_message_text(
        "3) Naqt uchun summa, sana, (ixtiyoriy) vaqt kiriting.\n"
        "Masalan: 600000-28.01.2026 14:23"
    )
    context.user_data.pop("amount_uzs", None)
    context.user_data.pop("date_iso", None)
    context.user_data.pop("time_hms", None)
    return STEP_AMOUNT_DATE


async def handle_manual_amount_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Endi “faqat topilmaganini” to‘ldiradi.
    """
    text = update.message.text or ""
    amount, date_iso, time_hms = _parse_amount_date_time_flexible(text)

    if amount is not None:
        context.user_data["amount_uzs"] = int(amount)
    if date_iso is not None:
        context.user_data["date_iso"] = str(date_iso)
    if time_hms is not None:
        context.user_data["time_hms"] = str(time_hms)

    missing = _missing_fields(context)
    if missing:
        await update.message.reply_text(_prompt_for_missing(missing))
        return STEP_AMOUNT_DATE

    pt = context.user_data.get("paytype")
    if pt == "card":
        await update.message.reply_text("✅ Ma’lumotlar tayyor.\nTo‘g‘rimi?", reply_markup=_review_keyboard())
        return STEP_REVIEW

    return await _ask_sales_channel(update.message, context)


async def handle_check_optional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    pt = context.user_data.get("paytype")

    if pt != "card":
        await msg.reply_text("❌ Bu bosqich karta uchun. /kiritish dan qaytadan boshlang.")
        return ConversationHandler.END

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

    # OCR (Vision)
    try:
        amount, date_iso, time_hms, raw_text = detect_amount_date_time(str(img_path))
        context.user_data["amount_uzs"] = amount if isinstance(amount, int) else None
        context.user_data["date_iso"] = date_iso
        context.user_data["time_hms"] = time_hms
        context.user_data["ocr_text"] = raw_text
    except Exception as e:
        await msg.reply_text(
            f"❌ OCR xatolik: {e}\n\n"
            "✏️ Summani / sanani / vaqtni qo‘lda kiriting.\n"
            "Masalan: 400000-28.01.2026 14:23"
        )
        return STEP_AMOUNT_DATE

    # ✅ TALAB: agar chekda sana/vaqt topilmasa -> avtomatik hozirgi real sana+vaqt qo'yamiz
    now = datetime.now(TZ)
    if not context.user_data.get("date_iso"):
        context.user_data["date_iso"] = now.date().isoformat()
    if not context.user_data.get("time_hms"):
        context.user_data["time_hms"] = now.strftime("%H:%M:%S")

    # amount topilmagan bo‘lsa — faqat amountni so‘raymiz
    missing = _missing_fields(context)
    # date/time endi auto bo'ldi, shuning uchun ular missing bo'lmaydi

    if missing:
        await msg.reply_text(_prompt_for_missing(missing))
        return STEP_AMOUNT_DATE

    a_show = f"{context.user_data['amount_uzs']:,} UZS"
    d_show = context.user_data["date_iso"]
    t_show = context.user_data["time_hms"]

    await msg.reply_text(
        "✅ Chek o‘qildi.\n\n"
        f"💵 Summa: {a_show}\n"
        f"📅 Sana: {d_show}\n"
        f"🕒 Vaqt: {t_show}\n\n"
        "To‘g‘rimi?",
        reply_markup=_review_keyboard(),
    )
    return STEP_REVIEW


async def on_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = (query.data or "").split("rv:", 1)[-1]

    if action == "edit":
        missing = _missing_fields(context)
        await query.edit_message_text(_prompt_for_missing(missing))
        return STEP_AMOUNT_DATE

    if action != "confirm":
        return STEP_REVIEW

    missing = _missing_fields(context)
    if missing:
        await query.edit_message_text(_prompt_for_missing(missing))
        return STEP_AMOUNT_DATE

    return await _ask_sales_channel(query, context)


async def on_sales_channel_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sc_id = (query.data or "").split("sc:", 1)[-1]
    sc_meta = (context.user_data.get("channels_map") or {}).get(sc_id)
    if not sc_meta:
        await query.edit_message_text("❌ Kanal topilmadi. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    operator = context.user_data.get("operator", {})
    pt = context.user_data.get("paytype")
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    time_hms = context.user_data.get("time_hms")
    check_path = context.user_data.get("check_path")
    cp = context.user_data.get("cp") or {}

    if pt not in ("cash", "card") or not isinstance(amount, int) or amount <= 0 or not date_iso or not cp.get("meta"):
        await query.edit_message_text("❌ Ma’lumot yetarli emas. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    try:
        org = get_default_organization()
        desc = (
            f"Counterparty: {cp.get('name')} | Phone: {cp.get('phone') or ''} | "
            f"Operator: {operator.get('name')} ({operator.get('phone')})"
        )

        if pt == "card":
            created = create_paymentin(
                organization_meta=org["meta"],
                agent_meta=cp["meta"],
                sales_channel_meta=sc_meta,
                sum_uzs=amount,
                date_iso=date_iso,
                description=desc,
                time_hms=time_hms,
            )
            doc_kind = "Входящий платёж"
            if created.get("id") and check_path and os.path.exists(check_path):
                attach_file_to_paymentin(str(created["id"]), str(check_path))
        else:
            created = create_cashin(
                organization_meta=org["meta"],
                agent_meta=cp["meta"],
                sales_channel_meta=sc_meta,
                sum_uzs=amount,
                date_iso=date_iso,
                description=desc,
                time_hms=time_hms,
            )
            doc_kind = "Приходный ордер"
            if created.get("id") and check_path and os.path.exists(check_path):
                attach_file_to_cashin(str(created["id"]), str(check_path))

        await query.edit_message_text(
            f"✅ MoySklad’ga {doc_kind} yuborildi (черновик).\n"
            f"📄 Doc: {created.get('name','N/A')}\n"
            f"🆔 ID: {created.get('id','N/A')}"
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="✅ Tayyor. Keyingi buyurtma uchun /kiritish ni bosing.",
            reply_markup=_menu_keyboard(),
        )

        if GROUP_CHAT_ID:
            caption = (
                f"✅ {doc_kind} (черновik)\n\n"
                f"👤 Kontragent: {_cp_title(cp)}\n"
                f"💳 To‘lov turi: {'Naqt' if pt=='cash' else 'Karta'}\n"
                f"💵 Summa: {amount:,} UZS\n"
                f"📅 Sana: {date_iso}\n"
                f"🕒 Vaqt: {time_hms or '00:00:00'}\n"
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

    _cleanup_after_done(context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END
