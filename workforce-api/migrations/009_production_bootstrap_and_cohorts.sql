-- Production bootstrap identity, product bindings and provisional mission baseline.
-- Runtime bootstrap is repeatable and writes only explicit registration/planning state.
CREATE TABLE IF NOT EXISTS wf_tenants (
  tenant_id text PRIMARY KEY,
  slug text NOT NULL UNIQUE,
  name text NOT NULL,
  status text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wf_product_adapter_bindings (
  tenant_id text NOT NULL REFERENCES wf_tenants(tenant_id),
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  adapter_version text NOT NULL,
  integration_status text NOT NULL CHECK (integration_status IN ('LIVE_BACKEND_WIRED','DISCOVERY_ONLY')),
  source_repositories jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(source_repositories)='array'),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,product_id)
);
CREATE INDEX IF NOT EXISTS wf_product_bindings_status_idx
  ON wf_product_adapter_bindings(tenant_id,integration_status,product_id);

CREATE TABLE IF NOT EXISTS wf_mission_baselines (
  tenant_id text PRIMARY KEY REFERENCES wf_tenants(tenant_id),
  currency char(3) NOT NULL DEFAULT 'INR' CHECK (currency='INR'),
  revenue_target_inr numeric(16,2) NOT NULL CHECK (revenue_target_inr >= 0),
  capital_target_inr numeric(16,2) NOT NULL CHECK (capital_target_inr >= 0),
  recognised_revenue_inr numeric(16,2) NOT NULL DEFAULT 0 CHECK (recognised_revenue_inr >= 0),
  raised_capital_inr numeric(16,2) NOT NULL DEFAULT 0 CHECK (raised_capital_inr >= 0),
  is_provisional boolean NOT NULL DEFAULT true,
  allocation_type text NOT NULL DEFAULT 'planning_allocation_not_forecast',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS wf_mission_product_allocations (
  tenant_id text NOT NULL REFERENCES wf_tenants(tenant_id),
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  revenue_target_inr numeric(16,2) NOT NULL CHECK (revenue_target_inr >= 0),
  capital_target_inr numeric(16,2) NOT NULL CHECK (capital_target_inr >= 0),
  is_provisional boolean NOT NULL DEFAULT true,
  allocation_type text NOT NULL DEFAULT 'planning_allocation_not_forecast',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id,product_id)
);
