# app/main.py
import logging
import sys

from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from .config import BOT_TOKEN
from .db import init_db

from .handlers.start import start
from .handlers.auth import (
    register_start,
    register_phone,
    register_name,
    register_pass,
    login_start,
    login_phone,
    login_pass,
    REG_PHONE,
    REG_NAME,
    REG_PASS,
    LOG_PHONE,
    LOG_PASS,
    cancel as cancel_auth,
)

# ✅ NEW ORDER FLOW IMPORTS (order.py ichida bo‘ladi)
from .handlers.order import (
    kiritish_start,

    # 1) paytype
    on_paytype_chosen,          # cb: pt:cash | pt:card

    # 2) counterparty search/select/create
    cp_search_text,             # msg text: qidiruv so‘zi (brand/ism/tel)
    on_cp_pick,                 # cb: cp:<id>
    on_cp_create_new,           # cb: cpnew:<query>

    # 3a) summa-sana qo'lda (cash yoki OCR fallback/edit)
    handle_manual_amount_date,  # msg text: 600000-28.01.2026

    # 3b) karta bo‘lsa: chek rasmi + OCR
    handle_check_optional,      # msg photo/pdf

    # 4) sales channel
    on_sales_channel_chosen,    # cb: sc:<id>

    # 5) review/confirm/back/edit
    on_review_action,           # cb: rv:confirm | rv:edit | rv:back

    # states
    STEP_PAYTYPE,
    STEP_CP_SEARCH,
    STEP_CP_PICK,
    STEP_AMOUNT_DATE,
    STEP_CHECK,
    STEP_CHANNEL,
    STEP_REVIEW,

    cancel as cancel_order,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    # START
    application.add_handler(CommandHandler("start", start))

    # REGISTER
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            REG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone)],
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REG_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_pass)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth)],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(register_conv)

    # LOGIN
    login_conv = ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            LOG_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass)],
        },
        fallbacks=[CommandHandler("cancel", cancel_auth)],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(login_conv)

    # ORDER FLOW (NEW)
    order_conv = ConversationHandler(
        entry_points=[CommandHandler("kiritish", kiritish_start)],
        states={
            # 1) paytype
            STEP_PAYTYPE: [CallbackQueryHandler(on_paytype_chosen, pattern=r"^pt:")],

            # 2) counterparty search -> pick/create
            STEP_CP_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_search_text)],
            STEP_CP_PICK: [
                CallbackQueryHandler(on_cp_pick, pattern=r"^cp:"),
                CallbackQueryHandler(on_cp_create_new, pattern=r"^cpnew:"),
            ],

            # ✅ 3a) summa-sana (cash yoki OCR fallback/edit)
            STEP_AMOUNT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_amount_date)],

            # 3b) karta bo‘lsa: chek foto (OCR shu yerda)
            STEP_CHECK: [MessageHandler(filters.PHOTO | filters.Document.PDF, handle_check_optional)],

            # 4) sales channel
            STEP_CHANNEL: [CallbackQueryHandler(on_sales_channel_chosen, pattern=r"^sc:")],

            # 5) review/confirm
            STEP_REVIEW: [CallbackQueryHandler(on_review_action, pattern=r"^rv:")],
        },
        fallbacks=[CommandHandler("cancel", cancel_order)],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(order_conv)

    return application


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables yoki .env ga BOT_TOKEN kiriting.")

    logger.info("🚀 Bot ishga tushmoqda...")
    init_db()

    app = build_app()

    try:
        app.run_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
    except Conflict as e:
        logger.error("❌ Telegram Conflict (409): boshqa instansiya ishlayapti. %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
