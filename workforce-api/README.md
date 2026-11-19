# RunGrow AI Workforce API

A separate service boundary in the existing static RunGrow repository. The marketing site remains static; the operator UI lives in /workforce/ and the API in workforce-api/.

## Open-source license review
- LiveKit Agents: Apache-2.0. Selected for realtime voice, telephony and agent sessions. Individual turn-detection model assets may have a separate model license; review each model before use.
- Crawlee: Apache-2.0. Selected for policy-governed public-site crawling and structured extraction.
- AgentSDR: MIT. Reference for SDR workflow patterns; no full application copied.
- OpenCloser v2: canonical repository and license not verified; not approved for code reuse until verified.
- OpenOutreach: GPL-3.0 per its repository page. Reference-only pending legal review; do not copy code into the proprietary product by default.

RunGrow owns the workforce hierarchy, orchestration, tenant isolation, permissions, approvals, audit and commercial policy. Follow `docs/REPOSITORY-REUSE-AND-INTEGRATION-STANDARD.md`: inspect existing RunGrow code first, pin exact upstream commits, verify licenses and retain required notices. Do not vendor upstream code without retaining license and NOTICE files.

## Local development
    cd workforce-api
    python -m venv .venv
    . .venv/bin/activate
    pip install -e '.[dev]'
    cp .env.example .env
    uvicorn app.main:app --reload
    pytest

The API exposes /health and /docs. Protected endpoints require a JWT issued by the trusted identity provider. No production bootstrap account or default credentials are included.

## Runtime boundary
Production startup requires `DATABASE_URL` and fails closed without it. Production operational routes use `PostgresWorkforceRepository`; no process-global in-memory state exists. Every repository transaction installs verified tenant, product, principal and company-admin context using transaction-local settings. `InMemoryWorkforceRepository` is an explicit per-test dependency override only.

The root RunGrow marketing site and existing product repositories remain separate. Workforce stores company-level tasks, agent runs, approvals, memory metadata, audit and model usage; it does not duplicate product CRM, billing, booking or conversion records. Approval decisions never execute the approved action.

## Local PostgreSQL and test harness
From the repository root:

```bash
docker compose -f workforce-api/docker-compose.test.yml up -d
bash ./workforce-api/scripts/init_test_db.sh
RUNGROW_TEST_DATABASE_URL=postgresql+asyncpg://rungrow_app_user:rungrow_app_local_only@localhost:55432/rungrow_workforce_test pytest -v workforce-api/tests
```

The bootstrap applies migrations 001–004 as the database owner and creates `rungrow_app_user` with `NOSUPERUSER NOCREATEDB NOCREATEROLE`, no table ownership and no DELETE grants. PostgreSQL integration tests skip only when `RUNGROW_TEST_DATABASE_URL` is unset; a configured but unreachable database fails the test.


## Controlled autonomy
Agents may research, classify, draft, summarise, prepare work and update explicitly permitted records. Outbound contact, proposal dispatch, price/discount changes, contract commitments, payments, exports, access changes, production deployments, campaign activation and destructive actions require human approval. The worker must re-check approval, tenant, consent and policy at execution time.

Voice and text share one authenticated command API. Browser speech recognition is an input adapter; production realtime voice and telephony use LiveKit Agents behind the API. Provider secrets never belong in browser code.

## Memory
Memory is scoped by tenant, department and role, with source references and retention metadata. Memory records are tenant/product/role scoped with source references and retention metadata. Apply the organisation's encryption-at-rest and key-management controls to the PostgreSQL volume and backups. Avoid sensitive personal information by default.

## Lead sourcing and outreach
Crawling must use allowlisted public sources, rate limits, robots/terms checks, provenance and suppression checks. A public phone number is not consent to call. Outreach requires channel-specific permission and applicable Indian DND/UCC, messaging and privacy checks. Use official WhatsApp Business APIs or an authorised provider, never consumer WhatsApp Web automation.


## Identity provider integration
In production set APP_ENV=production, configure the OIDC issuer, audience and RS256 verification public key, and map trusted identity-provider groups/roles to server-managed permissions and a signed `products` claim containing the principal's permitted product IDs. The API derives tenant and product scope only from verified JWT claims; request schemas reject caller-supplied tenant or permission fields. Never let a browser choose its own tenant ID or permission claims. The local HS256 token helper is for development only; production decoding rejects HS256 tokens.


## Priority 3: model gateway and durable execution

The provider-independent `ModelGateway` supports `mock`, OpenAI-compatible endpoints (OpenAI, OpenRouter, llama.cpp, Ollama-compatible `/v1` endpoints), and Anthropic Messages over async HTTP. Select with `WORKFORCE_MODEL_PROVIDER=mock|openai-compatible|anthropic`, `WORKFORCE_PRIMARY_MODEL`, optional `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and optional `WORKFORCE_FALLBACK_PROVIDER` / `WORKFORCE_FALLBACK_MODEL`. Configure `WORKFORCE_INPUT_COST_INR_PER_1K` and `WORKFORCE_OUTPUT_COST_INR_PER_1K` using your contracted provider pricing to calculate estimated INR cost; unset rates remain explicitly zero rather than fabricated. Tests use `MockModelProvider` and never require provider credentials. Structured output is Pydantic-validated with one schema-repair retry. Model usage and estimated INR cost are stored in the tenant/product-scoped `wf_model_usage` ledger and audit stream.

Apply migration 004 after migrations 001–003. It adds lease metadata, uppercase task/run execution states, sandbox flags, single-use approval consumption, immutable approval payloads, and the model usage ledger. Claims use `FOR UPDATE SKIP LOCKED`; expired leases are reclaimable and max-attempt exhaustion is terminal `DEAD_LETTER`.

Run a standalone worker from `workforce-api/`:

```bash
DATABASE_URL=postgresql+asyncpg://rungrow_app_user:***@localhost:55432/rungrow_workforce_test \
WORKFORCE_WORKER_TENANT_ID=your-tenant-id \
WORKFORCE_WORKER_PRODUCT_IDS=photo-studio-growth-os \
WORKFORCE_MODEL_PROVIDER=mock \
python -m app.worker
``

The worker only creates plans. External communication, financial, investor, access-control, export and deployment actions must be registered as tools and pass `ExecutionBoundaryGuard`. Sensitive action types always require human approval even if a tool is incorrectly registered with `requires_human_approval=False`. Approval consumption and the pre-execution audit commit before the handler is invoked. A consumed approval cannot be replayed. Sandbox runs cannot invoke tools with `allowed_in_sandbox=False`.


## Priority 4: Photo Studio Growth OS adapter v1

The adapter treats `studio-growth-os` as the authoritative source for its tenant, lead, quote, booking and conversation records. Workforce stores only adapter idempotency evidence and lifecycle/revenue telemetry; it does not create a parallel CRM, quote or booking database.

Configure:
- `STUDIO_GROWTH_OS_API_URL`: standalone Studio Growth OS API base URL.
- `RUNGROW_INTERNAL_TENANT_ID`: internal RunGrow organization tenant used for Workforce RLS; never set this to an external Studio tenant ID.
- `STUDIO_GROWTH_OS_TENANT_MAP`: JSON mapping of external Studio tenant IDs to their owning internal RunGrow organization tenant IDs.
- `STUDIO_GROWTH_OS_SERVICE_SECRET`: shared HMAC-SHA256 secret; never expose it to browser clients.
- `STUDIO_GROWTH_OS_TENANT_SERVICE_TOKENS`: JSON object mapping each external Studio Growth OS tenant ID to its short-lived service JWT. Existing protected Studio Growth OS routes require tenant membership JWTs; the adapter sends the tenant-matched JWT in addition to its HMAC signature when configured.
- `DATABASE_URL`: required for durable adapter idempotency and telemetry.

Outbound requests sign the exact request body using `X-RunGrow-Timestamp` and `X-RunGrow-Signature`, and propagate `X-RunGrow-Tenant-ID`, `X-RunGrow-Product-ID`, `X-Correlation-ID`, and `Idempotency-Key`. The verifier rejects timestamps outside a 300-second replay window. Product read and write scopes are distinct. All write tools are registered with `ExecutionBoundaryGuard`; outreach/handoff actions require a matching, unexpired, third-party-approved, single-use approval and are prohibited in sandbox runs.

Apply migration 005 after migrations 001–004. It creates the append-only `product_telemetry_events` ledger and `adapter_idempotency_keys`, both forced through tenant+product RLS. Only `invoice_recognised` may carry non-zero `recognised_revenue_inr`; onboarding, trial, conversion, booking and churn-risk events remain non-revenue pipeline telemetry.

The local ASGI ingress under `app/adapters/mocks/` and fixtures under `tests/fixtures/photo_studio_growth_os/` are synthetic-only. They are not production customer, booking or revenue evidence.
