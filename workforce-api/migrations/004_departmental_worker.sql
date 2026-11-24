-- Departmental worker leases, retry scheduling and liveness.
-- Existing departmental work orders remain tenant-scoped and retain head approval states.
ALTER TABLE IF EXISTS department_work_orders
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;

CREATE INDEX IF NOT EXISTS department_work_orders_retry_idx
  ON department_work_orders (tenant_id, status, next_retry_at, created_at);

CREATE INDEX IF NOT EXISTS department_work_orders_expired_lease_idx
  ON department_work_orders (tenant_id, lock_expires_at)
  WHERE status = 'IN_PROGRESS';

CREATE TABLE IF NOT EXISTS worker_heartbeats (
  worker_id text PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  started_at timestamptz NOT NULL DEFAULT now(),
  last_heartbeat_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL CHECK (status IN ('RUNNING','STOPPED','DEGRADED')),
  current_work_order_id uuid,
  CONSTRAINT worker_heartbeats_worker_tenant_unique UNIQUE(worker_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS worker_heartbeats_tenant_status_idx
  ON worker_heartbeats (tenant_id, status, last_heartbeat_at DESC);

ALTER TABLE worker_heartbeats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS worker_heartbeats_tenant_scope ON worker_heartbeats;
CREATE POLICY worker_heartbeats_tenant_scope ON worker_heartbeats
  USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
  WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
