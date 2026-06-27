# Small, non-root runtime image for the papertrail bridge.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAPERTRAIL_DB=/data/papertrail.db \
    PAPERTRAIL_SEED_FILE=/data/seed.json \
    PAPERTRAIL_FIRMWARE_DIR=/app/firmware

WORKDIR /app

# Install deps first for layer caching.
COPY server/requirements.txt /app/server/requirements.txt
RUN pip install --no-cache-dir -r /app/server/requirements.txt

# App code.
COPY server/ /app/server/

# Bundled Pico firmware, hashed at startup so it can be pull/delta OTA'd to
# devices (PAPERTRAIL_FIRMWARE_DIR=/app/firmware). The manifest excludes
# device-local config.py / secrets* and only serves manifest keys.
COPY firmware/ /app/firmware/

# Non-root user; /data is the writable sqlite volume mount.
RUN useradd --create-home --uid 10001 papertrail \
    && mkdir -p /data \
    && chown -R papertrail:papertrail /data /app
USER papertrail

EXPOSE 8000

# One worker: the in-memory rate-limit bucket is per-process (see auth.py).
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
