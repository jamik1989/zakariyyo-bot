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
    ms_get,  # kontragent qidirish uchun
    get_sales_channels,
    get_default_organization,
    get_or_create_counterparty,
    create_paymentin,
    create_cashin,
    attach_file_to_paymentin,
    attach_file_to_cashin,
)

# Agar vision ishlasa ishlatamiz (billing bo'lmasa fallback qo'lda)
try:
    from ..services.vision import detect_amount_and_date
except Exception:
    detect_amount_and_date = None  # type: ignore

# =========================
# STATES (main.py bilan MOS!)
# =========================
# /kiritish -> paytype -> cp_search -> cp_pick/create -> (cash: amount_date) (card: check+ocr) -> channel -> review(confirm) -> send
STEP_PAYTYPE, STEP_CP_SEARCH, STEP_CP_PICK, STEP_AMOUNT_DATE, STEP_CHECK, STEP_CHANNEL, STEP_REVIEW = range(7)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ---------------- UI helpers ----------------

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


def _review_keyboard_final() -> InlineKeyboardMarkup:
    # yakuniy review: confirm / edit(amount-date) / back(channel)
    kb = [
        [InlineKeyboardButton("✅ Tasdiq", callback_data="rv:confirm")],
        [InlineKeyboardButton("✏️ Tuzatish (summa/sana)", callback_data="rv:edit")],
        [InlineKeyboardButton("⬅️ Kanalni qayta tanlash", callback_data="rv:back")],
    ]
    return InlineKeyboardMarkup(kb)


# ---------------- helpers ----------------

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


def _parse_amount_date_one_line(text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    600000-28.01.2026
    600000 / 28.01.2026
    600 000-28.01.2026
    """
    s = (text or "").strip()
    m = re.match(r"^\s*([0-9][0-9\s.,]{2,20})\s*[-/,]\s*(.+?)\s*$", s)
    if not m:
        return None, None

    amount_raw = m.group(1)
    date_raw = m.group(2)

    amount_digits = _digits_only(amount_raw)
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


def _cp_title(cp: Dict[str, Any]) -> str:
    name = (cp.get("name") or "").strip() or "NoName"
    phone = (cp.get("phone") or "").strip()
    if phone:
        return f"{name} ({phone})"
    return name


def _search_counterparties(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    brand/ism/tel yozilganda MoySklad counterparty ro'yxatini beradi.
    - agar query ichida raqam bo'lsa: phone~digits filter
    - aks holda: search
    """
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
    """
    chat_update_obj: message yoki callback_query (unda .edit_message_text/.reply_text farqi bor)
    """
    try:
        channels = get_sales_channels(limit=50)
    except Exception as e:
        if hasattr(chat_update_obj, "edit_message_text"):
            await chat_update_obj.edit_message_text(f"❌ Kanal olishda xatolik: {e}")
        else:
            await chat_update_obj.reply_text(f"❌ Kanal olishda xatolik: {e}")
        return ConversationHandler.END

    if not channels:
        txt = "❌ MoySklad’da 'канал продаж' topilmadi. Avval sales channel yarating."
        if hasattr(chat_update_obj, "edit_message_text"):
            await chat_update_obj.edit_message_text(txt)
        else:
            await chat_update_obj.reply_text(txt)
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


def _build_review_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    pt = context.user_data.get("paytype")
    cp = context.user_data.get("cp") or {}
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    check_path = context.user_data.get("check_path")
    sc_name = context.user_data.get("sales_channel_name") or "TOPILMADI"

    pt_txt = "Naqt" if pt == "cash" else ("Karta" if pt == "card" else "N/A")
    a_show = f"{amount:,} UZS" if isinstance(amount, int) else "TOPILMADI"
    d_show = date_iso or "TOPILMADI"
    cp_show = _cp_title(cp) if cp else "TOPILMADI"
    img_show = "BOR ✅" if (check_path and os.path.exists(check_path)) else "YO‘Q ❌"

    return (
        "🔎 Tekshiruv:\n\n"
        f"👤 Kontragent: {cp_show}\n"
        f"💳 To‘lov turi: {pt_txt}\n"
        f"📊 Kanal: {sc_name}\n"
        f"💵 Summa: {a_show}\n"
        f"📅 Sana: {d_show}\n"
        f"🧾 Chek rasmi: {img_show}\n\n"
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
        "check_path",
        "ocr_text",
        "channels_map",
        "sales_channel_meta",
        "sales_channel_name",
    ):
        context.user_data.pop(k, None)


# ---------------- Conversation flow ----------------

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

    await update.message.reply_text(
        "Topilgan kontragentlar:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
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

    # cash: chek bo‘lmasa ham bo‘ladi, lekin summa-sana kerak
    await query.edit_message_text(
        "3) Naqt uchun summa va sanani bitta xabarda kiriting:\n"
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

    await query.edit_message_text(
        "3) Naqt uchun summa va sanani bitta xabarda kiriting:\n"
        "Masalan: 600000-28.01.2026"
    )
    return STEP_AMOUNT_DATE


# 3) CASH / CARD MANUAL AMOUNT-DATE (OCR bo'lmasa yoki edit bo'lsa)
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

    # Endi kanal tanlanadi (siz aytgandek)
    return await _ask_sales_channel(update.message, context)


# 3) CARD CHECK + OCR (agar billing bo'lsa)
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

    # OCR: bor bo'lsa urinamiz, bo'lmasa qo'lda kiritishga o'tamiz
    if detect_amount_and_date is None:
        await msg.reply_text(
            "ℹ️ OCR yoqilmagan.\n"
            "✏️ Summani va sanani qo‘lda bitta xabarda kiriting:\n"
            "Masalan: 600000-28.01.2026"
        )
        return STEP_AMOUNT_DATE

    try:
        amount, date_iso, raw_text = detect_amount_and_date(str(img_path))
        context.user_data["amount_uzs"] = amount
        context.user_data["date_iso"] = date_iso
        context.user_data["ocr_text"] = raw_text
    except Exception as e:
        await msg.reply_text(
            f"❌ OCR xatolik: {e}\n\n"
            "✏️ Summani va sanani qo‘lda bitta xabarda kiriting:\n"
            "Masalan: 600000-28.01.2026"
        )
        return STEP_AMOUNT_DATE

    # OCR topgan bo'lsa ham, siz aytgandek: kanal -> review -> confirm
    return await _ask_sales_channel(msg, context)


# 4) SALES CHANNEL CHOSEN -> FINAL REVIEW (no sending here)
async def on_sales_channel_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    sc_id = (query.data or "").split("sc:", 1)[-1]
    sc_meta = (context.user_data.get("channels_map") or {}).get(sc_id)
    if not sc_meta:
        await query.edit_message_text("❌ Kanal topilmadi. Qaytadan /kiritish qiling.")
        return ConversationHandler.END

    # meta saqlab qo'yamiz (tasdiqda yuboramiz)
    context.user_data["sales_channel_meta"] = sc_meta

    # name ni ham saqlab qo'yamiz (reviewda chiroyli ko'rsatish)
    # channels_mapda faqat meta turibdi, shuning uchun yana topib olamiz:
    try:
        channels = get_sales_channels(limit=50)
        name = next((c.get("name") for c in channels if c.get("id") == sc_id), None)
    except Exception:
        name = None
    context.user_data["sales_channel_name"] = name or "Tanlandi"

    # reviewga o'tish
    await query.edit_message_text(_build_review_text(context), reply_markup=_review_keyboard_final())
    return STEP_REVIEW


# 5) FINAL REVIEW ACTIONS
async def on_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action = (query.data or "").split("rv:", 1)[-1]

    if action == "back":
        # kanalni qayta tanlash
        return await _ask_sales_channel(query, context)

    if action == "edit":
        await query.edit_message_text(
            "✏️ Summa va sanani qo‘lda kiriting (bitta xabarda):\n"
            "Masalan: 600000-28.01.2026"
        )
        return STEP_AMOUNT_DATE

    if action != "confirm":
        return STEP_REVIEW

    # CONFIRM -> SEND TO MOYSKLAD (DRAFT)
    operator = context.user_data.get("operator", {})
    pt = context.user_data.get("paytype")
    amount = context.user_data.get("amount_uzs")
    date_iso = context.user_data.get("date_iso")
    check_path = context.user_data.get("check_path")
    cp = context.user_data.get("cp") or {}
    sc_meta = context.user_data.get("sales_channel_meta")

    if pt not in ("cash", "card") or not isinstance(amount, int) or amount <= 0 or not date_iso or not cp.get("meta") or not sc_meta:
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
                f"✅ {doc_kind} (черновик)\n\n"
                f"👤 Kontragent: {_cp_title(cp)}\n"
                f"💳 To‘lov turi: {'Naqt' if pt=='cash' else 'Karta'}\n"
                f"📊 Kanal: {context.user_data.get('sales_channel_name')}\n"
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
    _cleanup_after_done(context)
    return ConversationHandler.END
