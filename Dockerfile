FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY munim ./munim
COPY web ./web
COPY scripts ./scripts

# Mount a volume at /data to keep the encrypted ledger across restarts.
VOLUME /data
EXPOSE 8000

# Secrets (GNANI_API_KEY, LLM_API_KEY, ...) are passed as environment
# variables at run time. Never bake them into the image.
CMD ["python", "-m", "munim"]
