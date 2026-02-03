# app/services/vision.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple

from google.cloud import vision
from google.oauth2 import service_account

from ..config import GCP_SA_JSON


class VisionError(RuntimeError):
    pass


def _build_client() -> vision.ImageAnnotatorClient:
    if not GCP_SA_JSON:
        raise VisionError("GCP_SA_JSON topilmadi. Railway Variables ga GCP_SA_JSON qo‘ying.")
    info = json.loads(GCP_SA_JSON)
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
    resp = _client().text_detection(image=image)
    if resp.error and resp.error.message:
        raise VisionError(resp.error.message)

    if not resp.text_annotations:
        return ""

    # [0] = full text
    return resp.text_annotations[0].description or ""


def _find_amount_uzs(text: str) -> Optional[int]:
    """
    Ko‘pincha cheklarda:
      600 000
      600000
      600,000
      600.000
    Eng katta mantiqiy summani olamiz.
    """
    if not text:
        return None

    candidates = []
    for m in re.finditer(r"(?<!\d)(\d[\d\s.,]{2,15})(?!\d)", text):
        raw = m.group(1)
        digits = re.sub(r"\D", "", raw)
        if not digits:
            continue
        val = int(digits)
        # filtr: juda kichik yoki juda katta bo'lmasin (ixtiyoriy)
        if 1000 <= val <= 500_000_000:
            candidates.append(val)

    if not candidates:
        return None
    return max(candidates)


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

    # dd.mm.yyyy yoki dd.mm.yy
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if not m:
        return None

    d = int(m.group(1))
    mo = int(m.group(2))
    y = int(m.group(3))
    if y < 100:
        y += 2000

    try:
        iso = date(y, mo, d).isoformat()
    except Exception:
        return None
    return iso


def detect_amount_and_date(image_path: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    returns: amount_uzs, date_iso, raw_text
    """
    raw = extract_text(image_path)
    amount = _find_amount_uzs(raw)
    dt = _find_date(raw)
    return amount, dt, raw
