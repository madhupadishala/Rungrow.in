-- Priority 5: segregated investment capital ledger and immutable CEO briefing snapshots.
CREATE TABLE IF NOT EXISTS investment_capital_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  event_type text NOT NULL CHECK (event_type IN ('investor_lead','term_sheet_signed','commitment_approved','capital_received')),
  investor_ref text NOT NULL CHECK (length(trim(investor_ref)) BETWEEN 1 AND 240),
  amount_inr numeric(16,2) NOT NULL DEFAULT 0 CHECK (amount_inr >= 0),
  currency char(3) NOT NULL DEFAULT 'INR' CHECK (currency='INR'),
  is_received boolean NOT NULL DEFAULT false,
  approval_id uuid REFERENCES wf_approvals(id) ON DELETE RESTRICT,
  source_ref text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  CONSTRAINT investment_received_type CHECK (
    (event_type='capital_received' AND is_received=true AND amount_inr>0)
    OR (event_type<>'capital_received' AND is_received=false)
  ),
  CONSTRAINT investment_approval_required CHECK (
    event_type='investor_lead' OR approval_id IS NOT NULL
  )
);
CREATE INDEX IF NOT EXISTS investment_capital_tenant_time
 ON investment_capital_events(tenant_id,occurred_at DESC);
ALTER TABLE investment_capital_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE investment_capital_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investment_capital_tenant_scope ON investment_capital_events;
CREATE POLICY investment_capital_tenant_scope ON investment_capital_events
 FOR ALL USING (tenant_id=current_setting('app.current_tenant_id',true))
 WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));

CREATE OR REPLACE FUNCTION wf_validate_investment_approval() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE a wf_approvals%ROWTYPE;
BEGIN
 IF NEW.event_type <> 'investor_lead' THEN
   SELECT * INTO a FROM wf_approvals
    WHERE id=NEW.approval_id AND tenant_id=NEW.tenant_id
      AND lower(status)='approved' AND approver_id IS NOT NULL
      AND approver_id<>requester_id AND expires_at>NEW.recorded_at
      AND consumed_at IS NULL
    FOR UPDATE;
   IF NOT FOUND THEN RAISE EXCEPTION 'investment event requires a verified, unexpired, unused approval'; END IF;
   IF a.action_type NOT IN ('investment.commit','investor.commit','securities.commit')
      OR a.payload->>'event_type' IS DISTINCT FROM NEW.event_type
      OR a.payload->>'investor_ref' IS DISTINCT FROM NEW.investor_ref
      OR COALESCE((a.payload->>'amount_inr')::numeric,-1) <> NEW.amount_inr THEN
     RAISE EXCEPTION 'investment approval does not match event type, investor and amount';
   END IF;
   UPDATE wf_approvals SET consumed_at=NEW.recorded_at, execution_run_id=NEW.id
    WHERE id=NEW.approval_id AND tenant_id=NEW.tenant_id AND consumed_at IS NULL;
   IF NOT FOUND THEN RAISE EXCEPTION 'investment approval has already been consumed'; END IF;
 END IF;
 RETURN NEW;
END;
$fn$;
DROP TRIGGER IF EXISTS investment_capital_approval_guard ON investment_capital_events;
CREATE TRIGGER investment_capital_approval_guard
 BEFORE INSERT ON investment_capital_events
 FOR EACH ROW EXECUTE FUNCTION wf_validate_investment_approval();

CREATE OR REPLACE FUNCTION wf_reject_investment_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN RAISE EXCEPTION 'investment capital events are append-only'; END;
$fn$;
DROP TRIGGER IF EXISTS investment_capital_append_only ON investment_capital_events;
CREATE TRIGGER investment_capital_append_only
 BEFORE UPDATE OR DELETE ON investment_capital_events
 FOR EACH ROW EXECUTE FUNCTION wf_reject_investment_mutation();

CREATE TABLE IF NOT EXISTS ceo_briefings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id text NOT NULL,
 generated_at timestamptz NOT NULL,
 query_started_at timestamptz NOT NULL,
 query_completed_at timestamptz NOT NULL,
 briefing_version text NOT NULL DEFAULT '1.0',
 source_provenance jsonb NOT NULL,
 snapshot jsonb NOT NULL,
 created_by text NOT NULL,
 CHECK (query_completed_at >= query_started_at),
 CHECK (jsonb_typeof(source_provenance)='array'),
 CHECK (jsonb_typeof(snapshot)='object')
);
CREATE INDEX IF NOT EXISTS ceo_briefings_tenant_generated
 ON ceo_briefings(tenant_id,generated_at DESC);
ALTER TABLE ceo_briefings ENABLE ROW LEVEL SECURITY;
ALTER TABLE ceo_briefings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ceo_briefings_tenant_scope ON ceo_briefings;
CREATE POLICY ceo_briefings_tenant_scope ON ceo_briefings
 FOR ALL USING (tenant_id=current_setting('app.current_tenant_id',true))
 WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));
CREATE OR REPLACE FUNCTION wf_reject_ceo_briefing_mutation() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN RAISE EXCEPTION 'CEO briefing snapshots are immutable'; END;
$fn$;
DROP TRIGGER IF EXISTS ceo_briefings_immutable ON ceo_briefings;
CREATE TRIGGER ceo_briefings_immutable
 BEFORE UPDATE OR DELETE ON ceo_briefings
 FOR EACH ROW EXECUTE FUNCTION wf_reject_ceo_briefing_mutation();

-- Executive company-wide reporting is permitted only when the verified principal
-- carries the company-admin claim; tenant isolation remains mandatory.
DROP POLICY IF EXISTS adapter_idempotency_tenant_product_scope ON adapter_idempotency_keys;
CREATE POLICY adapter_idempotency_tenant_product_scope ON adapter_idempotency_keys
 FOR ALL USING (
   tenant_id=current_setting('app.current_tenant_id',true)
   AND (COALESCE(current_setting('app.is_company_admin',true),'false')='true'
     OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),',')))
 )
 WITH CHECK (
   tenant_id=current_setting('app.current_tenant_id',true)
   AND (COALESCE(current_setting('app.is_company_admin',true),'false')='true'
     OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),',')))
 );

DROP POLICY IF EXISTS product_telemetry_tenant_product_scope ON product_telemetry_events;
CREATE POLICY product_telemetry_tenant_product_scope ON product_telemetry_events
 FOR ALL USING (
   tenant_id=current_setting('app.current_tenant_id',true)
   AND (COALESCE(current_setting('app.is_company_admin',true),'false')='true'
     OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),',')))
 )
 WITH CHECK (
   tenant_id=current_setting('app.current_tenant_id',true)
   AND (COALESCE(current_setting('app.is_company_admin',true),'false')='true'
     OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),',')))
 );
