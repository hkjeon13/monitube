#!/bin/sh
set -eu

: "${S3_ENDPOINT_URL:?S3_ENDPOINT_URL is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"

# MinIO can accept connections before it is ready to authenticate. Retry the
# alias registration without printing credentials, then create the bucket
# idempotently so `docker compose up` remains restart-safe.
attempt=0
until mc alias set local "$S3_ENDPOINT_URL" "$S3_ACCESS_KEY" "$S3_SECRET_KEY" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "MinIO did not become ready while bootstrapping the local bucket." >&2
    exit 1
  fi
  sleep 1
done

mc mb --ignore-existing "local/$S3_BUCKET" >/dev/null
echo "Local MinIO bucket is ready: $S3_BUCKET"
