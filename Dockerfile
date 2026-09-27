FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY frontend ./frontend
COPY tests ./tests

# Bake the sample database into the image; conversation checkpoints go to a volume.
RUN python -m data.seed --force
ENV CHECKPOINT_DB_PATH=/app/state/checkpoints.db
RUN useradd --create-home appuser && mkdir -p /app/state && chown -R appuser /app/state
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
