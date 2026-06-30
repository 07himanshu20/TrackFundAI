# syntax=docker/dockerfile:1.6
# ---------------------------------------------------------------------------
# TrackFundAI backend image (Django 4.x, Python 3.10).
#
# Build context: the `backend/` directory.
#   docker build -t trackfundai-backend .
#
# Dev runs `manage.py runserver` (CMD below). For production, override the
# command to use gunicorn — see the docker-compose service comment.
# ---------------------------------------------------------------------------

FROM python:3.10-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ---------------------------------------------------------------------------
# System deps.
#   - libpq5: runtime for psycopg2-binary (binary wheels still dlopen libpq).
#   - build-essential + libpq-dev: only needed to compile any wheel that
#     lacks a manylinux build (we install then purge to keep the image slim).
#   - libjpeg/zlib: Pillow.
#   - curl: useful for healthchecks / debugging.
# ---------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libpq5 \
        libjpeg62-turbo \
        zlib1g \
        curl \
        ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching.
COPY requirements.txt /app/requirements.txt

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libjpeg-dev \
        zlib1g-dev \
 && pip install --upgrade pip \
 && pip install -r /app/requirements.txt \
 && pip install "gunicorn==22.0.0" \
 && apt-get purge -y --auto-remove build-essential libpq-dev libjpeg-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

# Copy source last so code changes don't bust the deps cache.
COPY . /app

EXPOSE 8000

# Default to the Django dev server (bind 0.0.0.0 so the host can reach it).
# Override in production with, e.g.:
#   command: ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
