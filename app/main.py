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

# ===== ORDER (kiritish) =====
from .handlers.order import (
    kiritish_start,
    on_paytype_chosen,
    cp_search_text,
    on_cp_pick,
    on_cp_create_new,
    handle_manual_amount_date,
    handle_check_optional,
    on_sales_channel_chosen,
    on_review_action,
    STEP_PAYTYPE,
    STEP_CP_SEARCH,
    STEP_CP_PICK,
    STEP_AMOUNT_DATE,
    STEP_CHECK,
    STEP_CHANNEL,
    STEP_REVIEW,
    cancel as cancel_order,
)

# ===== CONFIRM (tasdiq) =====
from .handlers.confirm import (
    tasdiq_start,
    on_pick,
    on_photo,
    on_kind,
    on_size,
    on_qty,
    on_channel_pick,
    on_group_pick,
    on_price,
    on_review,
    on_edit_choose,
    on_edit_value,
    CF_PICK,
    CF_PHOTO,
    CF_KIND,
    CF_SIZE,
    CF_QTY,
    CF_CHANNEL,
    CF_GROUP,
    CF_PRICE,
    CF_REVIEW,
    CF_EDIT_CHOOSE,
    CF_EDIT_VALUE,
    cancel as cancel_confirm,
)

# ===== ADMIN =====
from .handlers.admin import (
    admin_start,
    admin_menu_click,
    admin_add_phone,
    admin_add_name,
    admin_add_pass,
    admin_del_phone,
    admin_cancel,
    AD_MENU,
    AD_ADD_PHONE,
    AD_ADD_NAME,
    AD_ADD_PASS,
    AD_DEL_PHONE,
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

    # ADMIN
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            AD_MENU: [CallbackQueryHandler(admin_menu_click, pattern=r"^adm:")],
            AD_ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_phone)],
            AD_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_name)],
            AD_ADD_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_pass)],
            AD_DEL_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_del_phone)],
        },
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(admin_conv)


    # ORDER FLOW (/kiritish)
    order_conv = ConversationHandler(
        entry_points=[CommandHandler("kiritish", kiritish_start)],
        states={
            STEP_PAYTYPE: [CallbackQueryHandler(on_paytype_chosen, pattern=r"^pt:")],

            STEP_CP_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, cp_search_text)],
            STEP_CP_PICK: [
                CallbackQueryHandler(on_cp_pick, pattern=r"^cp:"),
                CallbackQueryHandler(on_cp_create_new, pattern=r"^cpnew:"),
            ],

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

    # CONFIRM FLOW (/tasdiq)
    confirm_conv = ConversationHandler(
        entry_points=[CommandHandler("tasdiq", tasdiq_start)],
        states={
            CF_PICK: [CallbackQueryHandler(on_pick, pattern=r"^cfpick:")],
            CF_PHOTO: [MessageHandler(filters.PHOTO, on_photo)],

            CF_KIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_kind)],
            CF_SIZE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_size)],
            CF_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_qty)],

            CF_CHANNEL: [CallbackQueryHandler(on_channel_pick, pattern=r"^cfsc:")],

            # ✅ MUHIM: pattern r"^cfg:" — confirm.py ham shu prefix bilan yuboradi
            CF_GROUP: [CallbackQueryHandler(on_group_pick, pattern=r"^cfg:")],

            CF_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_price)],

            CF_REVIEW: [CallbackQueryHandler(on_review, pattern=r"^cfr:")],
            CF_EDIT_CHOOSE: [CallbackQueryHandler(on_edit_choose, pattern=r"^cfe:")],
            CF_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_edit_value)],
        },
        fallbacks=[CommandHandler("cancel", cancel_confirm)],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(confirm_conv)

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
