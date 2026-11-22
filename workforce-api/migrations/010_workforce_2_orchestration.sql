-- Workforce 2.1: durable executive directives, department work orders and tool evidence.
CREATE TABLE IF NOT EXISTS executive_directives (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id text NOT NULL,
 product_id text,
 founder_id text NOT NULL,
 command text NOT NULL CHECK(length(trim(command)) BETWEEN 8 AND 8000),
 status text NOT NULL DEFAULT 'PLANNING' CHECK(status IN ('PLANNING','DELEGATED','IN_REVIEW','AWAITING_APPROVAL','COMPLETED','FAILED')),
 consolidated_proposal jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(consolidated_proposal)='object'),
 approval_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS executive_directives_tenant_created ON executive_directives(tenant_id,created_at DESC);
ALTER TABLE executive_directives ENABLE ROW LEVEL SECURITY;
ALTER TABLE executive_directives FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS executive_directives_tenant_scope ON executive_directives;
CREATE POLICY executive_directives_tenant_scope ON executive_directives FOR ALL
 USING(tenant_id=current_setting('app.current_tenant_id',true))
 WITH CHECK(tenant_id=current_setting('app.current_tenant_id',true));

CREATE TABLE IF NOT EXISTS department_work_orders (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id text NOT NULL,
 directive_id uuid NOT NULL REFERENCES executive_directives(id) ON DELETE CASCADE,
 product_id text,
 department text NOT NULL,
 department_head_id text NOT NULL,
 title text NOT NULL,
 objective text NOT NULL,
 dependencies jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(dependencies)='array'),
 specialist_role_id text NOT NULL,
 tool_name text NOT NULL,
 tool_input jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(tool_input)='object'),
 status text NOT NULL DEFAULT 'ASSIGNED' CHECK(status IN ('ASSIGNED','IN_PROGRESS','REVISION_REQUESTED','APPROVED_BY_HEAD','REJECTED')),
 revision_count integer NOT NULL DEFAULT 0 CHECK(revision_count BETWEEN 0 AND 2),
 review_history jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(review_history)='array'),
 deliverable jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(deliverable)='object'),
 last_error text,
 claimed_by text,
 claimed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(tenant_id,directive_id,department)
);
CREATE INDEX IF NOT EXISTS department_work_orders_claim
 ON department_work_orders(tenant_id,status,created_at);
CREATE INDEX IF NOT EXISTS department_work_orders_directive
 ON department_work_orders(tenant_id,directive_id,created_at);
ALTER TABLE department_work_orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE department_work_orders FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS department_work_orders_tenant_scope ON department_work_orders;
CREATE POLICY department_work_orders_tenant_scope ON department_work_orders FOR ALL
 USING(tenant_id=current_setting('app.current_tenant_id',true))
 WITH CHECK(tenant_id=current_setting('app.current_tenant_id',true));

CREATE TABLE IF NOT EXISTS employee_tool_executions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 tenant_id text NOT NULL,
 directive_id uuid NOT NULL REFERENCES executive_directives(id) ON DELETE CASCADE,
 work_order_id uuid NOT NULL REFERENCES department_work_orders(id) ON DELETE CASCADE,
 employee_id text NOT NULL,
 tool_name text NOT NULL,
 input_hash char(64) NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 input_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
 output_evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
 latency_ms integer NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
 status text NOT NULL CHECK(status IN ('PASSED','FAILED','BLOCKED')),
 error text,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS employee_tool_executions_directive
 ON employee_tool_executions(tenant_id,directive_id,created_at);
ALTER TABLE employee_tool_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE employee_tool_executions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS employee_tool_executions_tenant_scope ON employee_tool_executions;
CREATE POLICY employee_tool_executions_tenant_scope ON employee_tool_executions FOR ALL
 USING(tenant_id=current_setting('app.current_tenant_id',true))
 WITH CHECK(tenant_id=current_setting('app.current_tenant_id',true));


DO $do$
DECLARE table_name text;
BEGIN
 FOREACH table_name IN ARRAY ARRAY[
   'executive_directives','department_work_orders','employee_tool_executions'
 ] LOOP
   EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
   EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
 END LOOP;
END
$do$;
