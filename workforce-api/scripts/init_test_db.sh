#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${PGDATABASE:-rungrow_workforce_test}"
OWNER="${PGUSER:-rungrow_migrator}"
APP_USER="rungrow_app_user"
APP_PASSWORD="${RUNGROW_APP_PASSWORD:-rungrow_app_local_only}"
COMPOSE=(docker compose -f "$ROOT/docker-compose.test.yml")
if [[ ! -f "$ROOT/migrations/006_capital_and_ceo_reporting.sql" ]]; then
  echo "Required migration 006_capital_and_ceo_reporting.sql is missing" >&2
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
# Apply the canonical ordered migration chain only. 001_workforce.sql is a
# legacy bootstrap from the pre-tenant schema and intentionally is not part
# of this chain; applying it after 001_initial_workforce_schema.sql recreates
# shared identity tables and fails with "relation users already exists".
MIGRATIONS=(
  001_initial_workforce_schema.sql
  002_product_scope_and_approval_governance.sql
  003_durable_workforce_repositories.sql
  004_durable_task_orchestration.sql
  005_product_adapters_and_telemetry.sql
  006_capital_and_ceo_reporting.sql
)
for name in "${MIGRATIONS[@]}"; do
  migration="$ROOT/migrations/$name"
  if [[ ! -f "$migration" ]]; then
    echo "Required migration $name is missing" >&2
    exit 1
  fi
  echo "Applying $name"
  "${PSQL[@]}" -f - < "$migration"
done
"${PSQL[@]}" <<SQL
DO \$\$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$APP_USER') THEN
   CREATE ROLE $APP_USER LOGIN PASSWORD '$APP_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
 ELSE
   ALTER ROLE $APP_USER WITH LOGIN PASSWORD '$APP_PASSWORD' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
 END IF;
END
\$\$;
GRANT CONNECT ON DATABASE $DB TO $APP_USER;
GRANT USAGE ON SCHEMA public TO $APP_USER;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO $APP_USER;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM $APP_USER;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $OWNER IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO $APP_USER;
ALTER DEFAULT PRIVILEGES FOR ROLE $OWNER IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO $APP_USER;
SQL
echo "Canonical migrations 001_initial_workforce_schema and 002..006 applied. App role: $APP_USER (NOSUPERUSER NOCREATEDB NOCREATEROLE)."
echo "RUNGROW_TEST_DATABASE_URL=postgresql+asyncpg://$APP_USER:$APP_PASSWORD@localhost:55432/$DB"
