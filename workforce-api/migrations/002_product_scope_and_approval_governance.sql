-- RunGrow Workforce product scope and approval governance.
-- Existing tenant policies remain mandatory; these restrictive policies add product scope.
-- app.* settings must be set transaction-locally by a trusted repository from verified identity claims.

ALTER TABLE workforce_employees ADD COLUMN IF NOT EXISTS product_id text;
ALTER TABLE workforce_tasks ADD COLUMN IF NOT EXISTS product_id text;
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS product_id text;
ALTER TABLE workforce_memory ADD COLUMN IF NOT EXISTS product_id text;
ALTER TABLE approvals
  ADD COLUMN IF NOT EXISTS product_id text,
  ADD COLUMN IF NOT EXISTS employee_id text,
  ADD COLUMN IF NOT EXISTS action_type text,
  ADD COLUMN IF NOT EXISTS payload_hash text,
  ADD COLUMN IF NOT EXISTS expires_at timestamptz,
  ADD COLUMN IF NOT EXISTS approver_id uuid REFERENCES users(id);
ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS product_id text;
ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS product_id text;

ALTER TABLE approvals ADD CONSTRAINT approvals_governance_binding_ck
  CHECK (employee_id IS NOT NULL AND action_type IS NOT NULL
    AND payload_hash IS NOT NULL AND length(payload_hash) = 64
    AND payload_hash ~ '^[0-9a-f]+$' AND expires_at IS NOT NULL) NOT VALID;
ALTER TABLE approvals ADD CONSTRAINT approvals_separation_of_duties_ck
  CHECK (approver_id::text IS DISTINCT FROM requested_by::text) NOT VALID;
ALTER TABLE approvals ADD CONSTRAINT approvals_decision_actor_ck
  CHECK ((status = 'pending' AND approver_id IS NULL)
    OR (status IN ('approved', 'rejected') AND approver_id IS NOT NULL)) NOT VALID;

CREATE INDEX IF NOT EXISTS employees_tenant_product_role ON workforce_employees(tenant_id, product_id, role_id);
CREATE INDEX IF NOT EXISTS tasks_tenant_product_status ON workforce_tasks(tenant_id, product_id, status);
CREATE INDEX IF NOT EXISTS runs_tenant_product_status ON agent_runs(tenant_id, product_id, status);
CREATE INDEX IF NOT EXISTS memory_tenant_product_scope ON workforce_memory(tenant_id, product_id, department, role_id);
CREATE INDEX IF NOT EXISTS approvals_tenant_product_status ON approvals(tenant_id, product_id, status, expires_at);
CREATE INDEX IF NOT EXISTS audit_tenant_product_time ON audit_events(tenant_id, product_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS outbox_tenant_product_status ON outbox_events(tenant_id, product_id, status, available_at);

DO $$
DECLARE table_name text;
BEGIN
  FOREACH table_name IN ARRAY ARRAY['workforce_employees','workforce_tasks','agent_runs','workforce_memory','approvals','audit_events','outbox_events'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
    EXECUTE format('DROP POLICY IF EXISTS product_scope_guard ON %I', table_name);
    EXECUTE format(
      'CREATE POLICY product_scope_guard ON %I AS RESTRICTIVE FOR ALL
       USING (product_id IS NULL
         OR COALESCE(current_setting(''app.is_company_admin'', true), ''false'') = ''true''
         OR product_id = ANY(string_to_array(COALESCE(current_setting(''app.product_ids'', true), ''''), '','')))
       WITH CHECK (product_id IS NULL
         OR COALESCE(current_setting(''app.is_company_admin'', true), ''false'') = ''true''
         OR product_id = ANY(string_to_array(COALESCE(current_setting(''app.product_ids'', true), ''''), '','')))
      ', table_name);
  END LOOP;
END
$$;
