# app/handlers/confirm.py
import os
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes, ConversationHandler

from ..config import CONFIRM_CHAT_ID
from ..db import list_open_confirms, get_confirm, mark_confirm_done, create_confirm
from ..services.moysklad import (
    get_default_organization,
    get_sales_channels,
    get_product_folders,
    find_price_type_meta_by_name,
    create_product,
    attach_image_to_product,
    create_customerorder,
    attach_file_to_customerorder,
    get_or_create_counterparty,
)

# ===== States (main.py bilan MOS) =====
# PICK, NEW_CP, PHOTO, KIND, SIZE, BG, TEXT, QM, QTY, CHANNEL, GROUP, PRICE, REVIEW, EDIT_CHOOSE, EDIT_VALUE
(
    CF_PICK,
    CF_NEW_CP,
    CF_PHOTO,
    CF_KIND,
    CF_SIZE,
    CF_BG,
    CF_TEXT,
    CF_QM,          # ✅ NEW
    CF_QTY,
    CF_CHANNEL,
    CF_GROUP,
    CF_PRICE,
    CF_REVIEW,
    CF_EDIT_CHOOSE,
    CF_EDIT_VALUE,
) = range(15)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("Asia/Tashkent")
GROUPS_PAGE_SIZE = 10


# ============ UI ============

def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _review_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash (MoySklad + Kanal)", callback_data="cfr:send")],
        [InlineKeyboardButton("✏️ Tahrirlash", callback_data="cfr:edit")],
        [InlineKeyboardButton("⬅️ Orqaga (ro‘yxat)", callback_data="cfr:back")],
    ])


def _edit_choose_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏷 B (Brend)", callback_data="cfe:brand")],
        [InlineKeyboardButton("🧾 M.T (Maxsulot turi)", callback_data="cfe:item")],
        [InlineKeyboardButton("📏 R (Razmer)", callback_data="cfe:size")],
        [InlineKeyboardButton("🎨 F (Foni)", callback_data="cfe:bg")],
        [InlineKeyboardButton("🔤 TI (Text rangi)", callback_data="cfe:text")],
        [InlineKeyboardButton("📝 Q.M", callback_data="cfe:qm")],
        [InlineKeyboardButton("🔢 S (Soni)", callback_data="cfe:qty")],
        [InlineKeyboardButton("📊 KL (Kanal)", callback_data="cfe:channel")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="cfe:back")],
    ])


# ============ helpers ============

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


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


def _parse_brand_client_phone(text: str):
    # format: BRAND-ClientName-910175253
    parts = [p.strip() for p in (text or "").strip().split("-", maxsplit=2)]
    if len(parts) != 3:
        return None
    brand = parts[0].strip().upper()
    client = parts[1].strip()
    phone_plus = _normalize_phone_uz(parts[2])
    if not brand or not client or not phone_plus:
        return None
    return brand, client, phone_plus


def _fmt_int(n: Optional[int]) -> str:
    if not isinstance(n, int):
        return "N/A"
    return f"{n:,}".replace(",", " ")


def _item_abbr3(item_type: str) -> str:
    raw = (item_type or "").strip().lower()
    letters = re.sub(r"[^a-zа-яёўқғҳ]", "", raw, flags=re.IGNORECASE)
    if len(letters) >= 3:
        return letters[:3]
    raw2 = re.sub(r"\s+", "", raw)
    return (raw2[:3] or "itm").lower()


def _ensure_confirm_data(context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data.get("confirm_data") or {}
    d.setdefault("brand", "")
    d.setdefault("client_name", "")
    d.setdefault("phone_plus", "")
    d.setdefault("counterparty_meta", {})
    d.setdefault("image_path", "")

    d.setdefault("item_type", "")
    d.setdefault("size", "")
    d.setdefault("bg_color", "")
    d.setdefault("text_color", "")
    d.setdefault("qm_note", "")
    d.setdefault("qty", None)
    d.setdefault("price_uzs", None)

    d.setdefault("sales_channel_meta", None)
    d.setdefault("sales_channel_name", "")

    d.setdefault("group_meta", None)
    d.setdefault("group_name", "")

    context.user_data["confirm_data"] = d


def _render_review(context: ContextTypes.DEFAULT_TYPE) -> str:
    d = context.user_data.get("confirm_data") or {}
    img_ok = bool(d.get("image_path") and os.path.exists(d["image_path"]))
    img = "BOR ✅" if img_ok else "YO‘Q ❌"

    return (
        "🔎 Tekshiruv (Tasdiqlash):\n\n"
        f"🏷 B: {d.get('brand') or 'N/A'}\n"
        f"🧾 M.T: {d.get('item_type') or 'N/A'}\n"
        f"📏 R: {d.get('size') or 'N/A'}\n"
        f"🎨 F: {d.get('bg_color') or 'N/A'}\n"
        f"🔤 TI: {d.get('text_color') or 'N/A'}\n"
        f"📝 Q.M: {d.get('qm_note') or '—'}\n"
        f"🔢 S: {_fmt_int(d.get('qty'))}\n"
        f"💰 Narx: {_fmt_int(d.get('price_uzs'))}\n"
        f"📊 KL: {d.get('sales_channel_name') or 'N/A'}\n"
        f"📁 Группа: {d.get('group_name') or 'N/A'}\n"
        f"🖼 Rasm: {img}\n\n"
        "Davom etamizmi?"
    )


def _match_confirm_row(r: Dict[str, Any], qlow: str) -> bool:
    """CF_NEW_CP da format emas bo'lsa: brend/mijoz/telefon bo'yicha qidiruv."""
    if not qlow:
        return False
    b = str(r.get("brand") or "").lower()
    c = str(r.get("client_name") or "").lower()
    p = str(r.get("phone_plus") or "").lower()
    return (qlow in b) or (qlow in c) or (qlow in p)


def _search_results_kb(rows: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    kb: List[List[InlineKeyboardButton]] = []
    kb.append([InlineKeyboardButton("➕ Yangi tasdiq yaratish", callback_data="cfnew")])
    for r in rows[:30]:
        title = f"{r.get('brand','')} | {r.get('client_name','')} | {r.get('phone_plus','')}".strip()
        kb.append([InlineKeyboardButton(title[:64], callback_data=f"cfpick:{r['id']}")])
    return InlineKeyboardMarkup(kb)


# ============ SALES CHANNEL ============

async def _ask_sales_channel(update_obj, context: ContextTypes.DEFAULT_TYPE):
    channels = get_sales_channels(limit=300)
    if not channels:
        msg = "❌ MoySklad’da 'Канал продаж' topilmadi."
        if hasattr(update_obj, "edit_message_text"):
            await update_obj.edit_message_text(msg)
        else:
            await update_obj.reply_text(msg)
        return ConversationHandler.END

    channels = channels[:20]
    context.user_data["cf_channels_map"] = {str(c["id"]): c for c in channels}

    kb = [[InlineKeyboardButton(c["name"], callback_data=f"cfsc:{c['id']}")] for c in channels]
    markup = InlineKeyboardMarkup(kb)

    if hasattr(update_obj, "edit_message_text"):
        await update_obj.edit_message_text("📊 KL (Kanal) ni tanlang:", reply_markup=markup)
    else:
        await update_obj.reply_text("📊 KL (Kanal) ni tanlang:", reply_markup=markup)

    return CF_CHANNEL


# ============ GROUPS (FULL + PAGING) ============

def _build_groups_page_markup(groups: List[Dict[str, Any]], page: int) -> InlineKeyboardMarkup:
    total = len(groups)
    max_page = max(0, (total - 1) // GROUPS_PAGE_SIZE)
    page = max(0, min(page, max_page))

    start = page * GROUPS_PAGE_SIZE
    chunk = groups[start:start + GROUPS_PAGE_SIZE]

    kb: List[List[InlineKeyboardButton]] = []
    for g in chunk:
        kb.append([InlineKeyboardButton(g["name"], callback_data=f"cfg:{g['id']}")])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cfgp:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max_page+1}", callback_data="cfgp:noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cfgp:{page+1}"))

    if nav:
        kb.append(nav)

    return InlineKeyboardMarkup(kb)


async def _ask_product_group(update_obj, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    groups = get_product_folders(limit=5000)
    if not groups:
        msg = "❌ MoySklad’da 'Товары → Группы' topilmadi."
        if hasattr(update_obj, "edit_message_text"):
            await update_obj.edit_message_text(msg)
        else:
            await update_obj.reply_text(msg)
        return ConversationHandler.END

    context.user_data["cf_groups_all"] = groups
    markup = _build_groups_page_markup(groups, page)

    text = f"📁 Группа (Product folder) ni tanlang: (jami: {len(groups)})"
    if hasattr(update_obj, "edit_message_text"):
        await update_obj.edit_message_text(text, reply_markup=markup)
    else:
        await update_obj.reply_text(text, reply_markup=markup)

    return CF_GROUP


async def on_groups_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if data == "cfgp:noop":
        return CF_GROUP

    page_s = data.split("cfgp:", 1)[-1]
    try:
        page = int(page_s)
    except Exception:
        page = 0

    groups = context.user_data.get("cf_groups_all") or []
    if not groups:
        return await _ask_product_group(q, context, page=0)

    markup = _build_groups_page_markup(groups, page)
    await q.edit_message_text(
        f"📁 Группа (Product folder) ni tanlang: (jami: {len(groups)})",
        reply_markup=markup
    )
    return CF_GROUP


# ================== FLOW ==================

async def tasdiq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    op = context.user_data["operator"]
    op_id = int(op.get("id") or 0)
    if not op_id:
        await update.message.reply_text("❌ Operator ID topilmadi. Qayta /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    rows = list_open_confirms(op_id, limit=50)

    kb = [[InlineKeyboardButton("➕ Yangi tasdiq yaratish", callback_data="cfnew")]]
    if rows:
        for r in rows:
            title = f"{r.get('brand','')} | {r.get('phone_plus','')}".strip()
            kb.append([InlineKeyboardButton(title, callback_data=f"cfpick:{r['id']}")])

    await update.message.reply_text(
        "✅ Tasdiqlash: qaysi buyurtmani yuboramiz?",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return CF_PICK


async def on_new_confirm_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "🆕 Yangi tasdiq yaratish yoki qidirish\n\n"
        "✅ Yangi yaratish formati:\n"
        "BRAND-MijozNomi-910175253\n"
        "Masalan: LEAP-Akmal-910175253\n\n"
        "🔎 Yoki qidirish uchun brend/mijoz/telefon yozing.\n"
        "Masalan: LEAP yoki Akmal yoki 910175253"
    )
    return CF_NEW_CP


async def on_new_confirm_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    text_in = (update.message.text or "").strip()

    # 1) Avval eski formatni tekshiramiz
    triple = _parse_brand_client_phone(text_in)
    if not triple:
        # 2) Format bo'lmasa — qidiruv rejimi
        op = context.user_data["operator"]
        op_id = int(op.get("id") or 0)
        qlow = text_in.lower()

        rows = list_open_confirms(op_id, limit=200) or []
        found = [r for r in rows if _match_confirm_row(r, qlow)]

        if not found:
            await update.message.reply_text(
                "❌ Topilmadi.\n\n"
                "🔎 Qidirish: brend/mijoz/telefon yozing (masalan: LEAP / Akmal / 910175253)\n"
                "✅ Yangi yaratish: BRAND-MijozNomi-910175253\n"
                "Masalan: LEAP-Akmal-910175253"
            )
            return CF_NEW_CP

        await update.message.reply_text(
            f"🔎 Topildi: {len(found)} ta.\nTanlang yoki yangi yarating:",
            reply_markup=_search_results_kb(found),
        )
        # MUHIM: qaytib PICK state ga o'tamiz — tugmalar shu yerda ishlaydi
        return CF_PICK

    # ======== eski oqim (format to'g'ri) =========
    brand, client_name, phone_plus = triple

    cp_name = f"{brand} {client_name}".strip()
    cp = get_or_create_counterparty(name=cp_name, phone=phone_plus)

    op = context.user_data["operator"]
    op_id = int(op.get("id") or 0)
    if not op_id or not cp or not cp.get("meta"):
        await update.message.reply_text("❌ Kontragent yaratishda xatolik. Qayta urinib ko‘ring.")
        return ConversationHandler.END

    cid = create_confirm(
        operator_id=op_id,
        brand=brand,
        client_name=client_name,
        phone_plus=phone_plus,
        counterparty_meta=cp["meta"],
    )

    context.user_data["confirm_id"] = int(cid)
    context.user_data["confirm_data"] = {
        "brand": brand,
        "client_name": client_name,
        "phone_plus": phone_plus,
        "counterparty_meta": cp["meta"],
        "image_path": "",
        "item_type": "",
        "size": "",
        "bg_color": "",
        "text_color": "",
        "qm_note": "",
        "qty": None,
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
    }

    await update.message.reply_text("🖼 Buyurtma rasmini yuboring (Photo yoki File).")
    return CF_PHOTO


# ===== qolgan funksiyalaringiz O'ZGARMAGAN holda shu yerda davom etadi =====
# (siz yuborgan kodingizning qolgan qismi aynan o'sha-o'sha qolsin)
