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

DB_URL = os.getenv("RUNGROW_TEST_DATABASE_URL")
pytestmark = pytest.mark.asyncio


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
                service = WorkforceOrchestrator(repo, principal)
                created = await service.submit_directive(
                    "Prepare a campaign and lead pipeline for Photo Studio Growth OS in Hyderabad",
                    "photo-studio-growth-os")
                assert created["department_count"] == 5
                for _ in range(5):
                    order = await service.claim_work_order("integration-worker")
                    assert order is not None
                    outcome = await service.execute_work_order(order)
                    assert outcome["status"] == "APPROVED_BY_HEAD"
                detail = await service.get_directive(created["id"])
                assert detail["status"] == "AWAITING_APPROVAL"
                assert len(detail["department_work_orders"]) == 5
                assert all(x["status"] == "APPROVED_BY_HEAD" for x in detail["department_work_orders"])
                assert len(detail["tool_execution_proofs"]) == 5
                assert all(x["status"] == "PASSED" for x in detail["tool_execution_proofs"])
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
    finally:
        await engine.dispose()
