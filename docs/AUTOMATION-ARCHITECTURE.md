# RunGrow Automation Architecture

## System boundaries

The public RunGrow website is the acquisition surface. The Revenue Operations service is the system of record for prospects, accounts, opportunities, activities, consent, tasks and attribution. Product applications remain independent and expose only approved integration contracts. Never share product databases or customer records across tenants.

## Event-driven lead lifecycle

1. Website forms, product enquiry CTAs, approved WhatsApp Business integrations, inbound email, partner referrals, events and manually imported lists emit a LeadCaptured event.
2. Validate payload, anti-spam signals, idempotency key and consent evidence. Store the raw source event and normalized lead separately.
3. Deduplicate by normalized email/phone plus tenant and account identity; never merge records across tenants.
4. Enrich from approved sources. Store source, observed-at timestamp and confidence for every enrichment field. Unknown is a valid value.
5. Score fit, intent and engagement independently. Explain each score with reason codes; never use sensitive attributes.
6. Route by product interest, segment pod, territory, language, account ownership and capacity. Apply deterministic fallback queue if no rule matches.
7. Start the relevant cadence only when permission and channel eligibility are verified.
8. Book meetings through calendar availability and write confirmed events back to the CRM.
9. Generate discovery briefs, demo plans and proposal drafts from approved product catalogue data.
10. Create onboarding handoff only after a verified Closed Won event.
11. Monitor activation, usage, support signals and renewal windows; create CSM tasks and expansion recommendations.

## Agent registry

| Agent | Inputs | Permitted actions | Approval boundary |
|---|---|---|---|
| Lead Intake | Form, inbox, WhatsApp webhook | Validate, normalize, deduplicate, create lead | Quarantine ambiguous or suspicious records |
| Research & Enrichment | Business name, domain, public sources | Add sourced firmographic facts | No invented facts or sensitive profiling |
| Qualification | Lead context and product fit rules | Score, ask approved questions, route | Human validates qualification before opportunity creation |
| Conversation Assistant | Approved channel messages, catalogue, FAQs | Answer factual questions, collect requirements, draft follow-ups | Escalate pricing exceptions, complaints, security and legal questions |
| Scheduling | Calendar availability and meeting policy | Offer slots and create confirmed meetings | Never fabricate availability |
| Sales Copilot | CRM history, product demo assets | Prepare briefs, objection responses and proposal drafts | Human approves external proposal and commitments |
| Renewal & Adoption | Product usage events and contract dates | Detect risk, recommend actions, create CSM tasks | Human owns retention and commercial action |
| RevOps Monitor | Pipeline events and CRM data | Detect stale records, SLA breaches and routing anomalies | No silent deletion or reassignment without audit |

## Workflow reliability

- Use a transactional outbox for domain events and idempotent consumers.
- Persist workflow state, attempts, next-run time, last error and correlation ID.
- Retry transient failures with exponential backoff and jitter; dead-letter permanent failures for review.
- Enforce per-tenant and per-channel rate limits.
- Record actor type (human, agent, system), policy version, input references, output summary and approval decision.
- Use signed webhook verification, timestamp tolerance and replay protection.
- Keep secrets in managed environment variables; never expose provider tokens to browser code.
- Apply tenant_id filters at the database boundary and verify authorization in every API handler.
- Maintain an append-only audit trail for consent, exports, access changes, approvals and commercial edits.

## WhatsApp and messaging

Use the official WhatsApp Business Platform or an authorised BSP. Inbound webhooks must verify signatures and deduplicate message IDs. Outbound templates must comply with current template/category and opt-in rules. Store message status callbacks. Support human takeover, conversation assignment, quiet hours, opt-out keywords and an auditable hand-back to the AI agent. Do not use unofficial WhatsApp Web automation.

## Data model (logical)

- tenants, users, roles, role_permissions
- accounts, contacts, leads, lead_sources, lead_consents
- products, product_packages, price_books, territories, routing_rules
- opportunities, opportunity_stage_history, activities, tasks, meetings
- campaigns, campaign_touches, attribution_events
- conversations, messages, channel_templates, suppression_entries
- agent_runs, agent_approvals, workflow_runs, outbox_events, dead_letters
- proposals, contracts, subscriptions, onboarding_projects
- customer_health_snapshots, renewal_opportunities, support_cases
- audit_events

All business entities are tenant-scoped. Use foreign keys, unique idempotency constraints, row-level security where supported, and explicit retention policies.

## Phased delivery

### Foundation
CRM schema, authentication/RBAC, tenant isolation, audit log, lead intake API, consent/suppression, pipeline UI, manual assignment and activity timeline.

### Revenue automation
Scoring, routing, SLA timers, task generation, email/calendar integration, templates, dashboards and agent approval queue.

### Conversational sales
Official WhatsApp integration, knowledge-grounded product answers, qualification flows, human takeover and conversation-to-opportunity handoff.

### Growth and retention
Campaign attribution, partner portal, trial activation events, customer health, renewal workflows, expansion recommendations and forecasting.

External provider credentials are deployment configuration, not a reason to postpone local business logic, fixtures or contract tests.
