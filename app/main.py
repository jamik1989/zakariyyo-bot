# app/main.py
import logging

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

from .handlers.order import (
    kiritish_start,
    step_text,
    handle_check,
    handle_manual_amount_date,
    on_sales_channel_chosen,
    on_paytype_chosen,
    STEP_TEXT,
    STEP_CHECK,
    STEP_AMOUNT_DATE,
    STEP_CHANNEL,
    STEP_PAYTYPE,
    cancel as cancel_order,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))

    # REGISTER
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("register", register_start)],
            states={
                REG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_phone)],
                REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
                REG_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_pass)],
            },
            fallbacks=[CommandHandler("cancel", cancel_auth)],
        )
    )

    # LOGIN
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("login", login_start)],
            states={
                LOG_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
                LOG_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_pass)],
            },
            fallbacks=[CommandHandler("cancel", cancel_auth)],
        )
    )

    # ORDER
    application.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("kiritish", kiritish_start)],
            states={
                STEP_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_text)],
                STEP_CHECK: [MessageHandler(filters.PHOTO, handle_check)],
                STEP_AMOUNT_DATE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_amount_date)
                ],
                STEP_CHANNEL: [
                    CallbackQueryHandler(on_sales_channel_chosen, pattern=r"^sc:")
                ],
                STEP_PAYTYPE: [
                    CallbackQueryHandler(on_paytype_chosen, pattern=r"^pt:")
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_order)],
        )
    )

    return application


def main():
    logger.info("🚀 Bot ishga tushmoqda (POLLING)...")
    init_db()
    app = build_app()
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
