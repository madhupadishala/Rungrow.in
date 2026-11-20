import json
import time

import pytest

from app.main import PRODUCT_ADAPTERS


@pytest.mark.asyncio
async def test_generic_webhook_http_contract_uses_external_and_workforce_tenant_headers(
    client, install_test_repository, monkeypatch
):
    product_id = "clinics-counter"
    external_tenant = "external-clinic-tenant-http"
    workforce_tenant = "tenant-test"
    secret = "generic-http-contract-secret-with-more-than-32-characters"
    monkeypatch.setenv("RUNGROW_INTERNAL_TENANT_ID", workforce_tenant)
    adapter = PRODUCT_ADAPTERS.get(product_id)
    adapter.service_secret = secret.encode('utf-8')
    scopes = frozenset({product_id + ":write"})
    timestamp = str(int(time.time()))
    payload = {
        "product_id": product_id,
        "tenant_id": external_tenant,
        "event": {
            "event_id": "http-event-001",
            "event_type": "prospect.qualified",
            "recognised_revenue_inr": 0,
        },
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = adapter.sign(raw, timestamp, external_tenant_id=external_tenant,
        workforce_tenant_id=workforce_tenant, scopes=scopes)
    headers = {
        "X-RunGrow-Signature": signature,
        "X-RunGrow-Timestamp": timestamp,
        "X-RunGrow-Product-ID": product_id,
        "X-RunGrow-Tenant-ID": workforce_tenant,
        "X-RunGrow-External-Tenant-Id": external_tenant,
        "X-RunGrow-Scopes": product_id + ":write",
        "Idempotency-Key": "http-event-key-001",
        "X-Correlation-ID": "http-correlation-001",
        "Content-Type": "application/json",
    }
    response = await client.post("/v1/adapters/clinics-counter/webhooks", content=raw, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "ACCEPTED"

    unsorted = {**headers, "X-RunGrow-Scopes": product_id + ":write," + product_id + ":read"}
    rejected = await client.post("/v1/adapters/clinics-counter/webhooks", content=raw, headers=unsorted)
    assert rejected.status_code == 403
