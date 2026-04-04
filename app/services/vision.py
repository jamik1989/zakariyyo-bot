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
        raise VisionError("GCP_SA_JSON yo‘q")
    info = json.loads(GCP_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(info)
    return vision.ImageAnnotatorClient(credentials=creds)


_CLIENT = None


def _client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def extract_text(image_path: str) -> str:
    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)

    resp = _client().document_text_detection(image=image)
    if resp.error.message:
        raise VisionError(resp.error.message)

    if resp.full_text_annotation and resp.full_text_annotation.text:
        return resp.full_text_annotation.text

    resp2 = _client().text_detection(image=image)
    if resp2.text_annotations:
        return resp2.text_annotations[0].description

    return ""


# =========================
# TEXT NORMALIZE
# =========================

def _normalize_text(text: str) -> str:
    if not text:
        return ""

    t = text

    # OCR ko‘p chalkashtiradigan belgilar
    replacements = {
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
        "—": "-",
        "–": "-",
        "‚": ".",
        ",": ".",
    }

    # faqat vaqt/sana parsingga yordam beradigan yumshoq normalizatsiya
    for a, b in replacements.items():
        t = t.replace(a, b)

    # ortiqcha bo‘sh joylar
    t = re.sub(r"[ \t]+", " ", t)
    return t


# =========================
# AMOUNT
# =========================

def _find_amount(text: str) -> Optional[int]:
    """
    Chekdagi eng ehtimolli summani topish.
    Juda uzun ID/terminal/karta raqamlarini tashlab yuboradi.
    """
    candidates = []

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(lines)

    # Prioritetli patternlar (summa yonida keladigan so‘zlar)
    priority_patterns = [
        r"(?:сумма|summa|итого|jami|оплачено|к оплате|услуга|payment|to'lov|tolov)[^\d]{0,20}(\d[\d\s.,]{2,})",
        r"(\d[\d\s.,]{2,})\s*(?:сум|sum|uzs|so'm|som)\b",
    ]

    for pat in priority_patterns:
        for m in re.finditer(pat, joined, flags=re.IGNORECASE):
            raw = m.group(1)
            digits = re.sub(r"\D", "", raw)
            if not digits:
                continue
            if len(digits) >= 13:
                continue
            val = int(digits)
            if 1000 <= val <= 500_000_000:
                candidates.append((val, 100))  # priority

    # fallback umumiy raqamlar
    for m in re.finditer(r"\b(\d[\d\s.,]{3,})\b", joined):
        raw = m.group(1)
        digits = re.sub(r"\D", "", raw)
        if not digits:
            continue
        if len(digits) >= 13:
            continue
        val = int(digits)
        if 1000 <= val <= 500_000_000:
            score = 0

            # satr kontekstiga qarab score
            line = ""
            for ln in lines:
                if raw in ln:
                    line = ln.lower()
                    break

            if any(k in line for k in ["summa", "сумма", "итого", "jami", "оплачено", "to'lov", "tolov", "uzs", "sum"]):
                score += 20

            candidates.append((val, score))

    if not candidates:
        return None

    # eng katta priority, keyin katta summa
    candidates.sort(key=lambda x: (x[1], x[0]), reverse=True)
    return candidates[0][0]


# =========================
# DATE/TIME HELPERS
# =========================

def _safe_iso_date(d: int, m: int, y: int) -> Optional[str]:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d).isoformat()
    except Exception:
        return None


def _extract_date_candidates(text: str) -> List[Tuple[str, int]]:
    """
    [(iso_date, position), ...]
    """
    out: List[Tuple[str, int]] = []

    # 04.04.2026 / 04-04-2026 / 04/04/26
    for m in re.finditer(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text):
        d = int(m.group(1))
        mo = int(m.group(2))
        y = int(m.group(3))
        iso = _safe_iso_date(d, mo, y)
        if iso:
            out.append((iso, m.start()))

    # 2026.04.04 / 2026-04-04 / 2026/04/04
    for m in re.finditer(r"\b(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\b", text):
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3))
        iso = _safe_iso_date(d, mo, y)
        if iso:
            out.append((iso, m.start()))

    # duplicate remove
    seen = set()
    cleaned = []
    for item in out:
        if item not in seen:
            cleaned.append(item)
            seen.add(item)

    return cleaned


def _extract_time_candidates(text: str) -> List[Tuple[str, int]]:
    """
    [(HH:MM:SS, position), ...]
    """
    out: List[Tuple[str, int]] = []

    # 20:29 yoki 20:29:15
    for m in re.finditer(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?!\d)", text):
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3)) if m.group(3) else 0
        out.append((f"{hh:02d}:{mm:02d}:{ss:02d}", m.start()))

    return out


def _pick_best_date_time(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Sana va vaqtni bir-biriga eng yaqin juftlik sifatida tanlaydi.
    Bu chek OCR uchun ancha ishonchli.
    """
    dates = _extract_date_candidates(text)
    times = _extract_time_candidates(text)

    if not dates and not times:
        return None, None

    if dates and not times:
        return dates[-1][0], None

    if times and not dates:
        return None, times[-1][0]

    # eng yaqin sana-vaqt juftligini tanlash
    best = None
    best_score = None

    for d_iso, d_pos in dates:
        for t_hms, t_pos in times:
            dist = abs(d_pos - t_pos)

            # yaqin bo‘lsa yaxshi
            score = dist

            # chek oxiriga yaqin bo‘lsa odatda transaction moment bo‘ladi
            tail_bonus = abs(len(text) - max(d_pos, t_pos))
            score += tail_bonus * 0.2

            if best_score is None or score < best_score:
                best_score = score
                best = (d_iso, t_hms)

    if best:
        return best

    return dates[-1][0], times[-1][0]


# =========================
# PUBLIC API
# =========================

def detect_amount_date_time(image_path: str) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    raw = extract_text(image_path)
    norm = _normalize_text(raw)

    amount = _find_amount(norm)
    date_iso, time_hms = _pick_best_date_time(norm)

    return amount, date_iso, time_hms, raw