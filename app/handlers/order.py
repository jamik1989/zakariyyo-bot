# app/handlers/order.py
import re
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
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

from ..config import GROUP_CHAT_ID
from ..db import create_confirm_upsert
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

STEP_PAYTYPE, STEP_CP_SEARCH, STEP_CP_PICK, STEP_AMOUNT_DATE, STEP_CHECK, STEP_CHANNEL, STEP_REVIEW = range(7)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TG_TZ = ZoneInfo(os.getenv("TG_TZ", "Asia/Tashkent"))
MS_TZ = ZoneInfo(os.getenv("MOYSKLAD_TZ", "Europe/Moscow"))


def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _paytype_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Naqt", callback_data="pt:cash")],
        [InlineKeyboardButton("💳 Karta", callback_data="pt:card")],
    ])


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiq", callback_data="rv:confirm")],
        [InlineKeyboardButton("✏️ Tahrirlash", callback_data="rv:edit")],
    ])


def _edit_fields_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 Brend", callback_data="rv:field:brand")],
        [InlineKeyboardButton("👤 Mijoz nomi", callback_data="rv:field:client")],
        [InlineKeyboardButton("📞 Telefon", callback_data="rv:field:phone")],
        [InlineKeyboardButton("💰 Summa", callback_data="rv:field:amount")],
        [InlineKeyboardButton("📅 Sana", callback_data="rv:field:date")],
        [InlineKeyboardButton("🕒 Vaqt", callback_data="rv:field:time")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="rv:back")],
    ])


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _norm_brand(brand: str) -> str:
    return " ".join((brand or "").strip().upper().split())


def _normalize_phone_uz(phone_raw: str) -> str:
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


def _fmt_amount(amount: Optional[int]) -> str:
    if not isinstance(amount, int):
        return "TOPILMADI"
    return f"{amount:,}".replace(",", " ")


def _parse_amount(text: str) -> Optional[int]:
    d = _digits_only(text)
    if not d:
        return None
    if len(d) >= 13:
        return None
    val = int(d)
    if 1000 <= val <= 500_000_000:
        return val
    return None


def _parse_date(text: str) -> Optional[str]:
    s = (text or "").strip()
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", s)
    if not m:
        return None
    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return f"{y:04d}-{mo:02d}-{d:02d}"
    except Exception:
        return None


def _parse_time(text: str) -> Optional[str]:
    s = (text or "").strip()
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b", s)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _parse_brand_name_phone(text: str) -> Optional[Tuple[str, str, str]]:
    parts = [p.strip() for p in (text or "").strip().split("-", maxsplit=2)]
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

    if not isinstance(data, dict):
        return []

    rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


def _tg_now_as_ms_parts() -> Tuple[str, str]:
    dt_tg = datetime.now(TG_TZ)
    dt_ms = dt_tg.astimezone(MS_TZ)
    return dt_ms.strftime("%Y-%m-%d"), dt_ms.strftime("%H:%M:%S")


def _fmt_ms_to_tg(date_iso: Optional[str], time_hms: Optional[str]) -> str:
    if not date_iso:
        return "TOPILMADI"
    raw = f"{date_iso} {time_hms or '00:00:00'}"
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return raw


async def _ask_sales_channel(chat_update_obj, context: ContextTypes.DEFAULT_TYPE):
    channels = get_sales_channels(limit=50)
    if not channels:
        msg = "❌ MoySklad’da 'канал продаж' topilmadi. Avval sales channel yarating."
        if hasattr(chat_update_obj, "edit_message_text"):
            await chat_update_obj.edit_message_text(msg)
        else:
            await chat_update_obj.reply_text(msg)
        return ConversationHandler.END

    channels = channels[:10]
    context.user_data["channels_map"] = {str(c["id"]): c["meta"] for c in channels}

    kb = [[InlineKeyboardButton(c["name"], callback_data=f"sc:{c['id']}")] for c in channels]
    markup = InlineKeyboardMarkup(kb)

    text = (
        "🧾 *Kiritish — 6/7*\n"
        "📊 Kanal prodajni tanlang\n\n"
        "Quyidagilardan birini tanlang:"
    )

    if hasattr(chat_update_obj, "edit_message_text"):
        await chat_update_obj.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await chat_update_obj.reply_text(text, reply_markup=markup, parse_mode="Markdown")

    return STEP_CHANNEL


def _ensure_now_date_time(context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("date_iso") or not context.user_data.get("time_hms"):
        date_iso, time_hms = _tg_now_as_ms_parts()
        if not context.user_data.get("date_iso"):
            context.user_data["date_iso"] = date_iso
        if not context.user_data.get("time_hms"):
            context.user_data["time_hms"] = time_hms


def _card_line(label: str, value: str) -> str:
    return f"*{label}:* {value}"


def _build_review_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    cp = context.user_data.get("cp") or {}
    pt = context.user_data.get("paytype")
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    time_hms = context.user_data.get("time_hms")
    sc = context.user_data.get("sales_channel_meta")

    pay = "Naqt" if pt == "cash" else "Karta"
    cp_txt = _cp_title(cp) if cp else "TOPILMADI"
    amt_txt = _fmt_amount(amount)
    moment_txt = _fmt_ms_to_tg(date_iso, time_hms)
    ch_txt = "TANLANDI ✅" if sc else "TANLANMAGAN ❌"

    return (
        "🧾 *Kiritish — 7/7*\n"
        "━━━━━━━━━━━━━━\n"
        "✅ *Tekshiruv kartochkasi*\n\n"
        f"{_card_line('👤 Kontragent', cp_txt)}\n"
        f"{_card_line('💳 To‘lov turi', pay)}\n"
        f"{_card_line('💰 Summa', amt_txt + ' UZS')}\n"
        f"{_card_line('🕒 Vaqt', moment_txt)}\n"
        f"{_card_line('📊 Kanal', ch_txt)}\n\n"
        "━━━━━━━━━━━━━━\n"
        "Davom etamizmi?"
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
        "sales_channel_meta",
        "edit_target",
        "cp_last_q",
        "cp_new_text",
    ):
        context.user_data.pop(k, None)


def _infer_brand_client_from_cp_name(cp_name: str) -> Tuple[str, str]:
    s = (cp_name or "").strip()
    if not s:
        return "", ""
    parts = s.split(" ", 1)
    brand = parts[0].strip().upper()
    client = parts[1].strip() if len(parts) == 2 else ""
    return brand, client


async def kiritish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    await update.message.reply_text(
        "🧾 *Kiritish — 1/7*\n"
        "━━━━━━━━━━━━━━\n"
        "1) To‘lov turini tanlang:",
        reply_markup=_paytype_keyboard(),
        parse_mode="Markdown",
    )
    return STEP_PAYTYPE


async def on_paytype_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pt = (query.data or "").split("pt:", 1)[-1]
    if pt not in ("cash", "card"):
        return STEP_PAYTYPE

    context.user_data["paytype"] = pt

    await query.edit_message_text(
        "🧾 *Kiritish — 2/7*\n"
        "━━━━━━━━━━━━━━\n"
        "2) Kontragent qidirish:\n"
        "Brand / ism / telefon yozing.\n\n"
        "*Tez yaratish uchun:*\n"
        "`brendnomi-MijozNomi-910175253`",
        parse_mode="Markdown",
    )
    return STEP_CP_SEARCH


async def cp_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("❌ Qidiruv bo‘sh. Yozing.")
        return STEP_CP_SEARCH

    triple = _parse_brand_name_phone(q)
    if triple:
        try:
            brand, client_name, phone_plus = triple
            cp_name = f"{brand} {client_name}".strip()
            cp = get_or_create_counterparty(name=cp_name, phone=phone_plus)

            context.user_data["cp"] = {
                "id": cp.get("id"),
                "name": cp.get("name"),
                "phone": cp.get("phone"),
                "meta": cp.get("meta"),
            }

            pt = context.user_data.get("paytype")
            if pt == "card":
                await update.message.reply_text(
                    "🧾 *Kiritish — 3/7*\n"
                    "━━━━━━━━━━━━━━\n"
                    "3) Chek rasmini yuboring (foto).",
                    parse_mode="Markdown",
                )
                return STEP_CHECK

            context.user_data.pop("amount_uzs", None)
            context.user_data["date_iso"] = None
            context.user_data["time_hms"] = None
            context.user_data.pop("sales_channel_meta", None)

            await update.message.reply_text(
                "🧾 *Kiritish — 4/7*\n"
                "━━━━━━━━━━━━━━\n"
                "3) Summani kiriting (masalan: 5000000)",
                parse_mode="Markdown",
            )
            return STEP_AMOUNT_DATE
        except Exception as e:
            await update.message.reply_text(f"❌ Kontragent yaratishda xatolik: {e}")
            return STEP_CP_SEARCH

    context.user_data["cp_last_q"] = q

    try:
        rows = _search_counterparties(q, limit=10) or []
    except Exception as e:
        await update.message.reply_text(f"❌ Qidiruvda xatolik: {e}")
        return STEP_CP_SEARCH

    context.user_data["cp_candidates"] = {str(r.get("id")): r for r in rows if r.get("id")}

    kb: List[List[InlineKeyboardButton]] = []
    for r in rows[:10]:
        rid = r.get("id")
        if rid:
            kb.append([InlineKeyboardButton(_cp_title(r), callback_data=f"cp:{rid}")])

    context.user_data["cp_new_text"] = q
    kb.append([InlineKeyboardButton("➕ Topilmasa yaratish", callback_data="cpnew:1")])

    if not rows:
        await update.message.reply_text(
            "❌ Kontragent topilmadi.\n\n"
            "➕ Yangi yaratish uchun pastdagi tugmani bosing yoki format yuboring:\n"
            "BRAND-MijozNomi-910175253\n"
            "Masalan: LEAP-Akmal-910175253",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return STEP_CP_PICK

    await update.message.reply_text(
        "🧾 *Kiritish — 3/7*\n"
        "━━━━━━━━━━━━━━\n"
        "Topilgan kontragentlar:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )
    return STEP_CP_PICK


async def on_cp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = (query.data or "").strip()

    if data.startswith("cpnew:"):
        return await on_cp_create_new(update, context)

    if not data.startswith("cp:"):
        await query.edit_message_text("❌ Noto‘g‘ri tanlov. Qaytadan /kiritish qiling.", reply_markup=None)
        return ConversationHandler.END

    cp_id = data.split("cp:", 1)[-1].strip()
    cand = (context.user_data.get("cp_candidates") or {}).get(str(cp_id))

    if not cand:
        last_q = (context.user_data.get("cp_last_q") or "").strip()
        if last_q:
            try:
                rows = _search_counterparties(last_q, limit=20) or []
                for r in rows:
                    if str(r.get("id")) == str(cp_id):
                        cand = r
                        break
            except Exception:
                cand = None

    if not cand:
        await query.edit_message_text("❌ Kontragent topilmadi. Qaytadan /kiritish qiling.", reply_markup=None)
        return ConversationHandler.END

    context.user_data["cp"] = {
        "id": cand.get("id"),
        "name": cand.get("name"),
        "phone": cand.get("phone"),
        "meta": cand.get("meta"),
    }

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text(
            "🧾 *Kiritish — 3/7*\n"
            "━━━━━━━━━━━━━━\n"
            "3) Chek rasmini yuboring (foto).",
            parse_mode="Markdown",
        )
        return STEP_CHECK

    context.user_data.pop("amount_uzs", None)
    context.user_data["date_iso"] = None
    context.user_data["time_hms"] = None
    context.user_data.pop("sales_channel_meta", None)

    await query.edit_message_text(
        "🧾 *Kiritish — 4/7*\n"
        "━━━━━━━━━━━━━━\n"
        "3) Summani kiriting (masalan: 5000000)",
        parse_mode="Markdown",
    )
    return STEP_AMOUNT_DATE


async def on_cp_create_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = (context.user_data.get("cp_new_text") or "").strip()
    if not raw:
        raw = (query.data or "").split("cpnew:", 1)[-1].strip()

    phone_plus = ""
    digits = _digits_only(raw)
    if len(digits) >= 7:
        phone_plus = _normalize_phone_uz(digits)

    name = raw or "New Counterparty"
    cp = get_or_create_counterparty(name=name, phone=(phone_plus or None))

    context.user_data["cp"] = {
        "id": cp.get("id"),
        "name": cp.get("name"),
        "phone": cp.get("phone"),
        "meta": cp.get("meta"),
    }

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text(
            "🧾 *Kiritish — 3/7*\n"
            "━━━━━━━━━━━━━━\n"
            "3) Chek rasmini yuboring (foto).",
            parse_mode="Markdown",
        )
        return STEP_CHECK

    context.user_data.pop("amount_uzs", None)
    context.user_data["date_iso"] = None
    context.user_data["time_hms"] = None
    context.user_data.pop("sales_channel_meta", None)

    await query.edit_message_text(
        "🧾 *Kiritish — 4/7*\n"
        "━━━━━━━━━━━━━━\n"
        "3) Summani kiriting (masalan: 5000000)",
        parse_mode="Markdown",
    )
    return STEP_AMOUNT_DATE


async def handle_manual_amount_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    target = context.user_data.get("edit_target")

    if target == "amount" or (context.user_data.get("paytype") == "cash" and not target):
        amount = _parse_amount(text)
        if amount is None:
            await update.message.reply_text("❌ Summa noto‘g‘ri. Masalan: 5000000")
            return STEP_AMOUNT_DATE

        context.user_data["amount_uzs"] = amount
        _ensure_now_date_time(context)
        context.user_data.pop("edit_target", None)

        if context.user_data.get("paytype") == "cash" and not context.user_data.get("sales_channel_meta"):
            return await _ask_sales_channel(update.message, context)

        await update.message.reply_text(_build_review_text(context), reply_markup=_review_keyboard(), parse_mode="Markdown")
        return STEP_REVIEW

    if target == "brand":
        b = _norm_brand(text)
        if not b:
            await update.message.reply_text("❌ Brend bo‘sh bo‘lmasin.")
            return STEP_AMOUNT_DATE
        cp = context.user_data.get("cp") or {}
        old_name = (cp.get("name") or "").strip()
        parts = old_name.split(" ", 1)
        client = parts[1] if len(parts) == 2 else ""
        cp_name = f"{b} {client}".strip() if client else b
        new_cp = get_or_create_counterparty(name=cp_name, phone=cp.get("phone"))
        context.user_data["cp"] = {
            "id": new_cp.get("id"),
            "name": new_cp.get("name"),
            "phone": new_cp.get("phone"),
            "meta": new_cp.get("meta"),
        }
        context.user_data.pop("edit_target", None)

    elif target == "client":
        name = (text or "").strip()
        if not name:
            await update.message.reply_text("❌ Mijoz nomi bo‘sh bo‘lmasin.")
            return STEP_AMOUNT_DATE
        cp = context.user_data.get("cp") or {}
        old_name = (cp.get("name") or "").strip()
        brand = old_name.split(" ", 1)[0] if old_name else ""
        cp_name = f"{brand} {name}".strip() if brand else name
        new_cp = get_or_create_counterparty(name=cp_name, phone=cp.get("phone"))
        context.user_data["cp"] = {
            "id": new_cp.get("id"),
            "name": new_cp.get("name"),
            "phone": new_cp.get("phone"),
            "meta": new_cp.get("meta"),
        }
        context.user_data.pop("edit_target", None)

    elif target == "phone":
        phone_plus = _normalize_phone_uz(text)
        if not phone_plus:
            await update.message.reply_text("❌ Telefon noto‘g‘ri. Masalan: 910175253 yoki +998910175253")
            return STEP_AMOUNT_DATE
        cp = context.user_data.get("cp") or {}
        new_cp = get_or_create_counterparty(name=cp.get("name") or "NoName", phone=phone_plus)
        context.user_data["cp"] = {
            "id": new_cp.get("id"),
            "name": new_cp.get("name"),
            "phone": new_cp.get("phone"),
            "meta": new_cp.get("meta"),
        }
        context.user_data.pop("edit_target", None)

    elif target == "date":
        d = _parse_date(text)
        if not d:
            await update.message.reply_text("❌ Sana noto‘g‘ri. Masalan: 28.01.2026")
            return STEP_AMOUNT_DATE
        context.user_data["date_iso"] = d
        context.user_data.pop("edit_target", None)

    elif target == "time":
        t = _parse_time(text)
        if not t:
            await update.message.reply_text("❌ Vaqt noto‘g‘ri. Masalan: 14:23")
            return STEP_AMOUNT_DATE
        context.user_data["time_hms"] = t
        context.user_data.pop("edit_target", None)

    else:
        amount = _parse_amount(text)
        if amount is not None:
            context.user_data["amount_uzs"] = amount
            _ensure_now_date_time(context)
            await update.message.reply_text(_build_review_text(context), reply_markup=_review_keyboard(), parse_mode="Markdown")
            return STEP_REVIEW

        await update.message.reply_text("❌ Kiritish noto‘g‘ri.")
        return STEP_AMOUNT_DATE

    await update.message.reply_text(_build_review_text(context), reply_markup=_review_keyboard(), parse_mode="Markdown")
    return STEP_REVIEW


async def handle_check_optional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if context.user_data.get("paytype") != "card":
        await msg.reply_text("❌ Bu bosqich karta uchun. /kiritish dan qaytadan boshlang.", reply_markup=_menu_keyboard())
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

    amount, date_iso, time_hms, raw_text = detect_amount_date_time(str(img_path))
    context.user_data["amount_uzs"] = amount if isinstance(amount, int) else None
    context.user_data["date_iso"] = date_iso
    context.user_data["time_hms"] = time_hms
    context.user_data["ocr_text"] = raw_text

    _ensure_now_date_time(context)

    ocr_date = context.user_data.get("date_iso")
    ocr_time = context.user_data.get("time_hms")
    if ocr_date and ocr_time:
        moment_preview = _fmt_ms_to_tg(ocr_date, ocr_time)
        await msg.reply_text(
            f"🕒 Chekdan topildi: {moment_preview}\n"
            f"Agar noto‘g‘ri bo‘lsa, keyin *Tahrirlash* orqali o‘zgartirishingiz mumkin.",
            parse_mode="Markdown",
        )

    if not isinstance(context.user_data.get("amount_uzs"), int):
        await msg.reply_text(
            "🧾 *Kiritish — 4/7*\n"
            "━━━━━━━━━━━━━━\n"
            "✏️ Summa topilmadi. Summani kiriting (masalan: 5000000)",
            parse_mode="Markdown",
        )
        return STEP_AMOUNT_DATE

    return await _ask_sales_channel(msg, context)


async def on_sales_channel_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sc_id = (query.data or "").split("sc:", 1)[-1]
    sc_meta = (context.user_data.get("channels_map") or {}).get(str(sc_id))
    if not sc_meta:
        await query.edit_message_text("❌ Kanal topilmadi. Qaytadan /kiritish qiling.", reply_markup=None)
        return ConversationHandler.END

    context.user_data["sales_channel_meta"] = sc_meta

    await query.edit_message_text(_build_review_text(context), reply_markup=_review_keyboard(), parse_mode="Markdown")
    return STEP_REVIEW


async def on_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = (query.data or "").split("rv:", 1)[-1]

    if action == "edit":
        await query.edit_message_text("Qaysini tahrirlaymiz?", reply_markup=_edit_fields_keyboard())
        return STEP_REVIEW

    if action == "back":
        await query.edit_message_text(_build_review_text(context), reply_markup=_review_keyboard(), parse_mode="Markdown")
        return STEP_REVIEW

    if action.startswith("field:"):
        field = action.split("field:", 1)[-1]
        context.user_data["edit_target"] = field

        prompts = {
            "brand": "🏷 Brend nomini kiriting:",
            "client": "👤 Mijoz nomini kiriting:",
            "phone": "📞 Telefonni kiriting (+998... yoki 9 ta raqam):",
            "amount": "💰 Summani kiriting (masalan: 5000000):",
            "date": "📅 Sanani kiriting (masalan: 28.01.2026):",
            "time": "🕒 Vaqtni kiriting (masalan: 14:23):",
        }
        await query.edit_message_text(prompts.get(field, "Qiymatni kiriting:"))
        return STEP_AMOUNT_DATE

    if action != "confirm":
        return STEP_REVIEW

    pt = context.user_data.get("paytype")
    cp = context.user_data.get("cp") or {}
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    time_hms = context.user_data.get("time_hms")
    sc_meta = context.user_data.get("sales_channel_meta")
    check_path = context.user_data.get("check_path")
    operator = context.user_data.get("operator", {})

    if pt not in ("cash", "card") or not cp.get("meta") or not sc_meta or not isinstance(amount, int) or amount <= 0 or not date_iso:
        await query.edit_message_text("❌ Ma’lumot yetarli emas. Qaytadan /kiritish qiling.", reply_markup=None)
        return ConversationHandler.END

    org = get_default_organization()
    desc = f"Counterparty: {cp.get('name')} | Phone: {cp.get('phone') or ''} | Operator: {operator.get('name')} ({operator.get('phone')})"

    created = None
    doc_kind = "Hujjat"

    if pt == "card":
        created = create_paymentin(
            organization_meta=org["meta"],
            agent_meta=cp["meta"],
            sales_channel_meta=sc_meta,
            sum_uzs=amount,
            date_iso=date_iso,
            time_hms=time_hms,
            description=desc,
        )
        doc_kind = "Входящий платёж"
        if created and created.get("id") and check_path and os.path.exists(check_path):
            attach_file_to_paymentin(str(created["id"]), str(check_path))
    else:
        created = create_cashin(
            organization_meta=org["meta"],
            agent_meta=cp["meta"],
            sales_channel_meta=sc_meta,
            sum_uzs=amount,
            date_iso=date_iso,
            time_hms=time_hms,
            description=desc,
        )
        doc_kind = "Приходный ордер"
        if created and created.get("id") and check_path and os.path.exists(check_path):
            attach_file_to_cashin(str(created["id"]), str(check_path))

    try:
        op_id = int(operator.get("id") or 0)
        cp_name = (cp.get("name") or "").strip()
        brand, client_name = _infer_brand_client_from_cp_name(cp_name)
        phone_plus = (cp.get("phone") or "").strip()

        if op_id and cp.get("meta"):
            create_confirm_upsert(
                operator_id=op_id,
                brand=brand or (cp_name.split(" ", 1)[0] if cp_name else "N/A"),
                client_name=client_name,
                phone_plus=phone_plus,
                counterparty_meta=cp["meta"],
            )
    except Exception:
        pass

    try:
        await query.message.delete()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="✅ Sizning maʼlumotlaringiz yuborildi.",
        reply_markup=_menu_keyboard(),
    )

    if GROUP_CHAT_ID and created:
        moment_show = _fmt_ms_to_tg(date_iso, time_hms)
        caption = (
            f"✅ {doc_kind} (черновик)\n\n"
            f"👤 Kontragent: {_cp_title(cp)}\n"
            f"💳 To‘lov turi: {'Naqt' if pt=='cash' else 'Karta'}\n"
            f"💰 Summa: {_fmt_amount(amount)} UZS\n"
            f"🕒 Vaqt: {moment_show}\n"
            f"👨‍💼 Operator: {operator.get('name')} ({operator.get('phone')})\n"
            f"🧾 MoySklad: {created.get('name','N/A')}"
        )
        if check_path and os.path.exists(check_path):
            with open(check_path, "rb") as f:
                await context.bot.send_photo(chat_id=GROUP_CHAT_ID, photo=f, caption=caption)
        else:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=caption)

    _cleanup_after_done(context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END