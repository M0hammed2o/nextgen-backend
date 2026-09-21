#!/bin/sh
set -e

# ARQ background worker — process_whatsapp_message + all cron jobs
# (outbox delivery, delivery-fee timeout, payment timeout, billing
# suspension sweep, worker heartbeat). See backend/app/worker.py.
#
# Deliberately does NOT run migrations — the API service (start.sh) already
# runs `alembic upgrade head` on boot; running it again here would race
# against that if both services deploy at the same time.
echo "==> Starting NextGen ARQ worker..."
exec arq backend.app.worker.WorkerSettings
