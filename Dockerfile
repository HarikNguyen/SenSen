FROM python:3.11-slim

WORKDIR /app

# System deps: none required — PyMuPDF ships its own MuPDF binary wheel,
# spaCy en_core_web_sm is pure-Python + numpy. Keep the image lean.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_sm

COPY app/ ./app/
COPY static/ ./static/

ENV PORT=8000
EXPOSE 8000

# Single worker: spaCy's model is loaded once per process (singleton in app/main.py).
# Scale horizontally via Azure Container Apps replicas instead of uvicorn workers
# to keep per-instance RAM predictable on the free consumption plan.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
