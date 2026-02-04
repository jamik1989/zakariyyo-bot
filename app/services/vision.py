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


def _find_amount(text: str) -> Optional[int]:
    candidates = []
    for m in re.finditer(r"\b(\d[\d\s.,]{3,})\b", text):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) >= 13:
            continue
        val = int(digits)
        if 1000 <= val <= 500_000_000:
            candidates.append(val)
    return max(candidates) if candidates else None


def _find_date(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", text)
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d).isoformat()
    except Exception:
        return None


def _find_time(text: str) -> Optional[str]:
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:00"


def detect_amount_date_time(image_path: str) -> Tuple[Optional[int], Optional[str], Optional[str], str]:
    raw = extract_text(image_path)
    return _find_amount(raw), _find_date(raw), _find_time(raw), raw
