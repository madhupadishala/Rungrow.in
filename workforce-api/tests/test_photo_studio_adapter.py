import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.adapters.base import AdapterContext, SignedWebhookVerifier
from app.adapters.photo_studio_growth_os import PhotoStudioGrowthOSAdapter
from app.adapters.mocks.studio_growth_os_mock import app as studio_mock
from app.execution_boundary import ApprovalBoundaryError, SandboxViolation
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository


TENANT = "studio-customer-tenant-001"
STUDIO_TENANT = "studio-hyd-001"
WORKFORCE_TENANT = "tenant-test"
PRODUCT = "photo-studio-growth-os"
SECRET = "test-studio-secret"
READ = "photo-studio-growth-os:read"
WRITE = "photo-studio-growth-os:write"


@pytest.fixture(autouse=True)
def tenant_mapping_env(monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_TENANT_MAP", json.dumps({
        TENANT:WORKFORCE_TENANT, STUDIO_TENANT:WORKFORCE_TENANT
    }))


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("STUDIO_GROWTH_OS_TENANT_MAP", json.dumps({TENANT:WORKFORCE_TENANT,STUDIO_TENANT:WORKFORCE_TENANT}))
    return PhotoStudioGrowthOSAdapter(base_url="http://studio-mock", service_secret=SECRET,
        transport=ASGITransport(app=studio_mock))


@pytest.fixture
def read_context():
    return AdapterContext(TENANT, scopes=frozenset({READ}), correlation_id="corr-read-001", workforce_tenant_id=WORKFORCE_TENANT)


@pytest.mark.asyncio
async def test_signed_read_contract_for_leads_quotes_bookings_and_conversation(adapter, read_context):
    summary = await adapter.get_tenant_summary("studio-hyd-001", context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-summary", workforce_tenant_id=WORKFORCE_TENANT))
    leads = await adapter.list_leads("studio-hyd-001", limit=10, context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-leads", workforce_tenant_id=WORKFORCE_TENANT))
    quotes = await adapter.list_quotes("studio-hyd-001", context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-quotes", workforce_tenant_id=WORKFORCE_TENANT))
    bookings = await adapter.list_bookings("studio-hyd-001", context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-bookings", workforce_tenant_id=WORKFORCE_TENANT))
    conversation = await adapter.get_conversation_context("studio-hyd-001", "lead-hyd-001",
        context=AdapterContext("studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-conv", workforce_tenant_id=WORKFORCE_TENANT))
    assert summary["tenant_id"] == "studio-hyd-001"
    assert summary["is_synthetic"] is True
    assert leads["leads"][0]["stage"] == "qualified"
    assert quotes[0]["status"] == "approved"
    assert bookings[0]["state"] == "confirmed"
    assert conversation["lead_id"] == "lead-hyd-001"
    assert all(x["is_synthetic"] for x in leads["leads"] + quotes + bookings)
    assert conversation["is_synthetic"] is True


def test_context_enforces_tenant_and_least_privilege(adapter):
    with pytest.raises(PermissionError):
        adapter._bind("another-tenant", AdapterContext(TENANT, scopes=frozenset({READ}), workforce_tenant_id=WORKFORCE_TENANT), READ)
    with pytest.raises(PermissionError):
        adapter._bind(TENANT, AdapterContext(TENANT, workforce_tenant_id=WORKFORCE_TENANT), READ)
    wrong_org = AdapterContext(STUDIO_TENANT, scopes=frozenset({READ}),
        workforce_tenant_id="unmapped-workforce-org")
    with pytest.raises(PermissionError):
        adapter._bind(STUDIO_TENANT, wrong_org, READ)


def test_hmac_replay_window_and_product_binding():
    verifier = SignedWebhookVerifier(SECRET)
    body = json.dumps({"tenant_id":TENANT,"workforce_tenant_id":WORKFORCE_TENANT,"product_id":PRODUCT},separators=(",",":")).encode()
    timestamp = int(time.time())
    signature = verifier.sign(body, timestamp, tenant_id=TENANT, product_id=PRODUCT, workforce_tenant_id=WORKFORCE_TENANT)
    verifier.verify(body, signature, str(timestamp), tenant_id=TENANT, product_id=PRODUCT, workforce_tenant_id=WORKFORCE_TENANT)
    with pytest.raises(PermissionError):
        verifier.verify(body, "sha256=" + "0" * 64, str(timestamp),
                        tenant_id=TENANT, product_id=PRODUCT, workforce_tenant_id=WORKFORCE_TENANT)
    with pytest.raises(PermissionError):
        verifier.verify(body, signature, str(timestamp - 301), tenant_id=TENANT, product_id=PRODUCT, workforce_tenant_id=WORKFORCE_TENANT)
    with pytest.raises(PermissionError):
        verifier.verify(body, signature, str(timestamp), tenant_id=TENANT, product_id="wrong-product", workforce_tenant_id=WORKFORCE_TENANT)


def _webhook_payload(event_id="invoice-001", revenue=125000, event_type="invoice_recognised"):
    return {"tenant_id":TENANT,"workforce_tenant_id":WORKFORCE_TENANT,"product_id":PRODUCT,"event_id":event_id,
            "event_type":event_type,"recognised_revenue_inr":revenue,
            "occurred_at":"2026-11-18T10:00:00+05:30","source_ref":"studio-invoice-001",
            "metadata":{"currency":"INR","is_synthetic":True}}


def _signed_headers(payload, key="event-key-001", signature_override=None, timestamp_override=None):
    raw = json.dumps(payload, separators=(",",":"), ensure_ascii=False).encode()
    timestamp = str(int(time.time())) if timestamp_override is None else str(timestamp_override)
    signature = SignedWebhookVerifier(SECRET).sign(raw, timestamp, tenant_id=TENANT,
        product_id=PRODUCT, workforce_tenant_id=WORKFORCE_TENANT)
    return raw, {"X-RunGrow-Signature":signature_override or signature,
        "X-RunGrow-Timestamp":timestamp,"X-RunGrow-Tenant-ID":TENANT,
        "X-RunGrow-Product-ID":PRODUCT,"X-RunGrow-Org-ID":WORKFORCE_TENANT,"X-Correlation-ID":"corr-webhook-001",
        "Idempotency-Key":key}


@pytest.mark.asyncio
async def test_webhook_is_signed_idempotent_and_records_recognised_revenue(test_app, install_test_repository, monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", WORKFORCE_TENANT)
    payload = _webhook_payload()
    raw, headers = _signed_headers(payload)
    async with AsyncClient(transport=ASGITransport(app=test_app),base_url="http://workforce") as client:
        first = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
        second = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
        event_replay_headers = dict(headers)
        event_replay_headers["Idempotency-Key"] = "different-delivery-key"
        event_replay = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=event_replay_headers)
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert event_replay.status_code == 200 and event_replay.json()["idempotent"] is True
    assert len(install_test_repository.product_telemetry) == 1
    assert install_test_repository.product_telemetry[0]["recognised_revenue_inr"] == 125000
    assert install_test_repository.product_telemetry[0]["tenant_id"] == WORKFORCE_TENANT
    assert install_test_repository.product_telemetry[0]["external_tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature_expired_timestamp_and_wrong_product(
        test_app, monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", WORKFORCE_TENANT)
    async with AsyncClient(transport=ASGITransport(app=test_app),base_url="http://workforce") as client:
        payload = _webhook_payload()
        raw, headers = _signed_headers(payload,signature_override="sha256=" + "0" * 64)
        invalid = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
        raw, headers = _signed_headers(payload,timestamp_override=int(time.time())-301)
        expired = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
        wrong = dict(headers); wrong["X-RunGrow-Product-ID"] = "another-product"
        wrong_product = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=wrong)
    assert invalid.status_code == 401
    assert expired.status_code == 401
    assert wrong_product.status_code == 403


@pytest.mark.asyncio
async def test_non_revenue_lifecycle_event_cannot_carry_revenue(test_app, monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    raw, headers = _signed_headers(_webhook_payload("trial-001",100,"trial_started"))
    async with AsyncClient(transport=ASGITransport(app=test_app),base_url="http://workforce") as client:
        response = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
    assert response.status_code == 422


def _boundary_context(repository, *, sandbox=False):
    employee = SimpleNamespace(id="pgm-photo-studio-growth-os",
        permissions=(READ,WRITE,"crm.read","work.assign"),department="Sales & Revenue",
        product_id=PRODUCT)
    principal = repository.principal
    run = {"id":"run-adapter-001","tenant_id":WORKFORCE_TENANT,"product_id":PRODUCT,
           "employee_id":employee.id,"is_sandbox":sandbox,"execution_state":"QUEUED"}
    return principal, employee, run


@pytest.mark.asyncio
async def test_governed_followup_blocks_sandbox_and_missing_approval(adapter):
    principal = Principal("requester",WORKFORCE_TENANT,"founder",frozenset({WRITE}),
                          product_ids=frozenset({PRODUCT}))
    repository = InMemoryWorkforceRepository(principal,{PRODUCT})
    employee = SimpleNamespace(id="pgm-photo-studio-growth-os",permissions=(WRITE,),
        department="Sales & Revenue",product_id=PRODUCT)
    run = await repository.create_agent_run(product_id=PRODUCT,employee_id=employee.id,task="sandbox",
        status="planned",summary="",steps=[],correlation_id="corr-sandbox",is_sandbox=True)
    with pytest.raises(SandboxViolation):
        await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",{"message":"Hello"},
            None,"sandbox-key",repository=repository,principal=principal,employee=employee,run=run)
    run2 = await repository.create_agent_run(product_id=PRODUCT,employee_id=employee.id,task="follow-up",
        status="planned",summary="",steps=[],correlation_id="corr-no-approval",is_sandbox=False)
    with pytest.raises(ApprovalBoundaryError):
        await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",{"message":"Hello"},
            None,"no-approval-key",repository=repository,principal=principal,employee=employee,run=run2)


@pytest.mark.asyncio
async def test_governed_followup_consumes_third_party_approval_once(adapter):
    principal = Principal("requester",WORKFORCE_TENANT,"founder",frozenset({WRITE}),
                          product_ids=frozenset({PRODUCT}))
    repository = InMemoryWorkforceRepository(principal,{PRODUCT})
    employee = SimpleNamespace(id="pgm-photo-studio-growth-os",permissions=(WRITE,),
        department="Sales & Revenue",product_id=PRODUCT)
    run = await repository.create_agent_run(product_id=PRODUCT,employee_id=employee.id,task="follow-up",
        status="planned",summary="",steps=[],correlation_id="corr-run",is_sandbox=False)
    payload = {"tenant_id":TENANT,"workforce_tenant_id":WORKFORCE_TENANT,"lead_id":"lead-hyd-001","message_payload":{"quote_id":"quote-hyd-001","channel":"whatsapp","template_vars":{"lead_id":"lead-hyd-001"}},
               "idempotency_key":"approved-write-001","correlation_id":"corr-approved"}
    approval = await repository.create_approval(product_id=PRODUCT,employee_id=employee.id,
        action_type="whatsapp.send",payload=payload,expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    approval.update(status="APPROVED",approver_id="independent-approver",
                    decided_at=datetime.now(timezone.utc))
    sent=[]
    async def fake_send(request):
        if request.method == "GET":
            return httpx.Response(200,json={"id":"quote-hyd-001","tenant_id":TENANT,
                "lead_id":"lead-hyd-001","status":"approved","is_synthetic":True})
        sent.append(request)
        return httpx.Response(200,json={"status":"sent","is_synthetic":True})
    adapter.transport = httpx.MockTransport(fake_send)
    result = await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",
        {"quote_id":"quote-hyd-001","channel":"whatsapp","template_vars":{"lead_id":"lead-hyd-001"}},approval["id"],"approved-write-001",
        repository=repository,principal=principal,employee=employee,run=run,
        correlation_id="corr-approved")
    assert result["status"] == "sent"
    assert len(sent) == 1
    assert sent[0].headers["Idempotency-Key"] == "approved-write-001"
    assert sent[0].headers["X-RunGrow-Signature"].startswith("sha256=")
    assert sent[0].headers["X-Correlation-ID"] == "corr-approved"
    assert approval["consumed_at"] is not None
    with pytest.raises(ApprovalBoundaryError):
        await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",
            {"quote_id":"quote-hyd-001","channel":"whatsapp","template_vars":{"lead_id":"lead-hyd-001"}},approval["id"],"approved-write-001",
            repository=repository,principal=principal,employee=employee,run=run,
            correlation_id="corr-approved")
    assert len(sent) == 1
