# RunGrow Production Go-Live & Disaster-Recovery Runbook

**Scope:** flagship production cohort only: `photo-studio-growth-os` (Studio Growth OS + Studio X Photography) and `clinics-counter` (Conversion Agent Platform). The four unbuilt verticals remain product-scoped deferred acceptances. Growtoys remains `DISCOVERY_ONLY`.

**Production invariants:** PostgreSQL is the system of record; all runtime queries remain tenant-scoped; HMAC v1, single-use human approvals and fail-closed provider behavior remain mandatory. A product-scoped deferred acceptance cannot defer or override Security, Reliability, Engineering, AI Governance, single-use approvals, tenant isolation, HMAC authentication or fail-closed provider controls.

## 1. Neon PostgreSQL provisioning

1. Create the production Neon project and a production **root** branch. Use a dedicated database (for example `rungrow`) and keep the production branch protected where the selected Neon plan supports branch protection.
2. Open the SQL Editor on the production branch and enable the required extensions:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;
```

3. Create separate database identities:
   - **Migration role:** owner/DDL-capable role used only by Render pre-deploy migrations. Use the direct, non-pooler Neon connection string because the migration runner takes a session-level PostgreSQL advisory lock.
   - **Runtime role:** restricted `rungrow_app` login used by the API and worker. Use the pooled Neon connection string with TLS required. Do not use the migration role as the application runtime identity.

Example grants, executed as the migration/database owner after migrations have created the schema (replace the password out-of-band; never commit it):

```sql
CREATE ROLE rungrow_app LOGIN PASSWORD '<GENERATE_AND_STORE_IN_SECRET_MANAGER>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
GRANT CONNECT ON DATABASE rungrow TO rungrow_app;
GRANT USAGE ON SCHEMA public TO rungrow_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO rungrow_app;
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM rungrow_app;
GRANT SELECT, INSERT, DELETE ON rate_limit_events TO rungrow_app;
GRANT SELECT, INSERT ON release_gate_signoffs TO rungrow_app;
REVOKE UPDATE, DELETE, TRUNCATE ON release_gate_signoffs FROM rungrow_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rungrow_app;
ALTER DEFAULT PRIVILEGES FOR ROLE <MIGRATION_ROLE> IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO rungrow_app;
ALTER DEFAULT PRIVILEGES FOR ROLE <MIGRATION_ROLE> IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO rungrow_app;
```

The migration role must own the schema objects or have the required DDL privileges. Keep `MIGRATION_DATABASE_URL` as the direct (non-pooler) Neon connection because the runner uses a session advisory lock; keep `DATABASE_URL` as the pooled runtime connection if desired. If the runtime role already exists, rotate its password using the Neon SQL Editor/role controls instead of creating it again. Keep RLS enabled and forced; do not grant the runtime role `BYPASSRLS`.

### Apply the production migration chain

From the repository root in a trusted deployment shell, set the two connection strings and run the same checksum-verified migration runner used by CI:

```bash
cd /workspace/workforce-api
export APP_ENV=production
export MIGRATION_DATABASE_URL='postgresql://<MIGRATOR>:<PASSWORD>@<NEON_DIRECT_HOST>/<DB>?sslmode=require'
export DATABASE_URL='postgresql://rungrow_app:<PASSWORD>@<NEON_POOLER_HOST>/<DB>?sslmode=require'
python -m app.migrate
```

The runner applies migrations `001_initial_workforce_schema.sql` through `009_production_bootstrap_and_cohorts.sql`, verifies recorded SHA-256 checksums, and is safe to rerun. Do not edit an already-applied migration; add a new numbered migration.

## 2. Idempotent primary-tenant bootstrap

The primary tenant slug is `rungrow`; set `RUNGROW_INTERNAL_TENANT_ID=rungrow` so verified JWT tenant claims, product mappings and the bootstrap agree. Set the exact immutable deployed RunGrow commit SHA before running the bootstrap. The command refuses to run without a full SHA and refuses to run against a non-production environment.

```bash
cd workforce-api
export APP_ENV=production
export RUNGROW_INTERNAL_TENANT_ID=rungrow
export RUNGROW_RELEASE_CANDIDATE_SHA='<40-character-deployed-Rungrow.in-commit-SHA>'
export RELEASE_DEFERRED_REVIEW_DUE_DATE=2027-02-28
python -m app.bootstrap_prod
```

Run it after `python -m app.migrate` and after the runtime-role grants above. It is safe to run more than once for the same candidate SHA. Run it twice and verify the returned counts are unchanged. It provisions:
- `wf_tenants`: primary tenant `rungrow / RunGrow`.
- `wf_product_adapter_bindings`: live v1 bindings for `photo-studio-growth-os` and `clinics-counter`; a separate `DISCOVERY_ONLY` binding for Growtoys. The four no-repository products have no adapter binding and are recorded as accepted deferrals.
- `wf_mission_baselines`: provisional tenant baseline with ₹0 recognised revenue, ₹0 raised capital, ₹5,00,00,000 total revenue target and ₹5,00,00,000 total capital target.
- `wf_mission_product_allocations`: seven provisional product allocations for both targets. Allocations are management planning allocations, not forecasts.
- Product-scoped `DEFERRED_WITH_ACCEPTANCE` signoffs for the four `NO_REPO_YET` verticals and the Growtoys discovery-only boundary. The signoffs do not defer security or reliability.

The bootstrap uses `ON CONFLICT`/immutable identity keys so repeated execution does not duplicate bindings, baselines or deferred signoffs. Existing actual revenue/capital values are not overwritten by the baseline insert.

## 3. Render API and background worker

Connect the RunGrow repository to Render using the repository root as the Docker context.

### API web service

Use the existing `render.yaml` blueprint:
- Service: `rungrow-workforce-api`
- Runtime: Docker
- Dockerfile: `./workforce-api/Dockerfile`
- Docker context: repository root
- Pre-deploy command: `python -m app.migrate` (uses `MIGRATION_DATABASE_URL`)
- Health check path: `/health/ready`
- Start command: Dockerfile default, `uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers`

Configure the following environment values/secrets in Render. Never put credentials in Git:
- `APP_ENV=production`
- `DATABASE_URL`: restricted runtime role URL, TLS required; pooled Neon URL is supported for runtime transactions.
- `MIGRATION_DATABASE_URL`: direct Neon URL for the migration role, TLS required. `python -m app.migrate` prefers this URL and falls back to `DATABASE_URL` in CI.
- `RUNGROW_INTERNAL_TENANT_ID=rungrow`
- `JWT_ISSUER=https://rungrow.in`, `JWT_AUDIENCE=rungrow-workforce-api`, `JWT_JWKS_URL` (HTTPS OIDC JWKS URL).
- `STUDIO_GROWTH_OS_SERVICE_SECRET` and `RUNGROW_ADAPTER_WEBHOOK_SECRET`: independently generated random secrets of at least 32 bytes; configure matching product-side secrets in the respective product repositories.
- `STUDIO_GROWTH_OS_API_URL`, `STUDIO_GROWTH_OS_TENANT_MAP`, `STUDIO_GROWTH_OS_TENANT_SERVICE_TOKENS`: actual production endpoint and verified tenant/service-token mappings.
- `WORKFORCE_MODEL_PROVIDER=openai-compatible`, `WORKFORCE_PRIMARY_MODEL=gpt-4o-mini`, `OPENAI_API_KEY`.
- WhatsApp: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_API_VERSION=v21.0`.
- Email: `EMAIL_API_KEY`, `EMAIL_FROM_ADDRESS`.

Do not invent or use placeholder credentials. If WhatsApp or email credentials are absent, sends must continue returning audited HTTP 503. Do not sign off live channel delivery until provider receipt evidence exists.

### Background worker

Use the second blueprint service:
- Service: `rungrow-workforce-worker`
- Runtime: Docker, same Dockerfile and root context.
- Command: `python -m app.worker`
- Use the same restricted runtime `DATABASE_URL`, OIDC, HMAC and model-provider settings as the API.
- Set `WORKFORCE_WORKER_TENANT_ID=rungrow`, `WORKFORCE_WORKER_ID=rungrow-workforce-worker`, `WORKFORCE_WORKER_SCOPES=work.assign,agent.run`, and the product IDs in `WORKFORCE_WORKER_PRODUCT_IDS`.
- The API's pre-deploy migration must complete before the worker is started. Deploy/verify the API first, then start the worker.

The worker writes tenant-scoped heartbeats. `/health/ready` checks database connectivity, the exact migration chain, OIDC and HMAC configuration; the release readiness Reliability gate additionally requires a fresh heartbeat and restore evidence.

## 4. Vercel frontend

Deploy the same repository to Vercel:
- Framework preset: **Other** (static HTML/JavaScript frontend).
- Root directory: repository root.
- Build command: none.
- Output directory: repository root (`.`).
- Production branch: the approved RunGrow default branch after the stacked PR sequence is completed.
- Attach the approved `rungrow.in` domain and verify DNS/TLS in Vercel.

The committed `vercel.json` rewrites `/api/workforce/:path*` to the Render API at `https://rungrow-workforce-api.onrender.com/:path*`. Keep the Render service name/host aligned if the service is renamed.

Deploy the approved production branch from the linked Vercel project (or its deployment shell):

```bash
vercel deploy --prod
``` Do not expose database or provider credentials as Vercel browser environment variables.

## 5. Neon point-in-time recovery (PITR) and Gate 5 evidence

Neon point-in-time recovery depends on the history window and retention available on the selected plan. Confirm the actual retention in Neon Console under the project's backup/restore or history-window settings; record the configured value rather than assuming a plan limit.

### Restore rehearsal

1. Create an isolated DR rehearsal project/root branch with the same migration chain and sanitised representative data. Confirm the branch is a root branch; Instant Restore does not support child branches.
2. Record the rehearsal dataset timestamp and row-count/checksum baselines for critical tables: tenants, product bindings, financial baselines, release signoffs, approvals, audit events, idempotency and product telemetry.
3. In Neon Console open **Postgres → Backup & Restore → Restore from history**. Use Time Travel Assist/read-only queries to identify a point immediately before a controlled test mutation.
4. Restore the rehearsal root branch to that timestamp. Confirm the restore completed and Neon created the pre-restore backup branch automatically.
5. Reconnect using the branch connection string and verify the controlled mutation is absent, baseline row counts/checksums are restored, migrations are checksum-consistent, and `/health/ready` returns 200 against the rehearsal deployment.
6. Record restore start/end timestamps, selected recovery point, branch IDs, measured RTO, observed RPO, verification query output and operator in the release evidence record.

**Do not use production as the first restore drill.** A Neon instant restore overwrites all databases on the selected root branch, temporarily interrupts connections, and excludes changes after the selected point. Neon automatically preserves the pre-restore state in a backup branch; keep it until post-restore validation is complete. A production incident restore requires a separately approved incident/change record and explicit confirmation of the recovery point.

### Close Release 1 Reliability Gate

After the rehearsal succeeds, record a commit-bound human signoff using `POST /v1/release/signoffs` with:
- `release_layer=RELEASE_1_WORKFORCE`
- `gate_name=RELIABILITY`
- `status=PASS`
- `cohort_id=flagship-production`, `scope_key=gate`
- Evidence object `backup_restore_test` containing `status=PASS`, `evidence_ref`, recovery point, RTO, RPO, verification result and operator.
- The exact deployed candidate SHA and `environment=production`.

A deferred product acceptance cannot satisfy this gate.

## 6. Post-deploy smoke checks

Run after the API deploy and before enabling the worker or product traffic:

```bash
curl --fail https://rungrow-workforce-api.onrender.com/health/live
curl --fail https://rungrow-workforce-api.onrender.com/health/ready
curl --fail -H "Authorization: Bearer <WORKFORCE_READ_TOKEN>" \
  "https://rungrow-workforce-api.onrender.com/v1/release/readiness?cohort=flagship-production"
```

Expected:
- `/health/live`: HTTP 200 and `status=alive`.
- `/health/ready`: HTTP 200, `status=ready`, PostgreSQL + migration chain + repository + OIDC + HMAC checks all true.
- Readiness: `cohort=flagship`; required live products exactly `photo-studio-growth-os` and `clinics-counter`; portfolio deferrals are listed separately. A GO requires all three release layers' own evidence; missing live communication receipts, backup/restore evidence or commit-bound signoffs remain blockers.
- Verify `GET /v1/release/readiness?cohort=portfolio` lists the four deferred products and Growtoys without treating them as live integrations.
- Verify a live Growtoys handoff/outreach attempt returns HTTP 403.
- Verify unconfigured WhatsApp/email sends return audited HTTP 503, not simulated success.
- Verify API and worker heartbeat rows use tenant `rungrow`, and no other tenant's rows are visible.

## 7. Stacked PR merge and deployment order

The RunGrow control-plane PRs are stacked:
1. `Rungrow.in PR #1` (`feat/ai-workforce-platform` → `main`).
2. `Rungrow.in PR #2` (`feat/month-1-day-1-operating-foundation` → `feat/ai-workforce-platform`).
3. `Rungrow.in PR #4` (`feat/release-1-hardening` → `feat/month-1-day-1-operating-foundation`).

To land the full stack on `main`, after each parent merge retarget the next downstream PR's base to `main` (or otherwise promote the merged parent branch into `main`) before merging it. Do not merge PR #2 or #4 into a feature branch and assume those changes have reached `main`.

The product repositories remain separate. Merge their adapter PRs only into their own default branches: `studio-growth-os PR #1`, `studio-x-photography PR #3`, and `conversion-agent-platform PR #21`. Deploy those product services independently and verify their product-side HMAC/webhook contracts before enabling the corresponding RunGrow bindings. Never copy or merge their code into `Rungrow.in`.

No PR is auto-merged by this runbook. The Founder authorises each merge and production rollout separately.

## 8. Neon reference documentation

- [Neon Instant Restore / PITR](https://neon.com/docs/postgres/backup-restore/branch-restore)
- [Neon History Window and retention limits](https://neon.com/docs/postgres/backup-restore/history-window)
- [Neon Branches and branch types](https://neon.com/docs/manage/branches)
