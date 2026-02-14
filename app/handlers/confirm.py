# app/handlers/confirm.py
import os
import re
from pathlib import Path
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

CF_PICK, CF_NEW_CP, CF_PHOTO, CF_KIND, CF_SIZE, CF_QTY, CF_CHANNEL, CF_GROUP, CF_PRICE, CF_REVIEW = range(10)

TMP_DIR = Path(__file__).resolve().parent.parent / "storage" / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

TZ = ZoneInfo("Asia/Tashkent")


# ================= UI =================

def _menu_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/kiritish"), KeyboardButton("/tasdiq")]],
        resize_keyboard=True,
    )


def _review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Tasdiqlash", callback_data="cfr:send")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="cfr:back")],
    ])


# ================= HELPERS =================

def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _normalize_phone(phone: str) -> str:
    d = _digits_only(phone)
    if len(d) == 9:
        return "+998" + d
    if d.startswith("998"):
        return "+" + d
    return "+998" + d[-9:]


def _parse(text: str):
    parts = [p.strip() for p in (text or "").split("-", 2)]
    if len(parts) != 3:
        return None
    return parts[0].upper(), parts[1], _normalize_phone(parts[2])


# ================= START =================

async def tasdiq_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("operator"):
        await update.message.reply_text("❌ Avval /login qiling.", reply_markup=_menu_keyboard())
        return ConversationHandler.END

    op_id = int(context.user_data["operator"]["id"])
    rows = list_open_confirms(op_id, limit=20)

    kb = [[InlineKeyboardButton("➕ Yangi tasdiq", callback_data="cfnew")]]

    for r in rows:
        kb.append([
            InlineKeyboardButton(
                f"{r.get('brand')} | {r.get('phone_plus')}",
                callback_data=f"cfpick:{r['id']}"
            )
        ])

    await update.message.reply_text("Tasdiq bo‘limi:", reply_markup=InlineKeyboardMarkup(kb))
    return CF_PICK


# ================= NEW =================

async def on_new_confirm_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "Format: BRAND-Mijoz-910175253\nMasalan: LEAP-Akmal-910175253"
    )
    return CF_NEW_CP


async def on_new_confirm_cp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    triple = _parse(update.message.text)
    if not triple:
        await update.message.reply_text("❌ Format noto‘g‘ri.")
        return CF_NEW_CP

    brand, client, phone = triple
    cp = get_or_create_counterparty(name=f"{brand} {client}", phone=phone)

    op_id = int(context.user_data["operator"]["id"])

    cid = create_confirm(
        operator_id=op_id,
        brand=brand,
        client_name=client,
        phone_plus=phone,
        counterparty_meta=cp["meta"],
    )

    context.user_data["confirm_id"] = cid
    context.user_data["confirm_data"] = {
        "brand": brand,
        "client": client,
        "phone": phone,
        "cp_meta": cp["meta"],
    }

    await update.message.reply_text("🖼 Foto yuboring.")
    return CF_PHOTO


# ================= PICK =================

async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cfnew":
        return await on_new_confirm_click(update, context)

    op_id = int(context.user_data["operator"]["id"])
    cid = int(q.data.split(":")[1])

    row = get_confirm(op_id, cid)
    if not row:
        await q.edit_message_text("❌ Topilmadi.")
        return ConversationHandler.END

    context.user_data["confirm_id"] = cid
    context.user_data["confirm_data"] = row

    await q.edit_message_text("🖼 Foto yuboring.")
    return CF_PHOTO


# ================= PHOTO =================

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.photo[-1].get_file()
    path = TMP_DIR / f"{update.message.message_id}.jpg"
    await file.download_to_drive(str(path))

    context.user_data["confirm_data"]["image"] = str(path)

    await update.message.reply_text("🧾 Nimaligi:")
    return CF_KIND


async def on_kind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["confirm_data"]["item"] = update.message.text
    await update.message.reply_text("📏 Razmer:")
    return CF_SIZE


async def on_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["confirm_data"]["size"] = update.message.text
    await update.message.reply_text("🔢 Soni:")
    return CF_QTY


async def on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["confirm_data"]["qty"] = int(_digits_only(update.message.text))
    await update.message.reply_text("💰 Narx:")
    return CF_PRICE


async def on_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["confirm_data"]["price"] = int(_digits_only(update.message.text))

    d = context.user_data["confirm_data"]

    text = (
        f"Brend: {d['brand']}\n"
        f"Mijoz: {d['client']}\n"
        f"Tel: {d['phone']}\n"
        f"Nomi: {d['item']}\n"
        f"Razmer: {d['size']}\n"
        f"Soni: {d['qty']}\n"
        f"Narx: {d['price']}"
    )

    await update.message.reply_text(text, reply_markup=_review_kb())
    return CF_REVIEW


# ================= REVIEW =================

async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cfr:back":
        await q.edit_message_text("Bekor qilindi.")
        return ConversationHandler.END

    d = context.user_data["confirm_data"]
    op = context.user_data["operator"]

    moment = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    org = get_default_organization()

    prod = create_product(
        name=f"{d['brand']} {d['item']} {d['size']}",
        productfolder_meta=None,
        sale_price_uzs=d["price"],
        price_type_meta=find_price_type_meta_by_name("Цена продажи"),
    )

    order = create_customerorder(
        organization_meta=org["meta"],
        agent_meta=d["cp_meta"],
        sales_channel_meta=None,
        moment_iso=moment,
        description="BOT TASDIQ",
        positions=[{
            "assortment": {"meta": prod["meta"]},
            "quantity": float(d["qty"]),
            "price": d["price"] * 100,
        }],
    )

    mark_confirm_done(int(op["id"]), context.user_data["confirm_id"])

    await q.edit_message_text("✅ Buyurtma yuborildi.")

    return ConversationHandler.END


# ================= CANCEL =================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bekor qilindi.", reply_markup=_menu_keyboard())
    return ConversationHandler.END
