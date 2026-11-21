# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------
# MediFlow AI backend - production image
#
# Build:  docker build -t mediflow-backend .
# Run:    docker run -p 8000:8000 --env-file backend/.env mediflow-backend
#
# For local full-stack (backend + Postgres), use docker-compose.yml
# instead: `docker compose up --build`
# ---------------------------------------------------------------------

FROM python:3.11-slim AS base

# Prevent Python from buffering stdout/stderr (needed for real-time logs)
# and from writing .pyc files into the image layer.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build psycopg2 / bcrypt wheels on slim images.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt

# --- Application code -------------------------------------------------
COPY backend/ /app/backend/
COPY hospitals.csv doctors.csv doctor_hospital.csv appointments_seed.csv /app/

# --- Non-root user ------------------------------------------------------
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/api/health/ready || exit 1

# Apply database migrations (creates the schema on first run, applies
# any new ones on subsequent deploys - see backend/alembic/README.md),
# seed the reference dataset if empty, then start the API. gunicorn
# manages uvicorn workers for real concurrency and process supervision
# in production; set WEB_CONCURRENCY to tune worker count (defaults to
# 2, good for small/medium instances - increase for larger hosts).
CMD sh -c "alembic upgrade head && \
    python seed.py && \
    gunicorn main:app \
      --workers ${WEB_CONCURRENCY:-2} \
      --worker-class uvicorn.workers.UvicornWorker \
      --bind 0.0.0.0:${PORT:-8000} \
      --access-logfile - \
      --error-logfile -"
