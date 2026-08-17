#!/usr/bin/env bash

set -e

# ---- Load .env ----
if [ -f .env ]; then
  set -a
  source .env
  set +a
else
  echo ".env file not found"
  exit 1
fi

# ---- Config ----
CONTAINER_NAME="kodi-postgres"
DB_PORT="54329"

# ---- Create or start PostgreSQL container ----
if [ "$(docker ps -aq -f name=$CONTAINER_NAME)" ]; then
  echo "Container exists"

  if [ "$(docker ps -q -f name=$CONTAINER_NAME)" ]; then
    echo "Container already running"
  else
    echo "Starting existing container..."
    docker start $CONTAINER_NAME
  fi
else
  echo "Creating new PostgreSQL container..."
  docker run -d \
    --name $CONTAINER_NAME \
    -e POSTGRES_USER="$DB_USER" \
    -e POSTGRES_PASSWORD="$DB_PASSWORD" \
    -e POSTGRES_DB=kodi_dev \
    -p $DB_PORT:5432 \
    postgres:15-alpine
fi

# ---- Wait for DB to be ready ----
echo "Waiting for PostgreSQL to be ready..."
sleep 3

# ---- Set DATABASE_URL ----
export DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@localhost:${DB_PORT}/kodi_dev"

# ---- Run migrations ----
#echo "Running migrations..."
#python shared/validation/db_migrate.py

# ---- Start services ----
echo "Starting services..."

uvicorn services.gateway.app.main:app --reload --log-level debug --port 8000 &
uvicorn services.auth.app.main:app --reload --log-level debug --port 8001 &
uvicorn services.orchestration.app.main:app --reload --log-level debug --port 8002 &
uvicorn services.document_ai.app.main:app --reload --log-level debug --port 8003 &
uvicorn services.tax_core.app.main:app --reload --log-level debug --port 8004 &
uvicorn services.forms.app.main:app --reload --log-level debug --port 8005 &
uvicorn services.reports.app.main:app --reload --log-level debug --port 8006 &
uvicorn services.knowledge.app.main:app --reload --log-level debug --port 8007 &

echo "All services started."

# ---- Keep script running ----
wait
