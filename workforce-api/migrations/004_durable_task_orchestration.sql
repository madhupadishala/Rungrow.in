-- Priority 3: durable leases, state machine, sandbox and single-use approvals.
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS is_sandbox boolean NOT NULL DEFAULT false;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS result jsonb;
ALTER TABLE wf_tasks ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE wf_tasks DROP CONSTRAINT IF EXISTS wf_tasks_status_check;
ALTER TABLE wf_tasks ALTER COLUMN status SET DEFAULT 'QUEUED';
UPDATE wf_tasks SET status=CASE upper(status) WHEN 'FAILED' THEN 'DEAD_LETTER' ELSE upper(status) END;
ALTER TABLE wf_tasks ADD CONSTRAINT wf_tasks_status_check CHECK(status IN
 ('QUEUED','RUNNING','AWAITING_APPROVAL','COMPLETED','RETRY_SCHEDULED','DEAD_LETTER','CANCELLED'));
DO $
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='wf_tasks_attempt_bounds') THEN
  ALTER TABLE wf_tasks ADD CONSTRAINT wf_tasks_attempt_bounds CHECK(attempt_count >= 0 AND max_attempts > 0) NOT VALID;
 END IF;
END $;
ALTER TABLE wf_tasks VALIDATE CONSTRAINT wf_tasks_attempt_bounds;
CREATE INDEX IF NOT EXISTS wf_tasks_claim_idx ON wf_tasks(status,next_retry_at,lease_expires_at,created_at);

ALTER TABLE wf_agent_runs ADD COLUMN IF NOT EXISTS is_sandbox boolean NOT NULL DEFAULT false;
ALTER TABLE wf_agent_runs ADD COLUMN IF NOT EXISTS execution_state text NOT NULL DEFAULT 'QUEUED';
ALTER TABLE wf_agent_runs ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE wf_agent_runs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE wf_agent_runs ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;

ALTER TABLE wf_approvals ADD COLUMN IF NOT EXISTS consumed_at timestamptz;
ALTER TABLE wf_approvals ADD COLUMN IF NOT EXISTS execution_run_id uuid;
ALTER TABLE wf_approvals DROP CONSTRAINT IF EXISTS wf_approvals_status_check;
UPDATE wf_approvals SET status=upper(status);
ALTER TABLE wf_approvals ALTER COLUMN status SET DEFAULT 'PENDING';
ALTER TABLE wf_approvals ADD CONSTRAINT wf_approvals_status_check CHECK(status IN ('PENDING','APPROVED','REJECTED'));
DO $
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='wf_approval_consumption_binding') THEN
  ALTER TABLE wf_approvals ADD CONSTRAINT wf_approval_consumption_binding CHECK
   ((consumed_at IS NULL AND execution_run_id IS NULL) OR (consumed_at IS NOT NULL AND execution_run_id IS NOT NULL)) NOT VALID;
 END IF;
END $;
ALTER TABLE wf_approvals VALIDATE CONSTRAINT wf_approval_consumption_binding;

CREATE TABLE IF NOT EXISTS wf_model_usage (
 id uuid PRIMARY KEY, tenant_id text NOT NULL, product_id text REFERENCES wf_product_registry(product_id),
 employee_id text NOT NULL, run_id text NOT NULL, provider text NOT NULL, model text NOT NULL,
 prompt_tokens integer NOT NULL CHECK(prompt_tokens>=0),
 completion_tokens integer NOT NULL CHECK(completion_tokens>=0),
 estimated_cost_inr numeric(14,6) NOT NULL CHECK(estimated_cost_inr>=0),
 latency_ms integer NOT NULL CHECK(latency_ms>=0), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS wf_model_usage_tenant_product_time ON wf_model_usage(tenant_id,product_id,created_at DESC);
ALTER TABLE wf_model_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_model_usage FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_tenant_product_scope ON wf_model_usage;
CREATE POLICY wf_tenant_product_scope ON wf_model_usage FOR ALL
 USING (tenant_id=current_setting('app.current_tenant_id',true)
   AND (product_id IS NULL OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'
    OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))))
 WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true)
   AND (product_id IS NULL OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'
    OR product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))));

-- Approval payload and hash are immutable after request creation. Decisions only
-- change decision metadata; execution consumption only changes consumption metadata.
CREATE OR REPLACE FUNCTION wf_reject_approval_payload_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.payload IS DISTINCT FROM OLD.payload OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash THEN
   RAISE EXCEPTION 'approval payload and digest are immutable';
 END IF;
 RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS wf_approval_payload_immutable ON wf_approvals;
CREATE TRIGGER wf_approval_payload_immutable BEFORE UPDATE OF payload,payload_hash ON wf_approvals
FOR EACH ROW EXECUTE FUNCTION wf_reject_approval_payload_mutation();

DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='wf_agent_runs_execution_state_check') THEN
  ALTER TABLE wf_agent_runs ADD CONSTRAINT wf_agent_runs_execution_state_check
   CHECK(execution_state IN ('QUEUED','RUNNING','AWAITING_APPROVAL','COMPLETED','RETRY_SCHEDULED','DEAD_LETTER','CANCELLED')) NOT VALID;
 END IF;
END $$;
ALTER TABLE wf_agent_runs VALIDATE CONSTRAINT wf_agent_runs_execution_state_check;
