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

# ✅ ORDER FLOW IMPORTS (order.py ichida bor)
from .handlers.order import (
    kiritish_start,

    on_paytype_chosen,          # cb: pt:cash | pt:card

    cp_search_text,             # msg text: qidiruv (yoki brend-mijoz-tel)
    on_cp_pick,                 # cb: cp:<id>
    on_cp_create_new,           # cb: cpnew:<query>

    handle_manual_amount_date,  # msg text: amount/date/time yoki edit input

    handle_check_optional,      # msg photo/pdf

    on_sales_channel_chosen,    # cb: sc:<id>

    on_review_action,           # cb: rv:...

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

    # ORDER FLOW
    order_conv = ConversationHandler(
        entry_points=[CommandHandler("kiritish", kiritish_start)],
        states={
            STEP_PAYTYPE: [CallbackQueryHandler(on_paytype_chosen, pattern=r"^pt:")],

            STEP_CP_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_search_text)],
            STEP_CP_PICK: [
                CallbackQueryHandler(on_cp_pick, pattern=r"^cp:"),
                CallbackQueryHandler(on_cp_create_new, pattern=r"^cpnew:"),
            ],

            # ✅ amount/date/time manual yoki edit input shu state’da
            STEP_AMOUNT_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_amount_date)],

            STEP_CHECK: [MessageHandler(filters.PHOTO | filters.Document.PDF, handle_check_optional)],

            STEP_CHANNEL: [CallbackQueryHandler(on_sales_channel_chosen, pattern=r"^sc:")],

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
