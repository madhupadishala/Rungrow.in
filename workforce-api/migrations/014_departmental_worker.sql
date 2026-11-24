-- Workforce 2.5 departmental worker lease/retry extension.
-- This is deliberately numbered 014: 004 is already occupied by durable task
-- orchestration and 008 already owns the worker_heartbeats table.
ALTER TABLE department_work_orders
  ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3,
  ADD COLUMN IF NOT EXISTS next_retry_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_error text;

-- Add worker-only states without rewriting existing rows. NOT VALID keeps the
-- constraint change metadata-only; validation is safe because old rows comply.
ALTER TABLE department_work_orders
  DROP CONSTRAINT IF EXISTS department_work_orders_status_check;
ALTER TABLE department_work_orders
  ADD CONSTRAINT department_work_orders_status_check
  CHECK (status IN ('ASSIGNED','IN_PROGRESS','REVISION_REQUESTED','APPROVED_BY_HEAD',
                    'REJECTED','AWAITING_APPROVAL','QUEUED','PENDING','FAILED')) NOT VALID;
ALTER TABLE department_work_orders
  VALIDATE CONSTRAINT department_work_orders_status_check;

DO $constraints$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='department_work_orders_attempt_bounds_check') THEN
    ALTER TABLE department_work_orders
      ADD CONSTRAINT department_work_orders_attempt_bounds_check
      CHECK (attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts) NOT VALID;
  END IF;
END
$constraints$;
ALTER TABLE department_work_orders
  VALIDATE CONSTRAINT department_work_orders_attempt_bounds_check;

CREATE INDEX IF NOT EXISTS department_work_orders_retry_idx
  ON department_work_orders (tenant_id, status, next_retry_at, created_at);
CREATE INDEX IF NOT EXISTS department_work_orders_expired_lease_idx
  ON department_work_orders (tenant_id, lock_expires_at)
  WHERE status = 'IN_PROGRESS';

-- Extend the canonical 008 heartbeat table; do not recreate it with a conflicting
-- UUID tenant type or a second incompatible status CHECK.
ALTER TABLE worker_heartbeats
  ADD COLUMN IF NOT EXISTS started_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS current_work_order_id uuid;

ALTER TABLE worker_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE worker_heartbeats FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS worker_heartbeats_tenant_scope ON worker_heartbeats;
CREATE POLICY worker_heartbeats_tenant_scope ON worker_heartbeats FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true));
