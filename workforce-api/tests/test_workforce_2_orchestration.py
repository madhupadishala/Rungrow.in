"""PostgreSQL 16 integration coverage for Workforce 2.1 orchestration and RLS."""
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestration import WorkforceOrchestrator
from app.policy import Principal
from app.repositories import PostgresWorkforceRepository
from app.tool_registry import WorkforceToolRegistry
from app.commercial_intelligence import DiscoveryBatch

DB_URL = os.getenv("RUNGROW_TEST_DATABASE_URL")
pytestmark = pytest.mark.asyncio


class FixtureDirectoryFetcher:
    allow_domains = {"directory.example.test"}

    async def discover(self, *, query, city, product_id, source_urls):
        return DiscoveryBatch(candidates=[{
            "business_name": "Hyderabad Example Photography Studio",
            "segment": "wedding photography studio",
            "city": city,
            "source_url": "https://directory.example.test/studio",
            "evidence_snippet": "Public photography studio directory listing",
            "evidence_urls": ["https://directory.example.test/studio"],
        }], blocked=[])


@pytest.mark.asyncio
async def test_founder_directive_executes_department_graph_and_single_use_approval():
    assert DB_URL, "CI must supply the PostgreSQL 16 RUNGROW_TEST_DATABASE_URL"
    engine = create_async_engine(DB_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = "w2-integration-" + uuid4().hex
    principal = Principal("founder-requester", tenant, "founder",
        frozenset({"*"}), product_ids=frozenset({"photo-studio-growth-os"}))
    try:
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal) as repo:
                service = WorkforceOrchestrator(repo, principal, WorkforceToolRegistry(directory_fetcher=FixtureDirectoryFetcher()))
                created = await service.submit_directive(
                    "Prepare a campaign and lead pipeline for Photo Studio Growth OS in Hyderabad https://directory.example.test/list",
                    "photo-studio-growth-os")
                assert created["department_count"] == 5
                # Simulate one manager revision request, then a passing specialist resubmission.
                original_acceptance = service._acceptance
                review_attempts = {"legal": 0}
                def acceptance(tool_name, result):
                    if tool_name == "compliance_disclosure_check" and review_attempts["legal"] == 0:
                        review_attempts["legal"] += 1
                        return False
                    return original_acceptance(tool_name, result)
                service._acceptance = acceptance
                completed_orders = 0
                while True:
                    order = await service.claim_work_order("integration-worker")
                    if order is None:
                        break
                    assert order["locked_by_employee_id"] == "integration-worker"
                    assert order["lock_expires_at"] is not None
                    assert order["max_budget_cents"] >= order["spent_cents"]
                    outcome = await service.execute_work_order(order)
                    assert outcome["status"] in {"APPROVED_BY_HEAD", "REVISION_REQUESTED"}
                    completed_orders += 1
                    assert completed_orders <= 7
                detail = await service.get_directive(created["id"])
                assert detail["status"] == "AWAITING_APPROVAL"
                assert len(detail["department_work_orders"]) == 5
                assert completed_orders == 6
                legal_order = next(x for x in detail["department_work_orders"] if x["department"] == "Legal, Risk & Compliance")
                assert legal_order["revision_count"] == 1
                assert len(legal_order["review_history"]) == 2
                assert all(x["status"] == "APPROVED_BY_HEAD" for x in detail["department_work_orders"])
                assert len(detail["tool_execution_proofs"]) == 7
                assert sum(x["status"] == "FAILED" for x in detail["tool_execution_proofs"]) == 1
                assert sum(x["status"] == "PASSED" for x in detail["tool_execution_proofs"]) == 6
                approval_id = str(detail["approval_id"])
                assert approval_id

                # CEO is the requester; a distinct human principal performs the signoff.
                repo.principal = replace(principal, user_id="founder-approver")
                await repo.decide_approval(approval_id, "approved", "Reviewed consolidated departmental evidence",
                                           "photo-studio-growth-os")
                await repo.consume_approval(approval_id, product_id="photo-studio-growth-os",
                    employee_id="digital-ceo", action_type="directive.external_execution",
                    payload=detail["consolidated_proposal"], run_id=created["id"])
                completed = await service.approve_external(created["id"], approval_id)
                assert completed["status"] == "COMPLETED"
                assert completed["external_execution"] == "not_started"

                with pytest.raises(PermissionError):
                    await repo.consume_approval(approval_id, product_id="photo-studio-growth-os",
                        employee_id="digital-ceo", action_type="directive.external_execution",
                        payload=detail["consolidated_proposal"], run_id=created["id"])
        # Verify a second tenant cannot read the first tenant's directive under FORCE RLS.
        other = Principal("other-tenant-user", "w2-other-" + uuid4().hex, "founder",
                         frozenset({"*"}), product_ids=frozenset({"photo-studio-growth-os"}))
        async with factory() as session:
            async with PostgresWorkforceRepository(session, other):
                visible = await session.execute(text("SELECT count(*) FROM executive_directives"))
                assert visible.scalar_one() == 0

        # Budget ceiling pauses execution and requires a single-use approval.
        async with factory() as session:
            async with PostgresWorkforceRepository(session, principal) as repo:
                service = WorkforceOrchestrator(repo, principal, WorkforceToolRegistry(directory_fetcher=FixtureDirectoryFetcher()))
                budget_directive = await service.submit_directive(
                    "Check budget enforcement for a Hyderabad photography prospecting work order",
                    "photo-studio-growth-os")
                await session.execute(text("""UPDATE department_work_orders SET max_budget_cents=0
                    WHERE tenant_id=:tenant AND directive_id=:directive AND department='Growth & Marketing'"""),
                    {"tenant":tenant,"directive":budget_directive["id"]})
                await session.execute(text("""UPDATE department_work_orders SET status='APPROVED_BY_HEAD'
                    WHERE tenant_id=:tenant AND directive_id=:directive AND department<>'Growth & Marketing'"""),
                    {"tenant":tenant,"directive":budget_directive["id"]})
                held_order = await service.claim_work_order("budget-worker")
                assert held_order is not None
                assert held_order["locked_by_employee_id"] == "budget-worker"
                held = await service.execute_work_order(held_order)
                assert held["status"] == "AWAITING_APPROVAL"
                detail = await service.get_directive(budget_directive["id"])
                assert detail["status"] == "AWAITING_APPROVAL"
                growth = next(x for x in detail["department_work_orders"] if x["department"] == "Growth & Marketing")
                assert growth["status"] == "AWAITING_APPROVAL"
                assert growth["spent_cents"] == 0
    finally:
        await engine.dispose()
