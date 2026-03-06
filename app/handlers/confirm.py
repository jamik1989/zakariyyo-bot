# app/handlers/confirm.py
import os
import re
import copy
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes, ConversationHandler

from ..config import CONFIRM_CHAT_ID
from ..db import (
    list_open_confirms,
    search_open_confirms,
    get_confirm,
    mark_confirm_done,
    create_confirm,
)
from ..services.moysklad import (
    get_default_organization,
    get_sales_channels,
    get_product_folders,
    find_price_type_meta_by_name,
    find_store_meta_by_name,
    get_or_create_uom_meta,
    create_product,
    attach_image_to_product,
    create_customerorder,
    attach_file_to_customerorder,
    attach_image_to_customerorder,
    get_or_create_counterparty,
    search_counterparties,
)

(
    CF_PICK,
    CF_NEW_CLICK,
    CF_CP_SEARCH,
    CF_CP_PICK,
    CF_BRAND_ONLY,
    CF_PHOTO,
    CF_KIND,
    CF_SIZE,
    CF_BG,
    CF_TEXT,
    CF_QM,
    CF_QTY,
    CF_CHANNEL,
    CF_GROUP,
    CF_PRICE,
    CF_REVIEW,
    CF_EDIT_CHOOSE,
    CF_EDIT_VALUE,
    CF_TIME,
) = range(19)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TG_TZ = ZoneInfo(os.getenv("TG_TZ", "Asia/Tashkent"))
MS_TZ = ZoneInfo(os.getenv("MOYSKLAD_TZ", "Europe/Moscow"))

GROUPS_PAGE_SIZE = 10

ALLOWED_GROUPS = [
    "birka ip",
    "birka jakard",
    "birka karton",
    "birka koja",
    "birka pechat",
    "paket karton",
    "paket salafan",
    "pechat",
    "qolip",
    "karobka",
    "o'g",
    "pergament",
    "stiker",
]

CONFIRM_STORE_NAME = "Abusahiy 75"


def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton("/kiritish"),
            KeyboardButton("/tasdiq"),
            KeyboardButton("/takror"),
        ]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )


def _review_kb(has_batch: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✅ Tasdiqlash (MoySklad + Kanal)", callback_data="cfr:send")],
        [InlineKeyboardButton("➕ Buyurtma qo‘shish", callback_data="cfr:add")],
        [InlineKeyboardButton("🕒 Vaqtni tahrirlash", callback_data="cfr:time")],
        [InlineKeyboardButton("✏️ Tahrirlash", callback_data="cfr:edit")],
        [InlineKeyboardButton("⬅️ Orqaga (ro‘yxat)", callback_data="cfr:back")],
    ]
    return InlineKeyboardMarkup(rows)


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


def _norm_group_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_qty_and_unit(text: str) -> Tuple[Optional[int], str, str]:
    t = (text or "").strip().lower()
    if not t:
        return None, "", ""

    m = re.match(r"^\s*(\d[\d\s]*)\s*([a-zA-Zа-яА-ЯёЁ]*)\s*$", t)
    if not m:
        d = _digits_only(t)
        return (int(d) if d else None), "", ""

    num = _digits_only(m.group(1) or "")
    unit = (m.group(2) or "").strip().lower()

    if not num:
        return None, "", ""

    qty = int(num)

    if unit in ("sht", "sh", "шт", "sht.", "sh.", "dona", "dona."):
        return qty, "sht", "шт"
    if unit in ("rulon", "рулон", "rul", "rul."):
        return qty, "rulon", "рулон"
    if unit in ("kg", "кг"):
        return qty, "kg", "кг"
    if unit in ("m", "metr", "meter", "metre", "м"):
        return qty, "m", "м"

    return qty, (unit or ""), (unit or "")


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
    d.setdefault("qty_unit_lat", "")
    d.setdefault("qty_unit_ru", "")
    d.setdefault("price_uzs", None)

    d.setdefault("sales_channel_meta", None)
    d.setdefault("sales_channel_name", "")

    d.setdefault("group_meta", None)
    d.setdefault("group_name", "")

    d.setdefault("moment_iso_override", "")

    context.user_data["confirm_data"] = d


def _clone_item_for_batch(d: Dict[str, Any]) -> Dict[str, Any]:
    keep_keys = [
        "item_type", "size", "bg_color", "text_color", "qm_note",
        "qty", "qty_unit_lat", "qty_unit_ru", "price_uzs",
        "sales_channel_meta", "sales_channel_name",
        "group_meta", "group_name",
        "image_path",
    ]
    return {k: copy.deepcopy(d.get(k)) for k in keep_keys}


def _reset_item_fields_keep_cp_brand_channel(d: Dict[str, Any]) -> Dict[str, Any]:
    """
    Batch ichida keyingi buyurtma uchun:
    - brand/CP saqlanadi
    - channel saqlanadi
    - qolgan item fieldlar tozalanadi
    """
    keep_sc_meta = d.get("sales_channel_meta")
    keep_sc_name = d.get("sales_channel_name")

    d["image_path"] = ""
    d["item_type"] = ""
    d["size"] = ""
    d["bg_color"] = ""
    d["text_color"] = ""
    d["qm_note"] = ""
    d["qty"] = None
    d["qty_unit_lat"] = ""
    d["qty_unit_ru"] = ""
    d["price_uzs"] = None
    d["group_meta"] = None
    d["group_name"] = ""

    d["sales_channel_meta"] = keep_sc_meta
    d["sales_channel_name"] = keep_sc_name
    return d


def _item_is_complete(it: Dict[str, Any]) -> bool:
    try:
        return (
            bool(it.get("item_type"))
            and bool(it.get("size"))
            and bool(it.get("bg_color"))
            and bool(it.get("text_color"))
            and isinstance(it.get("qty"), int)
            and it.get("qty") > 0
            and isinstance(it.get("price_uzs"), int)
            and it.get("price_uzs") > 0
            and bool(it.get("sales_channel_meta"))
            and bool(it.get("group_meta"))
            and bool(it.get("image_path"))
            and os.path.exists(it.get("image_path"))
        )
    except Exception:
        return False


def _get_locked_batch_channel(context: ContextTypes.DEFAULT_TYPE):
    batch = context.user_data.get("confirm_batch") or []
    if batch:
        first = batch[0] or {}
        return first.get("sales_channel_meta"), (first.get("sales_channel_name") or "")

    d = context.user_data.get("confirm_data") or {}
    if d.get("sales_channel_meta"):
        return d.get("sales_channel_meta"), (d.get("sales_channel_name") or "")

    return None, ""


def _tg_now_as_ms_moment() -> str:
    dt_tg = datetime.now(TG_TZ)
    dt_ms = dt_tg.astimezone(MS_TZ)
    return dt_ms.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_moysklad_moment_for_tg(moment_iso: str) -> str:
    if not moment_iso:
        return ""

    s = (moment_iso or "").strip()

    try:
        dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return s

    dt = dt.replace(tzinfo=MS_TZ).astimezone(TG_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def _render_review(context: ContextTypes.DEFAULT_TYPE) -> str:
    d = context.user_data.get("confirm_data") or {}
    img_ok = bool(d.get("image_path") and os.path.exists(d["image_path"]))
    img = "BOR ✅" if img_ok else "YO‘Q ❌"

    qty_show = _fmt_int(d.get("qty"))
    if d.get("qty_unit_ru"):
        qty_show = f"{qty_show} {d.get('qty_unit_ru')}"

    moment_iso = (d.get("moment_iso_override") or "").strip()
    if not moment_iso:
        moment_iso = _tg_now_as_ms_moment()
    moment_show = _fmt_moysklad_moment_for_tg(moment_iso) or moment_iso

    batch = context.user_data.get("confirm_batch") or []
    batch_info = f"📦 Batch: {len(batch) + 1} ta buyurtma (yig‘ilmoqda)\n\n" if batch else ""

    locked_meta, locked_name = _get_locked_batch_channel(context)
    lock_info = f"🔒 Batch KL: {locked_name}\n" if locked_meta and locked_name else ""

    return (
        f"{batch_info}"
        "🔎 Tekshiruv (Tasdiqlash):\n\n"
        f"🏷 B: {d.get('brand') or 'N/A'}\n"
        f"🧾 M.T: {d.get('item_type') or 'N/A'}\n"
        f"📏 R: {d.get('size') or 'N/A'}\n"
        f"🎨 F: {d.get('bg_color') or 'N/A'}\n"
        f"🔤 TI: {d.get('text_color') or 'N/A'}\n"
        f"📝 Q.M: {d.get('qm_note') or '—'}\n"
        f"🔢 S: {qty_show}\n"
        f"💰 Narx: {_fmt_int(d.get('price_uzs'))}\n"
        f"📊 KL: {d.get('sales_channel_name') or 'N/A'}\n"
        f"{lock_info}"
        f"📁 Группа: {d.get('group_name') or 'N/A'}\n"
        f"🏬 Sklad: {CONFIRM_STORE_NAME}\n"
        f"🕒 Vaqt: {moment_show}\n"
        f"🖼 Rasm: {img}\n\n"
        "Davom etamizmi?"
    )


def _build_item_line_for_desc(idx: int, total: int, it: Dict[str, Any]) -> str:
    qty_ru = _fmt_int(it.get("qty"))
    if it.get("qty_unit_ru"):
        qty_ru = f"{qty_ru} {it.get('qty_unit_ru')}"
    return (
        f"{idx}/{total}) "
        f"MT:{it.get('item_type')} | "
        f"R:{it.get('size')} | "
        f"F:{it.get('bg_color')} | "
        f"TI:{it.get('text_color')} | "
        f"S:{qty_ru} | "
        f"QM:{it.get('qm_note') or '-'} | "
        f"Narx:{it.get('price_uzs')} | "
        f"Group:{it.get('group_name')}"
    )


def _build_channel_caption(
    *,
    idx: int,
    total: int,
    brand: str,
    item: Dict[str, Any],
    operator_name: str,
    moment_iso: str,
    order_name: str,
) -> str:
    qty_show = _fmt_int(item.get("qty"))
    unit_ru = (item.get("qty_unit_ru") or "").strip()
    if unit_ru:
        qty_show = f"{qty_show} {unit_ru}"

    qm = (item.get("qm_note") or "").strip()
    qm_show = qm if qm else "-"

    moment_show = _fmt_moysklad_moment_for_tg(moment_iso) or moment_iso

    return "\n".join([
        f"📦 Buyurtma: {idx}/{total}",
        f"🏷 {brand}",
        f"🧾 {item.get('item_type')}",
        f"📏 {item.get('size')}",
        f"🎨 {item.get('bg_color')}",
        f"🔤 {item.get('text_color')}",
        f"🔢 {qty_show}",
        f"📝 {qm_show}",
        "",
        f"👨‍💼 {operator_name}",
        f"🕒 {moment_show}",
        f"🏬 Sklad: {CONFIRM_STORE_NAME}",
        f"🧾 MS: {order_name}",
    ])


async def on_channel_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    action = (q.data or "").split("cfscforce:", 1)[-1]

    if action == "retry":
        return await _ask_sales_channel(q, context)

    locked_meta, locked_name = _get_locked_batch_channel(context)
    if not locked_meta:
        return await _ask_sales_channel(q, context)

    d = context.user_data["confirm_data"]
    d["sales_channel_meta"] = locked_meta
    d["sales_channel_name"] = locked_name
    context.user_data["confirm_data"] = d

    return await _ask_product_group(q, context, page=0)


async def _ask_sales_channel(update_obj, context: ContextTypes.DEFAULT_TYPE):
    locked_meta, locked_name = _get_locked_batch_channel(context)
    if locked_meta:
        d = context.user_data.get("confirm_data") or {}
        d["sales_channel_meta"] = locked_meta
        d["sales_channel_name"] = locked_name
        context.user_data["confirm_data"] = d
        return await _ask_product_group(update_obj, context, page=0)

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


def _filter_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    allowed = set(_norm_group_name(x) for x in ALLOWED_GROUPS)
    out: List[Dict[str, Any]] = []
    for g in groups:
        name = _norm_group_name(g.get("name") or "")
        if name in allowed:
            out.append(g)
    return out


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

    groups = _filter_groups(groups)
    if not groups:
        msg = "❌ Siz belgilagan gruppalar MoySklad’da topilmadi (nomlarini tekshiring)."
        if hasattr(update_obj, "edit_message_text"):
            await update_obj.edit_message_text(msg)
        else:
            await update_obj.reply_text(msg)
        return ConversationHandler.END

    context.user_data["cf_groups_all"] = groups
    markup = _build_groups_page_markup(groups, page)

    text = f"📁 Группа ni tanlang: (jami: {len(groups)})"
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
        f"📁 Группа ni tanlang: (jami: {len(groups)})",
        reply_markup=markup
    )
    return CF_GROUP


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

    kb = [
        [InlineKeyboardButton("🔎 Qidirish / Yaratish (1 tugma)", callback_data="cfnew:smart")],
    ]
    if rows:
        for r in rows:
            title = f"{r.get('brand','')} | {r.get('phone_plus','')}".strip()
            kb.append([InlineKeyboardButton(title[:64], callback_data=f"cfpick:{r['id']}")])

    await update.message.reply_text(
        "✅ Tasdiqlash: qaysi buyurtmani yuboramiz?\n\n"
        "Yangi tasdiq uchun bitta tugma orqali qidiring yoki format yuboring.",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return CF_PICK


async def on_new_confirm_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    await q.edit_message_text(
        "🔎 Qidirish / Yaratish\n\n"
        "Brend yoki mijoz yoki telefon yozing (MoySklad bo‘lsa chiqadi).\n"
        "Agar topilmasa — shu formatda yuborsangiz darrov yaratadi:\n"
        "BRAND-MijozNomi-910175253\n"
        "Masalan: LEAP-Akmal-910175253"
    )
    return CF_CP_SEARCH


async def on_cp_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    op = context.user_data["operator"]
    op_id = int(op.get("id") or 0)
    if not op_id:
        await update.message.reply_text("❌ Operator ID topilmadi. Qayta /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    qtxt = (update.message.text or "").strip()
    if not qtxt:
        await update.message.reply_text("❌ Qidiruv matni bo‘sh.")
        return CF_CP_SEARCH

    triple = _parse_brand_client_phone(qtxt)
    if triple:
        brand, client_name, phone_plus = triple
        cp = get_or_create_counterparty(name=client_name, phone=phone_plus)
        confirm_id = create_confirm(
            operator_id=op_id,
            brand=brand,
            client_name=cp.get("name") or client_name,
            phone_plus=_normalize_phone_uz(cp.get("phone") or phone_plus),
            counterparty_meta=cp.get("meta") or {},
        )
        context.user_data["confirm_id"] = int(confirm_id)

        context.user_data["confirm_data"] = {
            "brand": brand,
            "client_name": (cp.get("name") or client_name).strip(),
            "phone_plus": _normalize_phone_uz(cp.get("phone") or phone_plus),
            "counterparty_meta": cp.get("meta") or {},
            "image_path": "",
            "item_type": "",
            "size": "",
            "bg_color": "",
            "text_color": "",
            "qm_note": "",
            "qty": None,
            "qty_unit_lat": "",
            "qty_unit_ru": "",
            "price_uzs": None,
            "sales_channel_meta": None,
            "sales_channel_name": "",
            "group_meta": None,
            "group_name": "",
            "moment_iso_override": "",
        }

        await update.message.reply_text(
            f"✅ Yangi tasdiq yaratildi: *{brand}*\n\n🖼 Buyurtma rasmini yuboring (Photo yoki File).",
            parse_mode="Markdown",
        )
        return CF_PHOTO

    context.user_data["cf_last_q"] = qtxt

    try:
        open_hits = search_open_confirms(op_id, qtxt, limit=10) or []
    except Exception:
        open_hits = []

    try:
        rows = search_counterparties(qtxt, limit=20) or []
    except Exception as e:
        await update.message.reply_text(f"❌ Qidiruvda xatolik: {e}")
        return CF_CP_SEARCH

    if not rows and not open_hits:
        await update.message.reply_text(
            "❌ Hech narsa topilmadi.\n\n"
            "✅ Yangi yaratish uchun shu formatni yuboring:\n"
            "BRAND-MijozNomi-910175253\n"
            "Masalan: LEAP-Akmal-910175253"
        )
        return CF_CP_SEARCH

    mp: Dict[str, Dict[str, Any]] = {}
    kb: List[List[InlineKeyboardButton]] = []

    for r in open_hits:
        cid = int(r.get("id") or 0)
        if not cid:
            continue
        title = f"{(r.get('brand') or '').strip()} | {(r.get('client_name') or '').strip()} | {(r.get('phone_plus') or '').strip()}"
        kb.append([InlineKeyboardButton(("✅ " + title).strip()[:64], callback_data=f"cfpick:{cid}")])

    for r in rows:
        cid = str(r.get("id") or "")
        if not cid:
            continue
        name = (r.get("name") or "").strip() or "N/A"
        phone = (r.get("phone") or "").strip()
        title = f"{name} ({phone})" if phone else name
        mp[cid] = r
        kb.append([InlineKeyboardButton(title[:64], callback_data=f"cfcp:{cid}")])

    kb.append([InlineKeyboardButton("➕ Yangi kontragent yaratish", callback_data="cfcp:new")])

    context.user_data["cf_cp_map"] = mp
    await update.message.reply_text(
        "Natijalar:\n"
        "— Agar ✅ OPEN tasdiq chiqsa, o‘shani tanlang.\n"
        "— Aks holda kontragentni tanlang.",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return CF_CP_PICK


async def on_cp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if data == "cfcp:new":
        await q.edit_message_text(
            "✅ Yangi yaratish formati:\n"
            "BRAND-MijozNomi-910175253\n"
            "Masalan: LEAP-Akmal-910175253"
        )
        return CF_CP_SEARCH

    cid = data.split("cfcp:", 1)[-1].strip()
    mp = context.user_data.get("cf_cp_map") or {}
    cp = mp.get(str(cid))

    if not cp:
        last_q = (context.user_data.get("cf_last_q") or "").strip()
        if last_q:
            try:
                rows = search_counterparties(last_q, limit=50) or []
                for r in rows:
                    if str(r.get("id") or "") == str(cid):
                        cp = r
                        break
            except Exception:
                cp = None

    if not cp:
        await q.edit_message_text("❌ Kontragent topilmadi. Qaytadan qidirib ko‘ring.")
        return CF_CP_SEARCH

    context.user_data["confirm_data"] = {
        "brand": "",
        "client_name": (cp.get("name") or "").strip(),
        "phone_plus": _normalize_phone_uz(cp.get("phone") or ""),
        "counterparty_meta": cp.get("meta") or {},
        "image_path": "",
        "item_type": "",
        "size": "",
        "bg_color": "",
        "text_color": "",
        "qm_note": "",
        "qty": None,
        "qty_unit_lat": "",
        "qty_unit_ru": "",
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
        "moment_iso_override": "",
    }

    context.user_data["cf_brand_only"] = True
    await q.edit_message_text("🏷 Brend nomini yozing. Masalan: LEAP")
    return CF_BRAND_ONLY


async def on_new_confirm_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    op = context.user_data["operator"]
    op_id = int(op.get("id") or 0)
    if not op_id:
        await update.message.reply_text("❌ Operator ID topilmadi.")
        return ConversationHandler.END

    if context.user_data.get("cf_brand_only"):
        brand = (update.message.text or "").strip().upper()
        if not brand:
            await update.message.reply_text("❌ Brend bo‘sh bo‘lmasin.")
            return CF_BRAND_ONLY

        _ensure_confirm_data(context)
        d = context.user_data["confirm_data"]
        d["brand"] = brand
        context.user_data["confirm_data"] = d

        if not d.get("counterparty_meta"):
            await update.message.reply_text("❌ Kontragent meta yo‘q. Qaytadan /tasdiq qiling.")
            return ConversationHandler.END

        cid = create_confirm(
            operator_id=op_id,
            brand=d.get("brand") or "",
            client_name=d.get("client_name") or "",
            phone_plus=d.get("phone_plus") or "",
            counterparty_meta=d.get("counterparty_meta") or {},
        )

        context.user_data["confirm_id"] = int(cid)
        context.user_data.pop("cf_brand_only", None)
        context.user_data.pop("cf_cp_map", None)
        context.user_data.pop("confirm_batch", None)

        await update.message.reply_text("🖼 Buyurtma rasmini yuboring (Photo yoki File).")
        return CF_PHOTO

    triple = _parse_brand_client_phone(update.message.text or "")
    if not triple:
        await update.message.reply_text(
            "❌ Format noto‘g‘ri.\n"
            "To‘g‘ri format: BRAND-MijozNomi-910175253\n"
            "Masalan: LEAP-Akmal-910175253"
        )
        return CF_NEW_CLICK

    brand, client_name, phone_plus = triple
    cp_name = f"{brand} {client_name}".strip()
    cp = get_or_create_counterparty(name=cp_name, phone=phone_plus)

    if not cp or not cp.get("meta"):
        await update.message.reply_text("❌ Kontragent yaratishda xatolik.")
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
        "qty_unit_lat": "",
        "qty_unit_ru": "",
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
        "moment_iso_override": "",
    }

    context.user_data.pop("confirm_batch", None)

    await update.message.reply_text("🖼 Buyurtma rasmini yuboring (Photo yoki File).")
    return CF_PHOTO


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    op = context.user_data["operator"]
    op_id = int(op.get("id") or 0)

    cid = int((q.data or "").split("cfpick:", 1)[-1])
    row = get_confirm(op_id, cid)
    if not row:
        await q.edit_message_text("❌ Topilmadi yoki sizga tegishli emas.")
        return ConversationHandler.END

    context.user_data["confirm_id"] = cid
    context.user_data["confirm_data"] = {
        "brand": row.get("brand") or "",
        "client_name": row.get("client_name") or "",
        "phone_plus": row.get("phone_plus") or "",
        "counterparty_meta": row.get("counterparty_meta") or {},
        "image_path": "",
        "item_type": "",
        "size": "",
        "bg_color": "",
        "text_color": "",
        "qm_note": "",
        "qty": None,
        "qty_unit_lat": "",
        "qty_unit_ru": "",
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
        "moment_iso_override": "",
    }

    context.user_data.pop("confirm_batch", None)

    await q.edit_message_text("🖼 Buyurtma rasmini yuboring (Photo yoki File).")
    return CF_PHOTO


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    _ensure_confirm_data(context)

    img_path = TMP_DIR / f"confirm_{msg.message_id}.jpg"

    if msg.photo:
        file = await msg.photo[-1].get_file()
        await file.download_to_drive(str(img_path))
    elif msg.document and (msg.document.mime_type or "").startswith("image/"):
        file = await msg.document.get_file()
        await file.download_to_drive(str(img_path))
    else:
        await msg.reply_text("❌ Iltimos rasm yuboring (Photo yoki File sifatida rasm).")
        return CF_PHOTO

    d = context.user_data["confirm_data"]
    d["image_path"] = str(img_path)
    context.user_data["confirm_data"] = d

    await msg.reply_text("3) 🧾 M.T (Maxsulot turi) yozing. Masalan: karton birka")
    return CF_KIND


async def on_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Maxsulot turi bo‘sh bo‘lmasin.")
        return CF_KIND

    d = context.user_data["confirm_data"]
    d["item_type"] = text
    context.user_data["confirm_data"] = d

    await update.message.reply_text("4) 📏 R (Razmer) yozing. Masalan: 10x5")
    return CF_SIZE


async def on_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    text = (update.message.text or "").strip()
    s = text.lower().replace("х", "x").replace("*", "x").replace(" ", "")
    if "x" not in s:
        await update.message.reply_text("❌ Razmer noto‘g‘ri. Masalan: 10x5")
        return CF_SIZE

    d = context.user_data["confirm_data"]
    d["size"] = s
    context.user_data["confirm_data"] = d

    await update.message.reply_text("5) 🎨 F (Foni): Masalan: Oq / Qizil")
    return CF_BG


async def on_bg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    val = (update.message.text or "").strip()
    if not val:
        await update.message.reply_text("❌ Foni bo‘sh bo‘lmasin. Masalan: Oq")
        return CF_BG

    d = context.user_data["confirm_data"]
    d["bg_color"] = val
    context.user_data["confirm_data"] = d

    await update.message.reply_text("6) 🔤 TI (Text rangi): Masalan: Qora / Qizil")
    return CF_TEXT


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    val = (update.message.text or "").strip()
    if not val:
        await update.message.reply_text("❌ Text rangi bo‘sh bo‘lmasin. Masalan: Qizil")
        return CF_TEXT

    d = context.user_data["confirm_data"]
    d["text_color"] = val
    context.user_data["confirm_data"] = d

    await update.message.reply_text("7) 📝 Q.M: (izoh) yozing. Masalan: laminatsiya / teshik 2 ta / va hokazo")
    return CF_QM


async def on_qm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    val = (update.message.text or "").strip()
    d = context.user_data["confirm_data"]
    d["qm_note"] = val
    context.user_data["confirm_data"] = d

    await update.message.reply_text("8) 🔢 S (Soni) yozing. Masalan: 3000 yoki 3000 sht / 3000 rulon / 3000 kg / 3000 m / 3000 dona")
    return CF_QTY


async def on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    qty, unit_lat, unit_ru = _parse_qty_and_unit(update.message.text or "")
    if not qty:
        await update.message.reply_text("❌ Soni noto‘g‘ri. Masalan: 3000 yoki 3000 sht")
        return CF_QTY

    if qty <= 0 or qty > 10_000_000:
        await update.message.reply_text("❌ Soni juda katta/kichik. Masalan: 3000")
        return CF_QTY

    d = context.user_data["confirm_data"]
    d["qty"] = int(qty)
    d["qty_unit_lat"] = unit_lat
    d["qty_unit_ru"] = unit_ru
    context.user_data["confirm_data"] = d

    return await _ask_sales_channel(update.message, context)


async def on_channel_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    sc_id = (q.data or "").split("cfsc:", 1)[-1]
    ch = (context.user_data.get("cf_channels_map") or {}).get(str(sc_id))
    if not ch:
        await q.edit_message_text("❌ Kanal topilmadi. Qaytadan /tasdiq qiling.")
        return ConversationHandler.END

    chosen_meta = ch.get("meta")
    chosen_name = ch.get("name") or ""

    d = context.user_data["confirm_data"]
    d["sales_channel_meta"] = chosen_meta
    d["sales_channel_name"] = chosen_name
    context.user_data["confirm_data"] = d

    return await _ask_product_group(q, context, page=0)


async def on_group_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    gp_id = (q.data or "").split("cfg:", 1)[-1]
    groups = context.user_data.get("cf_groups_all") or []
    g = None
    for it in groups:
        if str(it.get("id")) == str(gp_id):
            g = it
            break

    if not g:
        await q.edit_message_text("❌ Группа topilmadi. Qaytadan /tasdiq qiling.")
        return ConversationHandler.END

    d = context.user_data["confirm_data"]
    d["group_meta"] = g.get("meta")
    d["group_name"] = g.get("name") or ""
    context.user_data["confirm_data"] = d

    await q.edit_message_text("10) 💰 Цена (narx) yozing. Masalan: 450")
    return CF_PRICE


async def on_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    ddd = _digits_only(update.message.text or "")
    if not ddd:
        await update.message.reply_text("❌ Narx noto‘g‘ri. Masalan: 450")
        return CF_PRICE

    price = int(ddd)
    if price <= 0 or price > 5_000_000_000:
        await update.message.reply_text("❌ Narx noto‘g‘ri. Masalan: 450")
        return CF_PRICE

    d = context.user_data["confirm_data"]
    d["price_uzs"] = price
    context.user_data["confirm_data"] = d

    await update.message.reply_text(
        _render_review(context),
        reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
    )
    return CF_REVIEW


async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    action = (q.data or "").split("cfr:", 1)[-1]

    if action == "back":
        await q.edit_message_text("⬅️ Orqaga qayting: /tasdiq ni bosing.")
        return ConversationHandler.END

    if action == "edit":
        await q.edit_message_text("Qaysi maydonni tahrirlaymiz?", reply_markup=_edit_choose_kb())
        return CF_EDIT_CHOOSE

    if action == "time":
        await q.edit_message_text(
            "🕒 Vaqtni kiriting:\n"
            "Format: YYYY-MM-DD HH:MM\n"
            "Masalan: 2026-02-18 21:30\n\n"
            "Yoki 'now' deb yozing (hozirgi vaqt)."
        )
        return CF_TIME

    if action == "add":
        d = context.user_data["confirm_data"]
        if not _item_is_complete(d):
            await q.edit_message_text("❌ Buyurtma to‘liq emas. Avval hamma maydonlarni to‘ldiring.")
            return CF_REVIEW

        batch = context.user_data.get("confirm_batch") or []
        batch.append(_clone_item_for_batch(d))
        context.user_data["confirm_batch"] = batch

        context.user_data["confirm_data"] = _reset_item_fields_keep_cp_brand_channel(d)

        await q.edit_message_text(
            f"✅ Buyurtma batchga qo‘shildi.\n"
            f"📦 Batchda: {len(batch)} ta tayyor buyurtma bor.\n\n"
            "🖼 Yangi buyurtma rasmini yuboring (Photo yoki File)."
        )
        return CF_PHOTO

    if action != "send":
        return CF_REVIEW

    op = context.user_data["operator"]
    cid = int(context.user_data.get("confirm_id") or 0)
    d = context.user_data["confirm_data"]

    brand = (d.get("brand") or "").strip()
    cp_meta = d.get("counterparty_meta") or {}
    if not brand or not cp_meta:
        await q.edit_message_text("❌ Brend yoki Kontragent topilmadi.")
        return ConversationHandler.END

    if not _item_is_complete(d):
        await q.edit_message_text("❌ Buyurtma to‘liq emas. Avval hamma maydonlarni to‘ldiring.")
        return ConversationHandler.END

    moment_iso = (d.get("moment_iso_override") or "").strip()
    if not moment_iso:
        moment_iso = _tg_now_as_ms_moment()

    items: List[Dict[str, Any]] = []
    batch = context.user_data.get("confirm_batch") or []
    if batch:
        items.extend(batch)
    items.append(_clone_item_for_batch(d))

    locked_meta, locked_name = _get_locked_batch_channel(context)
    if locked_meta:
        sc_meta = locked_meta
        sc_name = locked_name
    else:
        sc_meta = items[-1].get("sales_channel_meta")
        sc_name = items[-1].get("sales_channel_name") or ""

    for it in items:
        if not it.get("sales_channel_meta"):
            it["sales_channel_meta"] = sc_meta
            it["sales_channel_name"] = sc_name

    try:
        org = get_default_organization()

        store_meta = find_store_meta_by_name(CONFIRM_STORE_NAME)
        if not store_meta:
            raise RuntimeError(f"Склад topilmadi: '{CONFIRM_STORE_NAME}'. MoySklad’dagi sklad nomini tekshiring.")

        pt_meta = find_price_type_meta_by_name("Цена продажи")
        if not pt_meta:
            pt_meta = find_price_type_meta_by_name("Розница") or find_price_type_meta_by_name("Опт")

        total = len(items)

        positions: List[Dict[str, Any]] = []
        item_product_ids: List[str] = []

        desc_lines = [
            f"[BOT TASDIQLASH] B: {brand} | Operator: {op.get('name')} | Store: {CONFIRM_STORE_NAME}"
        ]

        for idx, it in enumerate(items, start=1):
            if not _item_is_complete(it):
                raise RuntimeError("Batch ichida to‘liq bo‘lmagan buyurtma bor (rasm/maydonlar).")

            desc_lines.append(_build_item_line_for_desc(idx, total, it))

            unit_ru = (it.get("qty_unit_ru") or "").strip()
            abbr = _item_abbr3(it.get("item_type") or "")
            product_name = f"{brand} {abbr} {it.get('size')}".strip()

            uom_meta = get_or_create_uom_meta(unit_ru) if unit_ru else None

            prod = create_product(
                name=product_name,
                productfolder_meta=it.get("group_meta"),
                sale_price_uzs=int(it.get("price_uzs")),
                price_type_meta=pt_meta,
                uom_meta=uom_meta,
            )

            prod_id = str(prod.get("id") or "")
            prod_meta = prod.get("meta")

            item_product_ids.append(prod_id)

            if prod_id:
                attach_image_to_product(prod_id, it.get("image_path"))

            if prod_meta:
                positions.append({
                    "assortment": {"meta": prod_meta},
                    "quantity": float(int(it.get("qty"))),
                    "price": int(it.get("price_uzs")) * 100,
                })

        desc = "\n".join(desc_lines)

        order = create_customerorder(
            organization_meta=org["meta"],
            agent_meta=cp_meta,
            sales_channel_meta=sc_meta,
            store_meta=store_meta,
            moment_iso=moment_iso,
            description=desc,
            positions=positions,
        )

        order_id = str(order.get("id") or "")
        order_name = order.get("name", "N/A")

        if order_id:
            # birinchi rasmni orderga asosiy ilova sifatida biriktirib qo'yamiz
            first_img = items[0].get("image_path")
            if first_img:
                try:
                    attach_file_to_customerorder(order_id, first_img)
                except Exception:
                    pass
                try:
                    attach_image_to_customerorder(order_id, first_img)
                except Exception:
                    pass

        if CONFIRM_CHAT_ID:
            for idx, it in enumerate(items, start=1):
                caption = _build_channel_caption(
                    idx=idx,
                    total=total,
                    brand=brand,
                    item=it,
                    operator_name=op.get("name"),
                    moment_iso=moment_iso,
                    order_name=order_name,
                )
                with open(it.get("image_path"), "rb") as f:
                    await context.bot.send_photo(chat_id=CONFIRM_CHAT_ID, photo=f, caption=caption)

        mark_confirm_done(int(op["id"]), cid)

        try:
            await q.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"✅ Buyurtma(lar) qabul qilindi. MoySklad’da 1 ta buyurtma va {len(positions)} ta position yaratildi.",
            reply_markup=_menu_keyboard(),
        )

    except Exception as e:
        await q.edit_message_text(f"❌ MoySklad yuborishda xatolik: {e}")
        return ConversationHandler.END

    for k in (
        "confirm_id", "confirm_data", "cf_channels_map", "cf_groups_all",
        "edit_key", "cf_cp_map", "cf_brand_only", "confirm_batch"
    ):
        context.user_data.pop(k, None)

    return ConversationHandler.END


async def on_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    txt_raw = (update.message.text or "").strip()
    txt = txt_raw.lower()

    if txt == "now":
        moment = _tg_now_as_ms_moment()
        d = context.user_data["confirm_data"]
        d["moment_iso_override"] = moment
        context.user_data["confirm_data"] = d

        await update.message.reply_text(
            _render_review(context),
            reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
        )
        return CF_REVIEW

    try:
        dt = datetime.strptime(txt_raw, "%Y-%m-%d %H:%M")
        dt_tg = dt.replace(tzinfo=TG_TZ)
        dt_ms = dt_tg.astimezone(MS_TZ)
        moment = dt_ms.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        await update.message.reply_text(
            "❌ Format noto‘g‘ri.\n"
            "To‘g‘ri: 2026-02-18 21:30 yoki 'now'"
        )
        return CF_TIME

    d = context.user_data["confirm_data"]
    d["moment_iso_override"] = moment
    context.user_data["confirm_data"] = d

    await update.message.reply_text(
        _render_review(context),
        reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
    )
    return CF_REVIEW


async def on_edit_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    key = (q.data or "").split("cfe:", 1)[-1]
    if key == "back":
        await q.edit_message_text(
            _render_review(context),
            reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
        )
        return CF_REVIEW

    if key not in ("brand", "item", "size", "bg", "text", "qm", "qty", "channel"):
        return CF_EDIT_CHOOSE

    context.user_data["edit_key"] = key

    prompts = {
        "brand": "🏷 B (Brend) kiriting:",
        "item": "🧾 M.T (masalan: karton birka):",
        "size": "📏 R (masalan: 10x5):",
        "bg": "🎨 F (masalan: Oq):",
        "text": "🔤 TI (masalan: Qora):",
        "qm": "📝 Q.M (izoh) kiriting:",
        "qty": "🔢 S (masalan: 3000 yoki 3000 sht/rulon/kg/m/dona):",
        "channel": "📊 KL ni qayta tanlash uchun OK yozing:",
    }
    await q.edit_message_text(prompts[key])
    return CF_EDIT_VALUE


async def on_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    key = context.user_data.get("edit_key")
    val = (update.message.text or "").strip()
    if not key:
        await update.message.reply_text("❌ Xatolik: edit_key topilmadi.")
        return CF_EDIT_VALUE

    d = context.user_data["confirm_data"]

    if key == "brand":
        if not val:
            await update.message.reply_text("❌ B bo‘sh bo‘lmasin.")
            return CF_EDIT_VALUE
        d["brand"] = val.strip().upper()

    elif key == "item":
        if not val:
            await update.message.reply_text("❌ M.T bo‘sh bo‘lmasin.")
            return CF_EDIT_VALUE
        d["item_type"] = val.strip()

    elif key == "size":
        s = val.lower().replace("х", "x").replace("*", "x").replace(" ", "")
        if "x" not in s:
            await update.message.reply_text("❌ Razmer noto‘g‘ri. Masalan: 10x5")
            return CF_EDIT_VALUE
        d["size"] = s

    elif key == "bg":
        if not val:
            await update.message.reply_text("❌ F bo‘sh bo‘lmasin.")
            return CF_EDIT_VALUE
        d["bg_color"] = val.strip()

    elif key == "text":
        if not val:
            await update.message.reply_text("❌ TI bo‘sh bo‘lmasin.")
            return CF_EDIT_VALUE
        d["text_color"] = val.strip()

    elif key == "qm":
        d["qm_note"] = val.strip()

    elif key == "qty":
        qty, unit_lat, unit_ru = _parse_qty_and_unit(val)
        if not qty:
            await update.message.reply_text("❌ S noto‘g‘ri. Masalan: 3000 yoki 3000 sht")
            return CF_EDIT_VALUE
        d["qty"] = int(qty)
        d["qty_unit_lat"] = unit_lat
        d["qty_unit_ru"] = unit_ru

    elif key == "channel":
        context.user_data.pop("edit_key", None)
        # batch ichida qayta channel so'ralmasin:
        locked_meta, locked_name = _get_locked_batch_channel(context)
        if locked_meta:
            d["sales_channel_meta"] = locked_meta
            d["sales_channel_name"] = locked_name
            context.user_data["confirm_data"] = d
            await update.message.reply_text(
                _render_review(context),
                reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
            )
            return CF_REVIEW

        await update.message.reply_text("📊 KL (Kanal) ni tanlaymiz...")
        return await _ask_sales_channel(update.message, context)

    context.user_data["confirm_data"] = d
    context.user_data.pop("edit_key", None)

    await update.message.reply_text(
        _render_review(context),
        reply_markup=_review_kb(bool(context.user_data.get("confirm_batch")))
    )
    return CF_REVIEW


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END