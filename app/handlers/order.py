# app/handlers/order.py
import re
import os
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any

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
from ..services.vision import detect_amount_and_date

# /kiritish -> paytype -> cp_search -> cp_pick/create -> (cash: amount/date) (card: check+ocr) -> channel -> done
STEP_PAYTYPE, STEP_CP_SEARCH, STEP_CP_PICK, STEP_AMOUNT_DATE, STEP_CHECK, STEP_CHANNEL, STEP_REVIEW = range(7)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish")], [KeyboardButton("/start")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _paytype_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💵 Naqt", callback_data="pt:cash")],
            [InlineKeyboardButton("💳 Karta", callback_data="pt:card")],
        ]
    )


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Tasdiq", callback_data="rv:confirm")],
            [InlineKeyboardButton("✏️ Tuzatish", callback_data="rv:edit")],
        ]
    )


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


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


def _parse_amount_only(text: str) -> Optional[int]:
    digits = _digits_only(text)
    if not digits:
        return None
    val = int(digits)
    if 1_000 <= val <= 500_000_000:
        return val
    return None


def _parse_date_only(text: str) -> Optional[str]:
    s = (text or "").strip()
    if not s:
        return None
    try:
        dt = du_parser.parse(_normalize_month_words(s), dayfirst=True, fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def _parse_amount_date_one_line(text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    600000-28.01.2026
    600000 / 28.01.2026
    """
    s = (text or "").strip()
    m = re.match(r"^\s*([0-9][0-9\s.,]{2,20})\s*[-/,]\s*(.+?)\s*$", s)
    if not m:
        return None, None
    amount = _parse_amount_only(m.group(1))
    date_iso = _parse_date_only(m.group(2))
    return amount, date_iso


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


async def _ask_sales_channel(chat_obj, context: ContextTypes.DEFAULT_TYPE):
    try:
        channels = get_sales_channels(limit=50)
    except Exception as e:
        if hasattr(chat_obj, "edit_message_text"):
            await chat_obj.edit_message_text(f"❌ Kanal olishda xatolik: {e}")
        else:
            await chat_obj.reply_text(f"❌ Kanal olishda xatolik: {e}")
        return ConversationHandler.END

    if not channels:
        msg = "❌ MoySklad’da 'канал продаж' topilmadi. Avval sales channel yarating."
        if hasattr(chat_obj, "edit_message_text"):
            await chat_obj.edit_message_text(msg)
        else:
            await chat_obj.reply_text(msg)
        return ConversationHandler.END

    channels = channels[:10]
    context.user_data["channels_map"] = {c["id"]: c["meta"] for c in channels}

    kb = [[InlineKeyboardButton(c["name"], callback_data=f"sc:{c['id']}")] for c in channels]
    markup = InlineKeyboardMarkup(kb)

    if hasattr(chat_obj, "edit_message_text"):
        await chat_obj.edit_message_text("📊 Kanal prodaj (канал продаж) ni tanlang:", reply_markup=markup)
    else:
        await chat_obj.reply_text("📊 Kanal prodaj (канал продаж) ni tanlang:", reply_markup=markup)

    return STEP_CHANNEL


def _cleanup_after_done(context: ContextTypes.DEFAULT_TYPE):
    for k in (
        "paytype",
        "cp_query",
        "cp_candidates",
        "cp",
        "amount_uzs",
        "date_iso",
        "check_path",
        "ocr_text",
        "channels_map",
        # NEW flags
        "need_amount",
        "need_date",
    ):
        context.user_data.pop(k, None)


async def kiritish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.")
        return ConversationHandler.END

    await update.message.reply_text("1) To‘lov turini tanlang:", reply_markup=_paytype_keyboard())
    return STEP_PAYTYPE


# 1) PAYTYPE
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
        "Masalan:\n"
        "- NIKE\n"
        "- Azamat\n"
        "- 998901234567"
    )
    return STEP_CP_SEARCH


# 2) COUNTERPARTY SEARCH TEXT
async def cp_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("❌ Qidiruv bo‘sh. Brand/ism/tel yozing.")
        return STEP_CP_SEARCH

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
        if not rid:
            continue
        keyboard.append([InlineKeyboardButton(_cp_title(r), callback_data=f"cp:{rid}")])

    keyboard.append([InlineKeyboardButton("➕ Yangi kontragent yaratish", callback_data=f"cpnew:{q}")])

    await update.message.reply_text("Topilgan kontragentlar:", reply_markup=InlineKeyboardMarkup(keyboard))
    return STEP_CP_PICK


# 2) COUNTERPARTY PICK
async def on_cp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cp_id = (query.data or "").split("cp:", 1)[-1]
    cand = (context.user_data.get("cp_candidates") or {}).get(cp_id)
    if not cand:
        await query.edit_message_text("❌ Kontragent topilmadi. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    context.user_data["cp"] = {"id": cand.get("id"), "name": cand.get("name"), "phone": cand.get("phone"), "meta": cand.get("meta")}

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text("3) Chek rasmini yuboring (foto).")
        return STEP_CHECK

    # cash: rasm shart emas, lekin summa/sana kerak
    context.user_data["need_amount"] = True
    context.user_data["need_date"] = True
    await query.edit_message_text(
        "3) Naqt uchun summa va sanani kiriting.\n"
        "Masalan: 600000-28.01.2026"
    )
    return STEP_AMOUNT_DATE


# 2) COUNTERPARTY CREATE NEW
async def on_cp_create_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    raw = (query.data or "").split("cpnew:", 1)[-1].strip()
    name = raw or "New Counterparty"
    digits = _digits_only(raw)
    phone = digits if len(digits) >= 7 else None

    try:
        cp = get_or_create_counterparty(name=name, phone=phone)
    except Exception as e:
        await query.edit_message_text(f"❌ Yangi kontragent yaratishda xatolik: {e}")
        return ConversationHandler.END

    context.user_data["cp"] = {"id": cp.get("id"), "name": cp.get("name"), "phone": cp.get("phone"), "meta": cp.get("meta")}

    pt = context.user_data.get("paytype")
    if pt == "card":
        await query.edit_message_text("3) Chek rasmini yuboring (foto).")
        return STEP_CHECK

    context.user_data["need_amount"] = True
    context.user_data["need_date"] = True
    await query.edit_message_text(
        "3) Naqt uchun summa va sanani kiriting.\n"
        "Masalan: 600000-28.01.2026"
    )
    return STEP_AMOUNT_DATE


# 3) CASH OR OCR-FALLBACK MANUAL INPUT (ONLY MISSING FIELDS)
async def handle_manual_amount_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Endi universal:
    - need_amount & need_date -> 600000-28.01.2026
    - faqat need_date -> 28.01.2026 (vaqt bo‘lsa ham bo‘ladi)
    - faqat need_amount -> 600000
    """
    need_amount = bool(context.user_data.get("need_amount"))
    need_date = bool(context.user_data.get("need_date"))

    text = (update.message.text or "").strip()

    if need_amount and need_date:
        amount, date_iso = _parse_amount_date_one_line(text)
        if amount is None or date_iso is None:
            await update.message.reply_text(
                "❌ Format noto‘g‘ri.\n"
                "Masalan: 600000-28.01.2026"
            )
            return STEP_AMOUNT_DATE
        context.user_data["amount_uzs"] = int(amount)
        context.user_data["date_iso"] = str(date_iso)

    elif need_amount and not need_date:
        amount = _parse_amount_only(text)
        if amount is None:
            await update.message.reply_text("❌ Summa noto‘g‘ri. Masalan: 400000")
            return STEP_AMOUNT_DATE
        context.user_data["amount_uzs"] = int(amount)

    elif need_date and not need_amount:
        date_iso = _parse_date_only(text)
        if not date_iso:
            await update.message.reply_text("❌ Sana noto‘g‘ri. Masalan: 28.01.2026")
            return STEP_AMOUNT_DATE
        context.user_data["date_iso"] = str(date_iso)

    # nima yetishmayapti tekshiramiz
    amount_ok = isinstance(context.user_data.get("amount_uzs"), int) and context.user_data.get("amount_uzs") > 0
    date_ok = bool(context.user_data.get("date_iso"))

    if not amount_ok or not date_ok:
        # yana nimadir yo‘q bo‘lsa, flaglarni yangilab qayta so‘raymiz
        context.user_data["need_amount"] = not amount_ok
        context.user_data["need_date"] = not date_ok

        if not amount_ok and not date_ok:
            await update.message.reply_text("✏️ Summa va sanani kiriting: 600000-28.01.2026")
        elif not amount_ok:
            await update.message.reply_text("✏️ Faqat summani kiriting: 400000")
        else:
            await update.message.reply_text("✏️ Faqat sanani kiriting: 28.01.2026")
        return STEP_AMOUNT_DATE

    # hammasi bor -> kanal prodaj
    return await _ask_sales_channel(update.message, context)


# 3) CARD CHECK + OCR
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

    # OCR
    try:
        amount, date_iso, raw_text = detect_amount_and_date(str(img_path))
        context.user_data["ocr_text"] = raw_text
        context.user_data["amount_uzs"] = amount
        context.user_data["date_iso"] = date_iso
    except Exception as e:
        # OCR butunlay yiqilsa -> ikkalasi kerak
        context.user_data["need_amount"] = True
        context.user_data["need_date"] = True
        await msg.reply_text(
            f"❌ OCR xatolik: {e}\n\n"
            "✏️ Summa va sanani kiriting:\n"
            "Masalan: 600000-28.01.2026"
        )
        return STEP_AMOUNT_DATE

    amount_ok = isinstance(context.user_data.get("amount_uzs"), int) and context.user_data.get("amount_uzs") > 0
    date_ok = bool(context.user_data.get("date_iso"))

    # ✅ Agar nimadir topilmasa: faqat o‘shani so‘raymiz
    if not amount_ok or not date_ok:
        context.user_data["need_amount"] = not amount_ok
        context.user_data["need_date"] = not date_ok

        if not amount_ok and not date_ok:
            await msg.reply_text("✏️ OCR topolmadi. Summa va sanani kiriting: 600000-28.01.2026")
        elif not amount_ok:
            await msg.reply_text("✏️ Faqat summani kiriting: 400000")
        else:
            await msg.reply_text("✏️ Faqat sanani kiriting: 28.01.2026")
        return STEP_AMOUNT_DATE

    # ikkala topilgan bo‘lsa review
    a_show = f"{context.user_data['amount_uzs']:,} UZS"
    d_show = context.user_data["date_iso"]

    await msg.reply_text(
        "✅ Chek o‘qildi.\n\n"
        f"💵 Summa: {a_show}\n"
        f"📅 Sana: {d_show}\n\n"
        "To‘g‘rimi?",
        reply_markup=_review_keyboard(),
    )
    return STEP_REVIEW


# 4) REVIEW ACTION
async def on_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = (query.data or "").split("rv:", 1)[-1]

    if action == "edit":
        # faqat nima yo‘q bo‘lsa shuni so‘raymiz
        amount_ok = isinstance(context.user_data.get("amount_uzs"), int) and context.user_data.get("amount_uzs") > 0
        date_ok = bool(context.user_data.get("date_iso"))
        context.user_data["need_amount"] = not amount_ok
        context.user_data["need_date"] = not date_ok

        if not amount_ok and not date_ok:
            await query.edit_message_text("✏️ Summa va sanani kiriting: 600000-28.01.2026")
        elif not amount_ok:
            await query.edit_message_text("✏️ Faqat summani kiriting: 400000")
        else:
            await query.edit_message_text("✏️ Faqat sanani kiriting: 28.01.2026")
        return STEP_AMOUNT_DATE

    if action != "confirm":
        return STEP_REVIEW

    amount_ok = isinstance(context.user_data.get("amount_uzs"), int) and context.user_data.get("amount_uzs") > 0
    date_ok = bool(context.user_data.get("date_iso"))

    if not amount_ok or not date_ok:
        context.user_data["need_amount"] = not amount_ok
        context.user_data["need_date"] = not date_ok
        await query.edit_message_text("❌ Ma’lumot yetarli emas. ✏️ Tuzatish ni bosing.")
        return STEP_REVIEW

    return await _ask_sales_channel(query, context)


# 5) SALES CHANNEL -> SEND TO MOYSKLAD
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
