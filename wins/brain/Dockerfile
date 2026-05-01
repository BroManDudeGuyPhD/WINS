FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wins/ wins/

ENV PYTHONPATH=/app

HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os,time; assert time.time()-os.path.getmtime('/tmp/heartbeat')<120"

CMD ["python", "-m", "wins.brain.main"]
