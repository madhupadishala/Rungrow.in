-- Priority 2: deterministic prospect qualification, evidence revalidation and reviewable sales artifacts.
ALTER TABLE prospect_records ADD COLUMN IF NOT EXISTS qualified_at timestamptz;
ALTER TABLE prospect_records ADD COLUMN IF NOT EXISTS qualification_score numeric(4,3)
  CHECK (qualification_score IS NULL OR qualification_score BETWEEN 0 AND 1);
CREATE TABLE IF NOT EXISTS sales_artifacts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id text NOT NULL,
  product_id text NOT NULL REFERENCES wf_product_registry(product_id),
  prospect_id uuid NOT NULL REFERENCES prospect_records(id) ON DELETE RESTRICT,
  artifact_type text NOT NULL CHECK (artifact_type IN ('PITCH_BRIEF','WHATSAPP_DRAFT','EMAIL_DRAFT')),
  channel text NOT NULL CHECK (channel IN ('RESEARCH_ONLY','WHATSAPP','EMAIL')),
  title text NOT NULL CHECK (length(trim(title)) BETWEEN 3 AND 300),
  content jsonb NOT NULL CHECK (jsonb_typeof(content)='object'),
  status text NOT NULL DEFAULT 'PENDING_HUMAN_APPROVAL'
    CHECK (status IN ('DRAFT','PENDING_HUMAN_APPROVAL','APPROVED','REJECTED')),
  created_by text NOT NULL,
  approved_by text,
  approved_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata)='object'),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((status='APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL) OR status<>'APPROVED')
);
CREATE INDEX IF NOT EXISTS sales_artifacts_tenant_prospect_created
  ON sales_artifacts(tenant_id,product_id,prospect_id,created_at DESC);
ALTER TABLE sales_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_artifacts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sales_artifacts_tenant_product_scope ON sales_artifacts;
CREATE POLICY sales_artifacts_tenant_product_scope ON sales_artifacts FOR ALL
  USING (tenant_id=current_setting('app.current_tenant_id',true)
    AND (product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
      OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'))
  WITH CHECK (tenant_id=current_setting('app.current_tenant_id',true)
    AND (product_id=ANY(string_to_array(COALESCE(current_setting('app.product_ids',true),''),','))
      OR COALESCE(current_setting('app.is_company_admin',true),'false')='true'));
