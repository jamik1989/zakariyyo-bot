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


def extract_text(image_path: str) -> str:
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


# ---------- AMOUNT ----------

def _find_amount_uzs(text: str) -> Optional[int]:
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
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return None
        val = int(digits)
        if 1_000 <= val <= 500_000_000:
            return val
        return None

    # 1) valuta qatordan
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

    # 2) fallback
    candidates = []
    for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,20})(?!\d)", text):
        val = parse_amount(m.group(1))
        if val:
            candidates.append(val)

    return max(candidates) if candidates else None


# ---------- DATE + TIME ----------

_DATE_WORDS = [
    "sana", "date", "дата", "vaqt", "time", "время",
    "chek", "чек", "receipt", "kvитан", "квитан"
]

def _parse_date_from_fragment(fragment: str) -> Optional[str]:
    # dd.mm.yyyy | dd/mm/yyyy | dd-mm-yyyy | dd.mm.yy
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", fragment)
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

def _parse_time_hm(fragment: str) -> Optional[str]:
    # HH:MM yoki H:MM
    m = re.search(r"\b([01]?\d|2[0-3])[:\.]([0-5]\d)\b", fragment)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    return f"{hh:02d}:{mm:02d}"

def _find_date_and_time(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Qaytaradi: (date_iso, time_hm)
    - Avval 'sana/date/дата' kabi so'zli qatorlardan qidiradi
    - Keyin fallback: butun tekst ichidan
    """
    if not text:
        return None, None

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # 1) so'zli qatorlardan
    for line in lines:
        low = line.lower()
        if any(w in low for w in _DATE_WORDS):
            dt = _parse_date_from_fragment(line)
            tm = _parse_time_hm(line)
            if dt or tm:
                return dt, tm

    # 2) qator+yonidagi qatorlarni ham tekshirish (ko‘p cheklarda sana alohida qatorda bo‘ladi)
    for i, line in enumerate(lines):
        dt = _parse_date_from_fragment(line)
        tm = _parse_time_hm(line)
        if dt or tm:
            # ehtimol sana boshqa qatorda, vaqt boshqa qatorda bo‘ladi
            if not dt and i > 0:
                dt = _parse_date_from_fragment(lines[i - 1]) or dt
            if not dt and i + 1 < len(lines):
                dt = _parse_date_from_fragment(lines[i + 1]) or dt

            if not tm and i > 0:
                tm = _parse_time_hm(lines[i - 1]) or tm
            if not tm and i + 1 < len(lines):
                tm = _parse_time_hm(lines[i + 1]) or tm

            return dt, tm

    # 3) fallback: butun text
    dt = _parse_date_from_fragment(text)
    tm = _parse_time_hm(text)
    return dt, tm


def detect_amount_and_date(image_path: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    returns: (amount_uzs, date_iso, raw_text)
    time_hm kerak bo‘lsa: contextga o‘zingiz saqlaysiz (order.py da)
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt, tm = _find_date_and_time(raw)

    # ixtiyoriy: vaqtni rawga qo‘shimcha saqlash uchun qulay
    # order.py da time_hm ni alohida contextga saqlaymiz
    return amount, dt, raw
