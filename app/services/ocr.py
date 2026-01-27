import re
from typing import Optional, Tuple
from dateutil import parser as dateparser
from PIL import Image
import pytesseract


def extract_text_from_image(image_path: str) -> str:
    img = Image.open(image_path)
    text = pytesseract.image_to_string(img, lang="rus+eng")
    return text


def find_amount(text: str) -> Optional[int]:
    """
    UZS / so'm / сум / итого bo‘yicha summani topadi.
    Natija: UZS (butun son)
    """
    patterns = [
        r"(итого|итог|sum|jami)[^\d]{0,10}([\d\s.,]+)",
        r"([\d\s.,]+)\s*(uzs|so['`]?m|сум)",
    ]

    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            raw = m.group(2) if m.lastindex >= 2 else m.group(1)
            cleaned = raw.replace(" ", "").replace(",", "").replace(".", "")
            if cleaned.isdigit():
                return int(cleaned)

    return None


def find_date(text: str) -> Optional[str]:
    """
    Sana topadi va ISO formatga o‘tkazadi: YYYY-MM-DD
    """
    candidates = re.findall(r"\d{2}[./]\d{2}[./]\d{4}", text)
    for c in candidates:
        try:
            d = dateparser.parse(c, dayfirst=True)
            return d.date().isoformat()
        except Exception:
            pass
    return None
