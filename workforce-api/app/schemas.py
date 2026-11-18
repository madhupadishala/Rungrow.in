from pydantic import BaseModel, Field
from typing import Literal


class StrictRequest(BaseModel):
    class Config:
        extra = "forbid"


class CommandRequest(StrictRequest):
    command: str = Field(min_length=3, max_length=2000)
    channel: Literal["text", "voice"] = "text"
    product_id: str | None = Field(default=None, min_length=2, max_length=100)


class TaskCreate(StrictRequest):
    title: str = Field(min_length=3, max_length=240)
    description: str = Field(default="", max_length=5000)
    assignee_role_id: str = Field(min_length=2, max_length=100)
    product_id: str | None = Field(default=None, min_length=2, max_length=100)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"


class ApprovalDecision(StrictRequest):
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=3, max_length=1000)
    product_id: str | None = Field(default=None, min_length=2, max_length=100)


class ApprovalExecutionValidation(StrictRequest):
    product_id: str | None = None
    employee_id: str = Field(min_length=2, max_length=100)
    action_type: str = Field(min_length=2, max_length=120)
    payload: dict


class MemoryWrite(StrictRequest):
    role_id: str = Field(min_length=2, max_length=100)
    product_id: str | None = Field(default=None, min_length=2, max_length=100)
    content: str = Field(min_length=1, max_length=10000)
    source_ref: str = Field(min_length=1, max_length=2000)
    retention_days: int = Field(default=90, ge=1, le=3650)


class AgentRunRequest(StrictRequest):
    role_id: str = Field(min_length=2, max_length=100)
    product_id: str | None = Field(default=None, min_length=2, max_length=100)
    task: str = Field(min_length=3, max_length=5000)
    source_refs: list[str] = Field(default_factory=list, max_length=30)
