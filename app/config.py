import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MOYSKLAD_TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
MOYSKLAD_BASE_URL = os.getenv("MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. .env faylga BOT_TOKEN kiriting.")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0") or "0")
