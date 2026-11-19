-- Release readiness evidence, worker liveness and shared sliding-window abuse protection.
CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id text PRIMARY KEY,
  tenant_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('STARTING','IDLE','BUSY','STOPPING','ERROR')),
  last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
  processed_count bigint NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
  dead_letter_count bigint NOT NULL DEFAULT 0 CHECK (dead_letter_count >= 0)
);
CREATE INDEX IF NOT EXISTS worker_heartbeats_tenant_seen_idx
  ON worker_heartbeats(tenant_id,last_heartbeat_at DESC);
ALTER TABLE worker_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE worker_heartbeats FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS worker_heartbeats_tenant_scope ON worker_heartbeats;
CREATE POLICY worker_heartbeats_tenant_scope ON worker_heartbeats FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));

CREATE TABLE IF NOT EXISTS release_gate_signoffs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  release_layer text NOT NULL CHECK (release_layer IN
    ('RELEASE_1_WORKFORCE','RELEASE_2_PRODUCTS','RELEASE_3_COMMERCIAL')),
  gate_name text NOT NULL CHECK (gate_name IN
    ('ENGINEERING','SECURITY','INTEGRATION','RELIABILITY','AI_GOVERNANCE','OPERATIONS','BUSINESS')),
  status text NOT NULL CHECK (status IN ('PASS','BLOCKED','DEFERRED_WITH_ACCEPTANCE')),
  evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object' AND evidence <> '{}'::jsonb),
  verifier_type text NOT NULL CHECK (verifier_type IN ('HUMAN','AUTOMATED')),
  verifier_id text NOT NULL CHECK (length(trim(verifier_id)) > 0),
  candidate_sha text NOT NULL CHECK (candidate_sha ~ '^[0-9a-fA-F]{40,64}
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS release_gate_signoffs_lookup_idx
  ON release_gate_signoffs(tenant_id,release_layer,gate_name,occurred_at DESC);
ALTER TABLE release_gate_signoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE release_gate_signoffs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS release_gate_signoffs_tenant_scope ON release_gate_signoffs;
CREATE POLICY release_gate_signoffs_tenant_scope ON release_gate_signoffs FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));

CREATE OR REPLACE FUNCTION wf_release_signoffs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'release_gate_signoffs is append-only; insert a superseding signoff';
END;
$fn$;
DROP TRIGGER IF EXISTS release_gate_signoffs_no_mutation ON release_gate_signoffs;
CREATE TRIGGER release_gate_signoffs_no_mutation
  BEFORE UPDATE OR DELETE ON release_gate_signoffs
  FOR EACH ROW EXECUTE FUNCTION wf_release_signoffs_append_only();
DROP TRIGGER IF EXISTS release_gate_signoffs_no_truncate ON release_gate_signoffs;
CREATE TRIGGER release_gate_signoffs_no_truncate
  BEFORE TRUNCATE ON release_gate_signoffs
  FOR EACH STATEMENT EXECUTE FUNCTION wf_release_signoffs_append_only();

-- Shared PostgreSQL event store makes rate limits consistent across API replicas.
CREATE TABLE IF NOT EXISTS rate_limit_events (
  id bigserial PRIMARY KEY,
  scope_key text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS rate_limit_events_scope_time_idx
  ON rate_limit_events(scope_key,occurred_at DESC);
CREATE INDEX IF NOT EXISTS rate_limit_events_occurred_idx
  ON rate_limit_events(occurred_at);

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='worker_heartbeats_processed_nonnegative') THEN
    ALTER TABLE worker_heartbeats ADD CONSTRAINT worker_heartbeats_processed_nonnegative
      CHECK (processed_count >= 0 AND dead_letter_count >= 0) NOT VALID;
  END IF;
END;
$do$;
ALTER TABLE worker_heartbeats VALIDATE CONSTRAINT worker_heartbeats_processed_nonnegative;
),
  environment text NOT NULL CHECK (length(trim(environment)) > 0),
  occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS release_gate_signoffs_lookup_idx
  ON release_gate_signoffs(tenant_id,release_layer,gate_name,occurred_at DESC);
ALTER TABLE release_gate_signoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE release_gate_signoffs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS release_gate_signoffs_tenant_scope ON release_gate_signoffs;
CREATE POLICY release_gate_signoffs_tenant_scope ON release_gate_signoffs FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));

CREATE OR REPLACE FUNCTION wf_release_signoffs_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  RAISE EXCEPTION 'release_gate_signoffs is append-only; insert a superseding signoff';
END;
$fn$;
DROP TRIGGER IF EXISTS release_gate_signoffs_no_mutation ON release_gate_signoffs;
CREATE TRIGGER release_gate_signoffs_no_mutation
  BEFORE UPDATE OR DELETE ON release_gate_signoffs
  FOR EACH ROW EXECUTE FUNCTION wf_release_signoffs_append_only();
DROP TRIGGER IF EXISTS release_gate_signoffs_no_truncate ON release_gate_signoffs;
CREATE TRIGGER release_gate_signoffs_no_truncate
  BEFORE TRUNCATE ON release_gate_signoffs
  FOR EACH STATEMENT EXECUTE FUNCTION wf_release_signoffs_append_only();

-- Shared PostgreSQL event store makes rate limits consistent across API replicas.
CREATE TABLE IF NOT EXISTS rate_limit_events (
  id bigserial PRIMARY KEY,
  scope_key text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS rate_limit_events_scope_time_idx
  ON rate_limit_events(scope_key,occurred_at DESC);
CREATE INDEX IF NOT EXISTS rate_limit_events_occurred_idx
  ON rate_limit_events(occurred_at);

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='worker_heartbeats_processed_nonnegative') THEN
    ALTER TABLE worker_heartbeats ADD CONSTRAINT worker_heartbeats_processed_nonnegative
      CHECK (processed_count >= 0 AND dead_letter_count >= 0) NOT VALID;
  END IF;
END;
$do$;
ALTER TABLE worker_heartbeats VALIDATE CONSTRAINT worker_heartbeats_processed_nonnegative;
