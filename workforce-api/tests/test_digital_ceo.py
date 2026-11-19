import pytest
from datetime import datetime, timedelta, timezone
from app.digital_ceo import DigitalCEOService, REVENUE_TARGET_INR, CAPITAL_TARGET_INR
from app.main import PRODUCTS
from app.registry import BY_ID

@pytest.mark.asyncio
async def test_ceo_briefing_never_invents_revenue_or_capital(principal, install_test_repository):
    repo=install_test_repository
    snapshot=await DigitalCEOService(repo,BY_ID,PRODUCTS).briefing("test-founder")
    assert snapshot["operating_revenue"]["actual_inr"] == 0
    assert snapshot["operating_revenue"]["target_inr"] == REVENUE_TARGET_INR
    assert snapshot["investment_capital"]["received_inr"] == 0
    assert snapshot["investment_capital"]["target_inr"] == CAPITAL_TARGET_INR
    assert all(row["is_provisional"] and row["allocation_label"]=="PROVISIONAL ALLOCATION"
               for row in snapshot["operating_revenue"]["by_product"])
    assert repo.ceo_briefings[-1]["snapshot"] == snapshot

@pytest.mark.asyncio
async def test_recognised_revenue_only_counts_invoice_recognised(principal, install_test_repository):
    repo=install_test_repository
    product="photo-studio-growth-os"
    for event_id,event_type,amount in [
        ("trial-1","trial_started",0),("booking-1","booking_confirmed",0),
        ("invoice-1","invoice_recognised",12500)]:
        await repo.record_product_telemetry(tenant_id=principal.tenant_id,
            external_tenant_id="studio-ext-1",product_id=product,
            event={"event_id":event_id,"event_type":event_type,
                   "recognised_revenue_inr":amount,"occurred_at":datetime.now(timezone.utc)},
            correlation_id="test-correlation")
    snapshot=await DigitalCEOService(repo,BY_ID,PRODUCTS).briefing("test-founder")
    assert snapshot["operating_revenue"]["actual_inr"] == 12500
    assert snapshot["operating_revenue"]["by_product"][0]["actual_recognised_inr"] == 12500

@pytest.mark.asyncio
async def test_capital_received_requires_separate_live_approval(principal, install_test_repository):
    repo=install_test_repository
    with pytest.raises(ValueError):
        await repo.record_investment_capital_event(event_type="capital_received",
            investor_ref="INV-1",amount_inr=100000,is_received=True)
    with pytest.raises(ValueError):
        await repo.record_investment_capital_event(event_type="commitment_approved",
            investor_ref="INV-1",amount_inr=100000,is_received=False)
    now=datetime.now(timezone.utc)
    repo.approvals.append({"id":"approval-verified","tenant_id":principal.tenant_id,
      "status":"APPROVED","approver_id":"founder-2","requester_id":"agent-1",
      "action_type":"investment.commit",
      "payload":{"event_type":"capital_received","investor_ref":"INV-1","amount_inr":100000},
      "consumed_at":None,"expires_at":now+timedelta(hours=1)})
    event=await repo.record_investment_capital_event(event_type="capital_received",
      investor_ref="INV-1",amount_inr=100000,is_received=True,
      approval_id="approval-verified",occurred_at=now)
    assert event["amount_inr"] == 100000
    assert event["is_received"] is True
    assert repo.approvals[-1]["consumed_at"] is not None
    with pytest.raises(PermissionError):
        await repo.record_investment_capital_event(event_type="capital_received",
          investor_ref="INV-1",amount_inr=100000,is_received=True,
          approval_id="approval-verified",occurred_at=now)

\n@pytest.mark.asyncio
async def test_synthetic_invoice_telemetry_is_excluded_from_recognised_revenue(principal, install_test_repository):
    repo=install_test_repository
    await repo.record_product_telemetry(tenant_id=principal.tenant_id,
        external_tenant_id="synthetic-fixture",product_id="photo-studio-growth-os",
        event={"event_id":"synthetic-invoice","event_type":"invoice_recognised",
               "recognised_revenue_inr":999999,"metadata":{"is_synthetic":True}},
        correlation_id="synthetic-test")
    snapshot=await DigitalCEOService(repo,BY_ID,PRODUCTS).briefing("test-founder")
    assert snapshot["operating_revenue"]["actual_inr"] == 0
    assert snapshot["operating_revenue"]["source_event_count"] == 0
