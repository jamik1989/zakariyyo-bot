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


def extract_text(image_path: str) -> str:
    """
    Chek/kvитанция uchun avval document_text_detection (aniqroq),
    agar bo‘lmasa text_detection fallback.
    """
    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)

    # 1) document_text_detection (cheklar uchun yaxshi)
    resp = _client().document_text_detection(image=image)
    if resp.error and resp.error.message:
        doc_err = resp.error.message
    else:
        doc_err = None

    if not doc_err:
        if resp.full_text_annotation and resp.full_text_annotation.text:
            return resp.full_text_annotation.text or ""
        doc_err = "document_text_detection returned empty"

    # 2) fallback: text_detection
    resp2 = _client().text_detection(image=image)
    if resp2.error and resp2.error.message:
        raise VisionError(resp2.error.message)

    if not resp2.text_annotations:
        return ""

    return resp2.text_annotations[0].description or ""


def _find_amount_uzs(text: str) -> Optional[int]:
    """
    ✅ Yaxshilangan (kontekstli) summa topish:
    - 'ИТОГО/JAMI/K OPLATE/UZS/so‘m' yaqinidagi raqamlarni ustun qiladi
    - 'ИНН/RRN/ФН/TERMINAL/QR/ID/ЧЕК №' yaqinidagi raqamlarni pasaytiradi
    - 400.000 / 400 000 / 400,000 / 400000 -> 400000
    """
    if not text:
        return None

    t = text.lower()

    POS_WORDS = [
        "итого", "итог", "к оплате", "кoплате", "оплате", "оплата",
        "jami", "jami:", "summa", "to'lov", "tolov", "total", "amount",
        "uzs", "so'm", "som", "сум", "сўм",
    ]
    NEG_WORDS = [
        "инн", "ррн", "фн", "фп", "фиск", "касса", "ккм", "смена",
        "чек", "чек№", "№", "terminal", "терминал", "qr", "id",
        "карта", "card", "pan", "auth", "ref", "rrn",
    ]

    def parse_num(raw: str) -> Optional[int]:
        s = (raw or "").strip()

        # Decimal ".00" yoki ",00" bo'lsa olib tashlaymiz (cheklarda ko'p uchraydi)
        s = re.sub(r"([.,])00\b", "", s)

        digits = re.sub(r"\D", "", s)
        if not digits:
            return None

        v = int(digits)
        if 1000 <= v <= 500_000_000:
            return v
        return None

    # 1) Eng ishonchli: keyword + summa yonma-yon
    kw_pattern = r"(?:%s)\s*[:\-]?\s*([0-9][0-9\s.,]{2,20})" % "|".join(
        [re.escape(w) for w in POS_WORDS]
    )
    kw_hits = []
    for m in re.finditer(kw_pattern, t, flags=re.IGNORECASE):
        val = parse_num(m.group(1))
        if val is not None:
            kw_hits.append(val)
    if kw_hits:
        return max(kw_hits)

    # 2) Umumiy kandidatlarni score qilamiz
    candidates = []
    for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,20})(?!\d)", t):
        val = parse_num(m.group(1))
        if val is None:
            continue

        start = m.start(1)
        end = m.end(1)
        window = t[max(0, start - 50): min(len(t), end + 50)]

        score = 0

        for w in POS_WORDS:
            if w in window:
                score += 10

        if ("uzs" in window) or ("so'm" in window) or ("som" in window) or ("сум" in window) or ("сўм" in window):
            score += 8

        for w in NEG_WORDS:
            if w in window:
                score -= 7

        # Juda katta raqamlar (ID/QR) ko'p bo'lgani uchun biroz pasaytiramiz
        if val >= 10_000_000:
            score -= 2

        candidates.append((score, val))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_score, best_val = candidates[0]

    if best_score <= -5:
        return None

    return best_val


def _find_date(text: str) -> Optional[str]:
    """
    Sana:
      28.01.2026
      28/01/2026
      28-01-2026
      28.01.26
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


def detect_amount_and_date(image_path: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    returns: (amount_uzs, date_iso, raw_text)
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt = _find_date(raw)
    return amount, dt, raw
