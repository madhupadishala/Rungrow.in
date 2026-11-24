"""Durable CEO -> department head -> specialist orchestration for Workforce 2.1."""
from __future__ import annotations
import asyncio, hashlib, json, re, time, os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, InterfaceError
import httpx
from .registry import BY_ID
from .tool_registry import WorkforceToolRegistry, ToolAuthorizationError
from .skill_registry import resolve_runtime_skill_bundle, PRODUCT_PLAYBOOKS

DEPARTMENT_PLAN = (
 {"department":"Growth & Marketing","head":"growth-director","specialist":"market-intel","tool":"public_directory_research","title":"Research target market and public directories"},
 {"department":"Sales & Revenue","head":"cro","specialist":"lead-research","tool":"icp_lead_scoring","title":"Qualify and score target prospects"},
 {"department":"Finance & Administration","head":"finance","specialist":"finance","tool":"campaign_budget_estimate","title":"Estimate campaign budget and unit economics"},
 {"department":"Legal, Risk & Compliance","head":"legal","specialist":"legal","tool":"compliance_disclosure_check","title":"Review AI disclosure and campaign compliance"},
 {"department":"Customer Success","head":"cs-director","specialist":"onboarding","tool":"product_handoff_draft","title":"Prepare customer-success handoff template"},
)

class TransientExecutionError(RuntimeError):
    """Retryable network, database, or upstream availability failure."""


class PermanentExecutionError(RuntimeError):
    """Non-retryable policy, authorization, schema, or capability failure."""


def classify_execution_error(exc: Exception) -> Exception:
    if isinstance(exc, (TransientExecutionError, PermanentExecutionError)):
        return exc
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError,
                        DBAPIError, OperationalError, InterfaceError,
                        httpx.TimeoutException, httpx.NetworkError)):
        return TransientExecutionError(str(exc))
    status = getattr(getattr(exc, "response", None), "status_code", getattr(exc, "status_code", None))
    if status == 429 or (isinstance(status, int) and status >= 500):
        return TransientExecutionError(str(exc))
    if isinstance(exc, (PermissionError, ValueError, KeyError, LookupError, ToolAuthorizationError)):
        return PermanentExecutionError(str(exc))
    return PermanentExecutionError(str(exc))


def _json(value): return json.dumps(value,ensure_ascii=False,default=str)
def _dict(row): return dict(row) if row else None

class WorkforceOrchestrator:
    def __init__(self, repository, principal, tool_registry=None):
        self.repo=repository
        self.session=repository.session
        self.principal=principal
        self.tools=tool_registry or WorkforceToolRegistry()
        self.model_gateway=None
        self.model_provider=os.getenv('WORKFORCE_MODEL_PROVIDER','mock').lower()
        if self.model_provider not in {'','mock','offline','none'}:
            from .main import get_model_gateway
            self.model_gateway=get_model_gateway(repository)

    async def submit_directive(self, command: str, product_id: str | None=None) -> dict:
        if not command or len(command.strip())<8: raise ValueError("Founder directive is too short")
        if not product_id: raise ValueError("A product scope is required for a Founder directive")
        if product_id and product_id not in {e.product_id for e in BY_ID.values() if e.product_id}:
            raise ValueError("Unknown product scope")
        if product_id and "*" not in self.principal.permissions and product_id not in self.principal.product_ids:
            raise PermissionError("Principal lacks product entitlement")
        tenant=self.principal.tenant_id; directive_id=str(uuid4())
        await self.session.execute(text("""INSERT INTO executive_directives
          (id,tenant_id,product_id,founder_id,command,status)
          VALUES (:id,:tenant,:product,:founder,:command,'PLANNING')"""),
          {"id":directive_id,"tenant":tenant,"product":product_id,"founder":self.principal.user_id,"command":command.strip()})
        city_match=re.search(r"\b(?:in|around|near)\s+([A-Z][A-Za-z .'-]{1,60}?)(?:\s|$|[,.;])",command)
        city=city_match.group(1).strip() if city_match else ""
        urls_match=re.search(r"https?://[^\s,;]+",command)
        urls=[urls_match.group(0)] if urls_match else []
        plan=list(DEPARTMENT_PLAN)
        generated={}
        if self.model_gateway is not None:
            from pydantic import BaseModel
            class DepartmentPlanItem(BaseModel):
                department: str
                title: str
                objective: str
                max_budget_cents: int
                dependencies: list[str] = []
            class CEOPlan(BaseModel):
                orders: list[DepartmentPlanItem]
            decision,_=await self.model_gateway.generate_structured(
                messages=[{"role":"system","content":"You are RunGrow Digital CEO. Return a concrete objective-specific plan for each relevant department. Use only these departments: "+", ".join(x["department"] for x in DEPARTMENT_PLAN)+". Budget is an integer INR paise ceiling, not spend authorization. Dependencies must refer to department names. Never enable live outreach or onboarding for DISCOVERY_ONLY products."},
                    {"role":"user","content":_json({"founder_objective":command,"product_id":product_id,"product_playbook":PRODUCT_PLAYBOOKS[product_id]})}],
                output_model=CEOPlan,tenant_id=tenant,product_id=product_id,employee_id="digital-ceo",run_id=directive_id)
            generated={x.department:x for x in decision.orders}
        elif product_id=="growtoys":
            # Offline plan preserves research-only discovery and removes commercial execution.
            plan=[x for x in plan if x["department"] in {"Growth & Marketing","Legal, Risk & Compliance"}]
        if product_id=="growtoys":
            allowed={"Growth & Marketing","Legal, Risk & Compliance"}
            plan=[x for x in plan if x["department"] in allowed]
            generated={k:v for k,v in generated.items() if k in allowed}
        for spec in plan:
            generated_item=generated.get(spec["department"])
            if generated_item:
                spec={**spec,"title":generated_item.title,"objective":generated_item.objective,
                    "budget":max(1,min(1000000,generated_item.max_budget_cents)),"dependencies":generated_item.dependencies}
            else:
                spec={**spec,"objective":command.strip(),"budget":10000}
            payload={"product_id":product_id,"query":command[:300],"city":city,"source_urls":urls,
                     "candidates":[],"lead_count":20,"cost_per_lead_inr":100,
                     "contingency_pct":0.15,"draft":command + " RunGrow AI assistant acting on behalf of RunGrow.","prospect":{},"sales_context":{},
                     "next_step":"human discovery","source_evidence":[],"delegated_specialists":[{"employee_id":e.id,"tool_name":tool_name,"skills":list(e.skills),"task":"Prepare independently verifiable evidence for "+spec["department"]} for e,tool_name in sorted([(x,{"market-intel":"icp_lead_scoring","lead-research":"icp_lead_scoring","outbound-sdr":"personalized_outreach_draft","finance":"campaign_budget_estimate","legal":"compliance_disclosure_check","onboarding":"product_handoff_draft"}.get(x.id)) for x in BY_ID.values() if x.department==spec["department"] and x.id!=spec["head"] and x.product_id in (None,product_id) and {"market-intel":"icp_lead_scoring","lead-research":"icp_lead_scoring","outbound-sdr":"personalized_outreach_draft","finance":"campaign_budget_estimate","legal":"compliance_disclosure_check","onboarding":"product_handoff_draft"}.get(x.id)],key=lambda pair:pair[0].id)[:2]]}
            deps=[]
            if spec["department"]=="Sales & Revenue": deps=["Growth & Marketing"]
            if spec["department"]=="Finance & Administration": deps=["Sales & Revenue"]
            if spec["department"]=="Legal, Risk & Compliance": deps=["Growth & Marketing","Sales & Revenue"] if product_id!="growtoys" else ["Growth & Marketing"]
            if spec["department"]=="Customer Success": deps=["Sales & Revenue"]
            await self.session.execute(text("""INSERT INTO department_work_orders
              (tenant_id,directive_id,product_id,department,department_head_id,title,objective,
               dependencies,specialist_role_id,tool_name,tool_input,max_budget_cents)
              VALUES (:tenant,:directive,:product,:department,:head,:title,:objective,
                CAST(:dependencies AS jsonb),:specialist,:tool,CAST(:payload AS jsonb),:budget)"""),
              {"tenant":tenant,"directive":directive_id,"product":product_id,"department":spec["department"],
               "head":spec["head"],"title":spec["title"],"objective":command,
               "dependencies":_json(spec.get("dependencies",deps)),"specialist":spec["specialist"],"tool":spec["tool"],"payload":_json(payload),"budget":spec.get("budget",10000)})
        await self.session.execute(text("UPDATE executive_directives SET status='DELEGATED',updated_at=now() WHERE id=:id"),{"id":directive_id})
        await self.repo.append_audit("directive.delegated","executive_directive",directive_id,product_id,
                                     {"department_count":len(plan),"command_hash":hashlib.sha256(command.encode()).hexdigest()})
        return {"id":directive_id,"status":"DELEGATED","department_count":len(plan)}

    async def claim_work_order(self, worker_id: str) -> dict | None:
        row=await self.session.execute(text("""WITH candidate AS (
          SELECT w.id FROM department_work_orders w
          WHERE w.tenant_id=:tenant AND COALESCE(w.attempt_count,0) < COALESCE(w.max_attempts,3)
            AND COALESCE(w.next_retry_at,now()) <= now()
            AND w.status IN ('ASSIGNED','REVISION_REQUESTED','QUEUED','PENDING')
            AND NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements_text(w.dependencies) d(dep)
              WHERE NOT EXISTS (SELECT 1 FROM department_work_orders prior
                WHERE prior.directive_id=w.directive_id AND prior.department=d.dep
                  AND prior.status='APPROVED_BY_HEAD'))
          ORDER BY w.created_at LIMIT 1 FOR UPDATE SKIP LOCKED
        ) UPDATE department_work_orders w SET status='IN_PROGRESS',claimed_by=:worker,
          claimed_at=now(),locked_by_employee_id=:worker,
          lock_expires_at=now()+interval '300 seconds',updated_at=now()
          FROM candidate c WHERE w.id=c.id RETURNING w.*"""),
          {"tenant":self.principal.tenant_id,"worker":worker_id})
        item=row.mappings().first()
        return _dict(item)
    async def recover_stale_leases(self) -> int:
        """Atomically recover only expired leases visible to this tenant."""
        result = await self.session.execute(text("""WITH stale AS (
          SELECT id,attempt_count,max_attempts
          FROM department_work_orders
          WHERE tenant_id=:tenant AND status='IN_PROGRESS'
            AND lock_expires_at <= now()
          ORDER BY lock_expires_at
          FOR UPDATE SKIP LOCKED
        )
        UPDATE department_work_orders w SET
          status=CASE WHEN COALESCE(s.attempt_count,0) < COALESCE(s.max_attempts,3)
                      THEN 'QUEUED' ELSE 'FAILED' END,
          attempt_count=CASE WHEN COALESCE(s.attempt_count,0) < COALESCE(s.max_attempts,3)
                             THEN COALESCE(s.attempt_count,0)+1
                             ELSE COALESCE(s.attempt_count,0) END,
          next_retry_at=CASE WHEN COALESCE(s.attempt_count,0) < COALESCE(s.max_attempts,3)
                             THEN now()+make_interval(secs =>
                               LEAST(900,5*power(2,LEAST(COALESCE(s.attempt_count,0),8)))::int)
                             ELSE NULL END,
          last_error='Worker lease expired before execution completed',
          claimed_by=NULL,claimed_at=NULL,locked_by_employee_id=NULL,
          lock_expires_at=NULL,updated_at=now()
        FROM stale s WHERE w.id=s.id AND w.tenant_id=:tenant
        RETURNING w.id,w.status,w.directive_id"""), {"tenant": self.principal.tenant_id})
        rows = result.mappings().all()
        for row in rows:
            if row["status"] == "FAILED":
                await self._refresh_directive(row["directive_id"])
        return len(rows)

    async def renew_lease(self, work_order_id: str, worker_id: str, lease_seconds: int=300) -> bool:
        result=await self.session.execute(text("""UPDATE department_work_orders
          SET lock_expires_at=now()+(:seconds * interval '1 second'),updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND claimed_by=:worker
            AND status='IN_PROGRESS' AND lock_expires_at > now()"""),
          {"id":work_order_id,"tenant":self.principal.tenant_id,
           "worker":worker_id,"seconds":lease_seconds})
        return result.rowcount == 1

    async def schedule_retry(self, order: dict, worker_id: str, error: Exception, max_attempts: int=3) -> dict:
        result=await self.session.execute(text("""UPDATE department_work_orders
          SET attempt_count=CASE WHEN COALESCE(attempt_count,0) < LEAST(COALESCE(max_attempts,:max_attempts),:max_attempts)
                                 THEN COALESCE(attempt_count,0)+1 ELSE COALESCE(attempt_count,0) END,
              status=CASE WHEN COALESCE(attempt_count,0) < LEAST(COALESCE(max_attempts,:max_attempts),:max_attempts)
                          THEN 'QUEUED' ELSE 'FAILED' END,
              next_retry_at=CASE WHEN COALESCE(attempt_count,0) < LEAST(COALESCE(max_attempts,:max_attempts),:max_attempts)
                                 THEN now()+make_interval(secs =>
                                   LEAST(900,5*power(2,LEAST(COALESCE(attempt_count,0),8)))::int)
                                 ELSE NULL END,
              last_error=:error,claimed_by=NULL,claimed_at=NULL,
              locked_by_employee_id=NULL,lock_expires_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND claimed_by=:worker AND status='IN_PROGRESS'
          RETURNING id,status,attempt_count,next_retry_at,directive_id"""),
          {"id":order["id"],"tenant":self.principal.tenant_id,"worker":worker_id,
           "error":str(error)[:1000],"max_attempts":max_attempts})
        row=result.mappings().first()
        if not row:
            raise PermissionError("Work-order lease ownership changed before retry scheduling")
        item=dict(row)
        if item["status"] == "FAILED":
            await self._refresh_directive(item["directive_id"])
        return item

    async def _reserve_work_order_budget(self, order: dict, reserve_cents: int) -> bool:
        worker=order.get('locked_by_employee_id') or order.get('claimed_by')
        row=await self.session.execute(text("""UPDATE department_work_orders
          SET spent_cents=spent_cents+:reserve,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='IN_PROGRESS'
            AND locked_by_employee_id=:worker AND spent_cents+:reserve<=max_budget_cents
          RETURNING spent_cents,max_budget_cents"""),
          {'reserve':reserve_cents,'id':order['id'],'tenant':self.principal.tenant_id,'worker':worker})
        if row.first():
            return True
        await self.session.execute(text("""UPDATE department_work_orders SET status='AWAITING_APPROVAL',
          last_error='Work-order budget ceiling reached; approval required',
          locked_by_employee_id=NULL,lock_expires_at=NULL,claimed_by=NULL,claimed_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant"""),
          {'id':order['id'],'tenant':self.principal.tenant_id})
        await self.session.execute(text("""UPDATE executive_directives SET status='AWAITING_APPROVAL',updated_at=now()
          WHERE id=:directive AND tenant_id=:tenant"""),
          {'directive':order['directive_id'],'tenant':self.principal.tenant_id})
        original=self.repo.principal
        self.repo.principal=replace(original,user_id='digital-ceo',actor_type='agent')
        try:
            approval=await self.repo.create_approval(product_id=order['product_id'],employee_id='digital-ceo',
              action_type='work_order.budget_increase',payload={'directive_id':str(order['directive_id']),
              'work_order_id':str(order['id']),'current_budget_cents':int(order.get('max_budget_cents',100)),
              'spent_cents':int(order.get('spent_cents',0)),'requested_additional_cents':reserve_cents},
              expires_at=datetime.now(timezone.utc)+timedelta(minutes=30))
        finally:
            self.repo.principal=original
        await self.session.execute(text("UPDATE executive_directives SET approval_id=:approval WHERE id=:id AND tenant_id=:tenant"),
          {'approval':approval['id'],'id':order['directive_id'],'tenant':self.principal.tenant_id})
        await self.repo.append_audit('work_order.budget_approval_required','department_work_order',str(order['id']),
          order['product_id'],{'approval_id':str(approval['id']),'reserve_cents':reserve_cents})
        return False

    async def _settle_work_order_budget(self, order: dict, reserved_cents: int, actual_cents: int) -> None:
        actual=max(0,min(int(actual_cents),reserved_cents))
        refund=reserved_cents-actual
        if refund:
            await self.session.execute(text("""UPDATE department_work_orders
              SET spent_cents=GREATEST(0,spent_cents-:refund),updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {'refund':refund,'id':order['id'],'tenant':self.principal.tenant_id})
    async def _reject_permanent_work_order(self, order: dict, reason: str,
                                           failure_type: str="invalid_configuration") -> dict:
        result = await self.session.execute(text("""UPDATE department_work_orders SET status='REJECTED',
          last_error=:error,claimed_by=NULL,claimed_at=NULL,locked_by_employee_id=NULL,
          lock_expires_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='IN_PROGRESS' AND claimed_by=:worker"""),
          {"error": reason[:1000], "id": order["id"], "tenant": self.principal.tenant_id,
           "worker": order.get("claimed_by")})
        if result.rowcount != 1:
            raise PermissionError("Work-order lease ownership changed before permanent failure was recorded")
        await self.repo.append_audit("work_order.permanent_failure", "department_work_order",
            str(order["id"]), order.get("product_id"),
            {"reason": reason[:1000], "failure_type": failure_type})
        await self._refresh_directive(order["directive_id"])
        return {"work_order_id": str(order["id"]), "status": "REJECTED", "error": reason[:1000]}

    async def execute_work_order(self, order: dict) -> dict:
        employee = BY_ID.get(order.get("specialist_role_id"))
        if employee is None:
            reason = f"Unregistered specialist role: {order.get('specialist_role_id')!r}"
            return await self._reject_permanent_work_order(order, reason, "missing_specialist_role")
        try:
            payload=order["tool_input"]
            if isinstance(payload,str):
                payload=json.loads(payload)
            if not isinstance(payload, dict):
                raise ValueError("Work-order tool_input must be a JSON object")
            skill_bundle=resolve_runtime_skill_bundle(order["department"],order.get("product_id"),employee.id)
            payload["runtime_skill_bundle"]=skill_bundle
            payload["skill_version_hash"]=skill_bundle["skill_version_hash"]
        except Exception as exc:
            classified = classify_execution_error(exc)
            if isinstance(classified, TransientExecutionError):
                raise classified from exc
            return await self._reject_permanent_work_order(
                order, str(classified), "invalid_specialist_or_skill_configuration"
            )
        delegated=payload.get("delegated_specialists",[])
        if len(delegated)>=2:
            return await self._execute_multi_specialist_work_order(order,payload,delegated)
        if order["tool_name"]=="icp_lead_scoring":
            prior=await self.session.execute(text("""SELECT deliverable FROM department_work_orders
              WHERE tenant_id=:tenant AND directive_id=:directive AND department='Growth & Marketing'
                AND status='APPROVED_BY_HEAD'"""),{"tenant":self.principal.tenant_id,"directive":order["directive_id"]})
            research=prior.mappings().first()
            if research:
                delivered=research["deliverable"]
                if isinstance(delivered,str): delivered=json.loads(delivered)
                payload={**payload,"candidates":delivered.get("candidates",[])}
        started=time.perf_counter()
        reserved_cents=0
        batch=None
        try:
            fetcher=self.tools.directory_fetcher
            if order["tool_name"]=="public_directory_research":
                source_urls=payload.get("source_urls",[])
                if not source_urls:
                    source_urls=await self.tools.directory_search.search(payload.get("query",""),payload.get("city",""),limit=5)
                payload["source_urls"]=source_urls
                if hasattr(fetcher,"max_steps"):
                    step_cost=max(1,int(os.getenv("TINYFISH_STEP_COST_CENTS","2")))
                    reserved_cents=max(1,len(source_urls))*int(fetcher.max_steps)*step_cost
                else:
                    reserved_cents=max(1,len(source_urls))
            else:
                reserved_cents=1
            if not await self._reserve_work_order_budget(order,reserved_cents):
                return {"work_order_id":str(order["id"]),"status":"AWAITING_APPROVAL","reason":"Work-order budget ceiling reached"}
            # Public research is an async, robots-aware operation; all other tools are pure.
            if order["tool_name"]=="public_directory_research":
                if "research.public" not in employee.permissions:
                    raise PermissionError("Specialist role lacks research.public permission")
                if "*" not in self.principal.permissions and not ({"agent.run","work.assign"} & set(self.principal.permissions)):
                    raise PermissionError("Orchestration principal lacks agent execution authority")
                source_urls=payload.get("source_urls",[])
                if not fetcher.allow_domains:
                    batch=type("BlockedBatch",(),{"candidates":[],"blocked":[{"url":u,"reason":"No research domain allowlist is configured"} for u in source_urls]})()
                else:
                    batch=await fetcher.discover(query=payload.get("query",""),city=payload.get("city",""),
                        product_id=payload.get("product_id") or "",source_urls=source_urls)
                result={"candidates":batch.candidates,"blocked":batch.blocked,
                        "robots_policy":"fail_closed","source_count":len(batch.candidates)}
                digest=hashlib.sha256(_json(payload).encode()).hexdigest()
                latency=int((time.perf_counter()-started)*1000)
            else:
                result,latency,digest=self.tools.execute(name=order["tool_name"],employee=employee,
                    principal_permissions=self.principal.permissions,tenant_id=self.principal.tenant_id,payload=payload)
            actual_cents=reserved_cents
            if batch is not None and hasattr(fetcher,"max_steps") and getattr(batch,"usage",None):
                actual_cents=sum(int(x.get("steps",fetcher.max_steps)) for x in batch.usage)*max(1,int(os.getenv("TINYFISH_STEP_COST_CENTS","2")))
            await self._settle_work_order_budget(order,reserved_cents,actual_cents)
            passed=self._acceptance(order["tool_name"],result)
            if passed:
                await self._queue_approval_event(order,employee,order["tool_name"],payload,result)
            status="PASSED" if passed else "FAILED"
            await self.session.execute(text("""INSERT INTO employee_tool_executions
              (tenant_id,directive_id,work_order_id,employee_id,tool_name,input_hash,input_evidence,
               output_evidence,latency_ms,status)
              VALUES (:tenant,:directive,:order,:employee,:tool,:hash,CAST(:input AS jsonb),
               CAST(:output AS jsonb),:latency,:status)"""),
              {"tenant":self.principal.tenant_id,"directive":order["directive_id"],"order":order["id"],
               "employee":employee.id,"tool":order["tool_name"],"hash":digest,"input":_json(payload),
               "output":_json(result),"latency":latency,"status":status})
            if not passed:
                return await self._review(order,result,False,"Department acceptance criteria failed")
            return await self._review(order,result,True,"Evidence and acceptance criteria passed")
        except Exception as exc:
            classified=classify_execution_error(exc)
            if isinstance(classified, TransientExecutionError):
                # Worker schedules retries in a fresh transaction after this one rolls back.
                raise classified from exc
            digest=hashlib.sha256(_json(payload).encode()).hexdigest()
            await self.session.execute(text("""INSERT INTO employee_tool_executions
              (tenant_id,directive_id,work_order_id,employee_id,tool_name,input_hash,input_evidence,
               output_evidence,latency_ms,status,error)
              VALUES (:tenant,:directive,:order,:employee,:tool,:hash,CAST(:input AS jsonb),
               '{}'::jsonb,:latency,'FAILED',:error)"""),
              {"tenant":self.principal.tenant_id,"directive":order["directive_id"],"order":order["id"],
               "employee":employee.id,"tool":order["tool_name"],"hash":digest,"input":_json(payload),
               "latency":int((time.perf_counter()-started)*1000),"error":str(classified)[:1000]})
            await self.session.execute(text("""UPDATE department_work_orders SET status='REJECTED',
              last_error=:error,locked_by_employee_id=NULL,lock_expires_at=NULL,claimed_by=NULL,claimed_at=NULL,updated_at=now()
              WHERE id=:id AND tenant_id=:tenant AND claimed_by=:worker"""),
              {"error":str(classified)[:1000],"id":order["id"],"tenant":self.principal.tenant_id,
               "worker":order.get("claimed_by")})
            await self._refresh_directive(order["directive_id"])
            return {"work_order_id":str(order["id"]),"status":"REJECTED","error":str(classified)[:1000]}

    async def _queue_approval_event(self,order,employee,tool_name,payload,result):
        """Persist a per-action, payload-bound approval and outbox event."""
        if tool_name=="personalized_outreach_draft" and result.get("approval_required"):
            channel=str(result.get("channel","whatsapp")).lower()
            dispatch_payload={"to":str(payload.get("to") or payload.get("recipient") or "").strip(),
                "message":str(result.get("draft","")).strip()}
            if channel=="email":
                dispatch_payload["subject"]=str(payload.get("subject") or "RunGrow introduction")
            action_type="whatsapp.send" if channel=="whatsapp" else "email.send"
            outbox_payload={"channel":channel,"dispatch_payload":dispatch_payload,
                "draft_result":{"ai_identity_disclosure":result.get("ai_identity_disclosure"),
                    "sent":False,"recipient_ref":result.get("recipient_ref")}}
        elif tool_name=="product_onboarding_trigger" and result.get("approval_required"):
            action_type="product.onboarding.dispatch"
            dispatch_payload=result
            outbox_payload={"channel":"product_adapter","dispatch_payload":dispatch_payload}
        else:
            return None
        original=self.repo.principal
        self.repo.principal=replace(original,user_id=employee.id,actor_type="agent")
        try:
            approval=await self.repo.create_approval(product_id=order.get("product_id"),
                employee_id=employee.id,action_type=action_type,payload=dispatch_payload,
                expires_at=datetime.now(timezone.utc)+timedelta(hours=24))
        finally:
            self.repo.principal=original
        await self.session.execute(text("""INSERT INTO wf_outbox_events
          (id,tenant_id,product_id,directive_id,work_order_id,approval_id,employee_id,
           action_type,payload_hash,payload,status)
          VALUES (:id,:tenant,:product,:directive,:work_order,:approval,:employee,
           :action,:hash,CAST(:payload AS jsonb),'QUEUED_FOR_APPROVAL')
          ON CONFLICT (tenant_id,approval_id) DO NOTHING"""),
          {"id":str(uuid4()),"tenant":self.principal.tenant_id,"product":order["product_id"],
           "directive":order["directive_id"],"work_order":order["id"],"approval":approval["id"],
           "employee":employee.id,"action":action_type,"hash":approval["payload_hash"],
           "payload":_json(outbox_payload)})
        await self.repo.append_audit("outbox.queued_for_approval","outbox_event",str(approval["id"]),
            order.get("product_id"),{"approval_id":str(approval["id"]),"action_type":action_type,
            "payload_hash":approval["payload_hash"],"work_order_id":str(order["id"])})
        return approval

    async def _execute_multi_specialist_work_order(self,order,payload,delegated):
        """Execute every head-assigned specialist and approve only the combined evidence bundle."""
        executions=[]
        reserve=max(1,len(delegated))
        if not await self._reserve_work_order_budget(order,reserve):
            return {"work_order_id":str(order["id"]),"status":"AWAITING_APPROVAL","reason":"Work-order budget ceiling reached"}
        try:
            for assignment in delegated:
                employee=BY_ID.get(assignment.get("employee_id"))
                tool_name=assignment.get("tool_name")
                if not employee or employee.department!=order["department"] or not tool_name:
                    raise PermissionError("Department Head delegated an invalid specialist assignment")
                feedback=""
                final_result=None
                for revision in range(3):
                    skill=resolve_runtime_skill_bundle(order["department"],order.get("product_id"),employee.id)
                    specialist_input={**payload,"runtime_skill_bundle":skill,
                        "skill_version_hash":skill["skill_version_hash"],
                        "specialist_task":assignment.get("task",""),
                        "revision_cycle":revision}
                    if feedback:
                        specialist_input["supervisor_feedback"]=feedback
                    result,latency,digest=self.tools.execute(name=tool_name,employee=employee,
                        principal_permissions=self.principal.permissions,
                        tenant_id=self.principal.tenant_id,payload=specialist_input)
                    passed=self._acceptance(tool_name,result)
                    await self.session.execute(text("""INSERT INTO employee_tool_executions
                      (tenant_id,directive_id,work_order_id,employee_id,tool_name,input_hash,input_evidence,
                       output_evidence,latency_ms,status)
                      VALUES (:tenant,:directive,:order,:employee,:tool,:hash,CAST(:input AS jsonb),
                       CAST(:output AS jsonb),:latency,:status)"""),
                      {"tenant":self.principal.tenant_id,"directive":order["directive_id"],
                       "order":order["id"],"employee":employee.id,"tool":tool_name,
                       "hash":digest,"input":_json(specialist_input),"output":_json(result),
                       "latency":latency,"status":"PASSED" if passed else "FAILED"})
                    final_result=result
                    if passed:
                        await self._queue_approval_event(order,employee,tool_name,specialist_input,result)
                        break
                    feedback=("REVISION_REQUESTED: provide a complete deliverable satisfying the department SOP. "
                              "Previous deterministic rubric failures: "+_json(result))
                executions.append({"employee_id":employee.id,"tool_name":tool_name,
                    "skill_version_hash":skill["skill_version_hash"],"result":final_result,
                    "accepted":self._acceptance(tool_name,final_result)})
            combined={"evidence_type":"departmental_combined_evidence","department":order["department"],
                "department_head_id":order["department_head_id"],"specialist_count":len(executions),
                "specialist_evidence":executions,
                "acceptance_rubric":{"all_specialists_accepted":all(x["accepted"] for x in executions),
                    "revision_cycles_max":2}}
            await self._settle_work_order_budget(order,reserve,reserve)
            return await self._review(order,combined,all(x["accepted"] for x in executions),
                "Combined departmental evidence passed specialist acceptance rubrics" if all(x["accepted"] for x in executions)
                else "One or more specialist deliverables failed after two revision cycles")
        except Exception as exc:
            await self._settle_work_order_budget(order,reserve,reserve)
            await self.session.execute(text("""UPDATE department_work_orders SET status='REJECTED',
              last_error=:error,locked_by_employee_id=NULL,lock_expires_at=NULL,claimed_by=NULL,claimed_at=NULL,updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {"error":str(exc)[:1000],"id":order["id"],"tenant":self.principal.tenant_id})
            await self._refresh_directive(order["directive_id"])
            return {"work_order_id":str(order["id"]),"status":"REJECTED","error":str(exc)[:1000]}

    @staticmethod
    def _acceptance(tool_name,result):
        if tool_name=="public_directory_research":
            return result.get("robots_policy")=="fail_closed" and int(result.get("source_count",0))>0 and all(x.get("source_url") for x in result.get("candidates",[]))
        if tool_name=="icp_lead_scoring": return isinstance(result.get("scores"),list)
        if tool_name=="campaign_budget_estimate": return result.get("recommended_budget_inr",0)>=0 and not result.get("spend_authorised",True)
        if tool_name=="compliance_disclosure_check": return result.get("passed") is True and result.get("publication_authorised") is False
        if tool_name=="product_handoff_draft": return result.get("external_contact_performed") is False
        if tool_name=="personalized_outreach_draft": return result.get("approval_required") is True and result.get("sent") is False and bool(result.get("ai_identity_disclosure"))
        return False

    async def _review(self,order,result,passed,reason):
        history=order.get("review_history") or []
        if isinstance(history,str): history=json.loads(history)
        if self.model_gateway is not None:
            from pydantic import BaseModel
            class SupervisorDecision(BaseModel):
                accepted: bool
                feedback: str
            decision,_=await self.model_gateway.generate_structured(
                messages=[{"role":"system","content":"You are the department supervisor. Evaluate evidence strictly against the injected SOP acceptance criteria. Request revision with concrete, actionable feedback if any criterion is unmet. Never approve external actions."},
                    {"role":"user","content":_json({"department":order["department"],"skill_bundle":order.get("tool_input",{}).get("runtime_skill_bundle",{}),"evidence":result,"deterministic_acceptance":passed,"reason":reason})}],
                output_model=SupervisorDecision,tenant_id=self.principal.tenant_id,product_id=order.get("product_id"),
                employee_id=order["department_head_id"],run_id=str(order["id"]))
            passed=bool(passed and decision.accepted)
            reason=decision.feedback
        feedback=reason if passed else ("Revision required by "+order["department_head_id"]+": address missing evidence and satisfy acceptance criteria before resubmission. "+reason)
        history.append({"reviewer":order["department_head_id"],"passed":passed,"reason":reason,"supervisor_feedback":feedback,"revision":order["revision_count"]})
        if passed:
            status="APPROVED_BY_HEAD"; deliverable=result
        elif int(order["revision_count"])<2:
            status="REVISION_REQUESTED"; deliverable=result
        else:
            status="REJECTED"; deliverable=result
        await self.session.execute(text("""UPDATE department_work_orders SET status=:status,
          revision_count=revision_count+CASE WHEN :status='REVISION_REQUESTED' THEN 1 ELSE 0 END,
          review_history=CAST(:history AS jsonb),deliverable=CAST(:deliverable AS jsonb),
          locked_by_employee_id=NULL,lock_expires_at=NULL,claimed_by=NULL,claimed_at=NULL,updated_at=now()
          WHERE id=:id AND tenant_id=:tenant"""),
          {"status":status,"history":_json(history),"deliverable":_json(deliverable),
           "id":order["id"],"tenant":self.principal.tenant_id})
        await self._refresh_directive(order["directive_id"])
        return {"work_order_id":str(order["id"]),"status":status,"review":history[-1],"deliverable":result}

    async def _refresh_directive(self,directive_id):
        rows=await self.session.execute(text("""SELECT status,department,deliverable FROM department_work_orders
          WHERE tenant_id=:tenant AND directive_id=:id"""),{"tenant":self.principal.tenant_id,"id":directive_id})
        items=[dict(x) for x in rows.mappings().all()]
        if any(x["status"]=="AWAITING_APPROVAL" for x in items): status="AWAITING_APPROVAL"
        elif any(x["status"]=="REJECTED" for x in items): status="FAILED"
        elif items and all(x["status"]=="APPROVED_BY_HEAD" for x in items): status="IN_REVIEW"
        else: status="DELEGATED"
        if status=="IN_REVIEW":
            proposal={"directive_id":str(directive_id),"departments":[{"department":x["department"],"deliverable":x["deliverable"]} for x in items],
                      "external_actions":"NOT_EXECUTED","human_approval_required":True}
            directive=await self.session.execute(text("SELECT product_id FROM executive_directives WHERE id=:id AND tenant_id=:tenant"),
                {"id":directive_id,"tenant":self.principal.tenant_id})
            product_id=directive.scalar_one()
            original_principal=self.repo.principal
            self.repo.principal=replace(original_principal,user_id="digital-ceo",actor_type="agent")
            try:
                approval=await self.repo.create_approval(product_id=product_id,employee_id="digital-ceo",
                    action_type="directive.external_execution",payload=proposal,
                    expires_at=datetime.now(timezone.utc)+timedelta(minutes=15))
            finally:
                self.repo.principal=original_principal
            status="AWAITING_APPROVAL"
            await self.session.execute(text("""UPDATE executive_directives SET status=:status,
              approval_id=:approval,consolidated_proposal=CAST(:proposal AS jsonb),updated_at=now()
              WHERE id=:id AND tenant_id=:tenant"""),
              {"status":status,"approval":approval["id"],"proposal":_json(proposal),"id":directive_id,"tenant":self.principal.tenant_id})
        else:
            await self.session.execute(text("UPDATE executive_directives SET status=:status,updated_at=now() WHERE id=:id AND tenant_id=:tenant"),
                {"status":status,"id":directive_id,"tenant":self.principal.tenant_id})

    async def get_directive(self,directive_id):
        row=await self.session.execute(text("SELECT * FROM executive_directives WHERE id=:id AND tenant_id=:tenant"),
            {"id":directive_id,"tenant":self.principal.tenant_id})
        directive=_dict(row.mappings().first())
        if not directive: return None
        orders=await self.session.execute(text("""SELECT * FROM department_work_orders
          WHERE directive_id=:id AND tenant_id=:tenant ORDER BY created_at"""),
          {"id":directive_id,"tenant":self.principal.tenant_id})
        proofs=await self.session.execute(text("""SELECT * FROM employee_tool_executions
          WHERE directive_id=:id AND tenant_id=:tenant ORDER BY created_at"""),
          {"id":directive_id,"tenant":self.principal.tenant_id})
        directive["department_work_orders"]=[dict(x) for x in orders.mappings().all()]
        directive["tool_execution_proofs"]=[dict(x) for x in proofs.mappings().all()]
        return directive

    async def approve_external(self,directive_id,approval_id):
        row=await self.session.execute(text("""UPDATE executive_directives SET approval_id=:approval,
          status='COMPLETED',completed_at=now(),updated_at=now()
          WHERE id=:id AND tenant_id=:tenant AND status='AWAITING_APPROVAL'
            AND approval_id=:approval RETURNING id"""),
          {"approval":approval_id,"id":directive_id,"tenant":self.principal.tenant_id})
        if not row.scalar_one_or_none(): raise PermissionError("Directive is not awaiting approval or approval already consumed")
        await self.repo.append_audit("directive.approved","executive_directive",directive_id,None,{"approval_id":approval_id,"external_execution":"not_started"})
        return {"directive_id":str(directive_id),"status":"COMPLETED","external_execution":"not_started","approval_consumed":True}
