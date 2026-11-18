import pytest
from datetime import datetime, timedelta, timezone
from httpx import ASGITransport, AsyncClient

from app.main import STATE, app, create_approval
from app.policy import Principal
from app.security import current_principal


@pytest.fixture(autouse=True)
def clear_state():
    for records in STATE.values():
        records.clear()
    yield
    app.dependency_overrides.clear()


def principal(user_id="user-a", tenant_id="tenant-a", permissions=None):
    return Principal(user_id, tenant_id, "founder", frozenset(permissions or {"*"}))


async def client_for(p):
    app.dependency_overrides[current_principal] = lambda: p
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_tenant_a_cannot_read_tenant_b_tasks_memory_runs_or_approvals():
    STATE["tasks"].append({"id":"task-b","tenant_id":"tenant-b","product_id":None})
    STATE["memory"].append({"id":"memory-b","tenant_id":"tenant-b","product_id":None})
    STATE["runs"].append({"id":"run-b","tenant_id":"tenant-b","product_id":None})
    STATE["approvals"].append({"id":"approval-b","tenant_id":"tenant-b","product_id":None})
    async with await client_for(principal()) as client:
        for path in ("/v1/workforce/tasks", "/v1/workforce/memory",
                     "/v1/workforce/agent-runs", "/v1/workforce/approvals"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.json() == []


@pytest.mark.asyncio
async def test_client_cannot_choose_tenant_when_creating_task():
    async with await client_for(principal()) as client:
        response = await client.post("/v1/workforce/tasks", json={
            "title":"Tenant-scoped task", "assignee_role_id":"market-intel",
            "tenant_id":"tenant-b", "product_id":"photo-studio-growth-os"
        })
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,payload", [
    ("post", "/v1/workforce/tasks", {"title":"Bad product", "assignee_role_id":"market-intel", "product_id":"not-a-product"}),
    ("post", "/v1/workforce/memory", {"role_id":"market-intel", "content":"x", "source_ref":"test", "product_id":"not-a-product"}),
    ("post", "/v1/workforce/agent-runs", {"role_id":"market-intel", "task":"Research prospects", "product_id":"not-a-product"}),
    ("post", "/v1/workforce/commands", {"command":"Prepare a report", "product_id":"not-a-product"}),
    ("get", "/v1/workforce/tasks?product_id=not-a-product", None),
    ("get", "/v1/workforce/memory?product_id=not-a-product", None),
    ("get", "/v1/workforce/agent-runs?product_id=not-a-product", None),
    ("get", "/v1/workforce/approvals?product_id=not-a-product", None),
])
async def test_unknown_product_rejected_on_product_scoped_routes(method, path, payload):
    async with await client_for(principal()) as client:
        response = await getattr(client, method)(path, json=payload) if payload else await getattr(client, method)(path)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_product_scoped_employee_rejects_mismatched_product():
    async with await client_for(principal()) as client:
        response = await client.post("/v1/workforce/agent-runs", json={
            "role_id":"pgm-photo-studio-growth-os",
            "product_id":"photo-studio-billing",
            "task":"Prepare the weekly acquisition plan"
        })
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_requester_cannot_approve_own_request():
    requester = principal("requester")
    approval = create_approval(requester, product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os", action_type="campaign.activate",
        payload={"campaign_id":"campaign-1"})
    async with await client_for(requester) as client:
        response = await client.post(f"/v1/workforce/approvals/{approval['id']}/decision",
            json={"decision":"approved", "reason":"Reviewed", "product_id":"photo-studio-growth-os"})
    assert response.status_code == 403
    assert approval["approver_id"] is None


@pytest.mark.asyncio
async def test_expired_approval_cannot_be_decided():
    requester = principal("requester")
    approval = create_approval(requester, product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os", action_type="campaign.activate",
        payload={"campaign_id":"campaign-1"})
    approval["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    async with await client_for(principal("independent-approver")) as client:
        response = await client.post(f"/v1/workforce/approvals/{approval['id']}/decision",
            json={"decision":"approved", "reason":"Reviewed", "product_id":"photo-studio-growth-os"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_approval_decision_requires_matching_product_scope():
    requester = principal("requester")
    approval = create_approval(requester, product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os", action_type="campaign.activate",
        payload={"campaign_id":"campaign-1"})
    async with await client_for(principal("independent-approver")) as client:
        response = await client.post(f"/v1/workforce/approvals/{approval['id']}/decision",
            json={"decision":"approved", "reason":"Reviewed", "product_id":"photo-studio-billing"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_payload_tampering_after_approval_is_rejected_at_execution_boundary():
    requester = principal("requester")
    approval = create_approval(requester, product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os", action_type="campaign.activate",
        payload={"campaign_id":"campaign-1", "audience":"consented"})
    async with await client_for(principal("independent-approver")) as client:
        decision = await client.post(f"/v1/workforce/approvals/{approval['id']}/decision",
            json={"decision":"approved", "reason":"Reviewed", "product_id":"photo-studio-growth-os"})
        assert decision.status_code == 200
        tampered = await client.post(f"/v1/workforce/approvals/{approval['id']}/validate-execution",
            json={"product_id":"photo-studio-growth-os", "employee_id":"pgm-photo-studio-growth-os",
                  "action_type":"campaign.activate",
                  "payload":{"campaign_id":"campaign-1", "audience":"all-users"}})
    assert tampered.status_code == 409


@pytest.mark.asyncio
async def test_approval_cannot_be_used_by_another_tenant():
    requester = principal("requester", "tenant-a")
    approval = create_approval(requester, product_id="photo-studio-growth-os",
        employee_id="pgm-photo-studio-growth-os", action_type="campaign.activate",
        payload={"campaign_id":"campaign-1"})
    async with await client_for(principal("other-tenant-user", "tenant-b")) as client:
        response = await client.post(f"/v1/workforce/approvals/{approval['id']}/decision",
            json={"decision":"approved", "reason":"Reviewed", "product_id":"photo-studio-growth-os"})
    assert response.status_code == 404
