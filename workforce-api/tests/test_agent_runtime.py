import pytest
from app.agent_runtime import AgentInput,AgentOrchestrator,build_system_prompt
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
    prompt=build_system_prompt(BY_ID["ae"])
    assert "Never claim to be human" in prompt
    assert "Human owns negotiation" in prompt


def test_universal_capabilities_are_injected_for_every_employee():
    for employee in BY_ID.values():
        prompt=build_system_prompt(employee)
        assert "Universal capabilities required of every employee" in prompt
        assert "tenant-scoped knowledge" in prompt
        assert "isolated sandbox" in prompt

def test_product_growth_manager_prompt_is_product_scoped():
    prompt=build_system_prompt(BY_ID["pgm-photo-studio-growth-os"])
    assert "Assigned product: photo-studio-growth-os" in prompt
