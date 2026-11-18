-- Durable Workforce operational persistence. Product records are references to
-- the company portfolio, not copies of product CRM/booking/billing data.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS wf_product_registry (
  product_id text PRIMARY KEY,
  display_name text NOT NULL,
  is_provisional boolean NOT NULL DEFAULT true,
  baseline_status text NOT NULL DEFAULT 'unreconciled'
    CHECK (baseline_status IN ('unreconciled','reconciled')),
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO wf_product_registry(product_id,display_name) VALUES
 ('photo-studio-growth-os','Photo Studio Growth OS / Growth Pro'),
 ('photo-studio-billing','Photo Studio Billing Software'),
 ('clinics-counter','Clinics / Counter Software'),
 ('entrepreneur-instagram-tools','Entrepreneur / Instagram Business Tools'),
 ('pharma-software-utilities','Pharma Software & Utilities'),
 ('artists-business-tools','Artists Business Tools'),
 ('growtoys','Growtoys')
ON CONFLICT(product_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS wf_tasks (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 department text NOT NULL, assignee_role_id text NOT NULL, title text NOT NULL,
 description text NOT NULL DEFAULT '', priority text NOT NULL CHECK(priority IN ('low','normal','high','urgent')),
 status text NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
 created_by text NOT NULL, idempotency_key text NOT NULL, created_at timestamptz NOT NULL,
 updated_at timestamptz, UNIQUE(tenant_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS wf_agent_runs (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 employee_id text NOT NULL, requested_by text NOT NULL, task text NOT NULL,
 status text NOT NULL, summary text NOT NULL, steps jsonb NOT NULL DEFAULT '[]',
 correlation_id text NOT NULL, created_at timestamptz NOT NULL, updated_at timestamptz
);
CREATE TABLE IF NOT EXISTS wf_memory (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 role_id text NOT NULL, department text NOT NULL, content text NOT NULL, source_ref text NOT NULL,
 retention_days integer NOT NULL CHECK(retention_days BETWEEN 1 AND 3650),
 embedding vector(1536), created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_approvals (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 employee_id text NOT NULL, action_type text NOT NULL, payload jsonb NOT NULL,
 payload_hash text NOT NULL CHECK(payload_hash ~ '^[0-9a-f]{64}$'),
 requester_id text NOT NULL, approver_id text, status text NOT NULL CHECK(status IN ('pending','approved','rejected')),
 expires_at timestamptz NOT NULL, decision_reason text, decided_at timestamptz, created_at timestamptz NOT NULL,
 CHECK(approver_id IS DISTINCT FROM requester_id),
 CHECK((status='pending' AND approver_id IS NULL AND decided_at IS NULL)
    OR (status IN ('approved','rejected') AND approver_id IS NOT NULL AND decided_at IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS wf_audit_events (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 actor_id text NOT NULL, action text NOT NULL, resource_type text NOT NULL, resource_id text NOT NULL,
 details jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS wf_tasks_tenant_product_created ON wf_tasks(tenant_id,product_id,created_at DESC);
CREATE INDEX IF NOT EXISTS wf_runs_tenant_product_created ON wf_agent_runs(tenant_id,product_id,created_at DESC);
CREATE INDEX IF NOT EXISTS wf_memory_tenant_product_created ON wf_memory(tenant_id,product_id,created_at DESC);
CREATE INDEX IF NOT EXISTS wf_approvals_tenant_product_status ON wf_approvals(tenant_id,product_id,status,expires_at);
CREATE INDEX IF NOT EXISTS wf_audit_tenant_product_time ON wf_audit_events(tenant_id,product_id,occurred_at DESC);

CREATE OR REPLACE FUNCTION wf_reject_audit_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'workforce audit events are append-only'; END;
$$;
DROP TRIGGER IF EXISTS wf_audit_immutable ON wf_audit_events;
CREATE TRIGGER wf_audit_immutable BEFORE UPDATE OR DELETE ON wf_audit_events
FOR EACH ROW EXECUTE FUNCTION wf_reject_audit_mutation();

DO $$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['wf_tasks','wf_agent_runs','wf_memory','wf_approvals','wf_audit_events'] LOOP
   EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('DROP POLICY IF EXISTS wf_tenant_product_scope ON %I',t);
   EXECUTE format(
    'CREATE POLICY wf_tenant_product_scope ON %I FOR ALL
     USING (tenant_id = current_setting(''app.current_tenant_id'',true)
       AND (product_id IS NULL
         OR COALESCE(current_setting(''app.is_company_admin'',true),''false'') = ''true''
         OR product_id = ANY(string_to_array(COALESCE(current_setting(''app.product_ids'',true),''''),'',''))))
     WITH CHECK (tenant_id = current_setting(''app.current_tenant_id'',true)
       AND (product_id IS NULL
         OR COALESCE(current_setting(''app.is_company_admin'',true),''false'') = ''true''
         OR product_id = ANY(string_to_array(COALESCE(current_setting(''app.product_ids'',true),''''),'',''))))',
    t);
 END LOOP;
END $$;

ALTER TABLE wf_product_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_product_registry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_registry_read ON wf_product_registry;
CREATE POLICY wf_registry_read ON wf_product_registry FOR SELECT USING (true);
