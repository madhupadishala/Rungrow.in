import pytest
from datetime import datetime,timedelta,timezone
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository

def make_principal(user="user-a",tenant="tenant-a",permissions=None,products=None):
    return Principal(user,tenant,"founder",frozenset(permissions or {"*"}),product_ids=frozenset(products or []))

@pytest.mark.asyncio
async def test_tenant_isolation_and_fresh_repository(client,install_test_repository):
    repo=install_test_repository
    repo.tasks.append({"id":"other","tenant_id":"tenant-b","product_id":None})
    assert (await client.get("/v1/workforce/tasks")).json()==[]
    assert repo.tasks[0]["tenant_id"]=="tenant-b"

@pytest.mark.asyncio
async def test_client_cannot_supply_tenant_id(client):
    response=await client.post("/v1/workforce/tasks",json={"title":"Task tenant spoof","assignee_role_id":"market-intel","tenant_id":"tenant-b"})
    assert response.status_code==422

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,payload",[
 ("post","/v1/workforce/tasks",{"title":"Bad product","assignee_role_id":"market-intel","product_id":"bad-product"}),
 ("post","/v1/workforce/memory",{"role_id":"market-intel","content":"x","source_ref":"test","product_id":"bad-product"}),
 ("post","/v1/workforce/agent-runs",{"role_id":"market-intel","task":"Research","product_id":"bad-product"}),
 ("get","/v1/workforce/tasks?product_id=bad-product",None),
 ("get","/v1/workforce/memory?product_id=bad-product",None),
])
async def test_unknown_product_rejected(client,method,path,payload):
    response=await getattr(client,method)(path,json=payload) if payload else await getattr(client,method)(path)
    assert response.status_code==404

@pytest.mark.asyncio
async def test_self_approval_rejected(install_test_repository,client):
    repo=install_test_repository
    item=await repo.create_approval(product_id=None,employee_id="market-intel",action_type="campaign.activate",payload={"id":"1"},expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    response=await client.post(f"/v1/workforce/approvals/{item['id']}/decision",json={"decision":"approved","reason":"Reviewed"})
    assert response.status_code==403

@pytest.mark.asyncio
async def test_expired_approval_rejected(install_test_repository,client,test_app):
    repo=install_test_repository
    item=await repo.create_approval(product_id=None,employee_id="market-intel",action_type="campaign.activate",payload={"id":"1"},expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    item["expires_at"]=datetime.now(timezone.utc)-timedelta(seconds=1)
    other=Principal("approver","tenant-test","founder",frozenset({"*"}))
    async def override(p=__import__("fastapi").Depends(__import__("app.security",fromlist=["current_principal"]).current_principal)):
        return InMemoryWorkforceRepository(other,set(__import__("app.main",fromlist=["PRODUCTS"]).PRODUCTS))
    # Same repository state, different verified principal for this request.
    from app.main import get_workforce_repo
    async def use_shared_repo(p=__import__("fastapi").Depends(__import__("app.security",fromlist=["current_principal"]).current_principal)):
        repo.principal=other
        return repo
    test_app.dependency_overrides[get_workforce_repo]=use_shared_repo
    response=await client.post(f"/v1/workforce/approvals/{item['id']}/decision",json={"decision":"approved","reason":"Reviewed"})
    assert response.status_code in (403,409)

@pytest.mark.asyncio
async def test_approval_payload_tampering_rejected(install_test_repository):
    repo=install_test_repository
    requester=repo.principal
    item=await repo.create_approval(product_id=None,employee_id="market-intel",action_type="campaign.activate",payload={"audience":"consented"},expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    repo.principal=Principal("approver","tenant-test","founder",frozenset({"*"}))
    await repo.decide_approval(item["id"],"approved","Reviewed")
    with pytest.raises(PermissionError):
        await repo.validate_approval(item["id"],product_id=None,employee_id="market-intel",action_type="campaign.activate",payload={"audience":"all"})
