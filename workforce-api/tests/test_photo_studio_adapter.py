import hashlib
import hmac
import json
import os
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
from app.main import app as workforce_app
from app.policy import Principal
from app.repositories import InMemoryWorkforceRepository


TENANT = "tenant-test"
PRODUCT = "photo-studio-growth-os"
SECRET = "test-studio-secret"
READ = "photo-studio-growth-os:read"
WRITE = "photo-studio-growth-os:write"


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    return PhotoStudioGrowthOSAdapter(base_url="http://studio-mock", service_secret=SECRET,
        transport=ASGITransport(app=studio_mock))


@pytest.fixture
def read_context():
    return AdapterContext(TENANT, scopes=frozenset({READ}), correlation_id="corr-read-001")


@pytest.mark.asyncio
async def test_signed_read_contract_for_leads_quotes_bookings_and_conversation(adapter, read_context):
    leads = await adapter.list_leads("studio-hyd-001", limit=10, context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-leads"))
    quotes = await adapter.list_quotes("studio-hyd-001", context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-quotes"))
    bookings = await adapter.list_bookings("studio-hyd-001", context=AdapterContext(
        "studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-bookings"))
    conversation = await adapter.get_conversation_context("studio-hyd-001", "lead-hyd-001",
        context=AdapterContext("studio-hyd-001", scopes=frozenset({READ}), correlation_id="corr-conv"))
    assert leads["leads"][0]["stage"] == "qualified"
    assert quotes[0]["status"] == "approved"
    assert bookings[0]["state"] == "confirmed"
    assert conversation["lead_id"] == "lead-hyd-001"
    assert all(x["is_synthetic"] for x in leads["leads"] + quotes + bookings)
    assert conversation["is_synthetic"] is True


def test_context_enforces_tenant_and_least_privilege(adapter):
    with pytest.raises(PermissionError):
        adapter._bind("another-tenant", AdapterContext(TENANT, scopes=frozenset({READ})), READ)
    with pytest.raises(PermissionError):
        adapter._bind(TENANT, AdapterContext(TENANT), READ)


def test_hmac_replay_window_and_product_binding():
    verifier = SignedWebhookVerifier(SECRET)
    body = b'{"tenant_id":"tenant-test","product_id":"photo-studio-growth-os"}'
    timestamp = int(time.time())
    signature = verifier.sign(body, timestamp)
    verifier.verify(body, signature, str(timestamp), tenant_id=TENANT, product_id=PRODUCT)
    with pytest.raises(PermissionError):
        verifier.verify(body, "sha256=" + "0" * 64, str(timestamp),
                        tenant_id=TENANT, product_id=PRODUCT)
    with pytest.raises(PermissionError):
        verifier.verify(body, signature, str(timestamp - 301), tenant_id=TENANT, product_id=PRODUCT)
    with pytest.raises(PermissionError):
        verifier.verify(body, signature, str(timestamp), tenant_id=TENANT, product_id="wrong-product")


def _webhook_payload(event_id="invoice-001", revenue=125000, event_type="invoice_recognised"):
    return {"tenant_id":TENANT,"product_id":PRODUCT,"event_id":event_id,
            "event_type":event_type,"recognised_revenue_inr":revenue,
            "occurred_at":"2026-11-18T10:00:00+05:30","source_ref":"studio-invoice-001",
            "metadata":{"currency":"INR","is_synthetic":True}}


def _signed_headers(payload, key="event-key-001", signature_override=None, timestamp_override=None):
    raw = json.dumps(payload, separators=(",",":"), ensure_ascii=False).encode()
    timestamp = str(int(time.time())) if timestamp_override is None else str(timestamp_override)
    signature = SignedWebhookVerifier(SECRET).sign(raw, timestamp)
    return raw, {"X-RunGrow-Signature":signature_override or signature,
        "X-RunGrow-Timestamp":timestamp,"X-RunGrow-Tenant-ID":TENANT,
        "X-RunGrow-Product-ID":PRODUCT,"X-Correlation-ID":"corr-webhook-001",
        "Idempotency-Key":key}


@pytest.mark.asyncio
async def test_webhook_is_signed_idempotent_and_records_recognised_revenue(test_app, install_test_repository, monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
    payload = _webhook_payload()
    raw, headers = _signed_headers(payload)
    async with AsyncClient(transport=ASGITransport(app=test_app),base_url="http://workforce") as client:
        first = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
        second = await client.post("/v1/adapters/photo-studio-growth-os/webhooks",content=raw,headers=headers)
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    assert second.status_code == 200 and second.json()["idempotent"] is True
    assert len(install_test_repository.product_telemetry) == 1
    assert install_test_repository.product_telemetry[0]["recognised_revenue_inr"] == 125000


@pytest.mark.asyncio
async def test_webhook_rejects_invalid_signature_expired_timestamp_and_wrong_product(
        test_app, monkeypatch):
    monkeypatch.setenv("STUDIO_GROWTH_OS_SERVICE_SECRET", SECRET)
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
    run = {"id":"run-adapter-001","tenant_id":TENANT,"product_id":PRODUCT,
           "employee_id":employee.id,"is_sandbox":sandbox,"execution_state":"QUEUED"}
    return principal, employee, run


@pytest.mark.asyncio
async def test_governed_followup_blocks_sandbox_and_missing_approval(adapter):
    principal = Principal("requester",TENANT,"founder",frozenset({WRITE}),
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
    principal = Principal("requester",TENANT,"founder",frozenset({WRITE}),
                          product_ids=frozenset({PRODUCT}))
    repository = InMemoryWorkforceRepository(principal,{PRODUCT})
    employee = SimpleNamespace(id="pgm-photo-studio-growth-os",permissions=(WRITE,),
        department="Sales & Revenue",product_id=PRODUCT)
    run = await repository.create_agent_run(product_id=PRODUCT,employee_id=employee.id,task="follow-up",
        status="planned",summary="",steps=[],correlation_id="corr-run",is_sandbox=False)
    payload = {"tenant_id":TENANT,"lead_id":"lead-hyd-001","message_payload":{"message":"Approved follow-up"},
               "idempotency_key":"approved-write-001","correlation_id":"corr-approved"}
    approval = await repository.create_approval(product_id=PRODUCT,employee_id=employee.id,
        action_type="whatsapp.send",payload=payload,expires_at=datetime.now(timezone.utc)+timedelta(minutes=5))
    approval.update(status="APPROVED",approver_id="independent-approver",
                    decided_at=datetime.now(timezone.utc))
    sent=[]
    async def fake_send(request):
        sent.append(request)
        return httpx.Response(200,json={"status":"sent","is_synthetic":True})
    adapter.transport = httpx.MockTransport(fake_send)
    result = await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",
        {"message":"Approved follow-up"},approval["id"],"approved-write-001",
        repository=repository,principal=principal,employee=employee,run=run,
        correlation_id="corr-approved")
    assert result["status"] == "sent"
    assert len(sent) == 1
    assert approval["consumed_at"] is not None
    with pytest.raises(ApprovalBoundaryError):
        await adapter.dispatch_governed_lead_followup(TENANT,"lead-hyd-001",
            {"message":"Approved follow-up"},approval["id"],"approved-write-001",
            repository=repository,principal=principal,employee=employee,run=run,
            correlation_id="corr-approved")
    assert len(sent) == 1
