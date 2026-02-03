# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()

def env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()

# --- Telegram ---
BOT_TOKEN = env("BOT_TOKEN")  # tokenni main.py da tekshirgan yaxshi
GROUP_CHAT_ID = int(env("GROUP_CHAT_ID", "0") or "0")

# --- MoySklad ---
MOYSKLAD_TOKEN = env("MOYSKLAD_TOKEN")
MOYSKLAD_BASE_URL = env("MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2")

# --- Runtime mode (Railway) ---
# Railway odatda PORT beradi: 8080
PORT = int(env("PORT", "8080") or "8080")

# Webhook ishlatmoqchi bo'lsangiz:
# PUBLIC_BASE_URL = "https://zakariyyo-bot-production.up.railway.app"
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL")  # bo‘sh bo‘lsa polling ishlatamiz

# Webhook path (xavfsizroq): tokenni URLga qo'ymaslik uchun alohida secret
WEBHOOK_SECRET = env("WEBHOOK_SECRET")  # ixtiyoriy, bo‘sh bo‘lishi ham mumkin
WEBHOOK_PATH = env("WEBHOOK_PATH", "/telegram")

# --- OCR (tesseract) - hozir vaqtincha o‘chirmoqchi bo‘lsangiz, flag qo‘shdik ---
OCR_ENABLED = env("OCR_ENABLED", "0") in ("1", "true", "True", "YES", "yes")

# Agar keyin tesseract qaytarilsa:
TESSERACT_CMD = env("TESSERACT_CMD")
TESS_LANG = env("TESS_LANG", "rus+eng")

# --- Google Cloud Vision (keyin qo‘shamiz) ---
# Railway Variables’da JSON yo‘li yoki json matn bilan ishlash mumkin
GCV_ENABLED = env("GCV_ENABLED", "0") in ("1", "true", "True", "YES", "yes")
GOOGLE_APPLICATION_CREDENTIALS = env("GOOGLE_APPLICATION_CREDENTIALS")  # fayl yo'li bo'lishi mumkin
GCV_PROJECT_ID = env("GCV_PROJECT_ID")  # ixtiyoriy
