FROM python:3.12-slim

ARG APP_VERSION=0.0.0-dev
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i "s/^version = .*/version = \"${APP_VERSION}\"/" pyproject.toml && \
    pip install --no-cache-dir -e .

RUN useradd --create-home --uid 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

USER appuser

ENV HOST=0.0.0.0
ENV PORT=8000

CMD ["sh", "-c", "uvicorn app.main:sio_asgi_app --host $HOST --port $PORT"]
