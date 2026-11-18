from datetime import datetime,timezone
from uuid import uuid4
import re
from fastapi import FastAPI,Depends,HTTPException
from fastapi.middleware.cors import CORSMiddleware
from .config import settings
from .registry import EMPLOYEES,BY_ID,DEPARTMENTS,public_registry
from .policy import Principal,evaluate
from .security import current_principal,require_permission
from .schemas import CommandRequest,TaskCreate,ApprovalDecision,MemoryWrite
app=FastAPI(title="RunGrow Workforce API",version="0.1.0")
app.add_middleware(CORSMiddleware,allow_origins=settings.allowed_origins,allow_credentials=True,allow_methods=["GET","POST"],allow_headers=["Authorization","Content-Type","Idempotency-Key"])
# Local adapter only. Production deployment must replace this with PostgreSQL repositories and a durable outbox.
STATE={"approvals":[],"tasks":[],"audit":[],"memory":[]}
def audit(p:Principal,action:str,kind:str,rid:str,outcome:str):
    STATE["audit"].append({"id":str(uuid4()),"tenant_id":p.tenant_id,"actor_id":p.user_id,"actor_type":p.actor_type,"action":action,"resource_type":kind,"resource_id":rid,"outcome":outcome,"at":datetime.now(timezone.utc).isoformat()})
def route(text:str):
    t=text.lower()
    mapping=[(("sales","pipeline","lead","prospect","deal"),("Sales & Revenue","sales-ops","crm.read")),(("campaign","seo","content","market"),("Growth & Marketing","market-intel","research.public")),(("customer","onboarding","renewal","adoption"),("Customer Success","cs-director","customer.read")),(("support","ticket","issue"),("Customer Support","support-lead","support.read")),(("product","roadmap","feature"),("Product & Design","pm","product.read")),(("code","engineering","deploy","bug"),("Engineering & IT","cto","engineering.read")),(("invoice","finance","billing","vendor"),("Finance & Administration","finance","finance.read")),(("hiring","candidate","employee"),("People & Culture","people-ops","people.read")),(("process","operations"),("Business Operations","bizops","operations.read")),(("contract","privacy","compliance","legal"),("Legal, Risk & Compliance","legal","compliance.read"))]
    for words,result in mapping:
        if any(w in t for w in words): return result
    return ("Executive Office","chief-staff","reports.read")
@app.get("/health")
async def health(): return {"status":"ok","service":"rungrow-workforce-api","adapter":"local-memory"}
@app.get("/v1/workforce/registry")
async def registry(p:Principal=Depends(require_permission("workforce.read"))): return {"departments":list(DEPARTMENTS),"employees":public_registry(),"count":len(EMPLOYEES)}
@app.post("/v1/workforce/commands")
async def command(body:CommandRequest,p:Principal=Depends(current_principal)):
    dept,role_id,permission=route(body.command); role=BY_ID[role_id]; cid=str(uuid4())
    if permission not in p.permissions and "*" not in p.permissions:
        audit(p,"command.route","command",cid,"denied")
        return {"status":"denied","message":"Your workspace role is not authorised for this department data.","command_id":cid}
    if re.search(r"\\b(send|launch|activate|charge|refund|delete|export|deploy|sign|purchase|discount|change price)\\b",body.command,re.I):
        aid=str(uuid4()); STATE["approvals"].append({"id":aid,"tenant_id":p.tenant_id,"requested_by":p.user_id,"action":"command.execution","payload":{"command":body.command,"role_id":role_id},"status":"pending"})
        audit(p,"command.approval_requested","approval",aid,"pending")
        return {"status":"approval_required","message":role.title+" is the proposed owner. Human approval is required before execution.","command_id":cid,"approval_id":aid,"routed_department":dept}
    audit(p,"command.accepted","command",cid,"accepted")
    return {"status":"accepted","message":"Routed to "+role.title+" in "+dept+". Accepted for planning; the durable worker is not connected in local mode.","command_id":cid,"routed_department":dept,"routed_role_id":role_id}
@app.post("/v1/workforce/tasks")
async def create_task(body:TaskCreate,p:Principal=Depends(require_permission("work.assign"))):
    if body.assignee_role_id not in BY_ID: raise HTTPException(404,"Unknown employee role")
    item={"id":str(uuid4()),"tenant_id":p.tenant_id,"title":body.title,"description":body.description,"assignee_role_id":body.assignee_role_id,"priority":body.priority,"status":"queued","created_by":p.user_id}
    STATE["tasks"].append(item); audit(p,"task.create","task",item["id"],"created"); return item
@app.get("/v1/workforce/tasks")
async def tasks(p:Principal=Depends(require_permission("work.read"))): return [x for x in STATE["tasks"] if x["tenant_id"]==p.tenant_id]
@app.get("/v1/workforce/approvals")
async def approvals(p:Principal=Depends(require_permission("approval.read"))): return [x for x in STATE["approvals"] if x["tenant_id"]==p.tenant_id]
@app.post("/v1/workforce/approvals/{approval_id}/decision")
async def decide(approval_id:str,body:ApprovalDecision,p:Principal=Depends(require_permission("approval.decide"))):
    item=next((x for x in STATE["approvals"] if x["id"]==approval_id and x["tenant_id"]==p.tenant_id),None)
    if not item: raise HTTPException(404,"Approval not found")
    if item["status"]!="pending": raise HTTPException(409,"Approval already decided")
    item.update({"status":body.decision,"decided_by":p.user_id,"reason":body.reason,"decided_at":datetime.now(timezone.utc).isoformat()})
    audit(p,"approval.decision","approval",approval_id,body.decision)
    return {"id":approval_id,"status":item["status"],"execution":"not_started"}
@app.post("/v1/workforce/memory")
async def memory(body:MemoryWrite,p:Principal=Depends(require_permission("memory.write"))):
    if body.role_id not in BY_ID: raise HTTPException(404,"Unknown role")
    item={"id":str(uuid4()),"tenant_id":p.tenant_id,"role_id":body.role_id,"department":BY_ID[body.role_id].department,"content":body.content,"source_ref":body.source_ref,"retention_days":body.retention_days}
    STATE["memory"].append(item); audit(p,"memory.write","memory",item["id"],"created"); return {"id":item["id"],"scope":"tenant+department+role"}
@app.get("/v1/workforce/audit")
async def audit_events(p:Principal=Depends(require_permission("audit.read"))): return [x for x in STATE["audit"] if x["tenant_id"]==p.tenant_id][-500:]
