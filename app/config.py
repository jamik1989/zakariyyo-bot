# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
MOYSKLAD_BASE_URL = os.getenv("MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2").strip()

# /kiritish modul kanali
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")

# /tasdiq modul kanali
CONFIRM_CHAT_ID = int(os.getenv("CONFIRM_CHAT_ID", "0") or "0")

# Adminlar (Telegram user ID), vergul bilan: 123,456
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = [int(x) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

# Google Cloud Vision (service account JSON content)
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables / .env ga BOT_TOKEN kiriting.")
