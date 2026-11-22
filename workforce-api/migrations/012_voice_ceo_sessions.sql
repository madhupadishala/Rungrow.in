-- Workforce 2.3 voice sessions. Version 012 is intentional: 011 is already
-- checksum-applied as workforce 2.2 approval outbox and must never be rewritten.
CREATE TABLE IF NOT EXISTS voice_sessions (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  principal_id text NOT NULL,
  principal_role text NOT NULL,
  product_id text,
  room_name text NOT NULL,
  livekit_url text,
  client_mode text NOT NULL CHECK (client_mode IN ('browser','livekit')),
  status text NOT NULL CHECK (status IN ('ACTIVE','BLOCKED_UNCONFIGURED','CLOSED','EXPIRED')),
  identity_disclosure text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  closed_at timestamptz,
  pending_approval_id uuid,
  pending_approval_requested_at timestamptz,
  UNIQUE (tenant_id, room_name)
);
CREATE INDEX IF NOT EXISTS voice_sessions_tenant_created
  ON voice_sessions (tenant_id, created_at DESC);
ALTER TABLE voice_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS voice_sessions_tenant_isolation ON voice_sessions;
CREATE POLICY voice_sessions_tenant_isolation ON voice_sessions
  FOR ALL USING (tenant_id = current_setting('app.current_tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));

CREATE TABLE IF NOT EXISTS voice_turns (
  id uuid PRIMARY KEY,
  tenant_id text NOT NULL,
  session_id uuid NOT NULL REFERENCES voice_sessions(id) ON DELETE CASCADE,
  speaker text NOT NULL CHECK (speaker IN ('FOUNDER','CEO','SYSTEM')),
  transcript text NOT NULL CHECK (length(transcript) BETWEEN 1 AND 8000),
  source text NOT NULL DEFAULT 'web_speech',
  interrupted_at_ms integer CHECK (interrupted_at_ms IS NULL OR interrupted_at_ms >= 0),
  context_turn_count integer NOT NULL DEFAULT 0 CHECK (context_turn_count >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS voice_turns_session_created
  ON voice_turns (tenant_id, session_id, created_at);
ALTER TABLE voice_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_turns FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS voice_turns_tenant_isolation ON voice_turns;
CREATE POLICY voice_turns_tenant_isolation ON voice_turns
  FOR ALL USING (tenant_id = current_setting('app.current_tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true));
