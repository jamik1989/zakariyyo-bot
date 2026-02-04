# app/services/vision.py
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


def _build_client() -> vision.ImageAnnotatorClient:
    if not GCP_SA_JSON:
        raise VisionError("GCP_SA_JSON topilmadi. Railway Variables ga GCP_SA_JSON qo‘ying (service account JSON matni).")
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


def extract_text(image_path: str) -> str:
    """
    Chek/kvитанция uchun avval document_text_detection (aniqroq),
    keyin text_detection fallback.
    """
    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)

    resp = _client().document_text_detection(image=image)
    if resp.error and resp.error.message:
        doc_err = resp.error.message
    else:
        doc_err = None

    if not doc_err:
        if resp.full_text_annotation and resp.full_text_annotation.text:
            return resp.full_text_annotation.text or ""
        doc_err = "document_text_detection returned empty"

    resp2 = _client().text_detection(image=image)
    if resp2.error and resp2.error.message:
        raise VisionError(resp2.error.message)

    if not resp2.text_annotations:
        return ""

    return resp2.text_annotations[0].description or ""


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _find_amount_uzs(text: str) -> Optional[int]:
    """
    Summani aniqlash:
    - karta raqami (13-19 digit) ni tashlab ketamiz
    - 'UZS', 'SO'M', 'SUM', 'СУМ' atrofidagi raqamlarni ustun qo'yamiz
    - aks holda eng katta mantiqiy summani olamiz
    """
    if not text:
        return None

    t = text.upper()

    preferred: list[int] = []
    all_candidates: list[int] = []

    # raqam tokenlar
    for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,18})(?!\d)", t):
        raw = m.group(1)
        digits = _digits_only(raw)
        if not digits:
            continue

        # karta raqamini kesamiz
        if 13 <= len(digits) <= 19:
            continue

        val = int(digits)

        if not (1000 <= val <= 500_000_000):
            continue

        all_candidates.append(val)

        # atrofida valuta kalit so'zlari bo'lsa preferred
        start = max(0, m.start() - 15)
        end = min(len(t), m.end() + 15)
        window = t[start:end]
        if any(k in window for k in ("UZS", "SO'M", "SOM", "SUM", "СУМ", "СОМ", "ИТОГО", "JAMI", "TOTAL")):
            preferred.append(val)

    if preferred:
        return max(preferred)
    if all_candidates:
        return max(all_candidates)
    return None


def _find_date(text: str) -> Optional[str]:
    """
    Kuchliroq sana qidirish:
    - dd.mm.yyyy / dd/mm/yy / dd-mm-yyyy
    - yyyy-mm-dd
    - bir nechta topilsa: eng real variantni tanlaymiz (2000-2099 oralig'i)
    """
    if not text:
        return None

    t = text

    candidates: list[date] = []

    # dd.mm.yyyy or dd.mm.yy
    for m in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", t):
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3))
        if y < 100:
            y += 2000
        if not (2000 <= y <= 2099):
            continue
        try:
            candidates.append(date(y, mo, d))
        except Exception:
            pass

    # yyyy-mm-dd
    for m in re.finditer(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", t):
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            candidates.append(date(y, mo, d))
        except Exception:
            pass

    if not candidates:
        return None

    # ko'pincha chekda keraklisi oxirroqda bo'ladi
    best = sorted(candidates)[-1]
    return best.isoformat()


def _find_time(text: str) -> Optional[str]:
    """
    Vaqt:
    - HH:MM
    - HH:MM:SS
    """
    if not text:
        return None

    # ko'pincha chekda vaqt ham bor
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b", text)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def detect_amount_date_time(image_path: str) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    """
    returns: (amount_uzs, date_iso, time_hms, raw_text)
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt = _find_date(raw)
    tm = _find_time(raw)
    return amount, dt, tm, raw
