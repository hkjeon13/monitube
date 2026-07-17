-- User-specific subscriptions to canonical, shared collection targets.
--
-- This is an expand-only migration.  collection_sources remains the
-- worker/legacy audit table and its owner_id is deliberately retained while
-- reads and writes move to collection_subscriptions.  Public channel/video/
-- comment rows and the existing target/job history are never copied or
-- deleted here.

CREATE TABLE IF NOT EXISTS collection_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  target_id UUID NOT NULL REFERENCES collection_targets(id) ON DELETE CASCADE,
  -- User-facing input presentation and per-user UI choices only.  Collection
  -- coverage continues to live on the shared canonical target/request.
  display_config JSONB NOT NULL DEFAULT '{}',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT collection_subscriptions_user_target_key UNIQUE (user_id, target_id)
);

-- A request is an audit record for a particular user subscription even when
-- the canonical target/job and its public results are shared by many users.
ALTER TABLE collection_requests
  ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS subscription_id UUID REFERENCES collection_subscriptions(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS collection_subscriptions_user_created_idx
  ON collection_subscriptions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS collection_subscriptions_target_idx
  ON collection_subscriptions (target_id);
CREATE INDEX IF NOT EXISTS collection_subscriptions_enabled_target_idx
  ON collection_subscriptions (target_id)
  WHERE enabled;
CREATE INDEX IF NOT EXISTS collection_requests_user_created_idx
  ON collection_requests (user_id, created_at DESC)
  WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS collection_requests_subscription_created_idx
  ON collection_requests (subscription_id, created_at DESC)
  WHERE subscription_id IS NOT NULL;

-- Existing collected data belongs to the established ``psyche`` account when
-- it exists.  Do not create or guess an account here, and do not overwrite an
-- owner already assigned to another account.  A target is claimed only when it
-- is backed by a source owned by psyche, so orphaned targets remain unclaimed.
UPDATE collection_sources source
SET owner_id = psyche.id
FROM app_users psyche
WHERE psyche.username = 'psyche'
  AND source.owner_id IS NULL
  AND source.target_id IS NOT NULL;

UPDATE collection_targets target
SET owner_id = psyche.id
FROM app_users psyche
WHERE psyche.username = 'psyche'
  AND target.owner_id IS NULL
  AND EXISTS (
    SELECT 1
    FROM collection_sources source
    WHERE source.target_id = target.id
      AND source.owner_id = psyche.id
  );

-- Multiple legacy source rows may already point to the same canonical target
-- for one user.  They become one subscription.  The earliest source preserves
-- the display input, while an enabled legacy row keeps the resulting
-- subscription enabled.  Existing subscription rows, if any, win so a prior
-- user display preference is never overwritten by this backfill.
WITH ranked_legacy_sources AS (
  SELECT
    source.owner_id AS user_id,
    source.target_id,
    source.id AS source_id,
    source.config,
    source.created_at,
    source.updated_at,
    row_number() OVER (
      PARTITION BY source.owner_id, source.target_id
      ORDER BY source.created_at ASC, source.id ASC
    ) AS subscription_rank,
    bool_or(source.enabled) OVER (
      PARTITION BY source.owner_id, source.target_id
    ) AS any_enabled,
    min(source.created_at) OVER (
      PARTITION BY source.owner_id, source.target_id
    ) AS subscription_created_at,
    max(source.updated_at) OVER (
      PARTITION BY source.owner_id, source.target_id
    ) AS subscription_updated_at
  FROM collection_sources source
  WHERE source.owner_id IS NOT NULL
    AND source.target_id IS NOT NULL
)
INSERT INTO collection_subscriptions (
  user_id,
  target_id,
  display_config,
  enabled,
  created_at,
  updated_at
)
SELECT
  legacy.user_id,
  legacy.target_id,
  jsonb_strip_nulls(
    jsonb_build_object(
      'input', NULLIF(btrim(COALESCE(legacy.config ->> 'input', '')), ''),
      'query', NULLIF(btrim(COALESCE(legacy.config ->> 'query', '')), '')
    )
  ),
  legacy.any_enabled,
  legacy.subscription_created_at,
  legacy.subscription_updated_at
FROM ranked_legacy_sources legacy
WHERE legacy.subscription_rank = 1
ON CONFLICT (user_id, target_id) DO NOTHING;

-- Every legacy request with an owned source now records the user and the
-- subscription created above.  This source mapping is authoritative for legacy
-- request rows, and rerunning the statement converges any previously partial
-- transition without creating a second request or subscription.
UPDATE collection_requests request
SET user_id = subscription.user_id,
    subscription_id = subscription.id
FROM collection_sources source
JOIN collection_subscriptions subscription
  ON subscription.user_id = source.owner_id
 AND subscription.target_id = source.target_id
WHERE request.source_id = source.id
  AND (
    request.user_id IS DISTINCT FROM subscription.user_id
    OR request.subscription_id IS DISTINCT FROM subscription.id
  );

-- A legacy request can omit source_id after it joined an existing shared
-- target.  Before subscription-aware writes existed, that target's owner was
-- the only durable requester identity, so use it as the non-destructive
-- fallback for these audit rows.
UPDATE collection_requests request
SET user_id = subscription.user_id,
    subscription_id = subscription.id
FROM collection_targets target
JOIN collection_subscriptions subscription
  ON subscription.user_id = target.owner_id
 AND subscription.target_id = target.id
WHERE request.source_id IS NULL
  AND request.target_id = target.id
  AND (
    request.user_id IS DISTINCT FROM subscription.user_id
    OR request.subscription_id IS DISTINCT FROM subscription.id
  );

-- The legacy target-global idempotency index would make one user's retry key
-- collide with another user's request for the same shared target.  Preserve
-- the target-first coordinator semantics while scoping that key to its user.
-- The preflight produces a useful atomic failure if an unexpected legacy race
-- created two rows that cannot satisfy the new invariant.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM collection_requests request
    WHERE request.user_id IS NOT NULL
      AND request.idempotency_key IS NOT NULL
    GROUP BY request.user_id, request.target_id, request.idempotency_key
    HAVING count(*) > 1
  ) THEN
    RAISE EXCEPTION 'cannot scope collection request idempotency: duplicate user/target/key rows exist';
  END IF;
END;
$$;

DROP INDEX IF EXISTS collection_requests_target_idempotency_key_idx;
CREATE UNIQUE INDEX IF NOT EXISTS collection_requests_user_target_idempotency_key_idx
  ON collection_requests (user_id, target_id, idempotency_key)
  WHERE user_id IS NOT NULL
    AND idempotency_key IS NOT NULL;

-- Do not infer ownership for unowned legacy sources or targets: an account
-- claim is only made above for an existing psyche account.  Verify that all
-- rows with a known owner received a non-destructive subscription link.
DO $$
BEGIN
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
    RAISE EXCEPTION 'collection subscription backfill left an owned source without a subscription';
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
END;
$$;
