-- Read-only post-migration verification for
-- database/migrations/010_user_collection_subscriptions.sql.
--
-- Run after the normal migration runner against a disposable/staging database:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f database/tests/010_user_collection_subscriptions_verify.sql
--
-- The script writes no application rows.  It verifies the schema plus the
-- legacy-source/request backfill invariant.  It permits an unowned legacy
-- source only when no existing app_users.username='psyche' account was
-- available for the one-time claim.

BEGIN READ ONLY;

DO $$
BEGIN
  IF to_regclass('public.collection_subscriptions') IS NULL THEN
    RAISE EXCEPTION 'collection_subscriptions table is missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint constraint_row
    WHERE constraint_row.conrelid = 'public.collection_subscriptions'::regclass
      AND constraint_row.contype = 'u'
      AND constraint_row.conkey = ARRAY[
        (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.collection_subscriptions'::regclass AND attname = 'user_id' AND NOT attisdropped),
        (SELECT attnum FROM pg_attribute WHERE attrelid = 'public.collection_subscriptions'::regclass AND attname = 'target_id' AND NOT attisdropped)
      ]::smallint[]
  ) THEN
    RAISE EXCEPTION 'collection_subscriptions must enforce unique (user_id, target_id)';
  END IF;

  IF to_regclass('public.collection_subscriptions_user_created_idx') IS NULL
     OR to_regclass('public.collection_subscriptions_target_idx') IS NULL
     OR to_regclass('public.collection_subscriptions_enabled_target_idx') IS NULL
     OR to_regclass('public.collection_requests_user_created_idx') IS NULL
     OR to_regclass('public.collection_requests_subscription_created_idx') IS NULL THEN
    RAISE EXCEPTION 'one or more subscription/request lookup indexes are missing';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'collection_requests'
      AND column_name = 'user_id'
  ) OR NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'collection_requests'
      AND column_name = 'subscription_id'
  ) THEN
    RAISE EXCEPTION 'collection_requests user/subscription columns are missing';
  END IF;

  IF to_regclass('public.collection_requests_target_idempotency_key_idx') IS NOT NULL THEN
    RAISE EXCEPTION 'legacy target-global collection request idempotency index still exists';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_index index_row
    JOIN pg_class index_class ON index_class.oid = index_row.indexrelid
    WHERE index_row.indrelid = 'public.collection_requests'::regclass
      AND index_class.relname = 'collection_requests_user_target_idempotency_key_idx'
      AND index_row.indisunique
  ) THEN
    RAISE EXCEPTION 'user-scoped collection request idempotency index is missing';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM collection_requests request
    WHERE request.user_id IS NOT NULL
      AND request.idempotency_key IS NOT NULL
    GROUP BY request.user_id, request.target_id, request.idempotency_key
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'duplicate user/target/idempotency request rows exist';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM collection_sources source
    LEFT JOIN collection_subscriptions subscription
      ON subscription.user_id = source.owner_id
     AND subscription.target_id = source.target_id
    WHERE source.owner_id IS NOT NULL
      AND source.target_id IS NOT NULL
      AND subscription.id IS NULL
  ) THEN
    RAISE EXCEPTION 'owned legacy source is missing a subscription';
  END IF;

  IF EXISTS (SELECT 1 FROM app_users WHERE username = 'psyche')
     AND EXISTS (
       SELECT 1
       FROM collection_sources source
       WHERE source.owner_id IS NULL
         AND source.target_id IS NOT NULL
     ) THEN
    RAISE EXCEPTION 'psyche legacy claim left a target-backed source unowned';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM collection_requests request
    JOIN collection_sources source ON source.id = request.source_id
    JOIN collection_subscriptions subscription
      ON subscription.user_id = source.owner_id
     AND subscription.target_id = source.target_id
    WHERE source.owner_id IS NOT NULL
      AND source.target_id IS NOT NULL
      AND (
        request.user_id IS DISTINCT FROM subscription.user_id
        OR request.subscription_id IS DISTINCT FROM subscription.id
      )
  ) THEN
    RAISE EXCEPTION 'legacy request ownership/subscription backfill is inconsistent';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM collection_requests request
    JOIN collection_targets target ON target.id = request.target_id
    JOIN collection_subscriptions subscription
      ON subscription.user_id = target.owner_id
     AND subscription.target_id = target.id
    WHERE request.source_id IS NULL
      AND (
        request.user_id IS DISTINCT FROM subscription.user_id
        OR request.subscription_id IS DISTINCT FROM subscription.id
      )
  ) THEN
    RAISE EXCEPTION 'target-owned legacy request ownership/subscription backfill is inconsistent';
  END IF;
END;
$$;

ROLLBACK;
