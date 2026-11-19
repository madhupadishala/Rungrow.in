# RunGrow Enterprise Release Governance Standard

- **Document ID:** RG-ENG-REL-001
- **Version:** 1.0
- **Owner:** CTO / Engineering
- **Applies to:** Every RunGrow product, service, agent, integration, database change, infrastructure change, and production release
- **Effective date:** 2026-11-19
- **Review cadence:** Quarterly and after every Severity 1 incident

## 1. Purpose and authority

This standard defines the minimum evidence required to release and operate RunGrow software in production. It applies across the RunGrow portfolio, including independently deployed product repositories. Product teams may add stricter domain-specific gates; they may not weaken the mandatory controls in this standard.

A passing CI run is necessary but is not, by itself, production approval. Release decisions must be based on evidence tied to the exact candidate commit and target environment.

## 2. Release outcomes

Every release receives exactly one decision:

- **GO:** All mandatory gates pass; evidence is recorded; required approvers authorize release.
- **CONDITIONAL GO:** Only non-critical items remain, each with a documented risk, compensating control, named owner, due date, and explicit approver acceptance. This decision cannot waive a hard-stop gate.
- **NO-GO:** Any hard-stop gate fails, evidence is missing, or a material risk is not accepted.

A release is not approved merely because a deployment pipeline completed successfully.

## 3. Non-waivable hard-stop gates

A release is **NO-GO** if any of the following is true:

1. Cross-tenant data access, tenant-context confusion, or authorization bypass is possible.
2. Authentication, service identity, secrets handling, or privileged access has a known critical weakness.
3. Data integrity, migration safety, backup recoverability, or durable task integrity is not demonstrated for the change.
4. A critical business action can execute without required authorization, approval, or idempotency protection.
5. A security-sensitive webhook or provider callback can be forged or replayed without effective controls.
6. A production path reports success when the underlying action was not performed or was not confirmed.
7. A known Severity 1 / critical defect remains open without an approved containment that removes exposure.
8. The release candidate cannot be identified by immutable commit SHA or its deployed artifact cannot be traced to that SHA.

No individual may waive these gates through a checklist exception. A failed hard-stop must be fixed and re-evidenced before release.

## 4. Mandatory release gates

### G1 — Source, build, and change integrity

- [ ] Release commit SHA, branch, artifact, and target environment are recorded.
- [ ] PR review is complete; required checks pass on the exact candidate commit.
- [ ] Dependency and build integrity checks pass; no unexplained generated or vendored changes.
- [ ] Configuration is environment-specific; no test/demo defaults are enabled in production.
- [ ] Database migrations are reviewed, ordered, repeatable where applicable, and rollback/forward-recovery behavior is documented.
- [ ] Deployment order and compatibility between API, workers, jobs, and clients are verified.
- [ ] Rollback or forward-fix procedure is documented and executable.

### G2 — Identity, authorization, and tenant isolation

- [ ] Authentication validates issuer, audience, signature, expiry, and required claims.
- [ ] RBAC/ABAC permissions are enforced server-side at each sensitive operation.
- [ ] Tenant scope is derived from trusted identity and enforced in queries, writes, caches, queues, files, exports, and background jobs.
- [ ] Cross-tenant negative tests pass for read, write, search, export, and asynchronous execution paths.
- [ ] Service-to-service identities are scoped, rotated, and least-privileged.
- [ ] Secrets are absent from source, logs, client bundles, and error responses.
- [ ] Privileged and sensitive actions have explicit approval controls where required.
- [ ] Audit events identify actor/service, tenant, action, resource, timestamp, outcome, and correlation ID.

### G3 — Data, persistence, and recovery

- [ ] Production database connectivity and required extensions/permissions are verified.
- [ ] Critical writes use durable persistence; no process-local or in-memory store backs production business state.
- [ ] Transactions, concurrency controls, idempotency keys, and uniqueness constraints protect critical workflows.
- [ ] Queue claim/lease, retry, timeout, dead-letter, and worker-restart behavior are tested.
- [ ] Backup schedule and retention are configured and a restore test is evidenced.
- [ ] Data retention, deletion, export, and privacy obligations are addressed for the product.
- [ ] Migration failure and partial-deployment recovery are understood.

### G4 — Integrations and external effects

- [ ] Every claimed production integration is connected to its real product/provider endpoint.
- [ ] Credentials, tenant/account mapping, scopes, webhook URLs, and callback verification are validated in the target environment.
- [ ] Provider timeouts, rate limits, transient errors, permanent errors, and retries are handled.
- [ ] Duplicate delivery and webhook replay do not duplicate business effects.
- [ ] External side effects are idempotent or protected by durable deduplication.
- [ ] Unsupported, dry-run, queued, or unconfirmed operations never return a false success state.
- [ ] Integration tests include deterministic local fixtures/mocks and target-environment verification where real provider behavior matters.

### G5 — AI agent and automation governance

- [ ] Agent execution preserves authenticated actor, tenant, product, task, and correlation context.
- [ ] Agent/tool selection and tool permissions are constrained by server-side policy.
- [ ] Tool arguments and outputs are validated against typed schemas.
- [ ] Human approval is required for configured high-impact, financial, external-communication, or irreversible actions.
- [ ] Prompt injection and untrusted retrieved content cannot override system policy or grant permissions.
- [ ] Ambiguous, failed, and low-confidence actions fail safely or escalate to a human.
- [ ] Conversation/task context is durable where continuity is a product requirement.
- [ ] Agent decisions, tool calls, approvals, and outcomes are auditable without logging secrets or unnecessary sensitive content.
- [ ] Duplicate messages, retries, and resumed tasks cannot repeat a non-idempotent action.

### G6 — Reliability, performance, and observability

- [ ] Liveness and readiness checks reflect actual service dependencies appropriately.
- [ ] Structured logs, metrics, traces, and correlation IDs cover critical request and job paths.
- [ ] Alerts exist for service availability, elevated errors, queue age/backlog, provider failures, and database saturation.
- [ ] Capacity and concurrency tests cover expected launch load and agreed headroom.
- [ ] Timeouts, backpressure, circuit breaking, and graceful degradation are defined for critical dependencies.
- [ ] Worker restart, queue recovery, and service restart behavior are tested.
- [ ] Incident owner, escalation path, and customer communication path are known.

### G7 — Product and commercial acceptance

- [ ] Core customer journeys pass end-to-end acceptance tests.
- [ ] Tenant provisioning, administrator bootstrap, role assignment, and offboarding are verified.
- [ ] Subscription, entitlements, pricing, billing, and usage metering are validated where applicable.
- [ ] Customer-facing communications and consent/permission requirements are verified.
- [ ] Support ownership, escalation, and operational runbooks are available.
- [ ] Product analytics and commercial metrics reconcile to authoritative source systems.
- [ ] Feature flags, staged rollout, and customer impact controls are configured where appropriate.

### G8 — Release authorization and post-release verification

- [ ] Release evidence is linked in the release record.
- [ ] Engineering owner confirms technical readiness.
- [ ] Security/privacy owner confirms relevant controls for material changes.
- [ ] Product/business owner confirms customer workflow acceptance.
- [ ] Release approver records GO / CONDITIONAL GO / NO-GO.
- [ ] Deployment is monitored against defined health and error thresholds.
- [ ] Smoke tests pass against the deployed version.
- [ ] Rollback/containment is initiated if agreed thresholds are breached.
- [ ] Release outcome and incidents are recorded.

## 5. Severity and exception policy

| Classification | Meaning | Release treatment |
|---|---|---|
| S0 — Hard stop | Tenant isolation, authorization, critical security, data integrity, irreversible unauthorized action, or untraceable artifact failure | NO-GO; never waivable |
| S1 — Critical | Core production workflow unavailable, major data loss/corruption risk, or broad customer impact | NO-GO until fixed or exposure is demonstrably contained |
| S2 — Major | Material degradation with a viable workaround and no hard-stop exposure | Conditional GO only with owner, mitigation, due date, and approver |
| S3 — Minor | Limited impact, cosmetic, or low-risk operational gap | May be deferred with owner and target date |

Every exception must state: control ID, affected product/environment, risk and customer impact, evidence, compensating control, owner, due date, approver, and expiry/review date. Exceptions cannot waive Section 3.

## 6. Evidence quality

Evidence must be:
- **Commit-bound:** identifies the exact source SHA and, where relevant, artifact digest.
- **Environment-bound:** identifies staging/production and relevant tenant/provider context without exposing secrets.
- **Time-bound:** includes execution or verification timestamp.
- **Reproducible:** links to CI runs, test reports, dashboards, traces, migration reports, restore records, or signed acceptance.
- **Truthful:** distinguishes mocked tests, sandbox provider tests, and live production-provider verification.

Never paste credentials, tokens, private keys, patient/customer sensitive data, or unredacted personal data into release records.

## 7. Release record and sign-off

Use [the Release Acceptance Record](../releases/RELEASE_ACCEPTANCE_TEMPLATE.md) for every production release. If a product has a regulated or domain-specific release process, attach that evidence and retain this standard's common gates.

Minimum sign-off roles:
- **Engineering owner:** source, build, data, integration, reliability evidence.
- **Security/privacy owner:** identity, authorization, tenant isolation, sensitive data, threat controls.
- **Product/business owner:** workflow and customer acceptance.
- **Release approver:** final GO / CONDITIONAL GO / NO-GO decision.

One person may hold multiple roles in a small team, but the evidence and decisions must still be explicitly recorded. The release approver must not approve a hard-stop failure.

## 8. Portfolio adoption

The RunGrow Workforce platform repository is the control-plane source of this standard. Each product repository must:
1. Link to this document from its engineering/release documentation.
2. Use the release acceptance record or an equivalent product-specific record containing every mandatory gate.
3. Add product-specific tests and controls without weakening the common gates.
4. Preserve release evidence with the product's source and operational records.

## 9. Review and change control

Changes to mandatory gates require CTO/Engineering approval and a version update. Review this standard quarterly, after a Severity 1 incident, and when a material architecture, regulatory, provider, or threat-model change occurs.
