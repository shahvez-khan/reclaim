#!/bin/sh
# Container entrypoint: bootstraps the DB + trained model on first run (both
# are gitignored / not baked into the image), then starts the API server.
# Re-running the container with an existing volume skips regeneration —
# this is a demo convenience, not a migration strategy; use
# `docker compose exec backend python migrate.py` for real schema changes
# against an existing DB.
set -e

cd /app/backend

if [ ! -f /app/data/revenue_recovery.db ]; then
  echo "[entrypoint] No existing DB found — bootstrapping fresh data..."
  python migrate.py
  python generate_data.py
fi

if [ ! -f /app/models/recovery_model.pkl ]; then
  echo "[entrypoint] No trained model found — training..."
  cd /app
  python ml/generate_training_data.py
  python ml/train_recovery_model.py
  cd /app/backend
fi

echo "[entrypoint] Starting API on ${API_HOST:-0.0.0.0}:${API_PORT:-8000}"
exec uvicorn api:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-8000}"
