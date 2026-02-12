from google.cloud import vision
import re
from typing import Optional, Tuple
from dateutil import parser as du_parser


def extract_amount_date_from_image(image_path: str) -> Tuple[Optional[int], Optional[str], str]:
    """
    Returns:
      amount_uzs, date_iso, raw_text
    """
    client = vision.ImageAnnotatorClient()

    with open(image_path, "rb") as f:
        content = f.read()

    image = vision.Image(content=content)
    response = client.text_detection(image=image)
    texts = response.text_annotations

    if not texts:
        return None, None, ""

    full_text = texts[0].description

    # -------- summa ----------
    amount = None
    amounts = re.findall(r"\b\d{2,3}[ \.,]?\d{3}\b", full_text)
    if amounts:
        amount = int(re.sub(r"\D", "", amounts[0]))

    # -------- sana ----------
    date_iso = None
    try:
        dt = du_parser.parse(full_text, dayfirst=True, fuzzy=True)
        date_iso = dt.date().isoformat()
    except Exception:
        pass

    return amount, date_iso, full_text
