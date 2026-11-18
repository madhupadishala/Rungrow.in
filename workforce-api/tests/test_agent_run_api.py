import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app, STATE
from app.policy import Principal
from app.security import current_principal

@pytest.mark.asyncio
async def test_agent_run_creates_separate_human_approval_for_sensitive_action():
    principal=Principal("founder-user","tenant-a","founder",frozenset({"*"}))
    app.dependency_overrides[current_principal]=lambda: principal
    STATE["approvals"].clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
            response=await client.post("/v1/workforce/agent-runs",json={
                "role_id":"outbound-sdr",
                "task":"Send this campaign to all leads"
            })
        assert response.status_code==200
        result=response.json()
        assert result["status"]=="approval_required"
        assert result["approval_id"]
        approval=next(x for x in STATE["approvals"] if x["id"]==result["approval_id"])
        assert approval["tenant_id"]=="tenant-a"
        assert approval["requested_by"]=="founder-user"
        assert approval["status"]=="pending"
    finally:
        app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_product_growth_agent_cannot_run_without_exact_product_scope():
    principal=Principal("founder-user","tenant-a","founder",frozenset({"*"}))
    app.dependency_overrides[current_principal]=lambda: principal
    try:
        async with AsyncClient(transport=ASGITransport(app=app),base_url="http://test") as client:
            response=await client.post("/v1/workforce/agent-runs",json={
                "role_id":"pgm-photo-studio-growth-os",
                "product_id":"photo-studio-billing",
                "task":"Prepare the weekly acquisition plan"
            })
        assert response.status_code==403
    finally:
        app.dependency_overrides.clear()
