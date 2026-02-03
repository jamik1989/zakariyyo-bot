# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
MOYSKLAD_BASE_URL = os.getenv("MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2").strip()

# Telegram group (optional)
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")

# Google Cloud Vision (service account JSON content)
# Railway Variables ichida GCP_SA_JSON sifatida saqlang (butun JSON string)
GCP_SA_JSON = os.getenv("GCP_SA_JSON", "").strip()

# (ixtiyoriy) agar yoqib-o‘chirish kerak bo‘lsa
VISION_ENABLED = os.getenv("VISION_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables / .env ga BOT_TOKEN kiriting.")
