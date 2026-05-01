FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wins/ wins/

ENV PYTHONPATH=/app

# Ingestion runs as a library imported by brain — no standalone entrypoint needed.
# This container is kept for future standalone ingestion scheduling if required.
CMD ["python", "-c", "import asyncio; from wins.ingestion.collector import collect_signal_bundles; asyncio.run(collect_signal_bundles())"]
