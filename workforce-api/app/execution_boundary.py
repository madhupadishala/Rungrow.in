"""Tool registry and fail-closed execution boundary."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from datetime import datetime, timezone
from .policy import SENSITIVE
import hashlib,json

class ApprovalBoundaryError(PermissionError):
    def __init__(self,message,status_code=403):
        super().__init__(message); self.status_code=status_code

class SandboxViolation(PermissionError): pass

@dataclass(frozen=True)
class ToolDefinition:
    tool_name: str
    required_scopes: frozenset[str]
    requires_human_approval: bool
    allowed_in_sandbox: bool
    action_type: str
    handler: Callable[...,Awaitable[Any]]
    allowed_departments: frozenset[str] = frozenset()

class ToolRegistry:
    def __init__(self): self._tools={}
    def register(self,tool: ToolDefinition):
        if tool.tool_name in self._tools: raise ValueError("Tool already registered")
        self._tools[tool.tool_name]=tool
    def get(self,name):
        if name not in self._tools: raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

class ExecutionBoundaryGuard:
    def __init__(self,repository,registry: ToolRegistry): self.repository=repository; self.registry=registry
    async def execute(self, *, tool_name: str, principal, employee, run: dict, payload: dict, approval_id: str | None=None):
        tool=self.registry.get(tool_name)
        missing=tool.required_scopes-set(principal.permissions)
        if "*" not in principal.permissions and missing:
            raise ApprovalBoundaryError("Principal lacks required tool scopes",403)
        employee_missing=tool.required_scopes-set(employee.permissions)
        if "*" not in employee.permissions and employee_missing:
            raise ApprovalBoundaryError("Employee role lacks tool scopes",403)
        if tool.allowed_departments and employee.department not in tool.allowed_departments:
            raise ApprovalBoundaryError("Employee department is not authorised for this tool",403)
        if employee.product_id and employee.product_id != run.get("product_id"):
            raise ApprovalBoundaryError("Employee product boundary mismatch",403)
        if employee.id != run.get("employee_id"):
            raise ApprovalBoundaryError("Employee identity mismatch",403)
        if run.get("tenant_id") != principal.tenant_id:
            raise ApprovalBoundaryError("Tenant boundary mismatch",403)
        if run.get("is_sandbox") and not tool.allowed_in_sandbox:
            await self.repository.append_audit("sandbox.violation","tool",tool_name,run.get("product_id"),{"run_id":run.get("id")})
            await self.repository.commit_boundary()
            raise SandboxViolation("Tool is prohibited in sandbox mode")
        consumed=None
        approval_required=tool.requires_human_approval or tool.action_type in SENSITIVE
        if approval_required:
            if not approval_id:
                await self.repository.pause_run_for_approval(run["id"],tool.action_type,payload)
                await self.repository.commit_boundary()
                raise ApprovalBoundaryError("Run paused: human approval required",409)
            try:
                consumed=await self.repository.consume_approval(approval_id,product_id=run.get("product_id"),
                    employee_id=employee.id,action_type=tool.action_type,payload=payload,run_id=run["id"])
            except Exception as exc:
                await self.repository.append_audit("approval.execution_denied","tool",tool_name,run.get("product_id"),{"run_id":run.get("id"),"reason":str(exc)})
                await self.repository.commit_boundary()
                raise ApprovalBoundaryError(str(exc),409) from exc
        await self.repository.append_audit("tool.execution.started","tool",tool_name,run.get("product_id"),{"run_id":run["id"],"approval_id":approval_id})
        await self.repository.commit_boundary()
        try:
            result=await tool.handler(**payload)
        except Exception as exc:
            await self.repository.append_audit("tool.execution.failed","tool",tool_name,run.get("product_id"),{"run_id":run["id"],"error":str(exc)})
            await self.repository.commit_boundary()
            raise
        await self.repository.append_audit("tool.execution.completed","tool",tool_name,run.get("product_id"),{"run_id":run["id"],"approval_id":approval_id})
        return result
