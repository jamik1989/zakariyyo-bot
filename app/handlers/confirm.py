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
CF_PICK, CF_NEW_CP, CF_PHOTO, CF_KIND, CF_SIZE, CF_QTY, CF_CHANNEL, CF_GROUP, CF_PRICE, CF_REVIEW, CF_EDIT_CHOOSE, CF_EDIT_VALUE = range(12)

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
        [InlineKeyboardButton("🏷 Brend", callback_data="cfe:brand")],
        [InlineKeyboardButton("🧾 Nimaligi", callback_data="cfe:item")],
        [InlineKeyboardButton("📏 Razmer", callback_data="cfe:size")],
        [InlineKeyboardButton("🔢 Soni", callback_data="cfe:qty")],
        [InlineKeyboardButton("📊 Kanal prodaj", callback_data="cfe:channel")],
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
    """
    'Karton birka' -> 'kar'
    birinchi 3 harf: harflarni olib, lower, boshidan 3 ta.
    """
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
        f"🏷 Brend: {d.get('brand') or 'N/A'}\n"
        f"👤 Mijoz: {d.get('client_name') or 'N/A'}\n"
        f"📞 Tel: {d.get('phone_plus') or 'N/A'}\n\n"
        f"🧾 Nimaligi: {d.get('item_type') or 'N/A'}\n"
        f"📏 Razmer: {d.get('size') or 'N/A'}\n"
        f"🔢 Soni: {_fmt_int(d.get('qty'))}\n"
        f"💰 Narx (Цены продажa): {_fmt_int(d.get('price_uzs'))}\n\n"
        f"📊 Kanal prodaj: {d.get('sales_channel_name') or 'N/A'}\n"
        f"📁 Группа: {d.get('group_name') or 'N/A'}\n"
        f"🖼 Rasm: {img}\n\n"
        "Davom etamizmi?"
    )


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
    context.user_data["cf_channels_map"] = {c["id"]: c for c in channels}

    kb = [[InlineKeyboardButton(c["name"], callback_data=f"cfsc:{c['id']}")] for c in channels]
    markup = InlineKeyboardMarkup(kb)

    if hasattr(update_obj, "edit_message_text"):
        await update_obj.edit_message_text("📊 Kanal prodajni tanlang:", reply_markup=markup)
    else:
        await update_obj.reply_text("📊 Kanal prodajni tanlang:", reply_markup=markup)

    return CF_CHANNEL


# ============ GROUPS (FULL + PAGING) ============

def _build_groups_page_markup(groups: List[Dict[str, Any]], page: int) -> InlineKeyboardMarkup:
    total = len(groups)
    max_page = max(0, (total - 1) // GROUPS_PAGE_SIZE)
    page = max(0, min(page, max_page))

    start = page * GROUPS_PAGE_SIZE
    chunk = groups[start:start + GROUPS_PAGE_SIZE]

    kb = []
    for g in chunk:
        kb.append([InlineKeyboardButton(g["name"], callback_data=f"cfg:{g['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cfgp:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{max_page+1}", callback_data="cfgp:noop"))
    if page < max_page:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"cfgp:{page+1}"))
    kb.append(nav)

    return InlineKeyboardMarkup(kb)


async def _ask_product_group(update_obj, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    # ✅ barcha gruppalarni olishga harakat qilamiz
    groups = get_product_folders(limit=5000)  # limitni katta qildik
    if not groups:
        msg = "❌ MoySklad’da 'Товары → Группы' topilmadi."
        if hasattr(update_obj, "edit_message_text"):
            await update_obj.edit_message_text(msg)
        else:
            await update_obj.reply_text(msg)
        return ConversationHandler.END

    # saqlab qo'yamiz
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
        # agar state yo'qolgan bo'lsa qayta yuklaymiz
        return await _ask_product_group(q, context, page=0)

    markup = _build_groups_page_markup(groups, page)
    await q.edit_message_text(f"📁 Группа (Product folder) ni tanlang: (jami: {len(groups)})", reply_markup=markup)
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
        "🆕 Yangi tasdiq yaratish\n\n"
        "Format: BRAND-MijozNomi-910175253\n"
        "Masalan: LEAP-Akmal-910175253"
    )
    return CF_NEW_CP


async def on_new_confirm_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    triple = _parse_brand_client_phone(update.message.text or "")
    if not triple:
        await update.message.reply_text(
            "❌ Format noto‘g‘ri.\n"
            "To‘g‘ri format: BRAND-MijozNomi-910175253\n"
            "Masalan: LEAP-Akmal-910175253"
        )
        return CF_NEW_CP

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
        "qty": None,
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
    }

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
        "qty": None,
        "price_uzs": None,
        "sales_channel_meta": None,
        "sales_channel_name": "",
        "group_meta": None,
        "group_name": "",
    }

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

    await msg.reply_text("3) 🧾 Nimaligini yozing. Masalan: karton birka")
    return CF_KIND


async def on_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Nimaligi bo‘sh bo‘lmasin.")
        return CF_KIND

    d = context.user_data["confirm_data"]
    d["item_type"] = text
    context.user_data["confirm_data"] = d

    await update.message.reply_text("4) 📏 Razmer yozing. Masalan: 10x5")
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

    await update.message.reply_text("5) 🔢 Soni yozing. Masalan: 3000")
    return CF_QTY


async def on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    ddd = _digits_only(update.message.text or "")
    if not ddd:
        await update.message.reply_text("❌ Soni noto‘g‘ri. Masalan: 3000")
        return CF_QTY

    qty = int(ddd)
    if qty <= 0 or qty > 10_000_000:
        await update.message.reply_text("❌ Soni juda katta/kichik. Masalan: 3000")
        return CF_QTY

    d = context.user_data["confirm_data"]
    d["qty"] = qty
    context.user_data["confirm_data"] = d

    return await _ask_sales_channel(update.message, context)


async def on_channel_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    sc_id = (q.data or "").split("cfsc:", 1)[-1]
    ch = (context.user_data.get("cf_channels_map") or {}).get(sc_id)
    if not ch:
        await q.edit_message_text("❌ Kanal topilmadi. Qaytadan /tasdiq qiling.")
        return ConversationHandler.END

    d = context.user_data["confirm_data"]
    d["sales_channel_meta"] = ch.get("meta")
    d["sales_channel_name"] = ch.get("name") or ""
    context.user_data["confirm_data"] = d

    return await _ask_product_group(q, context, page=0)


async def on_group_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    gp_id = (q.data or "").split("cfg:", 1)[-1]

    # groups map kerak emas: all ro'yxatdan topamiz
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

    await q.edit_message_text("8) 💰 Цены продажа (narx) yozing. Masalan: 450")
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

    await update.message.reply_text(_render_review(context), reply_markup=_review_kb())
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

    if action != "send":
        return CF_REVIEW

    # ===== SEND =====
    op = context.user_data["operator"]
    cid = int(context.user_data.get("confirm_id") or 0)
    d = context.user_data["confirm_data"]

    brand = (d.get("brand") or "").strip()
    client = (d.get("client_name") or "").strip()
    phone = (d.get("phone_plus") or "").strip()
    cp_meta = d.get("counterparty_meta") or {}

    item_type = (d.get("item_type") or "").strip()
    size = (d.get("size") or "").strip()
    qty = d.get("qty")
    price_uzs = d.get("price_uzs")

    sc_meta = d.get("sales_channel_meta")
    sc_name = d.get("sales_channel_name") or ""
    gp_meta = d.get("group_meta")
    gp_name = d.get("group_name") or ""

    image_path = d.get("image_path") or ""

    # Rasm majburiy
    if not (image_path and os.path.exists(image_path)):
        await q.edit_message_text("❌ Rasm topilmadi. Qaytadan /tasdiq qilib foto yuboring.")
        return ConversationHandler.END

    if not cp_meta:
        await q.edit_message_text("❌ Kontragent meta yo‘q. /kiritish dan qaytadan boshlang.")
        return ConversationHandler.END

    if not (item_type and size and isinstance(qty, int) and qty > 0 and isinstance(price_uzs, int) and price_uzs > 0):
        await q.edit_message_text("❌ Tasdiq ma’lumotlari to‘liq emas (nimaligi/razmer/son/narx).")
        return ConversationHandler.END

    if not sc_meta or not gp_meta:
        await q.edit_message_text("❌ Kanal prodaj yoki Группа tanlanmagan.")
        return ConversationHandler.END

    # MoySklad moment
    moment_iso = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    # ✅ Наименование товара: BRAND + first3(item_type) + size
    abbr = _item_abbr3(item_type)
    product_name = f"{brand} {abbr} {size}".strip()

    desc = (
        f"[BOT TASDIQLASH]\n"
        f"Brand: {brand}\n"
        f"Client: {client}\n"
        f"Phone: {phone}\n\n"
        f"Item: {item_type}\n"
        f"Size: {size}\n"
        f"Qty: {qty}\n"
        f"Price: {price_uzs}\n"
        f"SalesChannel: {sc_name}\n"
        f"Group: {gp_name}\n\n"
        f"Operator: {op.get('name')} ({op.get('phone')})"
    )

    try:
        org = get_default_organization()

        pt_meta = find_price_type_meta_by_name("Цена продажи")
        if not pt_meta:
            pt_meta = find_price_type_meta_by_name("Розница") or find_price_type_meta_by_name("Опт")

        prod = create_product(
            name=product_name,
            productfolder_meta=gp_meta,
            sale_price_uzs=price_uzs,
            price_type_meta=pt_meta,
        )
        prod_id = str(prod.get("id") or "")
        prod_meta = prod.get("meta")

        if prod_id:
            attach_image_to_product(prod_id, image_path)

        positions = []
        if prod_meta:
            positions = [{
                "assortment": {"meta": prod_meta},
                "quantity": float(qty),
                "price": int(price_uzs) * 100,
            }]

        order = create_customerorder(
            organization_meta=org["meta"],
            agent_meta=cp_meta,
            sales_channel_meta=sc_meta,
            moment_iso=moment_iso,
            description=desc,
            positions=positions,
        )
        order_id = str(order.get("id") or "")

        if order_id:
            attach_file_to_customerorder(order_id, image_path)

        mark_confirm_done(int(op["id"]), cid)

        # Operatorga 1 ta xabar
        try:
            await q.message.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="✅ Sizning buyurtmangiz qabul qilindi.",
            reply_markup=_menu_keyboard(),
        )

        # ✅ Telegram kanalga yuboriladigan format (siz aytgandek)
        if CONFIRM_CHAT_ID:
            caption = (
                f"🏷 Brend: {brand}\n"
                f"🧾 Nimaligi: {item_type}\n"
                f"📏 Razmer: {size}\n"
                f"🔢 Soni: {qty}\n"
                f"📊 Kanal: {sc_name}\n"
                f"👨‍💼 Operator: {op.get('name')}\n"
                f"🧾 MoySklad: {order.get('name','N/A')}"
            )
            with open(image_path, "rb") as f:
                await context.bot.send_photo(chat_id=CONFIRM_CHAT_ID, photo=f, caption=caption)

    except Exception as e:
        await q.edit_message_text(f"❌ MoySklad yuborishda xatolik: {e}")
        return ConversationHandler.END

    # cleanup
    for k in ("confirm_id", "confirm_data", "cf_channels_map", "cf_groups_all", "edit_key"):
        context.user_data.pop(k, None)

    return ConversationHandler.END


async def on_edit_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _ensure_confirm_data(context)

    key = (q.data or "").split("cfe:", 1)[-1]
    if key == "back":
        await q.edit_message_text(_render_review(context), reply_markup=_review_kb())
        return CF_REVIEW

    if key not in ("brand", "item", "size", "qty", "channel"):
        return CF_EDIT_CHOOSE

    context.user_data["edit_key"] = key

    prompts = {
        "brand": "🏷 Brend nomini kiriting:",
        "item": "🧾 Nimaligi (masalan: karton birka):",
        "size": "📏 Razmer (masalan: 10x5):",
        "qty": "🔢 Soni (masalan: 3000):",
        "channel": "📊 Kanalni qayta tanlash uchun OK yozing:",
    }
    await q.edit_message_text(prompts[key])
    return CF_EDIT_VALUE


async def on_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _ensure_confirm_data(context)
    key = context.user_data.get("edit_key")
    val = (update.message.text or "").strip()
    if not key or not val:
        await update.message.reply_text("❌ Qiymat bo‘sh bo‘lmasin.")
        return CF_EDIT_VALUE

    d = context.user_data["confirm_data"]

    if key == "brand":
        d["brand"] = val.strip().upper()

    elif key == "item":
        d["item_type"] = val.strip()

    elif key == "size":
        s = val.lower().replace("х", "x").replace("*", "x").replace(" ", "")
        if "x" not in s:
            await update.message.reply_text("❌ Razmer noto‘g‘ri. Masalan: 10x5")
            return CF_EDIT_VALUE
        d["size"] = s

    elif key == "qty":
        dd = _digits_only(val)
        if not dd:
            await update.message.reply_text("❌ Soni noto‘g‘ri. Masalan: 3000")
            return CF_EDIT_VALUE
        d["qty"] = int(dd)

    elif key == "channel":
        context.user_data.pop("edit_key", None)
        await update.message.reply_text("📊 Kanalni tanlaymiz...")
        return await _ask_sales_channel(update.message, context)

    context.user_data["confirm_data"] = d
    context.user_data.pop("edit_key", None)

    await update.message.reply_text(_render_review(context), reply_markup=_review_kb())
    return CF_REVIEW


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END
