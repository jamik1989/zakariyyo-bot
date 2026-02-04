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

# STATES
(
    STEP_PAYTYPE,
    STEP_CP_SEARCH,
    STEP_CP_PICK,
    STEP_AMOUNT,
    STEP_CHECK,
    STEP_CHANNEL,
    STEP_REVIEW,
    STEP_EDIT,
) = range(8)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("Asia/Tashkent")

# ---------- UI ----------

def menu_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/kiritish")]],
        resize_keyboard=True
    )


def paytype_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💵 Naqt", callback_data="pt:cash")],
        [InlineKeyboardButton("💳 Karta", callback_data="pt:card")],
    ])


def review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data="rv:ok")],
        [InlineKeyboardButton("✏️ Tahrirlash", callback_data="rv:edit")],
    ])


# ---------- HELPERS ----------

def digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def normalize_phone(raw: str) -> str:
    d = digits_only(raw)
    if len(d) == 9:
        return "+998" + d
    if d.startswith("998"):
        return "+" + d
    return "+998" + d[-9:]


def format_amount(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def parse_fast_cp(text: str):
    parts = text.split("-", 2)
    if len(parts) != 3:
        return None
    brand, name, phone = parts
    return brand.strip().upper(), name.strip(), normalize_phone(phone)


def review_text(ctx):
    return (
        "🔎 **Tekshiruv**\n\n"
        f"🏷 Brend: {ctx['brand']}\n"
        f"👤 Mijoz: {ctx['client']}\n"
        f"📞 Tel: {ctx['phone']}\n"
        f"💳 To‘lov: {'Naqt' if ctx['paytype']=='cash' else 'Karta'}\n"
        f"💵 Summa: {format_amount(ctx['amount'])}\n"
        f"📅 Sana: {ctx['date']}\n"
        f"🕒 Vaqt: {ctx['time']}\n"
    )


# ---------- FLOW ----------

async def kiritish_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling")
        return ConversationHandler.END

    await update.message.reply_text("To‘lov turini tanlang:", reply_markup=paytype_kb())
    return STEP_PAYTYPE


async def on_paytype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data.clear()
    context.user_data["paytype"] = q.data.split(":")[1]

    await q.edit_message_text(
        "Kontragent kiriting:\n"
        "brend-mijoz-910175253\n"
        "yoki qidiruv so‘zi"
    )
    return STEP_CP_SEARCH


async def cp_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()

    fast = parse_fast_cp(txt)
    if fast:
        brand, name, phone = fast
        cp = get_or_create_counterparty(f"{brand} {name}", phone)
        context.user_data.update({
            "brand": brand,
            "client": name,
            "phone": phone,
            "cp": cp,
        })
        return await after_cp(update, context)

    rows = ms_get("/entity/counterparty", {"search": txt, "limit": 5}).get("rows", [])
    kb = [[InlineKeyboardButton(r["name"], callback_data=f"cp:{r['id']}")] for r in rows]
    kb.append([InlineKeyboardButton("➕ Yangi", callback_data=f"cpnew:{txt}")])

    await update.message.reply_text("Topildi:", reply_markup=InlineKeyboardMarkup(kb))
    context.user_data["cp_rows"] = {r["id"]: r for r in rows}
    return STEP_CP_PICK


async def cp_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.data.split(":")[1]

    cp = context.user_data["cp_rows"][cid]
    context.user_data.update({
        "brand": cp["name"].split()[0].upper(),
        "client": " ".join(cp["name"].split()[1:]),
        "phone": cp.get("phone", ""),
        "cp": cp,
    })
    return await after_cp(q, context)


async def after_cp(obj, context):
    if context.user_data["paytype"] == "cash":
        await obj.edit_message_text("Summani kiriting (masalan: 5000000)")
        return STEP_AMOUNT
    else:
        await obj.edit_message_text("Chek rasmini yuboring")
        return STEP_CHECK


async def amount_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = int(digits_only(update.message.text))
    now = datetime.now(TZ)

    context.user_data.update({
        "amount": amt,
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
    })

    return await choose_channel(update, context)


async def check_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.photo[-1].get_file()
    path = TMP_DIR / f"{update.message.message_id}.jpg"
    await file.download_to_drive(str(path))

    amt, d, t, _ = detect_amount_date_time(str(path))
    now = datetime.now(TZ)

    context.user_data.update({
        "amount": amt,
        "date": d or now.date().isoformat(),
        "time": t or now.strftime("%H:%M:%S"),
        "check": str(path),
    })

    return await choose_channel(update, context)


async def choose_channel(obj, context):
    ch = get_sales_channels()[:6]
    context.user_data["channels"] = {c["id"]: c for c in ch}

    kb = [[InlineKeyboardButton(c["name"], callback_data=f"sc:{c['id']}")] for c in ch]
    await obj.reply_text("Kanalni tanlang:", reply_markup=InlineKeyboardMarkup(kb))
    return STEP_CHANNEL


async def on_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    context.user_data["channel"] = context.user_data["channels"][q.data.split(":")[1]]
    await q.edit_message_text(review_text(context.user_data), reply_markup=review_kb())
    return STEP_REVIEW


async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "rv:edit":
        await q.edit_message_text("Yangi summani kiriting:")
        return STEP_AMOUNT

    org = get_default_organization()
    ctx = context.user_data

    if ctx["paytype"] == "cash":
        doc = create_cashin(
            org["meta"], ctx["cp"]["meta"], ctx["channel"]["meta"],
            ctx["amount"], ctx["date"], "Bot"
        )
    else:
        doc = create_paymentin(
            org["meta"], ctx["cp"]["meta"], ctx["channel"]["meta"],
            ctx["amount"], ctx["date"], "Bot"
        )
        if ctx.get("check"):
            attach_file_to_paymentin(doc["id"], ctx["check"])

    await q.edit_message_text("✅ MoySklad’ga yuborildi")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi", reply_markup=menu_kb())
    return ConversationHandler.END
