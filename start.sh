#!/bin/sh
set -e

echo "==> Running database migrations..."
alembic upgrade head
echo "==> Migrations complete. Starting server..."

exec uvicorn backend.app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}" \
    --workers "${WEB_CONCURRENCY:-1}"
