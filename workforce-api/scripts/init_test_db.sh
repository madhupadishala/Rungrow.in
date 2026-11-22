#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${PGDATABASE:-rungrow_workforce_test}"
OWNER="${PGUSER:-rungrow_migrator}"
APP_USER="rungrow_app_user"
APP_PASSWORD="${RUNGROW_APP_PASSWORD:-rungrow_app_local_only}"
COMPOSE=(docker compose -f "$ROOT/docker-compose.test.yml")
if [[ ! -f "$ROOT/migrations/010_workforce_2_orchestration.sql" ]]; then
  echo "Required migration 010_workforce_2_orchestration.sql is missing" >&2
  exit 1
fi
ready=false
for attempt in {1..30}; do
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U "$OWNER" -d "$DB" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != "true" ]]; then
  echo "PostgreSQL did not become ready within 30 seconds" >&2
  exit 1
fi
PSQL=("${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U "$OWNER" -d "$DB")
# Use the same checksum-verified transactional runner used by production.
# The legacy 001_workforce.sql is intentionally excluded from the canonical chain.
export DATABASE_URL="postgresql://${OWNER}:${PGPASSWORD:-${POSTGRES_PASSWORD:-rungrow_migrator_local_only}}@${PGHOST:-localhost}:${PGPORT:-55432}/${DB}"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
(
  cd "$ROOT"
  python -m app.migrate
  # The second invocation must observe matching checksums and apply nothing.
  python -m app.migrate
)
"${PSQL[@]}" <<SQL
DO \$do\$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER') THEN
   CREATE ROLE $APP_USER LOGIN PASSWORD '$APP_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
 ELSE
   ALTER ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
 END IF;
END
\$do\$;
GRANT CONNECT ON DATABASE $DB TO $APP_USER;
GRANT USAGE ON SCHEMA public TO $APP_USER;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO $APP_USER;
GRANT SELECT, INSERT, UPDATE ON TABLE prospect_records, campaign_plans TO $APP_USER;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM $APP_USER;
-- Workforce 2.1 tables are durable application records and require explicit DELETE
-- grants after the global least-privilege revoke above.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
  executive_directives, department_work_orders, employee_tool_executions TO $APP_USER;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $APP_USER;
GRANT SELECT, INSERT, DELETE ON rate_limit_events TO $APP_USER;
GRANT SELECT, INSERT ON release_gate_signoffs TO $APP_USER;
REVOKE UPDATE, DELETE, TRUNCATE ON release_gate_signoffs FROM $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $OWNER IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $OWNER IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO $APP_USER;
SQL
echo "Checksum-verified migrations 001_initial_workforce_schema and 002..010 applied. App role: $APP_USER (NOSUPERUSER NOCREATEDB NOCREATEROLE)."
echo "RUNGROW_TEST_DATABASE_URL=postgresql+asyncpg://$APP_USER:$APP_PASSWORD@localhost:55432/$DB"
