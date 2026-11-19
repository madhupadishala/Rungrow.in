-- Initial Workforce control-plane schema. Product-specific CRM, booking and billing
-- data remain in their existing product repositories.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS users (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id text NOT NULL,
 external_subject text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,external_subject)
);
CREATE TABLE IF NOT EXISTS workforce_employees (
 id text PRIMARY KEY, tenant_id text NOT NULL, role_id text NOT NULL,
 department text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS workforce_tasks (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 title text NOT NULL, description text NOT NULL DEFAULT '', assignee_role_id text NOT NULL,
 priority text NOT NULL DEFAULT 'normal', status text NOT NULL DEFAULT 'queued',
 created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 employee_id text NOT NULL, requested_by text NOT NULL, task text NOT NULL,
 status text NOT NULL, summary text NOT NULL DEFAULT '', steps jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz
);
CREATE TABLE IF NOT EXISTS workforce_memory (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 role_id text NOT NULL, department text NOT NULL, content text NOT NULL,
 source_ref text NOT NULL, retention_days integer NOT NULL DEFAULT 90,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS approvals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 requested_by uuid NOT NULL, status text NOT NULL DEFAULT 'pending',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 actor_id text NOT NULL, action text NOT NULL, resource_type text NOT NULL,
 resource_id text NOT NULL, outcome text NOT NULL DEFAULT 'success',
 occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS outbox_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id text NOT NULL,
 event_type text NOT NULL, payload jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'pending', available_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now()
);

DO $do$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['workforce_employees','workforce_tasks','agent_runs','workforce_memory','approvals','audit_events','outbox_events'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('DROP POLICY IF EXISTS tenant_scope_guard ON %I',t);
  EXECUTE format('CREATE POLICY tenant_scope_guard ON %I AS PERMISSIVE FOR ALL USING (tenant_id = current_setting(''app.current_tenant_id'',true)) WITH CHECK (tenant_id = current_setting(''app.current_tenant_id'',true))',t);
 END LOOP;
END $do$;
