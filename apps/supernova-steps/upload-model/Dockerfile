FROM python:3.12-slim

ARG APP_VERSION=0.1.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app

COPY upload-model/requirements.txt ./requirements.txt
COPY certs/damit-cloud-root-ca.crt /usr/local/share/ca-certificates/damit-cloud-root-ca.crt
RUN update-ca-certificates && \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY upload-model/entrypoint.py ./entrypoint.py

RUN useradd --create-home --uid 1000 stepuser && \
    chown -R stepuser:stepuser /app

USER stepuser

ENTRYPOINT ["python", "entrypoint.py"]
