"""Photo Studio Growth OS v1 adapter; product remains its own system of record."""
from __future__ import annotations
import hashlib, json, os, time
from uuid import uuid4
from decimal import Decimal, InvalidOperation
from datetime import datetime
import httpx
from .base import AdapterContext, IdempotencyStore, PRODUCT_ID, SignedWebhookVerifier
from ..execution_boundary import ToolDefinition

class PhotoStudioGrowthOSAdapter:
    product_id = PRODUCT_ID
    api_version = "v1"
    READ_SCOPE = "photo-studio-growth-os:read"
    WRITE_SCOPE = "photo-studio-growth-os:write"

    def __init__(self, base_url=None, service_secret=None, *, transport=None, timeout=10.0):
        self.base_url = (base_url or os.getenv("STUDIO_GROWTH_OS_API_URL", "http://studio-growth-os")).rstrip("/")
        self.secret = service_secret or os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET", "")
        self.verifier = SignedWebhookVerifier(self.secret) if self.secret else None
        try:
            self.tenant_service_tokens = json.loads(os.getenv("STUDIO_GROWTH_OS_TENANT_SERVICE_TOKENS", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("STUDIO_GROWTH_OS_TENANT_SERVICE_TOKENS must be a JSON object") from exc
        if not isinstance(self.tenant_service_tokens, dict):
            raise ValueError("STUDIO_GROWTH_OS_TENANT_SERVICE_TOKENS must be a JSON object")
        try:
            self.tenant_map = json.loads(os.getenv("STUDIO_GROWTH_OS_TENANT_MAP", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("STUDIO_GROWTH_OS_TENANT_MAP must be a JSON object") from exc
        if not isinstance(self.tenant_map, dict):
            raise ValueError("STUDIO_GROWTH_OS_TENANT_MAP must be a JSON object")
        self.transport, self.timeout = transport, timeout

    def _headers(self, context: AdapterContext, body=b""):
        if context.product_id != self.product_id: raise PermissionError("Product adapter binding mismatch")
        if not self.secret: raise RuntimeError("STUDIO_GROWTH_OS_SERVICE_SECRET is required")
        timestamp = str(int(time.time()))
        signature = self.verifier.sign(body, timestamp, tenant_id=context.tenant_id,
            product_id=self.product_id, workforce_tenant_id=context.workforce_tenant_id)
        headers = {"X-RunGrow-Signature":signature, "X-RunGrow-Timestamp":timestamp,
                   "X-Correlation-ID":context.correlation_id, "X-RunGrow-Tenant-ID":context.tenant_id,
                   "X-RunGrow-Org-ID":context.workforce_tenant_id,
                   "X-RunGrow-Product-ID":self.product_id, "Content-Type":"application/json"}
        tenant_token = self.tenant_service_tokens.get(context.tenant_id)
        if tenant_token:
            headers["Authorization"] = "Bearer " + tenant_token
        return headers

    async def _request(self, method, path, context, *, params=None, payload=None, idempotency_key=None):
        body = json.dumps(payload or {}, separators=(",",":"), ensure_ascii=False).encode()
        headers = self._headers(context, body)
        if idempotency_key: headers["Idempotency-Key"] = idempotency_key
        async with httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=self.timeout) as client:
            response = await client.request(method, path, params=params, content=body, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else {}

    def _bind(self, tenant_id, context, scope):
        if tenant_id != context.tenant_id: raise PermissionError("External customer tenant mismatch")
        if context.product_id != PRODUCT_ID: raise PermissionError("Product scope mismatch")
        if not context.workforce_tenant_id or context.workforce_tenant_id == context.tenant_id:
            raise PermissionError("External and internal tenant scopes must remain distinct")
        if self.tenant_map.get(tenant_id) != context.workforce_tenant_id:
            raise PermissionError("External tenant is not mapped to this Workforce organization")
        context.require(scope)

    async def get_tenant_summary(self, tenant_id, *, context):
        self._bind(tenant_id, context, self.READ_SCOPE)
        return await self._request("GET", "/api/v1/analytics/dashboard", context, params={"range":"30d"})

    async def list_leads(self, tenant_id, status=None, limit=50, *, context):
        self._bind(tenant_id, context, self.READ_SCOPE)
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        params = {"page":1, "page_size":limit}
        if status: params["stage"] = status
        return await self._request("GET", "/api/v1/leads", context, params=params)

    async def list_quotes(self, tenant_id, status=None, limit=50, *, context):
        self._bind(tenant_id, context, self.READ_SCOPE)
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        return await self._request("GET", "/api/v1/quotes", context,
                                   params={"status":status,"page":1,"page_size":limit})

    async def list_bookings(self, tenant_id, status=None, limit=50, *, context):
        self._bind(tenant_id, context, self.READ_SCOPE)
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        return await self._request("GET", "/api/v1/bookings", context,
                                   params={"state":status,"page":1,"page_size":limit})

    async def get_conversation_context(self, tenant_id, lead_id, *, context):
        self._bind(tenant_id, context, self.READ_SCOPE)
        return await self._request("GET", "/api/v1/conversations", context,
                                   params={"lead_id":lead_id,"include_messages":"true"})

    async def _governed_write(self, path, payload, context, key, repository):
        self._bind(context.tenant_id, context, self.WRITE_SCOPE)
        if context.is_sandbox: raise PermissionError("Product writes are prohibited in sandbox mode")
        store = IdempotencyStore(repository)
        claim = await store.begin(tenant_id=context.workforce_tenant_id,
            external_tenant_id=context.tenant_id, product_id=self.product_id,
            direction="outbound", key=key, payload=payload, correlation_id=context.correlation_id)
        if not claim.get("created"): return claim.get("response") or {"status":"duplicate","idempotent":True}
        # Commit the durable PROCESSING claim before the remote side effect.
        # A crash after the remote accepts the command can never make a retry send it twice.
        await repository.commit_boundary()
        result = await self._request("POST", path, context, payload=payload, idempotency_key=key)
        await store.complete(tenant_id=context.workforce_tenant_id,
            external_tenant_id=context.tenant_id, product_id=self.product_id,
            direction="outbound", key=key, response=result)
        return result

    async def _send_lead_followup(self, tenant_id, workforce_tenant_id, lead_id, message_payload,
                                  idempotency_key, correlation_id, repository):
        ctx = AdapterContext(tenant_id, scopes=frozenset({self.WRITE_SCOPE}),
            correlation_id=correlation_id, workforce_tenant_id=workforce_tenant_id)
        quote_id = message_payload.get("quote_id")
        if not quote_id:
            raise ValueError("message_payload.quote_id is required by the Studio Growth OS send_quote tool")
        channel = message_payload.get("channel", "whatsapp")
        if channel not in {"whatsapp","email","instagram_dm"}:
            raise ValueError("Unsupported Studio Growth OS quote delivery channel")
        quote = await self._request("GET", f"/api/v1/quotes/{quote_id}", ctx)
        if quote.get("tenant_id") != tenant_id or quote.get("lead_id") != lead_id:
            raise PermissionError("Quote does not belong to the supplied external tenant and lead")
        tool_call = {"tool":"send_quote","arguments":{"quote_id":quote_id,"channel":channel,
            "template_vars":message_payload.get("template_vars") or {}},
            "idempotency_key":idempotency_key}
        return await self._governed_write("/api/v1/agent/execute-tool",
            tool_call, ctx, idempotency_key, repository)

    async def _handoff_prospect(self, tenant_id, workforce_tenant_id, prospect_payload,
                                idempotency_key, correlation_id, repository):
        if not prospect_payload.get("contact_id"):
            raise ValueError("prospect_payload.contact_id is required by Studio Growth OS LeadCreate")
        ctx = AdapterContext(tenant_id, scopes=frozenset({self.WRITE_SCOPE}),
            correlation_id=correlation_id, workforce_tenant_id=workforce_tenant_id)
        return await self._governed_write("/api/v1/leads", prospect_payload, ctx, idempotency_key, repository)

    async def dispatch_governed_lead_followup(self, tenant_id, lead_id, message_payload,
            approval_id, idempotency_key, *, repository, principal, employee, run, correlation_id=None):
        if self.tenant_map.get(tenant_id) != principal.tenant_id:
            raise PermissionError("External tenant is not mapped to authenticated Workforce organization")
        ctx = AdapterContext(tenant_id, scopes=frozenset({self.WRITE_SCOPE}),
            correlation_id=correlation_id or str(uuid4()), is_sandbox=bool(run.get("is_sandbox")),
            workforce_tenant_id=principal.tenant_id)
        payload = {"tenant_id":tenant_id,"workforce_tenant_id":principal.tenant_id,
                   "lead_id":lead_id,"message_payload":message_payload,
                   "idempotency_key":idempotency_key,"correlation_id":ctx.correlation_id}
        from ..execution_boundary import ExecutionBoundaryGuard, ToolRegistry
        registry = ToolRegistry()
        registry.register(ToolDefinition("studio_growth_os.lead_followup",frozenset({self.WRITE_SCOPE}),
            True,False,"whatsapp.send",lambda **kw:self._send_lead_followup(**kw,repository=repository)))
        return await ExecutionBoundaryGuard(repository,registry).execute(tool_name="studio_growth_os.lead_followup",
            principal=principal,employee=employee,run=run,payload=payload,approval_id=approval_id)

    async def register_qualified_prospect_handoff(self, tenant_id, prospect_payload, approval_id,
            idempotency_key, *, repository, principal, employee, run, correlation_id=None):
        if tenant_id != principal.tenant_id: raise PermissionError("Outbound tenant does not match authenticated principal")
        ctx = AdapterContext(tenant_id, scopes=frozenset({self.WRITE_SCOPE}),
            correlation_id=correlation_id or str(uuid4()), is_sandbox=bool(run.get("is_sandbox")))
        payload = {"tenant_id":tenant_id,"workforce_tenant_id":principal.tenant_id,
                   "prospect_payload":prospect_payload,"idempotency_key":idempotency_key,
                   "correlation_id":ctx.correlation_id}
        from ..execution_boundary import ExecutionBoundaryGuard, ToolRegistry
        registry = ToolRegistry()
        registry.register(ToolDefinition("studio_growth_os.prospect_handoff",frozenset({self.WRITE_SCOPE}),
            True,False,"lead.bulk_contact",lambda **kw:self._handoff_prospect(**kw,repository=repository)))
        return await ExecutionBoundaryGuard(repository,registry).execute(tool_name="studio_growth_os.prospect_handoff",
            principal=principal,employee=employee,run=run,payload=payload,approval_id=approval_id)

    async def handle_webhook(self, *, body, signature, timestamp, tenant_id, product_id,
                             idempotency_key, correlation_id, repository):
        if not self.verifier: raise RuntimeError("STUDIO_GROWTH_OS_SERVICE_SECRET is required")
        payload = json.loads(body)
        workforce_tenant_id = payload.get("workforce_tenant_id")
        self.verifier.verify(body, signature, timestamp, tenant_id=tenant_id,
            product_id=product_id, workforce_tenant_id=workforce_tenant_id)
        if payload.get("tenant_id") != tenant_id or payload.get("product_id") != self.product_id:
            raise PermissionError("Webhook tenant/product binding mismatch")
        workforce_tenant_id = payload.get("workforce_tenant_id")
        if workforce_tenant_id != repository.principal.tenant_id or workforce_tenant_id == tenant_id:
            raise PermissionError("Webhook internal Workforce tenant binding mismatch")
        if self.tenant_map.get(tenant_id) != workforce_tenant_id:
            raise PermissionError("Webhook external tenant is not mapped to Workforce organization")
        event_id = str(payload.get("event_id") or idempotency_key)
        event_type = payload.get("event_type")
        allowed = {"studio_tenant_onboarded","trial_started","lead_converted","booking_confirmed",
                   "invoice_recognised","churn_risk_flagged"}
        if event_type not in allowed: raise ValueError("Unsupported product telemetry event type")
        try:
            revenue = Decimal(str(payload.get("recognised_revenue_inr",0) or 0))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Recognised revenue must be a valid INR amount") from exc
        if not revenue.is_finite() or revenue < 0 or (event_type != "invoice_recognised" and revenue != 0):
            raise ValueError("Recognised revenue is allowed only on invoice_recognised events")
        occurred_at = payload.get("occurred_at")
        if isinstance(occurred_at, str):
            try:
                occurred_at = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("occurred_at must be an ISO-8601 timestamp") from exc
        event = {"event_id":event_id,"event_type":event_type,"recognised_revenue_inr":revenue,
                 "occurred_at":occurred_at,"source_ref":payload.get("source_ref"),
                 "metadata":payload.get("metadata") or {}}
        store = IdempotencyStore(repository)
        claim = await store.begin(tenant_id=workforce_tenant_id,external_tenant_id=tenant_id,
            product_id=self.product_id,direction="inbound",key=idempotency_key,
            payload=payload,correlation_id=correlation_id)
        if not claim.get("created"):
            return {"status":"accepted","idempotent":True,"event_id":event_id,"correlation_id":correlation_id}
        inserted = await repository.record_product_telemetry(tenant_id=workforce_tenant_id,
            external_tenant_id=tenant_id,product_id=self.product_id,event=event,
            correlation_id=correlation_id)
        response = {"status":"accepted","idempotent":not bool(inserted),
                    "event_id":event_id,"correlation_id":correlation_id}
        await store.complete(tenant_id=workforce_tenant_id,external_tenant_id=tenant_id,
            product_id=self.product_id,direction="inbound",key=idempotency_key,response=response)
        return response

def register_photo_studio_growth_os_tools(registry, adapter, repository_factory):
    """Register product-scoped read and governed write tools for Workforce roles."""
    def assert_tenant(tenant_id):
        if tenant_id == repository_factory.principal.tenant_id or adapter.tenant_map.get(tenant_id) != repository_factory.principal.tenant_id:
            raise PermissionError("External tenant is not mapped to authenticated Workforce organization")
    async def read_leads(tenant_id, status=None, limit=50, correlation_id=None):
        assert_tenant(tenant_id)
        ctx=AdapterContext(tenant_id,scopes=frozenset({adapter.READ_SCOPE}),
            correlation_id=correlation_id or str(uuid4()),workforce_tenant_id=repository_factory.principal.tenant_id)
        return await adapter.list_leads(tenant_id,status,limit,context=ctx)
    async def read_quotes(tenant_id,status=None,limit=50,correlation_id=None):
        assert_tenant(tenant_id)
        ctx=AdapterContext(tenant_id,scopes=frozenset({adapter.READ_SCOPE}),
            correlation_id=correlation_id or str(uuid4()),workforce_tenant_id=repository_factory.principal.tenant_id)
        return await adapter.list_quotes(tenant_id,status,limit,context=ctx)
    async def read_bookings(tenant_id,status=None,limit=50,correlation_id=None):
        assert_tenant(tenant_id)
        ctx=AdapterContext(tenant_id,scopes=frozenset({adapter.READ_SCOPE}),
            correlation_id=correlation_id or str(uuid4()),workforce_tenant_id=repository_factory.principal.tenant_id)
        return await adapter.list_bookings(tenant_id,status,limit,context=ctx)
    async def read_summary(tenant_id,correlation_id=None):
        assert_tenant(tenant_id)
        ctx=AdapterContext(tenant_id,scopes=frozenset({adapter.READ_SCOPE}),
            correlation_id=correlation_id or str(uuid4()),workforce_tenant_id=repository_factory.principal.tenant_id)
        return await adapter.get_tenant_summary(tenant_id,context=ctx)
    async def read_conversation(tenant_id,lead_id,correlation_id=None):
        assert_tenant(tenant_id)
        ctx=AdapterContext(tenant_id,scopes=frozenset({adapter.READ_SCOPE}),
            correlation_id=correlation_id or str(uuid4()),workforce_tenant_id=repository_factory.principal.tenant_id)
        return await adapter.get_conversation_context(tenant_id,lead_id,context=ctx)
    async def governed_followup(tenant_id, lead_id, message_payload, idempotency_key, correlation_id):
        assert_tenant(tenant_id)
        return await adapter._send_lead_followup(tenant_id,repository_factory.principal.tenant_id,
            lead_id,message_payload,idempotency_key,correlation_id,repository_factory)
    async def governed_handoff(tenant_id, prospect_payload, idempotency_key, correlation_id):
        assert_tenant(tenant_id)
        return await adapter._handoff_prospect(tenant_id,repository_factory.principal.tenant_id,
            prospect_payload,idempotency_key,correlation_id,repository_factory)
    for name,handler in (("studio_growth_os.summary.read",read_summary),
                         ("studio_growth_os.leads.read",read_leads),
                         ("studio_growth_os.quotes.read",read_quotes),
                         ("studio_growth_os.bookings.read",read_bookings),
                         ("studio_growth_os.conversation.read",read_conversation)):
        registry.register(ToolDefinition(name,frozenset({adapter.READ_SCOPE}),False,True,"product.read",handler))
    registry.register(ToolDefinition("studio_growth_os.lead_followup",frozenset({adapter.WRITE_SCOPE}),
        True,False,"whatsapp.send",governed_followup))
    registry.register(ToolDefinition("studio_growth_os.prospect_handoff",frozenset({adapter.WRITE_SCOPE}),
        True,False,"lead.bulk_contact",governed_handoff))
    return registry
