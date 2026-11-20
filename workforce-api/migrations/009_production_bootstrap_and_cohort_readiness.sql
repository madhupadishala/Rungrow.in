-- Production bootstrap primitives: tenant identity, product bindings, cohort evidence and provisional mission baselines.
ALTER TABLE release_gate_signoffs
  ADD COLUMN IF NOT EXISTS cohort_id text NOT NULL DEFAULT 'portfolio',
  ADD COLUMN IF NOT EXISTS scope_key text NOT NULL DEFAULT 'cohort';
ALTER TABLE release_gate_signoffs
  ADD CONSTRAINT release_gate_signoffs_cohort_nonempty
  CHECK (length(trim(cohort_id)) BETWEEN 1 AND 80) NOT VALID;
ALTER TABLE release_gate_signoffs
  ADD CONSTRAINT release_gate_signoffs_scope_nonempty
  CHECK (length(trim(scope_key)) BETWEEN 1 AND 160) NOT VALID;
ALTER TABLE release_gate_signoffs VALIDATE CONSTRAINT release_gate_signoffs_cohort_nonempty;
ALTER TABLE release_gate_signoffs VALIDATE CONSTRAINT release_gate_signoffs_scope_nonempty;
CREATE UNIQUE INDEX IF NOT EXISTS release_gate_signoffs_bootstrap_identity
  ON release_gate_signoffs(tenant_id,release_layer,gate_name,cohort_id,scope_key,candidate_sha);

CREATE TABLE IF NOT EXISTS wf_tenants (
  id text PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUSPENDED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE wf_tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_tenants_tenant_scope ON wf_tenants;
CREATE POLICY wf_tenants_tenant_scope ON wf_tenants FOR ALL
  USING (id=current_setting('app.current_tenant_id',true))
  WITH CHECK (id=current_setting('app.current_tenant_id',true));

CREATE TABLE IF NOT EXISTS wf_product_bindings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL REFERENCES wf_tenants(id),
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  adapter_repository text,
  adapter_branch text,
  integration_status text NOT NULL CHECK(integration_status IN ('LIVE_BACKEND_WIRED','NO_REPO_YET','DISCOVERY_ONLY')),
  governance_mode text NOT NULL CHECK(governance_mode IN ('LIVE','DEFERRED','DISCOVERY_ONLY')),
  is_active boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,product_id)
);
CREATE INDEX IF NOT EXISTS wf_product_bindings_tenant_status
  ON wf_product_bindings(tenant_id,integration_status,governance_mode);
ALTER TABLE wf_product_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_product_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_product_bindings_tenant_scope ON wf_product_bindings;
CREATE POLICY wf_product_bindings_tenant_scope ON wf_product_bindings FOR ALL
  USING(tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK(tenant_id=current_setting('app.current_tenant_id',true));

CREATE TABLE IF NOT EXISTS wf_financial_baselines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL REFERENCES wf_tenants(id),
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  currency char(3) NOT NULL DEFAULT 'INR' CHECK(currency='INR'),
  recognised_revenue_inr numeric(16,2) NOT NULL DEFAULT 0 CHECK(recognised_revenue_inr>=0),
  raised_capital_inr numeric(16,2) NOT NULL DEFAULT 0 CHECK(raised_capital_inr>=0),
  revenue_target_allocation_inr numeric(16,2) NOT NULL CHECK(revenue_target_allocation_inr>=0),
  capital_target_allocation_inr numeric(16,2) NOT NULL CHECK(capital_target_allocation_inr>=0),
  revenue_target_total_inr numeric(16,2) NOT NULL DEFAULT 50000000 CHECK(revenue_target_total_inr=50000000),
  capital_target_total_inr numeric(16,2) NOT NULL DEFAULT 50000000 CHECK(capital_target_total_inr=50000000),
  is_provisional boolean NOT NULL DEFAULT true,
  allocation_type text NOT NULL DEFAULT 'planning_allocation_not_forecast'
    CHECK(allocation_type='planning_allocation_not_forecast'),
  baseline_note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(tenant_id,product_id)
);
CREATE INDEX IF NOT EXISTS wf_financial_baselines_tenant_product
  ON wf_financial_baselines(tenant_id,product_id);
ALTER TABLE wf_financial_baselines ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_financial_baselines FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_financial_baselines_tenant_scope ON wf_financial_baselines;
CREATE POLICY wf_financial_baselines_tenant_scope ON wf_financial_baselines FOR ALL
  USING(tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK(tenant_id=current_setting('app.current_tenant_id',true));
