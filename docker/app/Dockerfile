# Runtime image for LitRes Advanced Filters app
# Build from project root: docker build -f docker/app/Dockerfile -t litres-advanced-filters .
# Run with persistent mount: docker run -p 5000:5000 -v "$(pwd)/persistent:/app/persistent" litres-advanced-filters
#
# Security: app runs as non-root user (appuser, UID 10001). For the mounted
# persistent/ volume to be writable, ensure host dir is writable by that UID
# (e.g. on first use: chown -R 10001:10001 persistent).

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=app

WORKDIR /app

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

# Application code and mount point for persistent data
COPY app ./app
COPY templates ./templates
COPY static ./static
RUN mkdir -p /app/persistent && chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

# Production WSGI server (multi-worker); Flask dev server is single-threaded and not for production
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:5000", "app:create_app()"]
