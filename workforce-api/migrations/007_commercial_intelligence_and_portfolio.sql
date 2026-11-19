-- Priority 6: tenant/product-scoped commercial intelligence and governed campaigns.
CREATE TABLE IF NOT EXISTS prospect_records (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  dedupe_key text NOT NULL,
  business_name text NOT NULL CHECK (length(trim(business_name)) BETWEEN 2 AND 300),
  city text NOT NULL CHECK (length(trim(city)) BETWEEN 2 AND 160),
  segment text NOT NULL CHECK (length(trim(segment)) BETWEEN 2 AND 160),
  decision_maker_name text,
  public_contact_channel text,
  source_url text NOT NULL,
  evidence_urls jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_urls)='array'),
  evidence_snippet text NOT NULL DEFAULT '',
  discovered_at timestamptz NOT NULL DEFAULT now(),
  last_evidenced_at timestamptz NOT NULL DEFAULT now(),
  confidence_score numeric(4,3) NOT NULL DEFAULT 0.500 CHECK (confidence_score BETWEEN 0.000 AND 1.000),
  icp_fit_tier text NOT NULL DEFAULT 'UNASSESSED' CHECK (icp_fit_tier IN ('HIGH','MEDIUM','LOW','UNASSESSED')),
  stage text NOT NULL DEFAULT 'DISCOVERED' CHECK (stage IN ('DISCOVERED','QUALIFIED','HANDED_OFF','DISQUALIFIED')),
  is_synthetic boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT prospect_records_tenant_product_dedupe UNIQUE (tenant_id,product_id,dedupe_key)
);
CREATE INDEX IF NOT EXISTS prospect_records_tenant_product_stage_idx
  ON prospect_records(tenant_id,product_id,stage,confidence_score DESC,discovered_at DESC);
CREATE INDEX IF NOT EXISTS prospect_records_tenant_product_city_idx
  ON prospect_records(tenant_id,product_id,lower(city),lower(segment));
ALTER TABLE prospect_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE prospect_records FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS prospect_records_tenant_product_scope ON prospect_records;
CREATE POLICY prospect_records_tenant_product_scope ON prospect_records FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true)
    AND product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
    OR tenant_id=current_setting('app.current_tenant_id',true)
    AND COALESCE(current_setting('app.is_company_admin',true),'false')='true')
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true)
    AND (product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
      OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'));

CREATE TABLE IF NOT EXISTS campaign_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  name text NOT NULL CHECK (length(trim(name)) BETWEEN 3 AND 240),
  target_icp_segment text NOT NULL CHECK (length(trim(target_icp_segment)) BETWEEN 2 AND 240),
  channel text NOT NULL CHECK (channel IN ('EMAIL','WHATSAPP','PHONE','SOCIAL','IN_PRODUCT','PARTNER','RESEARCH_ONLY')),
  messaging_brief text NOT NULL CHECK (length(trim(messaging_brief)) BETWEEN 20 AND 12000),
  ai_identity_disclosure text NOT NULL CHECK (length(trim(ai_identity_disclosure)) >= 20),
  status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','AWAITING_APPROVAL','APPROVED','ACTIVE','PAUSED')),
  approval_id uuid REFERENCES wf_approvals(id),
  execution_run_id uuid REFERENCES wf_agent_runs(id),
  is_sandbox boolean NOT NULL DEFAULT true,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  activated_at timestamptz,
  CONSTRAINT campaign_plans_tenant_product_name_unique UNIQUE (tenant_id,product_id,name)
);
CREATE INDEX IF NOT EXISTS campaign_plans_tenant_product_status_idx
  ON campaign_plans(tenant_id,product_id,status,created_at DESC);
ALTER TABLE campaign_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE campaign_plans FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS campaign_plans_tenant_product_scope ON campaign_plans;
CREATE POLICY campaign_plans_tenant_product_scope ON campaign_plans FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true)
    AND (product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
      OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true)
    AND (product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
      OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'));

CREATE OR REPLACE FUNCTION wf_guard_campaign_approval() RETURNS trigger
LANGUAGE plpgsql AS $fn$
DECLARE
  approval_row wf_approvals%ROWTYPE;
BEGIN
  NEW.updated_at := now();
  IF OLD.status IN ('APPROVED','ACTIVE') AND
     (NEW.approval_id IS DISTINCT FROM OLD.approval_id OR
      NEW.execution_run_id IS DISTINCT FROM OLD.execution_run_id) THEN
    RAISE EXCEPTION 'campaign approval binding is immutable after approval';
  END IF;
  IF NEW.status NOT IN ('APPROVED','ACTIVE') THEN
    IF NEW.status='AWAITING_APPROVAL' AND OLD.status NOT IN ('DRAFT','PAUSED') THEN
      RAISE EXCEPTION 'campaign cannot request approval from current state %',OLD.status;
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.approval_id IS NULL THEN
    RAISE EXCEPTION 'campaign approval_id is required before APPROVED or ACTIVE';
  END IF;

  IF NEW.status='APPROVED' AND OLD.status<>'APPROVED' THEN
    SELECT * INTO approval_row FROM wf_approvals
      WHERE id=NEW.approval_id AND tenant_id=NEW.tenant_id
        AND product_id=NEW.product_id AND action_type='campaign.activate'
        AND status='APPROVED' AND approver_id IS NOT NULL
        AND approver_id<>requester_id AND expires_at>now()
        AND consumed_at IS NOT NULL
        AND execution_run_id=NEW.execution_run_id
        AND payload->>'campaign_id'=NEW.id::text
      FOR UPDATE;
    IF NOT FOUND OR NEW.execution_run_id IS NULL OR NOT EXISTS (
      SELECT 1 FROM wf_agent_runs r
      WHERE r.id=NEW.execution_run_id AND r.tenant_id=NEW.tenant_id
        AND r.product_id=NEW.product_id
    ) THEN
      RAISE EXCEPTION 'campaign requires a matching approval consumed by its authorised execution run';
    END IF;
    RETURN NEW;
  END IF;

  IF NEW.status='ACTIVE' THEN
    IF OLD.status<>'APPROVED' OR OLD.approval_id IS DISTINCT FROM NEW.approval_id THEN
      RAISE EXCEPTION 'campaign must be APPROVED with the same approval before activation';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM wf_approvals
      WHERE id=NEW.approval_id AND tenant_id=NEW.tenant_id
        AND product_id=NEW.product_id AND action_type='campaign.activate'
        AND status='APPROVED' AND consumed_at IS NOT NULL
        AND execution_run_id=NEW.execution_run_id
    ) THEN
      RAISE EXCEPTION 'campaign activation approval binding is invalid';
    END IF;
    NEW.activated_at := COALESCE(NEW.activated_at,now());
    RETURN NEW;
  END IF;
  RETURN NEW;
END;
$fn$;
DROP TRIGGER IF EXISTS campaign_plans_approval_guard ON campaign_plans;
CREATE TRIGGER campaign_plans_approval_guard
  BEFORE UPDATE OF status,approval_id ON campaign_plans
  FOR EACH ROW EXECUTE FUNCTION wf_guard_campaign_approval();

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='prospect_records_confidence_bounds') THEN
    ALTER TABLE prospect_records ADD CONSTRAINT prospect_records_confidence_bounds
      CHECK (confidence_score BETWEEN 0.000 AND 1.000) NOT VALID;
  END IF;
END;
$do$;
ALTER TABLE prospect_records VALIDATE CONSTRAINT prospect_records_confidence_bounds;
