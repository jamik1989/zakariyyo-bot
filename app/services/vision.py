# app/services/vision.py
from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional, Tuple, List

from google.cloud import vision
from google.oauth2 import service_account

from ..config import GCP_SA_JSON


class VisionError(RuntimeError):
    pass


def _build_client() -> vision.ImageAnnotatorClient:
    if not GCP_SA_JSON:
        raise VisionError(
            "GCP_SA_JSON topilmadi. Railway Variables ga GCP_SA_JSON qo‘ying (service account JSON matni)."
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


# ---------------- TEXT EXTRACTION ----------------

def extract_text(image_path: str) -> str:
    """
    Chek/kvitansiya uchun document_text_detection aniqroq.
    Agar bo‘sh qaytsa, text_detection fallback.
    """
    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)

    # 1) document_text_detection
    resp = _client().document_text_detection(image=image)
    if resp.error and resp.error.message:
        doc_err = resp.error.message
    else:
        doc_err = None

    if not doc_err:
        txt = (resp.full_text_annotation.text if resp.full_text_annotation else "") or ""
        if txt.strip():
            return txt
        doc_err = "document_text_detection returned empty"

    # 2) fallback: text_detection
    resp2 = _client().text_detection(image=image)
    if resp2.error and resp2.error.message:
        raise VisionError(resp2.error.message)

    if not resp2.text_annotations:
        return ""

    return resp2.text_annotations[0].description or ""


# ---------------- AMOUNT (UZS) ----------------

_AMOUNT_HINTS = [
    "JAMI", "JAMI:", "ITOG", "ИТОГ", "TOTAL", "SUM", "SUMMA", "СУММА",
    "K OPLATE", "К ОПЛАТЕ", "OPLATA", "ОПЛАТА", "PAYMENT",
    "UZS", "SO'M", "SOM", "СУМ", "СУММ",
]


def _looks_like_card_number(s: str) -> bool:
    # 16-digit yoki 4-4-4-4 ko‘rinishlar
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 16:
        return True
    if re.search(r"\b(\d{4}[\s-]\d{4}[\s-]\d{4}[\s-]\d{4})\b", s or ""):
        return True
    return False


def _normalize_amount_candidate(raw: str) -> Optional[int]:
    """
    400.000 -> 400000
    400 000 -> 400000
    400,000 -> 400000
    40.400.000 (OCR xato bo‘lishi mumkin) -> 40400000 (lekin keyin scoring bilan tushiramiz)
    """
    if not raw:
        return None

    # faqat raqam va ajratgichlar
    cleaned = raw.strip()

    # kartaga o‘xshasa summaga kiritmaymiz
    if _looks_like_card_number(cleaned):
        return None

    # Juda uzun raqamlar (masalan 20+ raqam) kerak emas
    digits_only = re.sub(r"\D", "", cleaned)
    if len(digits_only) > 12:
        return None

    # Asosiy: hamma ajratgichlarni olib tashlab int qilish
    if not digits_only:
        return None

    val = int(digits_only)
    return val


def _line_has_amount_hint(line: str) -> bool:
    u = (line or "").upper()
    return any(h in u for h in _AMOUNT_HINTS)


def _find_amount_uzs(text: str) -> Optional[int]:
    """
    Strategiya:
    1) Avval "JAMI / ИТОГ / TOTAL / SUMMA / UZS / SO'M" bor qatorlardan qidiramiz.
    2) Topilmasa, barcha matndan qidiramiz.
    3) Karta raqami / uzun raqamlarni tashlab ketamiz.
    4) "mantiqiy" oraliq: 1 000 ... 500 000 000
    """
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    preferred_lines = [ln for ln in lines if _line_has_amount_hint(ln)]
    scan_groups = [preferred_lines, lines]

    candidates: List[Tuple[int, int]] = []  # (score, value)

    for gi, group in enumerate(scan_groups):
        for line in group:
            # kartaga o‘xshagan qatorlarni o‘tkazib yuboramiz
            if _looks_like_card_number(line):
                continue

            # line ichidan "raqam-ajratuvchi" fragmentlarni topamiz
            for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,15})(?!\d)", line):
                raw = m.group(1)
                val = _normalize_amount_candidate(raw)
                if val is None:
                    continue

                # mantiqiy filtr
                if not (1000 <= val <= 500_000_000):
                    continue

                score = 0
                # hint bor qatorlar kuchli
                if _line_has_amount_hint(line):
                    score += 50
                # preferred_lines guruhiga birinchi o‘rinda qaraymiz
                score += (20 if gi == 0 else 0)
                # "UZS / SO'M" bo‘lsa yana bonus
                if "UZS" in line.upper() or "SO" in line.upper() or "СУМ" in line.upper():
                    score += 10

                # Juda katta qiymatlarni biroz penalti (OCR xato bo‘lishi ehtimoli)
                if val >= 50_000_000:
                    score -= 5
                if val >= 200_000_000:
                    score -= 20

                candidates.append((score, val))

        # agar hintli qatordan yaxshi kandidat topsak, shu bosqichdayoq qaytaramiz
        if candidates and gi == 0:
            best = sorted(candidates, key=lambda x: (x[0], x[1]))[-1]
            return best[1]

    if not candidates:
        return None

    best = sorted(candidates, key=lambda x: (x[0], x[1]))[-1]
    return best[1]


# ---------------- DATE + TIME ----------------

_MONTH_MAP = {
    "JAN": 1, "YAN": 1, "ЯНВ": 1,
    "FEB": 2, "FEV": 2, "ФЕВ": 2,
    "MAR": 3, "МАР": 3,
    "APR": 4, "АПР": 4,
    "MAY": 5, "МАЙ": 5,
    "JUN": 6, "ИЮН": 6,
    "JUL": 7, "ИЮЛ": 7,
    "AUG": 8, "AVG": 8, "АВГ": 8,
    "SEP": 9, "SEN": 9, "СЕН": 9,
    "OCT": 10, "OKT": 10, "ОКТ": 10,
    "NOV": 11, "NOY": 11, "НОЯ": 11, "НОВ": 11,
    "DEC": 12, "DEK": 12, "ДЕК": 12,
}


def _find_time(text: str) -> Optional[str]:
    """
    14:23
    14:23:05
    """
    if not text:
        return None

    # hh:mm(:ss)?
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?\b", text)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) else 0
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def _find_date(text: str) -> Optional[str]:
    """
    Kuchliroq qidiruv:
    - dd.mm.yyyy / dd/mm/yyyy / dd-mm-yyyy
    - dd.mm.yy
    - yyyy-mm-dd
    - dd Mon yyyy (Mon = yan/fev/... / jan/feb/...)
    """
    if not text:
        return None

    # 1) dd.mm.yyyy yoki dd.mm.yy
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except Exception:
            pass

    # 2) yyyy-mm-dd
    m = re.search(r"\b(\d{4})[./-](\d{1,2})[./-](\d{1,2})\b", text)
    if m:
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        try:
            return date(y, mo, d).isoformat()
        except Exception:
            pass

    # 3) dd mon yyyy (mon 3 harf yoki kirill)
    m = re.search(r"\b(\d{1,2})\s*([A-Za-zА-Яа-яЁё]{3})\s*(\d{2,4})\b", text)
    if m:
        d = int(m.group(1))
        mon = m.group(2).upper()
        y = int(m.group(3))
        if y < 100:
            y += 2000
        mo = _MONTH_MAP.get(mon)
        if mo:
            try:
                return date(y, mo, d).isoformat()
            except Exception:
                pass

    return None


def detect_amount_date_time(image_path: str) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    """
    returns: (amount_uzs, date_iso, time_hms, raw_text)
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt = _find_date(raw)
    tm = _find_time(raw)
    return amount, dt, tm, raw
