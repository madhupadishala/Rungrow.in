"""Governed live communication gateways. Every send passes the execution boundary."""
from __future__ import annotations
import os
import httpx
from .execution_boundary import ExecutionBoundaryGuard, ToolDefinition, ToolRegistry
from .commercial_intelligence import AI_IDENTITY_DISCLOSURE

class IntegrationNotConfiguredError(RuntimeError):
    status_code=503

class CommunicationGateway:
    action_type=""
    tool_name=""
    required_env=()
    def __init__(self, repository, *, http_client=None, timeout=15.0):
        self.repository=repository
        self.http_client=http_client
        self.timeout=timeout

    async def _post(self,url,headers,payload):
        if self.http_client:
            response=await self.http_client.post(url,headers=headers,json=payload,timeout=self.timeout)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response=await client.post(url,headers=headers,json=payload)
        response.raise_for_status()
        return response.json()

    async def send(self, *, principal, employee, run: dict, payload: dict, approval_id: str | None):
        if run.get("is_sandbox") is True:
            await self.repository.append_audit("communication.sandbox_blocked","communication",
                self.tool_name,run.get("product_id"),{"run_id":run.get("id")})
            await self.repository.commit_boundary()
            raise PermissionError("Outbound communication is prohibited in sandbox mode")
        missing=[key for key in self.required_env if not os.getenv(key,"").strip()]
        if missing:
            await self.repository.append_audit("communication.integration_not_configured","communication",
                self.tool_name,run.get("product_id"),{"run_id":run.get("id"),"missing_configuration":missing})
            await self.repository.commit_boundary()
            raise IntegrationNotConfiguredError("Live communication provider is not configured: "+", ".join(missing))
        registry=ToolRegistry()
        async def handler(**data):
            missing=[key for key in self.required_env if not os.getenv(key,"").strip()]
            if missing:
                raise IntegrationNotConfiguredError("Live communication provider is not configured: "+", ".join(missing))
            message=str(data.get("message","")).strip()
            if not message:
                raise ValueError("Message body is required")
            # Disclosure is always added by the gateway, not trusted to caller-provided copy.
            message=message.rstrip()+"\n\n"+AI_IDENTITY_DISCLOSURE
            data["message"]=message
            return await self._send_provider(data)
        registry.register(ToolDefinition(self.tool_name,frozenset({"communication.send"}),
            True,False,self.action_type,handler))
        return await ExecutionBoundaryGuard(self.repository,registry).execute(
            tool_name=self.tool_name,principal=principal,employee=employee,run=run,
            payload=payload,approval_id=approval_id)

class WhatsAppCloudGateway(CommunicationGateway):
    action_type="whatsapp.send"
    tool_name="communication.whatsapp.send"
    required_env=("WHATSAPP_ACCESS_TOKEN","WHATSAPP_PHONE_NUMBER_ID")
    async def _send_provider(self,data):
        token=os.environ["WHATSAPP_ACCESS_TOKEN"]
        phone_id=os.environ["WHATSAPP_PHONE_NUMBER_ID"]
        version=os.getenv("WHATSAPP_GRAPH_API_VERSION","v21.0")
        body={"messaging_product":"whatsapp","to":data["to"],"type":"text",
              "text":{"preview_url":False,"body":data["message"]}}
        return await self._post(f"https://graph.facebook.com/{version}/{phone_id}/messages",
            {"Authorization":f"Bearer {token}","Content-Type":"application/json"},body)

class TransactionalEmailGateway(CommunicationGateway):
    action_type="email.send"
    tool_name="communication.email.send"
    required_env=("EMAIL_API_KEY","EMAIL_FROM_ADDRESS")
    async def _send_provider(self,data):
        import base64
        import hashlib
        from pathlib import Path
        body={"from":os.environ["EMAIL_FROM_ADDRESS"],
              "to":data["to"] if isinstance(data["to"],list) else [data["to"]],
              "subject":data["subject"],"text":data["message"]}
        attachments=[]
        artifact_root=Path(os.getenv("RUNGROW_ARTIFACT_ROOT","/tmp/rungrow-artifacts")).resolve()
        for item in data.get("attachments",[]):
            path=Path(str(item.get("path",""))).resolve()
            if not path.is_relative_to(artifact_root) or not path.is_file():
                raise IntegrationNotConfiguredError("Email attachment is not a verified RunGrow artifact")
            raw=path.read_bytes()
            digest=hashlib.sha256(raw).hexdigest()
            if item.get("sha256") and digest!=item["sha256"]:
                raise IntegrationNotConfiguredError("Email attachment checksum validation failed")
            attachments.append({"filename":path.name,"content":base64.b64encode(raw).decode("ascii")})
        if attachments:
            body["attachments"]=attachments
        return await self._post(os.getenv("EMAIL_API_URL","https://api.resend.com/emails"),
            {"Authorization":f"Bearer {os.environ['EMAIL_API_KEY']}","Content-Type":"application/json"},body)
