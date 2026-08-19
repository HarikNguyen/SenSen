FROM python:3.11-slim

WORKDIR /app

# tesseract-ocr + Vietnamese language data for OCR fallback on scanned PDFs
# (app/extract.py) — the only system dep; PyMuPDF/spaCy still ship pure wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-vie \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_sm

COPY app/ ./app/
COPY static/ ./static/

ENV PORT=8000
EXPOSE 8000

# Single worker (model loaded once, singleton); scale via replicas, not uvicorn workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
