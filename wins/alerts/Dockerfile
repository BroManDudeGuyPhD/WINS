FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wins/ wins/

ENV PYTHONPATH=/app

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,time; assert time.time()-os.path.getmtime('/tmp/heartbeat')<30"

CMD ["python", "-m", "wins.alerts.main"]
