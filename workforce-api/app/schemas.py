from pydantic import BaseModel,Field
from typing import Literal
class CommandRequest(BaseModel):
    command:str=Field(min_length=3,max_length=2000)
    channel:Literal["text","voice"]="text"
class TaskCreate(BaseModel):
    title:str=Field(min_length=3,max_length=240)
    description:str=""
    assignee_role_id:str
    priority:Literal["low","normal","high","urgent"]="normal"
class ApprovalDecision(BaseModel):
    decision:Literal["approved","rejected"]
    reason:str=Field(min_length=3,max_length=1000)
class MemoryWrite(BaseModel):
    role_id:str
    content:str=Field(min_length=1,max_length=10000)
    source_ref:str
    retention_days:int=Field(default=90,ge=1,le=3650)
