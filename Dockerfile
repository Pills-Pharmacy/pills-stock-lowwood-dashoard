FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIRMED_MATCHES_DIR=/data

WORKDIR /app

COPY requirements.lock .
RUN pip install -r requirements.lock

COPY . .
RUN mkdir -p /data

EXPOSE 8000
CMD ["streamlit", "run", "app.py", "--server.port=8000", "--server.address=0.0.0.0", "--server.headless=true"]
