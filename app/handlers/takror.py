# app/handlers/takror.py
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo
import os

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes, ConversationHandler

from ..config import CONFIRM_CHAT_ID
from ..db import get_latest_open_confirm
from ..services.moysklad import (
    search_products,
    get_product_by_id,
    get_default_organization,
    create_customerorder,
    find_store_meta_by_name,
)

TK_SEARCH, TK_PICK, TK_EXTRA, TK_QTY, TK_EDIT_VALUE = range(5)

TG_TZ = ZoneInfo(os.getenv("TG_TZ", "Asia/Tashkent"))
MS_TZ = ZoneInfo(os.getenv("MOYSKLAD_TZ", "Europe/Moscow"))

CONFIRM_STORE_NAME = "Abusahiy 75"


def _menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton("/tasdiq"), KeyboardButton("/takror")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        selective=True,
    )




def _tk_normalize_size(text: str) -> str:
    s = (text or "").strip().lower()
    s = s.replace("??", "x").replace("*", "x").replace(",", ".")
    s = re.sub(r"\s+", "", s)
    return s


def _tk_normalize_qm(text: str) -> str:
    s = (text or "").strip().lower()
    if s == "kb":
        return "kesib buklash"
    return (text or "").strip()


def _tk_build_preview(context: ContextTypes.DEFAULT_TYPE) -> str:
    d = context.user_data.get("tk_form") or {}
    brand = d.get("brand") or "-"
    item_type = d.get("item_type") or "-"
    size = d.get("size") or "-"
    qm = d.get("qm_note") or "-"
    qty = d.get("qty") or 0
    qty_unit = d.get("qty_unit_lat") or ""
    price = d.get("price_uzs") or 0
    channel = d.get("channel_name") or "Zakariyo 02"
    group_name = d.get("group_name") or "karobka"

    qty_show = f"{_fmt_qty(qty)} {qty_unit}".strip()

    return "\n".join([
        "???? Takror buyurtma preview:",
        "",
        f"???? {brand}",
        f"???? {item_type}",
        f"???? {size}",
        f"???? {qm}",
        f"???? {qty_show}",
        f"???? {_fmt_qty(price)}",
        f"???? {channel}",
        f"???? {group_name}",
        f"???? {CONFIRM_STORE_NAME}",
    ])


def _tk_preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("??? Tasdiqlash", callback_data="tkr:ok")],
        [InlineKeyboardButton("?????? Tahrirlash", callback_data="tkr:edit")],
        [InlineKeyboardButton("??? Bekor qilish", callback_data="tkr:cancel")],
    ])


def _tk_edit_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("???? Brend", callback_data="tkr_edit:brand"),
         InlineKeyboardButton("???? Turi", callback_data="tkr_edit:item_type")],
        [InlineKeyboardButton("???? Razmer", callback_data="tkr_edit:size"),
         InlineKeyboardButton("???? Q.M", callback_data="tkr_edit:qm")],
        [InlineKeyboardButton("???? Soni", callback_data="tkr_edit:qty"),
         InlineKeyboardButton("???? Narx", callback_data="tkr_edit:price")],
        [InlineKeyboardButton("?????? Orqaga", callback_data="tkr:back")],
    ])

def _fmt_qty(qty: Optional[int]) -> str:
    if not isinstance(qty, int):
        return "TOPILMADI"
    return f"{qty:,}".replace(",", " ")


def _digits_only(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _parse_qty(text: str) -> Optional[int]:
    d = _digits_only(text)
    if not d:
        return None
    val = int(d)
    if val <= 0:
        return None
    if val > 10_000_000:
        return None
    return val


def _product_title(prod: Dict[str, Any]) -> str:
    name = (prod.get("name") or "").strip() or "NoName"
    return name


def _cleanup(context: ContextTypes.DEFAULT_TYPE):
    for k in (
        "tk_last_q",
        "tk_products_map",
        "tk_product",
        "tk_extra",
        "tk_qty",
    ):
        context.user_data.pop(k, None)


def _build_review(context: ContextTypes.DEFAULT_TYPE) -> str:
    prod = context.user_data.get("tk_product") or {}
    extra = (context.user_data.get("tk_extra") or "").strip() or "-"
    qty = context.user_data.get("tk_qty")

    return (
        "🔁 *Takror buyurtma*\n"
        "━━━━━━━━━━━━━━\n"
        f"*📦 Tovar:* {_product_title(prod)}\n"
        f"*📝 Qo‘shimcha o‘zgartirish:* {extra}\n"
        f"*🔢 Soni:* {_fmt_qty(qty)}\n"
        "━━━━━━━━━━━━━━\n"
        "✅ Takror buyurtma tayyor.\n"
        "Yuborilmoqda..."
    )


def _tg_now_as_ms_moment() -> str:
    dt_tg = datetime.now(TG_TZ)
    dt_ms = dt_tg.astimezone(MS_TZ)
    return dt_ms.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_moysklad_moment_for_tg(moment_iso: str) -> str:
    if not moment_iso:
        return ""
    try:
        dt = datetime.strptime(moment_iso[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return moment_iso
    dt = dt.replace(tzinfo=MS_TZ).astimezone(TG_TZ)
    return dt.strftime("%d.%m.%Y %H:%M")


def _extract_sale_price_uzs(prod: Dict[str, Any]) -> int:
    sale_prices = prod.get("salePrices") or []
    if not sale_prices:
        return 0

    first = sale_prices[0] or {}
    value = first.get("value")
    if not isinstance(value, int):
        return 0

    # MoySklad value ko'pincha tiyin formatida bo'ladi
    if value >= 100:
        return int(value // 100)
    return int(value)


def _extract_uom_name(prod: Dict[str, Any]) -> str:
    uom = prod.get("uom") or {}
    name = (uom.get("name") or "").strip()
    if not name:
        return ""
    return name


async def takror_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    op = context.user_data.get("operator") or {}
    op_id = int(op.get("id") or 0)
    latest = get_latest_open_confirm(op_id)

    if not latest:
        await update.message.reply_text(
            "❌ Takror uchun faol mijoz topilmadi.\n\n"
            "Avval /tasdiq orqali mijozni tanlab oling, keyin /takror ishlating.",
            reply_markup=_menu_keyboard(),
        )
        return ConversationHandler.END

    _cleanup(context)
    context.user_data["tk_confirm_ctx"] = latest
    context.user_data["tk_form"] = {
        "brand": (latest.get("brand") or "").strip(),
        "item_type": "",
        "size": "",
        "qm_note": "",
        "qty": None,
        "qty_unit_lat": "sht",
        "qty_unit_ru": "????",
        "price_uzs": None,
        "channel_name": "Zakariyo 02",
        "group_name": "karobka",
    }

    await update.message.reply_text(
        "🔁 *Takror buyurtma*\n"
        "━━━━━━━━━━━━━━\n"
        "1) Tovar nomini yozing.\n\n"
        "Masalan:\n"
        "`mirand`\n"
        "`jakard`\n"
        "`birka 4x4`",
        parse_mode="Markdown",
    )
    return TK_SEARCH


async def takror_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("❌ Tovar nomini yozing.")
        return TK_SEARCH

    context.user_data["tk_last_q"] = q

    try:
        rows = search_products(q, limit=10) or []
    except Exception as e:
        await update.message.reply_text(f"❌ Tovar qidirishda xatolik: {e}")
        return TK_SEARCH

    if not rows:
        await update.message.reply_text(
            "❌ Tovar topilmadi.\n\n"
            "Boshqa nom bilan qayta yozing."
        )
        return TK_SEARCH

    products_map: Dict[str, Dict[str, Any]] = {}
    kb: List[List[InlineKeyboardButton]] = []

    for r in rows[:10]:
        pid = str(r.get("id") or "")
        if not pid:
            continue
        products_map[pid] = r
        kb.append([
            InlineKeyboardButton(
                _product_title(r)[:64],
                callback_data=f"tkp:{pid}",
            )
        ])

    context.user_data["tk_products_map"] = products_map

    await update.message.reply_text(
        "2) Tovardan birini tanlang:",
        reply_markup=InlineKeyboardMarkup(kb),
    )
    return TK_PICK


async def takror_pick_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    pid = (q.data or "").split("tkp:", 1)[-1].strip()
    prod = (context.user_data.get("tk_products_map") or {}).get(pid)

    if not prod:
        try:
            prod = get_product_by_id(pid)
        except Exception:
            prod = None

    if not prod:
        await q.edit_message_text("❌ Tovar topilmadi. Qaytadan /takror qiling.")
        return ConversationHandler.END

    context.user_data["tk_product"] = prod
    d = context.user_data.get("tk_form") or {}
    d["item_type"] = _product_title(prod)
    d["price_uzs"] = _extract_sale_price_uzs(prod)
    context.user_data["tk_form"] = d

    await q.edit_message_text(
        "3) Qo‘shimcha o‘zgartirishni kiriting.\n\n"
        "Masalan:\n"
        "- flajok bilan\n"
        "- buklash bilan\n"
        "- o‘zgartirish yo‘q"
    )
    return TK_EXTRA


async def takror_extra_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = _tk_normalize_qm((update.message.text or "").strip())
    context.user_data["tk_extra"] = extra
    d = context.user_data.get("tk_form") or {}
    d["qm_note"] = extra
    context.user_data["tk_form"] = d

    await update.message.reply_text(
        "4) Sonini kiriting.\n\n"
        "Masalan:\n"
        "`3000`",
        parse_mode="Markdown",
    )
    return TK_QTY


async def takror_qty_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qty = _parse_qty(update.message.text or "")
    if not qty:
        await update.message.reply_text("??? Soni noto???g???ri. Masalan: 3000 yoki 3000 sh")
        return TK_QTY

    d = context.user_data.get("tk_form") or {}
    d["qty"] = qty

    raw = (update.message.text or "").lower()
    if "dona" in raw or re.search(r"\bd\b", raw):
        d["qty_unit_lat"] = "dona"
        d["qty_unit_ru"] = "????????"
    else:
        d["qty_unit_lat"] = "sht"
        d["qty_unit_ru"] = "????"

    context.user_data["tk_form"] = d

    await update.message.reply_text(
        _tk_build_preview(context),
        reply_markup=_tk_preview_kb(),
    )
    return TK_PICK


async def takror_review_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""

    if data == "tkr:cancel":
        _cleanup(context)
        context.user_data.pop("tk_confirm_ctx", None)
        context.user_data.pop("tk_form", None)
        await q.edit_message_text("??? Bekor qilindi.")
        return ConversationHandler.END

    if data == "tkr:edit":
        await q.edit_message_text(
            _tk_build_preview(context),
            reply_markup=_tk_edit_kb(),
        )
        return TK_PICK

    if data == "tkr:back":
        await q.edit_message_text(
            _tk_build_preview(context),
            reply_markup=_tk_preview_kb(),
        )
        return TK_PICK

    if data == "tkr:ok":
        prod = context.user_data.get("tk_product") or {}
        d = context.user_data.get("tk_form") or {}
        confirm_ctx = context.user_data.get("tk_confirm_ctx") or {}
        operator = context.user_data.get("operator") or {}

        brand = (d.get("brand") or confirm_ctx.get("brand") or "").strip()
        cp_meta = confirm_ctx.get("counterparty_meta") or {}
        product_meta = prod.get("meta")

        if not brand or not cp_meta or not product_meta:
            await q.edit_message_text("??? Ma'lumot yetarli emas. Qaytadan /takror qiling.")
            return ConversationHandler.END

        try:
            org = get_default_organization()
            store_meta = find_store_meta_by_name(CONFIRM_STORE_NAME)
            if not store_meta:
                raise RuntimeError(f"Sklad topilmadi: {CONFIRM_STORE_NAME}")

            qty = int(d.get("qty") or 0)
            price_uzs = int(d.get("price_uzs") or 0)

            positions = [{
                "assortment": {"meta": product_meta},
                "quantity": float(qty),
                "price": int(price_uzs) * 100 if price_uzs > 0 else 0,
            }]

            desc = "\n".join([
                f"[BOT TAKROR] B: {brand} | Operator: {operator.get('name')}",
                f"Product: {d.get('item_type')}",
                f"Size: {d.get('size')}",
                f"Qty: {qty}",
                f"QM: {d.get('qm_note') or '-'}",
            ])

            moment_iso = _tg_now_as_ms_moment()

            order = create_customerorder(
                organization_meta=org["meta"],
                agent_meta=cp_meta,
                sales_channel_meta=None,
                store_meta=store_meta,
                moment_iso=moment_iso,
                description=desc,
                positions=positions,
            )

            moment_show = _fmt_moysklad_moment_for_tg(moment_iso)
            qty_show = f"{_fmt_qty(qty)} {d.get('qty_unit_lat') or ''}".strip()

            if CONFIRM_CHAT_ID:
                text = "\n".join([
                    "???? Takror buyurtma",
                    f"???? {brand}",
                    f"???? {d.get('item_type')}",
                    f"???? {d.get('size') or '-'}",
                    f"???? {d.get('qm_note') or '-'}",
                    f"???? {qty_show}",
                    f"???? {_fmt_qty(d.get('price_uzs') or 0)}",
                    f"???? {d.get('channel_name')}",
                    f"???? {d.get('group_name')}",
                    f"???? {CONFIRM_STORE_NAME}",
                    f"???? {moment_show}",
                    f"???? MS: {order.get('name', 'N/A')}",
                ])
                await context.bot.send_message(chat_id=CONFIRM_CHAT_ID, text=text)

            await q.edit_message_text("??? Takror buyurtma MoySkladga yuborildi.")
            _cleanup(context)
            context.user_data.pop("tk_confirm_ctx", None)
            context.user_data.pop("tk_form", None)
            return ConversationHandler.END

        except Exception as e:
            await q.edit_message_text(f"??? Takror buyurtmani yuborishda xatolik: {e}")
            return ConversationHandler.END

    return TK_PICK


async def takror_edit_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if not data.startswith("tkr_edit:"):
        return TK_PICK

    key = data.split(":", 1)[1]
    context.user_data["tk_edit_key"] = key

    prompts = {
        "brand": "???? Brendni kiriting:",
        "item_type": "???? Maxsulot turini kiriting:",
        "size": "???? Razmerni kiriting. Masalan: 1.5x5",
        "qm": "???? Q.M ni kiriting. Masalan: kb",
        "qty": "???? Sonini kiriting. Masalan: 3000 sh yoki 3000 d",
        "price": "???? Narxini kiriting. Masalan: 450",
    }

    await q.edit_message_text(prompts.get(key, "Qiymatni kiriting:"))
    return TK_EDIT_VALUE


async def takror_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = context.user_data.get("tk_edit_key")
    d = context.user_data.get("tk_form") or {}
    val = (update.message.text or "").strip()

    if key == "brand":
        d["brand"] = val.upper()
    elif key == "item_type":
        d["item_type"] = val
    elif key == "size":
        d["size"] = _tk_normalize_size(val)
    elif key == "qm":
        d["qm_note"] = _tk_normalize_qm(val)
    elif key == "qty":
        qty = _parse_qty(val)
        if not qty:
            await update.message.reply_text("??? Soni noto???g???ri. Masalan: 3000 sh yoki 3000 d")
            return TK_EDIT_VALUE
        d["qty"] = qty
        raw = val.lower()
        if "dona" in raw or re.search(r"\bd\b", raw):
            d["qty_unit_lat"] = "dona"
            d["qty_unit_ru"] = "????????"
        else:
            d["qty_unit_lat"] = "sht"
            d["qty_unit_ru"] = "????"
    elif key == "price":
        nums = re.findall(r"(\d+)", val)
        if not nums:
            await update.message.reply_text("??? Narx noto???g???ri. Masalan: 450")
            return TK_EDIT_VALUE
        d["price_uzs"] = int(nums[-1])

    context.user_data["tk_form"] = d
    context.user_data.pop("tk_edit_key", None)

    await update.message.reply_text(
        _tk_build_preview(context),
        reply_markup=_tk_preview_kb(),
    )
    return TK_PICK


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cleanup(context)
    context.user_data.pop("tk_confirm_ctx", None)
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END