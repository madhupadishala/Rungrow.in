"""Versioned v1 adapter contract for RunGrow's non-flagship product verticals."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

from .base import IdempotencyStore
from ..execution_boundary import ExecutionBoundaryGuard, ToolDefinition, ToolRegistry

GENERIC_PRODUCT_IDS = (
    "photo-studio-billing",
    "clinics-counter",
    "entrepreneur-instagram-tools",
    "pharma-software-utilities",
    "artists-business-tools",
    "growtoys",
)


class AdapterGovernanceError(PermissionError):
    def __init__(self, message: str, status_code: int = 403):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class GenericAdapterContext:
    tenant_id: str
    workforce_tenant_id: str
    product_id: str
    correlation_id: str
    scopes: frozenset[str]
    is_sandbox: bool = False

    def __post_init__(self):
        if not self.tenant_id or not self.workforce_tenant_id or self.tenant_id == self.workforce_tenant_id:
            raise ValueError("External and internal tenant bindings must be distinct")
        if self.product_id not in GENERIC_PRODUCT_IDS or not self.correlation_id:
            raise ValueError("A registered product and correlation ID are required")


class GenericProductAdapter:
    api_version = "v1"

    def __init__(self, product_id: str, *, service_secret: str, commercial_stage: str = "COMMERCIAL"):
        if product_id not in GENERIC_PRODUCT_IDS:
            raise ValueError("Generic adapter is only for the six standardised product verticals")
        
        if commercial_stage not in {"COMMERCIAL", "DISCOVERY_ONLY"}:
            raise ValueError("Unsupported commercial stage")
        self.product_id = product_id
        self.service_secret = service_secret.encode("utf-8") if service_secret else None
        self.commercial_stage = commercial_stage

    def sign(self, body: bytes, timestamp: int | str, *, external_tenant_id: str,
             workforce_tenant_id: str) -> str:
        if not self.service_secret:
            raise AdapterGovernanceError("Product webhook secret is not configured", 503)
        binding = ".".join((str(timestamp), external_tenant_id, workforce_tenant_id, self.product_id)).encode()
        return "sha256=" + hmac.new(self.service_secret, binding + b"." + body, hashlib.sha256).hexdigest()

    def verify(self, body: bytes, signature: str | None, timestamp: str | None, *,
               external_tenant_id: str, workforce_tenant_id: str, now: float | None = None):
        if not signature or not timestamp:
            raise AdapterGovernanceError("Webhook signature and timestamp are required", 401)
        try:
            issued = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise AdapterGovernanceError("Invalid webhook timestamp", 401) from exc
        if abs((time.time() if now is None else now) - issued) > 300:
            raise AdapterGovernanceError("Webhook timestamp outside replay window", 401)
        expected = self.sign(body, issued, external_tenant_id=external_tenant_id,
                             workforce_tenant_id=workforce_tenant_id)
        supplied = signature if signature.startswith("sha256=") else "sha256=" + signature
        if not hmac.compare_digest(expected, supplied):
            raise AdapterGovernanceError("Invalid webhook signature", 401)

    async def read(self, *, external_tenant_id: str, context: GenericAdapterContext, repository) -> dict[str, Any]:
        if context.product_id != self.product_id or context.tenant_id != external_tenant_id:
            raise AdapterGovernanceError("Adapter tenant/product context mismatch")
        if "product.read" not in context.scopes and "*" not in context.scopes:
            raise AdapterGovernanceError("Missing product.read scope")
        events = await repository.list_product_telemetry()
        rows = [x for x in events if x.get("product_id") == self.product_id
                and x.get("external_tenant_id") == external_tenant_id]
        return {"product_id": self.product_id, "api_version": self.api_version,
                "event_count": len(rows),
                "recognised_revenue_inr": sum(float(x.get("recognised_revenue_inr") or 0)
                                               for x in rows if x.get("event_type") == "invoice_recognised"),
                "latest_events": rows[-25:]}

    def register_handoff_tool(self, registry: ToolRegistry, *, handler):
        async def governed_handoff(**payload):
            if self.product_id == "growtoys" and self.commercial_stage == "DISCOVERY_ONLY":
                raise AdapterGovernanceError("Growtoys is DISCOVERY_ONLY; live sales handoff is disabled")
            return await handler(**payload)
        registry.register(ToolDefinition(
            tool_name=f"{self.product_id}.handoff",
            required_scopes=frozenset({"campaign.activate"}),
            requires_human_approval=True,
            allowed_in_sandbox=False,
            action_type="outreach.send",
            handler=governed_handoff,
            allowed_departments=frozenset({"Sales & Revenue", "Customer Success"}),
        ))

    async def handoff(self, *, payload: dict, context: GenericAdapterContext,
                      principal, employee, run: dict, approval_id: str | None,
                      repository, handoff_handler=None) -> dict[str, Any]:
        if context.product_id != self.product_id or run.get("product_id") != self.product_id:
            raise AdapterGovernanceError("Adapter handoff product binding mismatch")
        if context.workforce_tenant_id != principal.tenant_id or run.get("tenant_id") != principal.tenant_id:
            raise AdapterGovernanceError("Adapter handoff tenant binding mismatch")
        if self.product_id == "growtoys" and self.commercial_stage == "DISCOVERY_ONLY" and not (context.is_sandbox or run.get("is_sandbox")):
            raise AdapterGovernanceError("Growtoys is DISCOVERY_ONLY; live sales handoff is disabled", 403)
        if context.is_sandbox or run.get("is_sandbox"):
            await repository.append_audit("adapter.handoff.sandbox_simulated", "product_adapter",
                self.product_id, self.product_id, {"outbound_actions": 0, "is_sandbox": True})
            return {"status": "SIMULATED", "product_id": self.product_id,
                    "api_version": self.api_version, "outbound_actions": 0}
        if handoff_handler is None:
            async def handoff_handler(**data):
                # No network delivery is performed here; this records a governed
                # handoff request for the product-side adapter to consume.
                return {"status": "HANDOFF_AUTHORISED", "product_id": self.product_id,
                        "handoff": data, "delivery": "not_performed"}
        registry = ToolRegistry()
        self.register_handoff_tool(registry, handler=handoff_handler)
        return await ExecutionBoundaryGuard(repository, registry).execute(
            tool_name=f"{self.product_id}.handoff", principal=principal, employee=employee,
            run=run, payload=payload, approval_id=approval_id)

    async def handle_webhook(self, *, raw_body: bytes, signature: str | None, timestamp: str | None,
                             external_tenant_id: str, workforce_tenant_id: str,
                             idempotency_key: str, correlation_id: str, repository) -> dict[str, Any]:
        self.verify(raw_body, signature, timestamp, external_tenant_id=external_tenant_id,
                    workforce_tenant_id=workforce_tenant_id)
        try:
            payload = json.loads(raw_body)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid webhook JSON") from exc
        if not isinstance(payload, dict) or payload.get("product_id") != self.product_id:
            raise AdapterGovernanceError("Webhook product binding mismatch", 403)
        if payload.get("tenant_id") != external_tenant_id:
            raise AdapterGovernanceError("Webhook external tenant binding mismatch", 403)
        event = payload.get("event")
        if not isinstance(event, dict) or not event.get("event_id") or not event.get("event_type"):
            raise ValueError("Webhook event_id and event_type are required")
        store = IdempotencyStore(repository)
        claim = await store.begin(tenant_id=workforce_tenant_id, external_tenant_id=external_tenant_id,
            product_id=self.product_id, direction="inbound", key=idempotency_key,
            payload=payload, correlation_id=correlation_id)
        if not claim.get("created"):
            if claim.get("response") is not None:
                return {**claim["response"], "replayed": True}
            return {"status": "PROCESSING", "replayed": True, "product_id": self.product_id}
        await repository.record_product_telemetry(tenant_id=workforce_tenant_id,
            external_tenant_id=external_tenant_id, product_id=self.product_id,
            event={"event_id": str(event["event_id"]), "event_type": str(event["event_type"]),
                   "recognised_revenue_inr": event.get("recognised_revenue_inr", 0),
                   "metadata": event.get("metadata") or {}, "source_ref": event.get("source_ref")},
            correlation_id=correlation_id)
        response = {"status": "ACCEPTED", "product_id": self.product_id,
                    "event_id": str(event["event_id"]), "api_version": self.api_version}
        await store.complete(tenant_id=workforce_tenant_id, external_tenant_id=external_tenant_id,
            product_id=self.product_id, direction="inbound", key=idempotency_key, response=response)
        return response
