#!/bin/sh
set -e

echo "==> Running database migrations..."
alembic upgrade head
echo "==> Migrations complete. Starting server..."

# --proxy-headers: trust X-Forwarded-For from the platform proxy (Render) so
# request.client.host is the REAL client IP — required for correct rate
# limiting and login audit trails. FORWARDED_ALLOW_IPS defaults to "*"
# because the platform proxy strips client-supplied forwarded headers.
exec uvicorn backend.app.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-10000}" \
    --workers "${WEB_CONCURRENCY:-1}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
