# Runtime image for LitRes Advanced Filters app
# Build from project root: docker build -f docker/app/Dockerfile -t litres-advanced-filters .
# Run with persistent mount: docker run -p 5000:5000 -v "$(pwd)/persistent:/app/persistent" litres-advanced-filters
#
# Security: entrypoint runs as root only to chown the mounted persistent/
# volume, then drops to appuser via gosu. The app itself always runs as
# non-root (appuser).

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=app

WORKDIR /app

# gosu: lightweight tool to drop from root to appuser in entrypoint
# (avoids installing sudo; used only during container startup)
RUN apt-get update && apt-get install -y --no-install-recommends gosu cron \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies into system Python (virtualenvs.create false = use system Python)
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir poetry \
    && poetry config virtualenvs.create false \
    && poetry install --only main --no-interaction --no-root \
    && pip uninstall -y poetry

# Create non-root user (Docker best practice: never run as root)
# https://docs.docker.com/go/dockerfile-user-best-practices/
ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# Application code, migrations, and mount point for persistent data
COPY app ./app
COPY templates ./templates
COPY static ./static
COPY alembic.ini ./
COPY db ./db
COPY docker/app/entrypoint.sh docker/app/crontab ./
RUN mkdir -p /app/persistent && chown -R appuser:appuser /app

EXPOSE 5000

# Entrypoint runs as root to fix persistent/ ownership, then drops to appuser
ENTRYPOINT ["sh", "entrypoint.sh"]
# Production WSGI server (multi-worker); Flask dev server is single-threaded and not for production
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:create_app()"]
