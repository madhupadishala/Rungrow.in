-- Priority 4: durable adapter idempotency and product lifecycle/revenue telemetry.
CREATE TABLE IF NOT EXISTS adapter_idempotency_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  external_tenant_id text NOT NULL,
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  direction text NOT NULL CHECK (direction IN ('inbound','outbound')),
  idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
  payload_hash text NOT NULL CHECK (length(payload_hash)=64),
  status text NOT NULL DEFAULT 'PROCESSING' CHECK (status IN ('PROCESSING','COMPLETED')),
  response_json jsonb,
  correlation_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT adapter_idempotency_unique UNIQUE (tenant_id,external_tenant_id,product_id,direction,idempotency_key),
  CONSTRAINT adapter_idempotency_completion CHECK (
    (status='PROCESSING' AND completed_at IS NULL) OR
    (status='COMPLETED' AND completed_at IS NOT NULL AND response_json IS NOT NULL)
  )
);
CREATE INDEX IF NOT EXISTS adapter_idempotency_tenant_product_time
  ON adapter_idempotency_keys(tenant_id,external_tenant_id,product_id,created_at DESC);
ALTER TABLE adapter_idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE adapter_idempotency_keys FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS adapter_idempotency_tenant_product_scope ON adapter_idempotency_keys;
CREATE POLICY adapter_idempotency_tenant_product_scope ON adapter_idempotency_keys
  FOR ALL USING (
    tenant_id=current_setting('app.current_tenant_id',true)
    AND product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
  ) WITH CHECK (
    tenant_id=current_setting('app.current_tenant_id',true)
    AND product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
  );

CREATE TABLE IF NOT EXISTS product_telemetry_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  external_tenant_id text NOT NULL,
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  event_id text NOT NULL,
  event_type text NOT NULL CHECK (event_type IN (
    'studio_tenant_onboarded','trial_started','lead_converted','booking_confirmed',
    'invoice_recognised','churn_risk_flagged'
  )),
  recognised_revenue_inr numeric(14,2) NOT NULL DEFAULT 0 CHECK (recognised_revenue_inr >= 0),
  occurred_at timestamptz,
  source_ref text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  correlation_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT product_telemetry_event_unique UNIQUE (tenant_id,external_tenant_id,product_id,event_id),
  CONSTRAINT product_telemetry_revenue_event_check CHECK (
    (event_type='invoice_recognised') OR recognised_revenue_inr=0
  )
);
CREATE INDEX IF NOT EXISTS product_telemetry_tenant_product_time
  ON product_telemetry_events(tenant_id,external_tenant_id,product_id,received_at DESC);
CREATE INDEX IF NOT EXISTS product_telemetry_revenue_time
  ON product_telemetry_events(tenant_id,external_tenant_id,product_id,occurred_at DESC)
  WHERE event_type='invoice_recognised';
ALTER TABLE product_telemetry_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE product_telemetry_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS product_telemetry_tenant_product_scope ON product_telemetry_events;
CREATE POLICY product_telemetry_tenant_product_scope ON product_telemetry_events
  FOR ALL USING (
    tenant_id=current_setting('app.current_tenant_id',true)
    AND product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
  ) WITH CHECK (
    tenant_id=current_setting('app.current_tenant_id',true)
    AND product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
  );

-- Product telemetry is an append-only evidence ledger.
CREATE OR REPLACE FUNCTION wf_reject_product_telemetry_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'product telemetry events are immutable';
END;
$$;
DROP TRIGGER IF EXISTS product_telemetry_append_only ON product_telemetry_events;
CREATE TRIGGER product_telemetry_append_only
  BEFORE UPDATE OR DELETE ON product_telemetry_events
  FOR EACH ROW EXECUTE FUNCTION wf_reject_product_telemetry_mutation();

CREATE OR REPLACE FUNCTION wf_guard_adapter_idempotency_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
     OR NEW.external_tenant_id IS DISTINCT FROM OLD.external_tenant_id
     OR NEW.product_id IS DISTINCT FROM OLD.product_id
     OR NEW.direction IS DISTINCT FROM OLD.direction
     OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
     OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
     OR NEW.correlation_id IS DISTINCT FROM OLD.correlation_id THEN
    RAISE EXCEPTION 'adapter idempotency identity is immutable';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS adapter_idempotency_identity_immutable ON adapter_idempotency_keys;
CREATE TRIGGER adapter_idempotency_identity_immutable
  BEFORE UPDATE ON adapter_idempotency_keys
  FOR EACH ROW EXECUTE FUNCTION wf_guard_adapter_idempotency_identity();

CREATE OR REPLACE FUNCTION wf_guard_adapter_idempotency_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status='COMPLETED' THEN
    RAISE EXCEPTION 'completed adapter idempotency records are immutable';
  END IF;
  IF NEW.status <> 'COMPLETED' OR NEW.response_json IS NULL OR NEW.completed_at IS NULL THEN
    RAISE EXCEPTION 'adapter idempotency may only transition PROCESSING to COMPLETED';
  END IF;
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS adapter_idempotency_completion_guard ON adapter_idempotency_keys;
CREATE TRIGGER adapter_idempotency_completion_guard
  BEFORE UPDATE ON adapter_idempotency_keys
  FOR EACH ROW EXECUTE FUNCTION wf_guard_adapter_idempotency_transition();
