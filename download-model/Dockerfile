FROM python:3.12-slim

ARG APP_VERSION=0.1.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY entrypoint.py .

RUN useradd --create-home --uid 1000 stepuser && \
    chown -R stepuser:stepuser /app

USER stepuser

ENTRYPOINT ["python", "entrypoint.py"]
