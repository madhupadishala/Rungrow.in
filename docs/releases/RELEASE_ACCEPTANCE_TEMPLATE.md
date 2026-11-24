# RunGrow Production Release Acceptance Record

> Complete one record per production release. Link evidence; do not paste secrets or sensitive customer data. This record supplements RG-ENG-REL-001 and does not replace product-specific acceptance.

## Release identity

| Field | Value |
|---|---|
| Product / service | |
| Repository | |
| Release version / tag | |
| Candidate commit SHA | |
| Artifact digest / image tag | |
| Target environment | |
| Planned deployment window | |
| Engineering owner | |
| Product/business owner | |
| Security/privacy reviewer | |
| Release approver | |

## Gate evidence

For each gate, select **PASS**, **FAIL**, **N/A** (with rationale), or **EXCEPTION** (only for eligible non-critical items). Link the evidence and record the verifier/date.

| Gate | Status | Evidence URL / report | Verifier / date | Notes / exception ID |
|---|---|---|---|---|
| G1 Source, build, change integrity | | | | |
| G2 Identity, authorization, tenant isolation | | | | |
| G3 Data, persistence, recovery | | | | |
| G4 Integrations and external effects | | | | |
| G5 AI agent and automation governance | | | | |
| G6 Reliability, performance, observability | | | | |
| G7 Product and commercial acceptance | | | | |
| G8 Release authorization and post-release verification | | | | |

## Mandatory hard-stop attestation

- [ ] No known cross-tenant data exposure or authorization bypass.
- [ ] No unresolved critical security or privileged-access weakness.
- [ ] Data integrity, migration safety, and recovery are evidenced.
- [ ] Critical business actions are authorized and idempotency-protected.
- [ ] Security-sensitive callbacks are authenticated and replay-protected.
- [ ] No unsupported or unconfirmed operation is represented as successful.
- [ ] No unresolved S0/S1 issue remains without effective containment.
- [ ] Deployed artifact is traceable to the recorded candidate SHA.

Any unchecked item means **NO-GO**.

## Exceptions (S2/S3 only)

| Control / issue | Risk and customer impact | Compensating control | Owner | Due date | Approver | Expiry / review |
|---|---|---|---|---|---|---|
| | | | | | | |

Do not use this table to waive a hard-stop gate.

## Deployment and post-release verification

| Verification | Result | Evidence |
|---|---|---|
| Deployment completed with expected artifact | | |
| Database migration outcome verified | | |
| API health/readiness verified | | |
| Worker/job processing verified | | |
| Authentication and tenant-scoped smoke tests passed | | |
| Critical customer journeys passed | | |
| External provider callbacks/effects verified | | |
| Error rate, latency, queue age and resource signals within thresholds | | |
| Rollback/containment readiness confirmed | | |

## Final decision

- [ ] **GO**
- [ ] **CONDITIONAL GO** — non-critical exceptions documented and explicitly accepted
- [ ] **NO-GO**

**Decision rationale:**

**Approver name / role:**

**Approval timestamp:**

**Post-release observation window and owner:**

**Incident / rollback reference (if applicable):**
