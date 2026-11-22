-- Workforce 2.2 governed, tenant-scoped dispatch outbox.
CREATE TABLE IF NOT EXISTS wf_outbox_events (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  product_id text NOT NULL,
  directive_id uuid NOT NULL REFERENCES executive_directives(id),
  work_order_id uuid NOT NULL REFERENCES department_work_orders(id),
  approval_id uuid NOT NULL REFERENCES wf_approvals(id),
  employee_id text NOT NULL,
  action_type text NOT NULL,
  payload_hash char(64) NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'QUEUED_FOR_APPROVAL'
    CHECK (status IN ('QUEUED_FOR_APPROVAL','DISPATCHING','SENT','DISPATCH_ACCEPTED',
      'BLOCKED_UNCONFIGURED','BLOCKED_INVALID_RECIPIENT','FAILED')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  last_error text,
  provider_response jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz,
  CONSTRAINT wf_outbox_approval_once UNIQUE (tenant_id,approval_id)
);
CREATE INDEX IF NOT EXISTS idx_wf_outbox_pending
  ON wf_outbox_events (tenant_id,status,created_at);
ALTER TABLE wf_outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE wf_outbox_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS wf_outbox_tenant_isolation ON wf_outbox_events;
CREATE POLICY wf_outbox_tenant_isolation ON wf_outbox_events
  USING (tenant_id = current_setting('app.current_tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
