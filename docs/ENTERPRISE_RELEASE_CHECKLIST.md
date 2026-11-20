# RunGrow Enterprise Production Release Checklist

- **Standard:** RG-ENG-REL-001
- **Version:** 1.0
- **Owner:** CTO / Principal Platform Engineering
- **Effective:** 2026-11-19
- **Scope:** All RunGrow products, APIs, agents, workers, data stores, integrations and production changes

This is the permanent minimum release standard. Product-specific controls may strengthen these gates but must not weaken them. A passing CI pipeline is necessary evidence, not production authorization.

## Release architecture: three layers

### Release 1 — Workforce Foundation
Proves the shared RunGrow control plane: identity, tenant isolation, durable work execution, approvals, observability, health probes, recovery, and secure operator access.

### Release 2 — Product Integration
Proves each product adapter against its real product backend, tenant mapping, provider credentials, webhook security, idempotent external effects, and product-specific end-to-end workflows. A registered adapter or configured secret alone is not proof of connectivity.

### Release 3 — Commercial Operations
Proves customer onboarding, entitlement and billing, compliant prospect and customer communications, support operations, commercial reporting, revenue reconciliation, and governed AI-assisted execution.

The three layers are cumulative. Release 2 depends on Release 1; Release 3 depends on Release 1 and the applicable Release 2 product integrations.

## The seven mandatory release gates

### Gate 1 — Architecture & Release Integrity
- [ ] Candidate commit SHA, build artifact digest, target environment and deployment record are traceable.
- [ ] PR review and required CI checks pass on the exact candidate commit.
- [ ] API, worker, job and client versions are deployment-compatible.
- [ ] Database migrations are ordered, checksum-verified, repeatable and recovery-aware.
- [ ] Production contains no test-only, demo, process-local or in-memory business persistence.
- [ ] Configuration is environment-specific; secrets are not committed or exposed to clients/logs.
- [ ] Rollback or forward-recovery procedure is documented and executable.

### Gate 2 — Security & Tenant Isolation
- [ ] OIDC/JWT validates signature, issuer, audience, expiry and required identity claims.
- [ ] RBAC and product entitlements are enforced server-side for every sensitive operation.
- [ ] Tenant scope is derived from trusted identity and enforced across database, cache, queue, files, exports and worker execution.
- [ ] Cross-tenant negative tests cover reads, writes, search, exports, webhooks and asynchronous jobs.
- [ ] Service identities are scoped, authenticated, rotated and least-privileged.
- [ ] Webhook HMAC/signature, timestamp and replay protections are verified.
- [ ] Secrets and sensitive data are excluded from source, logs, browser bundles and error responses.
- [ ] Human approvals enforce separation of duties, payload binding, expiry and single-use consumption.

### Gate 3 — Backend & Integration Readiness
- [ ] Production PostgreSQL connectivity, TLS, schema and required privileges are verified.
- [ ] Durable queues, worker claims/leases, retries, timeouts and dead-letter behavior are tested.
- [ ] Every claimed production adapter reaches its actual product backend.
- [ ] Provider credentials, tenant/account mapping, scopes and callback URLs are verified in the target environment.
- [ ] External effects are idempotent or protected by durable deduplication.
- [ ] Provider failures, rate limits and transient errors are handled without false-success responses.
- [ ] Unsupported or unconfigured integrations fail closed and create an audit failure.

### Gate 4 — AI Workforce Acceptance
- [ ] Agent execution preserves authenticated actor, tenant, product, task and correlation context.
- [ ] Agent and tool selection is constrained by server-side policy and typed input/output schemas.
- [ ] Untrusted prompts and retrieved content cannot override policy or grant permissions.
- [ ] Configured high-impact, financial, irreversible and external communication actions require valid human approval.
- [ ] Sandbox mode cannot perform production side effects.
- [ ] Mandatory AI identity disclosure is present in outbound communications.
- [ ] Ambiguous, failed and low-confidence actions safely stop or escalate.
- [ ] Conversation continuity, tool calls, approvals and outcomes are auditable without exposing secrets.

### Gate 5 — Reliability & Disaster Recovery
- [ ] Liveness and readiness probes reflect process state and critical dependencies respectively.
- [ ] Worker heartbeat is current and reports status and processed/dead-letter counts.
- [ ] Structured logs, metrics, traces, correlation IDs and actionable alerts cover critical paths.
- [ ] Capacity, concurrency, timeout, backpressure and graceful-degradation tests pass.
- [ ] Worker restart and in-flight task recovery are verified.
- [ ] Backups, retention and an actual restore test are evidenced.
- [ ] Deployment rollback or forward recovery is tested.

### Gate 6 — Product & Commercial Readiness
- [ ] Product-specific customer journeys pass end-to-end acceptance.
- [ ] Tenant provisioning, company-admin bootstrap, role assignment and offboarding are verified.
- [ ] Pricing, entitlements, billing and usage metering are validated where applicable.
- [ ] Prospect/customer communications honor consent, suppression and channel permissions.
- [ ] Support ownership, escalation and incident communication are operational.
- [ ] Revenue, customer, pipeline and capital metrics reconcile to authoritative source systems.
- [ ] Product-specific release evidence is recorded for every enabled vertical.

### Gate 7 — Final Release Authorization
- [ ] All six preceding gates have commit-bound, environment-bound, time-bound evidence.
- [ ] Automated evidence is distinguished from human signoff and mocked/sandbox verification.
- [ ] Engineering, Security, Integration, Reliability, AI Governance, Operations and Business gate signoffs are recorded.
- [ ] All S0/S1 blockers are closed; eligible S2/S3 exceptions have a compensating control, owner, due date and approver.
- [ ] Engineering and product owners confirm acceptance.
- [ ] Release approver records GO or NO_GO and the rationale.
- [ ] Post-deployment smoke tests and observation window are complete.

## Release decision policy

**GO** requires all mandatory evidence for the requested release layer and no hard-stop issue. **NO_GO** applies when evidence is missing, a critical control fails, or an integration reports success without confirmed execution. Non-critical deferrals must be explicit, time-bound and accepted; they cannot waive tenant isolation, authorization, critical security, data integrity, recovery, approval enforcement or artifact traceability.

The readiness API reports missing evidence as missing. It must never infer or fabricate a PASS from an adapter registration, a configured secret, a successful mock, or an old heartbeat.

## Evidence and signoff record

Every gate signoff records release layer, gate name, status, evidence reference, verifier identity/type, timestamp and candidate commit/environment metadata. Signoffs are append-only. Corrections are new records, not edits to historical evidence.

Allowed release layers: `RELEASE_1_WORKFORCE`, `RELEASE_2_PRODUCTS`, `RELEASE_3_COMMERCIAL`.

Allowed gate names: `ENGINEERING`, `SECURITY`, `INTEGRATION`, `RELIABILITY`, `AI_GOVERNANCE`, `OPERATIONS`, `BUSINESS`.

## Operational ownership

The CTO owns this standard. Engineering owns technical evidence, Security owns identity/privacy controls, Product owns workflow acceptance, Operations owns recovery and monitoring, and the release approver owns the final decision. This standard is reviewed quarterly and after every Severity 1 incident.
