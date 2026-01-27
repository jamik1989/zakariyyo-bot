FROM python:3.11-slim

# System deps (OCR uchun)
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Project files
COPY . .

ENV PYTHONUNBUFFERED=1
ENV TESS_LANG=rus+eng

CMD ["python", "-m", "app.main"]
