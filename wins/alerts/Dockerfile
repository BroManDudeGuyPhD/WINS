FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wins/ wins/

ENV PYTHONPATH=/app

CMD ["python", "-m", "wins.alerts.main"]
