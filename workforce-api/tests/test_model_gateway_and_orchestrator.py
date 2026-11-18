from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
import pytest
from pydantic import BaseModel

from app.execution_boundary import ToolDefinition, ToolRegistry, ExecutionBoundaryGuard, ApprovalBoundaryError, SandboxViolation
from app.model_gateway import ModelGateway, MockModelProvider
from app.orchestrator import DurableTaskOrchestrator
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository


PRODUCTS={"photo-studio-growth-os"}
def principal(user="requester"):
    return Principal(user,"tenant-a","founder",frozenset({"*"}),product_ids=frozenset(PRODUCTS))

class Plan(BaseModel):
    summary: str
    count: int

@pytest.mark.asyncio
async def test_structured_generation_repairs_once_and_records_usage():
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    gateway=ModelGateway({"mock":MockModelProvider(["not-json",{"summary":"repaired","count":2}])},repository=repo)
    result,telemetry=await gateway.generate_structured(
        messages=[{"role":"user","content":"make plan"}],output_model=Plan,
        tenant_id="tenant-a",product_id="photo-studio-growth-os",employee_id="market-intel",run_id="run-1")
    assert result.count==2 and len(repo.model_usage)==1
    assert telemetry["provider"]=="mock" and telemetry["run_id"]=="run-1"

@pytest.mark.asyncio
async def test_transient_primary_failure_routes_to_fallback_and_records_cost():
    request=httpx.Request("POST","https://primary.invalid")
    failure=httpx.HTTPStatusError("busy",request=request,response=httpx.Response(503,request=request))
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    gateway=ModelGateway({"primary":MockModelProvider([failure]),"fallback":MockModelProvider([{"summary":"ok","count":1}])},
        primary_provider="primary",primary_model="p1",fallback_provider="fallback",fallback_model="f1",repository=repo)
    result,usage=await gateway.generate_structured(messages=[],output_model=Plan,tenant_id="tenant-a",
        product_id="photo-studio-growth-os",employee_id="market-intel",run_id="r2",
        estimated_input_cost_inr_per_1k=1,estimated_output_cost_inr_per_1k=2)
    assert result.summary=="ok" and usage["model"]=="f1"
    assert repo.model_usage[0]["estimated_cost_inr"]>=0

@pytest.mark.asyncio
async def test_lease_recovery_reclaims_expired_task_and_idempotent_handler_completes_once():
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    task=await repo.enqueue_task(product_id="photo-studio-growth-os",department="Growth",
        assignee_role_id="market-intel",title="research",description="",priority="normal",max_attempts=3)
    first=await repo.claim_task("worker-1",1)
    assert first["id"]==task["id"]
    first["lease_expires_at"]=datetime.now(timezone.utc)-timedelta(seconds=1)
    second=await repo.claim_task("worker-2",30)
    assert second["id"]==first["id"] and second["attempt_count"]==2
    side_effect_keys=set()
    async def idempotent_handler(item):
        side_effect_keys.add(item["id"])
        return {"done":True}
    result=await DurableTaskOrchestrator(repo,"worker-2").dispatch_once(idempotent_handler)
    assert result["status"]=="COMPLETED" and len(side_effect_keys)==1
    with pytest.raises(PermissionError):
        await repo.finish_task(task["id"],"worker-1",{"duplicate":True})

@pytest.mark.asyncio
async def test_max_attempts_transitions_task_to_dead_letter():
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    task=await repo.enqueue_task(product_id="photo-studio-growth-os",department="Growth",
        assignee_role_id="market-intel",title="retry me",description="",priority="normal",max_attempts=1)
    claimed=await repo.claim_task("worker",5)
    assert claimed["id"]==task["id"]
    failed=await repo.fail_task(task["id"],"worker","deterministic failure",0,1)
    assert failed["status"]=="DEAD_LETTER"
    assert await repo.claim_task("worker-2",5) is None

def make_tool(calls, requires=True, sandbox=False):
    async def handler(**payload):
        calls.append(payload)
        return {"accepted":True}
    return ToolDefinition("customer.outreach",frozenset(),requires,sandbox,"message.send",handler)

async def approved_repo():
    requester=principal()
    repo=InMemoryWorkforceRepository(requester,PRODUCTS)
    run=await repo.create_agent_run(product_id="photo-studio-growth-os",employee_id="employee-1",
        task="send",status="planned",summary="",steps=[],correlation_id="run-1")
    approval=await repo.create_approval(product_id="photo-studio-growth-os",employee_id="employee-1",
        action_type="message.send",payload={"text":"hello"},expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    repo.principal=principal("independent-approver")
    await repo.decide_approval(approval["id"],"approved","Reviewed","photo-studio-growth-os")
    repo.principal=requester
    return repo,run,approval

@pytest.mark.asyncio
async def test_sensitive_tool_requires_approval_then_executes_once_and_blocks_replay():
    repo,run,approval=await approved_repo()
    calls=[]; registry=ToolRegistry(); registry.register(make_tool(calls))
    employee=SimpleNamespace(id="employee-1",permissions=frozenset(),department="Growth",product_id="photo-studio-growth-os")
    guard=ExecutionBoundaryGuard(repo,registry)
    result=await guard.execute(tool_name="customer.outreach",principal=repo.principal,employee=employee,
        run=run,payload={"text":"hello"},approval_id=approval["id"])
    assert result["accepted"] and len(calls)==1
    with pytest.raises(ApprovalBoundaryError):
        await guard.execute(tool_name="customer.outreach",principal=repo.principal,employee=employee,
            run=run,payload={"text":"hello"},approval_id=approval["id"])
    assert len(calls)==1

@pytest.mark.asyncio
async def test_missing_approval_pauses_run_without_invoking_tool():
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    run=await repo.create_agent_run(product_id="photo-studio-growth-os",employee_id="employee-1",
        task="send",status="planned",summary="",steps=[],correlation_id="r")
    calls=[]; registry=ToolRegistry(); registry.register(make_tool(calls))
    employee=SimpleNamespace(id="employee-1",permissions=frozenset(),department="Growth",product_id="photo-studio-growth-os")
    with pytest.raises(ApprovalBoundaryError):
        await ExecutionBoundaryGuard(repo,registry).execute(tool_name="customer.outreach",principal=repo.principal,
            employee=employee,run=run,payload={"text":"hello"})
    assert run["execution_state"]=="AWAITING_APPROVAL" and len(calls)==0
    assert repo.approvals[0]["status"]=="PENDING"

@pytest.mark.asyncio
@pytest.mark.parametrize("mutation",["expired","self_approved","tampered"])
async def test_invalid_approval_variants_fail_closed(mutation):
    repo,run,approval=await approved_repo()
    if mutation=="expired": approval["expires_at"]=datetime.now(timezone.utc)-timedelta(seconds=1)
    elif mutation=="self_approved": approval["approver_id"]=approval["requester_id"]
    else: approval["payload_hash"]="0"*64
    calls=[]; registry=ToolRegistry(); registry.register(make_tool(calls))
    employee=SimpleNamespace(id="employee-1",permissions=frozenset(),department="Growth",product_id="photo-studio-growth-os")
    with pytest.raises(ApprovalBoundaryError):
        await ExecutionBoundaryGuard(repo,registry).execute(tool_name="customer.outreach",principal=repo.principal,
            employee=employee,run=run,payload={"text":"hello"},approval_id=approval["id"])
    assert calls==[]

@pytest.mark.asyncio
async def test_sandbox_cannot_call_production_tool_and_records_violation():
    repo=InMemoryWorkforceRepository(principal(),PRODUCTS)
    run=await repo.create_agent_run(product_id="photo-studio-growth-os",employee_id="employee-1",
        task="practice",status="planned",summary="",steps=[],correlation_id="sandbox",is_sandbox=True)
    calls=[]; registry=ToolRegistry(); registry.register(make_tool(calls,requires=False,sandbox=False))
    employee=SimpleNamespace(id="employee-1",permissions=frozenset(),department="Growth",product_id="photo-studio-growth-os")
    with pytest.raises(SandboxViolation):
        await ExecutionBoundaryGuard(repo,registry).execute(tool_name="customer.outreach",principal=repo.principal,
            employee=employee,run=run,payload={"text":"never send"})
    assert calls==[] and any(x["action"]=="sandbox.violation" for x in repo.audit)

def test_outbound_prompt_always_contains_explicit_ai_identity_disclosure():
    messages=ModelGateway.with_ai_identity([{"role":"user","content":"Draft a customer reply"}],outbound=True)
    assert messages[0]["role"]=="system"
    assert "AI assistant" in messages[0]["content"]
    assert "Never claim or imply that you are human" in messages[0]["content"]
