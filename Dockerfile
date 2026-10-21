# one image for the central side: consumer, alert and ui
# (pick which one runs with the command in docker-compose.yml)
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py alert_config.json ./

# print logs straight away instead of buffering them
ENV PYTHONUNBUFFERED=1
CMD ["python", "consumer.py"]