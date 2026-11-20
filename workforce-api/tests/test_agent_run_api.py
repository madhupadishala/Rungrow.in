import pytest
from app.policy import Principal
from app.main import app
from app.agent_runtime import AgentInput,AgentOrchestrator
from app.registry import BY_ID

@pytest.mark.asyncio
async def test_employee_run_creates_bounded_plan():
    plan=await AgentOrchestrator().run(BY_ID["market-intel"],AgentInput("tenant-a","user-a","Research independent photo studios",("https://example.com/about",)))
    assert plan.status=="planned" and plan.employee_id=="market-intel"
    assert plan.runtime_mode=="deterministic-fixture"

@pytest.mark.asyncio
async def test_sensitive_task_requires_human_approval():
    plan=await AgentOrchestrator().run(BY_ID["outbound-sdr"],AgentInput("tenant-a","user-a","Send this campaign to all leads"))
    assert plan.approval_required and plan.status=="approval_required"

def test_prompt_contains_role_boundary_and_disclosure():
    from app.agent_runtime import build_system_prompt
    assert "Never claim to be human" in build_system_prompt(BY_ID["ae"])

def test_universal_capabilities_are_injected_for_every_employee():
    from app.agent_runtime import build_system_prompt
    for employee in BY_ID.values():
        prompt=build_system_prompt(employee)
        assert "Universal capabilities required of every employee" in prompt
        assert "tenant-scoped knowledge" in prompt and "isolated sandbox" in prompt

def test_product_growth_manager_prompt_is_product_scoped():
    from app.agent_runtime import build_system_prompt
    assert "Assigned product: photo-studio-growth-os" in build_system_prompt(BY_ID["pgm-photo-studio-growth-os"])

@pytest.mark.asyncio
async def test_product_growth_agent_cannot_run_without_exact_product_scope(install_test_repository, client):
    response=await client.post("/v1/workforce/agent-runs",json={"role_id":"pgm-photo-studio-growth-os","product_id":"photo-studio-billing","task":"Prepare weekly acquisition plan"})
    assert response.status_code==403
