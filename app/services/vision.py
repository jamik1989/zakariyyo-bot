from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional, Tuple

from google.cloud import vision
from google.oauth2 import service_account

from ..config import GCP_SA_JSON


class VisionError(RuntimeError):
    pass


# ================= CLIENT =================

def _build_client() -> vision.ImageAnnotatorClient:
    if not GCP_SA_JSON:
        raise VisionError(
            "GCP_SA_JSON topilmadi. Railway Variables ga service account JSON qo‘ying."
        )
    try:
        info = json.loads(GCP_SA_JSON)
    except Exception as e:
        raise VisionError(f"GCP_SA_JSON JSON emas yoki buzilgan: {e}")

    creds = service_account.Credentials.from_service_account_info(info)
    return vision.ImageAnnotatorClient(credentials=creds)


_CLIENT: Optional[vision.ImageAnnotatorClient] = None


def _client() -> vision.ImageAnnotatorClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


# ================= OCR TEXT =================

def extract_text(image_path: str) -> str:
    """
    Cheklar uchun avval document_text_detection,
    fallback sifatida text_detection.
    """
    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)

    # 1) document_text_detection
    resp = _client().document_text_detection(image=image)
    if not (resp.error and resp.error.message):
        if resp.full_text_annotation and resp.full_text_annotation.text:
            return resp.full_text_annotation.text or ""

    # 2) fallback
    resp2 = _client().text_detection(image=image)
    if resp2.error and resp2.error.message:
        raise VisionError(resp2.error.message)

    if not resp2.text_annotations:
        return ""

    return resp2.text_annotations[0].description or ""


# ================= AMOUNT =================

def _find_amount_uzs(text: str) -> Optional[int]:
    """
    Faqat valuta qatordagi summani oladi:
    so'm / uzs / сум / итого / jami / total
    Karta raqamlari, RRN, ID lar inkor qilinadi.
    """
    if not text:
        return None

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    CURRENCY_WORDS = [
        "so'm", "som", "сум", "сўм", "uzs",
        "итого", "итог", "jami", "total", "amount", "summa"
    ]

    BLOCK_WORDS = [
        "card", "карта", "pan", "auth", "rrn",
        "terminal", "терминал", "qr", "id",
        "чек", "№"
    ]

    def parse_amount(raw: str) -> Optional[int]:
        # 400.000 / 400 000 / 400,000 → 400000
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return None
        val = int(digits)
        if 1_000 <= val <= 500_000_000:
            return val
        return None

    # 1️⃣ ENG ISHONCHLI: valuta so‘zi bor qator
    for line in lines:
        low = line.lower()

        if any(w in low for w in BLOCK_WORDS):
            continue

        if any(w in low for w in CURRENCY_WORDS):
            nums = re.findall(r"\d[\d\s.,]{2,20}", line)
            values = [parse_amount(n) for n in nums]
            values = [v for v in values if v]
            if values:
                return max(values)

    # 2️⃣ Fallback (kamdan-kam ishlaydi)
    candidates = []
    for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,20})(?!\d)", text):
        val = parse_amount(m.group(1))
        if val:
            candidates.append(val)

    if not candidates:
        return None

    return max(candidates)


# ================= DATE =================

def _find_date(text: str) -> Optional[str]:
    """
    Sana formatlari:
    28.01.2026 / 28-01-2026 / 28/01/26
    """
    if not text:
        return None

    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if not m:
        return None

    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3))
    if y < 100:
        y += 2000

    try:
        return date(y, mo, d).isoformat()
    except Exception:
        return None


# ================= MAIN =================

def detect_amount_and_date(image_path: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    returns: (amount_uzs, date_iso, raw_text)
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt = _find_date(raw)
    return amount, dt, raw
