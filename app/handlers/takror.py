# app/handlers/takror.py
from typing import Dict, Any, List, Optional, Tuple
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import ContextTypes, ConversationHandler

from ..services.moysklad import search_products, get_product_by_id

TK_SEARCH, TK_PICK, TK_EXTRA, TK_QTY = range(4)


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
        "✅ Takror buyurtma tayyor."
    )


async def takror_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    _cleanup(context)

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

    await q.edit_message_text(
        "3) Qo‘shimcha o‘zgartirishni kiriting.\n\n"
        "Masalan:\n"
        "- flajok bilan\n"
        "- buklash bilan\n"
        "- o‘zgartirish yo‘q"
    )
    return TK_EXTRA


async def takror_extra_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = (update.message.text or "").strip()
    context.user_data["tk_extra"] = extra

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
        await update.message.reply_text("❌ Soni noto‘g‘ri. Masalan: 3000")
        return TK_QTY

    context.user_data["tk_qty"] = qty

    await update.message.reply_text(
        _build_review(context),
        parse_mode="Markdown",
        reply_markup=_menu_keyboard(),
    )

    _cleanup(context)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cleanup(context)
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END