"""Shared authenticated product-adapter contracts."""
from __future__ import annotations
import hashlib, hmac, json, time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from uuid import uuid4

PRODUCT_ID = "photo-studio-growth-os"

@dataclass(frozen=True)
class AdapterContext:
    tenant_id: str
    product_id: str = PRODUCT_ID
    scopes: frozenset[str] = field(default_factory=frozenset)
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    actor_id: str = "rungrow-workforce"
    is_sandbox: bool = False
    workforce_tenant_id: str = ""
    def __post_init__(self):
        if not self.tenant_id or not self.workforce_tenant_id or self.tenant_id == self.workforce_tenant_id:
            raise ValueError("External customer tenant and internal Workforce tenant must be distinct")
        if self.product_id != PRODUCT_ID or not self.correlation_id:
            raise ValueError("Adapter product and correlation bindings are required")
    def require(self, scope: str):
        if scope not in self.scopes and "*" not in self.scopes:
            raise PermissionError(f"Missing adapter scope: {scope}")

@runtime_checkable
class ProductAdapterProtocol(Protocol):
    product_id: str
    api_version: str
    async def get_tenant_summary(self, tenant_id: str, *, context: AdapterContext) -> dict: ...
    async def list_leads(self, tenant_id: str, status: str | None = None, limit: int = 50, *, context: AdapterContext) -> dict: ...
    async def list_quotes(self, tenant_id: str, status: str | None = None, limit: int = 50, *, context: AdapterContext) -> list: ...
    async def list_bookings(self, tenant_id: str, status: str | None = None, limit: int = 50, *, context: AdapterContext) -> list: ...
    async def get_conversation_context(self, tenant_id: str, lead_id: str, *, context: AdapterContext) -> dict: ...
    async def dispatch_governed_lead_followup(self, tenant_id: str, lead_id: str, message_payload: dict,
        approval_id: str, idempotency_key: str, **boundary_context) -> dict: ...
    async def register_qualified_prospect_handoff(self, tenant_id: str, prospect_payload: dict,
        approval_id: str, idempotency_key: str, **boundary_context) -> dict: ...

class SignedWebhookVerifier:
    """HMAC-SHA256 over timestamp + dot + exact raw request body."""
    def __init__(self, secret: str, tolerance_seconds: int = 300):
        if not secret: raise ValueError("A product webhook secret is required")
        if tolerance_seconds < 1 or tolerance_seconds > 300:
            raise ValueError("Replay window must be between 1 and 300 seconds")
        self.secret, self.tolerance_seconds = secret.encode(), tolerance_seconds
    def sign(self, body: bytes, timestamp: int | str) -> str:
        message = str(timestamp).encode("ascii") + b"." + body
        return "sha256=" + hmac.new(self.secret, message, hashlib.sha256).hexdigest()
    def verify(self, body: bytes, signature: str | None, timestamp: str | None, *,
               tenant_id: str, product_id: str, now: float | None = None):
        if product_id != PRODUCT_ID or not tenant_id: raise PermissionError("Invalid product or tenant binding")
        if not signature or not timestamp: raise PermissionError("Missing service signature or timestamp")
        try: issued = int(timestamp)
        except (TypeError, ValueError) as exc: raise PermissionError("Invalid signature timestamp") from exc
        if abs((time.time() if now is None else now) - issued) > self.tolerance_seconds:
            raise PermissionError("Service signature timestamp outside replay window")
        expected = self.sign(body, issued)
        supplied = signature if signature.startswith("sha256=") else "sha256=" + signature
        if not hmac.compare_digest(expected, supplied): raise PermissionError("Invalid service signature")

class IdempotencyStore:
    """Durable tenant/product/direction scoped idempotency ledger."""
    def __init__(self, repository): self.repository = repository
    async def begin(self, *, tenant_id, external_tenant_id, product_id, direction, key, payload, correlation_id):
        if not key or len(key) > 200: raise ValueError("A valid Idempotency-Key is required")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()
        return await self.repository.claim_adapter_idempotency(tenant_id=tenant_id,
            external_tenant_id=external_tenant_id, product_id=product_id, direction=direction,
            idempotency_key=key, payload_hash=digest, correlation_id=correlation_id)
    async def complete(self, *, tenant_id, external_tenant_id, product_id, direction, key, response):
        await self.repository.complete_adapter_idempotency(tenant_id=tenant_id,
            external_tenant_id=external_tenant_id, product_id=product_id, direction=direction,
            idempotency_key=key, response=response)
