# RunGrow AI Workforce API

A separate service boundary in the existing static RunGrow repository. The marketing site remains static; the operator UI lives in /workforce/ and the API in workforce-api/.

## Open-source license review
- LiveKit Agents: Apache-2.0. Selected for realtime voice, telephony and agent sessions. Individual turn-detection model assets may have a separate model license; review each model before use.
- Crawlee: Apache-2.0. Selected for policy-governed public-site crawling and structured extraction.
- AgentSDR: MIT. Reference for SDR workflow patterns; no full application copied.
- OpenCloser v2: MIT. Reference for voice sales workflows; desktop application not adopted as backend.
- OpenOutreach: Apache-2.0. Reference for prospecting workflows; provider data terms still apply.

RunGrow owns the workforce hierarchy, orchestration, tenant isolation, permissions, approvals, audit and commercial policy. Do not vendor upstream code without retaining license and NOTICE files.

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
The API currently uses an in-memory adapter for local policy prototyping. It does not survive restarts and is not suitable for multi-worker or production customer data. The SQL migration is the durable tenant-scoped schema contract; wire the PostgreSQL repository, tenant session context, transactional outbox and durable worker before production data is enabled. Approval decisions are recorded but deliberately do not execute actions.

## Controlled autonomy
Agents may research, classify, draft, summarise, prepare work and update explicitly permitted records. Outbound contact, proposal dispatch, price/discount changes, contract commitments, payments, exports, access changes, production deployments, campaign activation and destructive actions require human approval. The worker must re-check approval, tenant, consent and policy at execution time.

Voice and text share one authenticated command API. Browser speech recognition is an input adapter; production realtime voice and telephony use LiveKit Agents behind the API. Provider secrets never belong in browser code.

## Memory
Memory is scoped by tenant, department and role, with source references and retention metadata. SQL stores encrypted memory bytes, not plaintext. Avoid sensitive personal information by default.

## Lead sourcing and outreach
Crawling must use allowlisted public sources, rate limits, robots/terms checks, provenance and suppression checks. A public phone number is not consent to call. Outreach requires channel-specific permission and applicable Indian DND/UCC, messaging and privacy checks. Use official WhatsApp Business APIs or an authorised provider, never consumer WhatsApp Web automation.


## Identity provider integration
In production set APP_ENV=production, configure the OIDC issuer, audience and RS256 verification public key, and map trusted identity-provider groups/roles to server-managed permissions. Never let a browser choose its own tenant ID or permission claims. The local HS256 token helper is for development only; production decoding rejects HS256 tokens.
