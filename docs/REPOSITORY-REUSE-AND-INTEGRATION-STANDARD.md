# RunGrow repository reuse and integration standard

**Status:** Day 1 engineering standard  
**Applies to:** RunGrow products, internal workforce, integrations and shared platform code

## 1. Default engineering decision

RunGrow uses an **inspect → reuse → combine → adapt → harden → integrate** workflow. New implementation is justified only when an existing component cannot meet the functional, security, licensing, reliability or maintainability requirement.

Do not rebuild a working product or duplicate a capability already present in another RunGrow repository. Search the connected GitHub repositories and approved upstream repositories before creating a new module.

## 2. Repository discovery and selection

For each requested capability:

1. Search RunGrow-owned repositories first for existing implementations, schemas, UI components, API contracts, migrations, tests and deployment patterns.
2. Search public upstream repositories for mature components that solve the same bounded problem.
3. Inspect the actual source, dependency graph, maintenance activity, open security issues, license and NOTICE requirements. A README or star count is not sufficient.
4. Prefer extracting a small, well-bounded module or adopting a maintained dependency over copying an entire application.
5. Record the exact upstream repository, immutable commit/tag, license, files reused, modifications, attribution and local tests.
6. Keep an adapter boundary around replaceable providers and external services.

## 3. Approved integration principles

- Preserve RunGrow-owned product logic, tenant model, RBAC, audit trail, approvals and commercial policy.
- Reuse infrastructure and specialist capabilities; do not inherit another project's assumptions about user identity, tenant ownership, data retention or permission scope.
- Never copy secrets, production customer records, proprietary assets or private source code into an upstream project.
- Do not import code until its license is verified at the exact commit being used.
- Keep upstream license and NOTICE files where required. Record modifications and source provenance.
- GPL/AGPL and other reciprocal or network-copyleft licenses require a separate legal and distribution-model review before incorporation.
- Treat public data as untrusted, retain provenance and respect site terms, robots rules, access controls and applicable privacy/marketing rules.
- No public contact detail is treated as marketing consent.
- Keep each component modular, testable and replaceable. Do not merge unrelated UI, CSS, scripts or backend domains into monoliths.

## 4. Initial source review register

| Source | Intended bounded use | License / status | RunGrow decision |
|---|---|---|---|
| `livekit/agents` | Realtime voice sessions, turn handling, telephony and agent lifecycle | Apache-2.0 framework; model assets may have separate terms | Approved candidate for voice adapter; pin version and review model/provider terms |
| `apify/crawlee` | Public-source crawling and extraction | Apache-2.0 | Existing adapter retained; use tenant allowlists, robots checks, quotas and provenance |
| `Kandid-ai/AgentSDR` | SDR workflow patterns, lead pipeline and human-reviewed action queue | MIT per repository page | Reference/adapt selectively; inspect current source and preserve MIT notices |
| `eracle/OpenOutreach` | B2B prospect research workflow patterns | GPL-3.0 per repository page | Reference only pending legal review; do not copy or link code into a proprietary distributed product by default |
| `OpenCloser v2` | Voice-sales workflow reference | Exact canonical repository/license not verified | Not approved for code reuse until exact source and license are verified |

This register is a discovery snapshot, not a blanket approval to vendor upstream code. The exact commit and license text must be checked before code is imported.

## 5. RunGrow architecture boundaries

- `/` marketing website remains isolated from workforce application changes.
- `/workforce/` is the operator interface; component styles and scripts stay modular.
- `workforce-api/` owns authentication, authorization, employee registry, task orchestration, approvals and audit.
- `workforce-crawler/` is a bounded research adapter; it does not authorize outreach.
- PostgreSQL with tenant-scoped transactions and RLS is the durable data boundary.
- Voice, model providers, search providers, CRM and communication channels are adapters behind RunGrow-owned contracts.

## 6. Required evidence for every reuse

Each integration PR must include: upstream URL, commit/tag, license identifier and license-file reference, selected files/modules, changes made, dependency/security review, data-flow and tenant-boundary review, tests and rollback/removal path.

## 7. Prohibited shortcuts

- Copying a whole SaaS application and relabelling it.
- Importing code based only on a README or an AI-generated summary.
- Assuming all files in a repository share the same license.
- Mixing customer data across tenants or training one tenant's agent on another tenant's data.
- Using browser automation to bypass platform access controls or messaging policies.
- Replacing an existing RunGrow module before a migration and regression plan exists.
