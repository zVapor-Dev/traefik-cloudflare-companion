FROM docker.io/library/python:3.15-alpine

ARG VERSION=dev

LABEL org.opencontainers.image.title="traefik-cloudflare-companion" \
      org.opencontainers.image.description="Automatically create Cloudflare DNS records for services behind Traefik" \
      org.opencontainers.image.source="https://github.com/zVapor-Dev/traefik-cloudflare-companion" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    IMAGE_VERSION=${VERSION}

COPY requirements.txt /tmp/requirements.txt

RUN apk add --no-cache su-exec tzdata && \
    addgroup -S -g 8080 tcc && \
    adduser -S -D -H -u 8080 -G tcc -s /sbin/nologin tcc && \
    mkdir -p /logs && chown tcc:tcc /logs && \
    pip install -r /tmp/requirements.txt && \
    rm -rf /tmp/requirements.txt /root/.cache

COPY app/cloudflare_companion.py /app/cloudflare_companion.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "/app/cloudflare_companion.py"]
