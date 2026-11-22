-- Workforce 2.4 tenant-scoped composable skill definitions and sandbox proof ledger.
CREATE TABLE IF NOT EXISTS agent_skills (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  skill_id text NOT NULL,
  department text NOT NULL,
  role_id text NOT NULL,
  category text NOT NULL,
  skill_name text NOT NULL,
  instructions text NOT NULL,
  rubric_json jsonb NOT NULL,
  status text NOT NULL DEFAULT 'DRAFT'
    CHECK (status IN ('DRAFT','SANDBOX_TESTING','PROMOTED','REJECTED')),
  latest_score numeric(5,4),
  version_hash text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, skill_id)
);
CREATE INDEX IF NOT EXISTS agent_skills_tenant_role ON agent_skills(tenant_id, role_id, status);
ALTER TABLE agent_skills ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_skills FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS agent_skills_tenant_isolation ON agent_skills;
CREATE POLICY agent_skills_tenant_isolation ON agent_skills
  FOR ALL USING (tenant_id = current_setting('app.current_tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));

CREATE TABLE IF NOT EXISTS skill_test_runs (
  test_run_id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  skill_id text NOT NULL,
  scenario_input jsonb NOT NULL,
  agent_output jsonb NOT NULL,
  rubric_scores jsonb NOT NULL,
  overall_score numeric(5,4) NOT NULL CHECK (overall_score >= 0 AND overall_score <= 1),
  passed boolean NOT NULL,
  executed_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (tenant_id, skill_id) REFERENCES agent_skills(tenant_id, skill_id)
);
CREATE INDEX IF NOT EXISTS skill_test_runs_tenant_skill
  ON skill_test_runs(tenant_id, skill_id, executed_at DESC);
ALTER TABLE skill_test_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_test_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS skill_test_runs_tenant_isolation ON skill_test_runs;
CREATE POLICY skill_test_runs_tenant_isolation ON skill_test_runs
  FOR ALL USING (tenant_id = current_setting('app.current_tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
