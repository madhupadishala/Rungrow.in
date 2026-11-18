from pydantic import BaseModel,Field
from typing import Literal

class CommandRequest(BaseModel):
    command:str=Field(min_length=3,max_length=2000)
    channel:Literal["text","voice"]="text"
    product_id:str|None=Field(default=None,min_length=2,max_length=100)

class TaskCreate(BaseModel):
    title:str=Field(min_length=3,max_length=240)
    description:str=""
    assignee_role_id:str
    product_id:str|None=Field(default=None,min_length=2,max_length=100)
    priority:Literal["low","normal","high","urgent"]="normal"

class ApprovalDecision(BaseModel):
    decision:Literal["approved","rejected"]
    reason:str=Field(min_length=3,max_length=1000)

class MemoryWrite(BaseModel):
    role_id:str
    product_id:str|None=Field(default=None,min_length=2,max_length=100)
    content:str=Field(min_length=1,max_length=10000)
    source_ref:str
    retention_days:int=Field(default=90,ge=1,le=3650)

class AgentRunRequest(BaseModel):
    role_id:str
    product_id:str|None=Field(default=None,min_length=2,max_length=100)
    task:str=Field(min_length=3,max_length=5000)
    source_refs:list[str]=Field(default_factory=list,max_length=30)
