import json
import time
from types import SimpleNamespace

import pytest

from app.adapters.generic_product_adapter import (
    AdapterGovernanceError,
    GenericAdapterContext,
    GenericProductAdapter,
)
from app.policy import Principal


SECRET = "generic-adapter-ci-secret-with-at-least-32-bytes"
EXTERNAL = "external-clinic-tenant"
INTERNAL = "internal-workforce-tenant"
SCOPES = frozenset({"clinics-counter:write"})


def test_generic_adapter_hmac_binds_product_tenants_scopes_and_raw_body():
    adapter = GenericProductAdapter("clinics-counter", service_secret=SECRET)
    body = b'{"event":{"event_id":"e-1"}}'
    timestamp = str(int(time.time()))
    signature = adapter.sign(body, timestamp, external_tenant_id=EXTERNAL,
        workforce_tenant_id=INTERNAL, scopes=SCOPES)
    adapter.verify(body, signature, timestamp, external_tenant_id=EXTERNAL,
        workforce_tenant_id=INTERNAL, scopes=SCOPES)
    with pytest.raises(AdapterGovernanceError):
        adapter.verify(body, signature, timestamp, external_tenant_id=EXTERNAL,
            workforce_tenant_id=INTERNAL, scopes={"clinics-counter:read"})
    with pytest.raises(AdapterGovernanceError):
        adapter.verify(body + b" ", signature, timestamp, external_tenant_id=EXTERNAL,
            workforce_tenant_id=INTERNAL, scopes=SCOPES)


def test_generic_adapter_missing_secret_fails_closed_with_503():
    adapter = GenericProductAdapter("clinics-counter", service_secret="")
    with pytest.raises(AdapterGovernanceError) as error:
        adapter.sign(b"{}", int(time.time()), external_tenant_id=EXTERNAL,
            workforce_tenant_id=INTERNAL, scopes=SCOPES)
    assert error.value.status_code == 503


class FakeRepository:
    def __init__(self):
        self.events = []
        self.responses = []

    async def claim_adapter_idempotency(self, **kwargs):
        return {"created": True, "response": None}

    async def complete_adapter_idempotency(self, **kwargs):
        self.responses.append(kwargs["response"])

    async def record_product_telemetry(self, **kwargs):
        self.events.append(kwargs["event"])


@pytest.mark.asyncio
async def test_generic_webhook_forces_zero_revenue_for_non_invoice_events():
    adapter = GenericProductAdapter("clinics-counter", service_secret=SECRET)
    repository = FakeRepository()
    timestamp = str(int(time.time()))
    payload = {
        "product_id": "clinics-counter",
        "tenant_id": EXTERNAL,
        "event": {
            "event_id": "lifecycle-1",
            "event_type": "prospect.qualified",
            "recognised_revenue_inr": 123456,
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = adapter.sign(raw, timestamp, external_tenant_id=EXTERNAL,
        workforce_tenant_id=INTERNAL, scopes=SCOPES)
    result = await adapter.handle_webhook(raw_body=raw, signature=signature, timestamp=timestamp,
        external_tenant_id=EXTERNAL, workforce_tenant_id=INTERNAL, scopes=SCOPES,
        idempotency_key="event-1", correlation_id="corr-1", repository=repository)
    assert result["status"] == "ACCEPTED"
    assert repository.events[0]["recognised_revenue_inr"] == 0


@pytest.mark.asyncio
async def test_growtoys_discovery_only_live_handoff_is_http_403():
    adapter = GenericProductAdapter("growtoys", service_secret=SECRET, commercial_stage="DISCOVERY_ONLY")
    context = GenericAdapterContext(tenant_id=EXTERNAL, workforce_tenant_id=INTERNAL,
        product_id="growtoys", correlation_id="corr-growtoys", scopes=frozenset({"campaign.activate"}))
    principal = Principal("founder", INTERNAL, "founder", frozenset({"*"}),
        product_ids=frozenset({"growtoys"}))
    run = {"product_id": "growtoys", "tenant_id": INTERNAL, "is_sandbox": False}
    with pytest.raises(AdapterGovernanceError) as error:
        await adapter.handoff(payload={}, context=context, principal=principal,
            employee=SimpleNamespace(product_id="growtoys"), run=run,
            approval_id="approval-1", repository=FakeRepository())
    assert error.value.status_code == 403
