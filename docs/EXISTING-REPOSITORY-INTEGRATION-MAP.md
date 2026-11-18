# RunGrow existing repository integration map

**Review date:** 18 November 2026  
**Purpose:** Build the RunGrow portfolio by composing existing product repositories and shared capabilities. This is not a greenfield rebuild plan.

## Operating rule

Product repositories remain independently owned and deployable unless a deliberate architecture decision approves a merge. Reuse bounded modules through versioned APIs, packages, adapters or controlled code extraction. Do not copy entire applications into the RunGrow workforce repository.

## Repository inventory and proposed reuse

| Repository | What is present in the inspected source | Reuse boundary | Current decision |
|---|---|---|---|
| `madhupadishala/Rungrow.in` | RunGrow marketing site, Workforce operator UI, FastAPI workforce service, role registry, policy/approval contracts, PostgreSQL/RLS migration and Crawlee adapter | Company operating system, AI employee registry, orchestration, governance and shared workforce UI | Keep as the company-level workforce/control plane. Do not absorb product applications into it. |
| `madhupadishala/studio-growth-os` | Next.js 14 + Tailwind 3 web workspace, FastAPI service, Prisma/PostgreSQL schema, lead/quote/booking/conversation/agent/webhook/payment modules, Redis/Celery dependencies | Photo Studio Growth OS product domain: CRM, quote/pricing, booking, channel connectors, customer workspace | Primary existing product source for Photo Studio Growth OS and billing-related capabilities. Reuse modules after security, tenant, migration and test review. |
| `madhupadishala/studio-x-photography` | Next.js 15 application, Neon serverless PostgreSQL, R2 upload support, tenant onboarding and owner workspace described in README; SQL schema includes tenants, memberships, leads, consent and channel metadata | Studio-specific tenant website/experience, onboarding, lead intake, WhatsApp and social attribution | Keep as a tenant-facing implementation/reference. Reconcile its schema and capabilities with Studio Growth OS before consolidating any code. |
| `madhupadishala/conversion-agent-platform` | TypeScript conversion domain state machine, provider ports, lead qualification, slot holds, payment, reminders, audit and governed voice-agent flow; current runtime store is in-memory and bootstrap config is single-test-tenant oriented | Shared conversion orchestration patterns for inbound sales across product verticals | Reuse the domain workflow and provider-port patterns selectively. Replace in-memory persistence, generalise tenant/product config and add RunGrow approval/consent/RBAC contracts before production use. |
| `madhupadishala/creativ-i-studios` | Next.js 15, Tailwind 4, Prisma 6 and synthetic Studio X preview; README explicitly says the preview contains no real lead storage, database, admin or live integrations and is not the full agency source ZIP | Visual/design reference and potential Tailwind 4 component patterns only | Not a production backend or authoritative source for Studio Growth OS. Reuse visual components only after inspecting the exact files and confirming ownership. |

## Product mapping

### Photo Studio Growth OS / Growth Pro

- Core product source: `studio-growth-os`.
- Tenant experience and onboarding reference: `studio-x-photography`.
- Design-only reference: `creativ-i-studios`.
- Shared conversion flow candidate: `conversion-agent-platform`.
- Do not create another CRM, quote engine, booking engine, or WhatsApp qualification engine in the Workforce repo when these capabilities already exist in the product sources.

### Photo Studio Billing Software

- Start by inspecting billing, quote, payment, invoice and reconciliation modules in `studio-growth-os`.
- Reuse the existing domain and data model where it meets requirements; do not create a second ledger or pricing source without a documented reason.
- Validate invoice numbering, GST/tax treatment, credit notes, payment reconciliation, audit and role boundaries before commercial release.

### Other RunGrow product lines

No repository mapping is asserted for Clinics / Counter, Entrepreneur / Instagram Business Tools, Pharma Software & Utilities, Artists' Business Tools or Growtoys in this first pass. Discover their existing repositories before implementing new product logic. Do not infer that a TheClinixAI repository belongs to RunGrow or copy it across the company boundary without explicit approval.

## Cross-repository integration contract

1. **Identity:** Use a trusted RunGrow identity/tenant context. A product app must not trust a browser-supplied tenant ID or permission list.
2. **Tenant model:** Keep customer tenants isolated inside each product. RunGrow internal workforce tenant and product ownership are separate from customer tenants.
3. **Product scope:** Every task, agent run, customer record and event must carry product ID and tenant ID where applicable.
4. **Events:** Use idempotent, signed webhooks or a transactional outbox. Do not poll or directly mutate another product's database.
5. **Approvals:** Product services enforce their own action policy at the execution boundary. Workforce approvals are evidence and orchestration, not a substitute for product-side authorisation.
6. **Data:** No cross-tenant memory or training. Product learning uses only approved tenant-scoped material or properly anonymised aggregates.
7. **Contracts:** Publish versioned API schemas and event contracts; use adapters to isolate differences between FastAPI, Next.js and TypeScript services.
8. **Operations:** Each repo retains its own tests, migration history, deployment lifecycle and rollback path. Shared packages must be versioned and tested against every consumer.
9. **Licensing:** Verify the exact source commit and license before copying code. Preserve notices. GPL-3.0 sources remain reference-only pending legal review.
10. **Evidence:** A README capability statement is a discovery lead, not proof of production operation. Mark integrations as implemented, simulated, unverified or live-verified.

## Immediate engineering sequence

1. Preserve all product repos and their current deployment boundaries.
2. Complete a module-level audit of Studio Growth OS and Studio X: auth, tenant isolation, pricing, quote/booking, webhook signature, consent, outbox, payment and tests.
3. Select one canonical product data model for photo studio; build explicit migrations/adapters rather than merging schemas blindly.
4. Extract the reusable conversion state machine behind a RunGrow-owned interface and replace its in-memory store with durable persistence.
5. Connect the Workforce Digital CEO and Product Growth Managers to versioned product APIs and event feeds; never give them direct database credentials.
6. Repeat repository discovery for each of the other six product lines before writing new product code.
