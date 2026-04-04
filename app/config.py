# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

APP_MODE = os.getenv("APP_MODE", "all_in_one").strip().lower()

LEGACY_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ORDER_BOT_TOKEN = os.getenv("ORDER_BOT_TOKEN", "").strip()
CONFIRM_BOT_TOKEN = os.getenv("CONFIRM_BOT_TOKEN", "").strip()

if APP_MODE == "order_bot":
    BOT_TOKEN = ORDER_BOT_TOKEN or LEGACY_BOT_TOKEN
elif APP_MODE == "confirm_bot":
    BOT_TOKEN = CONFIRM_BOT_TOKEN or LEGACY_BOT_TOKEN
else:
    BOT_TOKEN = LEGACY_BOT_TOKEN or ORDER_BOT_TOKEN or CONFIRM_BOT_TOKEN

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
MOYSKLAD_BASE_URL = os.getenv("MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2").strip()

# /kiritish modul kanali
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")

# /tasdiq modul kanali
CONFIRM_CHAT_ID = int(os.getenv("CONFIRM_CHAT_ID", "0") or "0")

# /takror uchun alohida kanal (hozircha rezerv)
REPEAT_CHAT_ID = int(os.getenv("REPEAT_CHAT_ID", "0") or "0")

# /tasdiq modulidan tushadigan buyurtmalar uchun sklad nomi
TASDIQ_STORE_NAME = os.getenv("TASDIQ_STORE_NAME", "Abusahiy 75").strip()

# Adminlar (Telegram user ID), vergul bilan: 123,456
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

# Google Cloud Vision (service account JSON content)
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")

if not BOT_TOKEN:
    raise RuntimeError(
        "Bot token topilmadi. "
        "APP_MODE=order_bot uchun ORDER_BOT_TOKEN, "
        "APP_MODE=confirm_bot uchun CONFIRM_BOT_TOKEN, "
        "yoki fallback sifatida BOT_TOKEN kiriting."
    )
