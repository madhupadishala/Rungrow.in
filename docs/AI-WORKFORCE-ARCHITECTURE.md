# RunGrow AI Workforce Architecture

## Approved decisions
- Controlled autonomy: AI employees operate within least-privilege role permissions. High-impact actions require explicit human approval.
- Voice and text: both resolve to the same authenticated command and task APIs.
- All departments: 11 departments, 42 role profiles, with AI and human employees sharing reporting lines and task ownership.
- AI identity is disclosed where relevant. Agents never impersonate human employees.

## Boundaries
- Existing marketing site remains static HTML/CSS/JS.
- /workforce/ is the operator command centre with isolated styles and scripts.
- workforce-api/ is an independent service boundary for JWT authentication, role registry, authorization, approvals, memory contracts and audit events.
- PostgreSQL is the durable source of truth. migrations/001_workforce.sql defines tenant-scoped tables and row-level policies.
- LiveKit Agents is the voice runtime; Crawlee is the public-source research adapter. AgentSDR, OpenCloser and OpenOutreach are reference patterns, not copied applications.

## Employee contract
Each employee has a stable role ID, department, manager, mission, skills, tool scopes, approval boundary and memory scope. Agent runs receive a task and explicit context references. Natural-language prompts cannot grant permissions or borrow another employee's identity.

## Memory hierarchy
Tenant memory contains approved company facts, product catalogues and decisions. Department memory contains procedures, targets and playbooks. Role memory contains methods and bounded preferences. Task memory is short-lived and source-linked. Retrieval must filter tenant and scope before semantic ranking. Sensitive personal data is excluded by default; deletion must propagate to derived indexes and backups according to retention policy.

## Authorization
JWT claims identify user and tenant. Authorization is evaluated server-side. Production identity provider should supply MFA, SSO, SCIM and lifecycle controls. PostgreSQL RLS is a second boundary, not a replacement for API checks. Agent tool calls use their own scoped service identity, never the founder's token.

Approval gates include outbound contact, proposal dispatch, pricing changes, discounts, contracts, payments, exports, user/role changes, deployments, campaign activation and destructive actions. Approval records are tenant-scoped and auditable. Execution must revalidate consent and policy at the worker boundary.

## Delivery truth
The UI, role catalogue, API contract, local command router, JWT validation, policy checks, approval endpoints and schema migration are implemented. The API currently uses an in-memory adapter for local prototyping; it is not production persistence. Production enablement requires PostgreSQL repository wiring, transactional outbox/worker, trusted identity provider, secrets manager, model gateway and approved channel integrations. Local command acceptance is not live agent execution.

## Repository structure
- workforce/index.html, workforce/styles/workforce.css, workforce/scripts/workforce.js
- workforce-api/app/: config, auth, policy, registry, API and schemas
- workforce-api/migrations/: PostgreSQL tenant schema and RLS
- workforce-api/tests/: policy and registry contract tests
