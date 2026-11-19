"""Standalone durable worker with tenant-scoped heartbeat telemetry."""
from __future__ import annotations
import asyncio, os, signal, uuid
from app.config import settings, validate_production_environment
from app.policy import Principal
from app.repositories import PostgresWorkforceRepository, session_factory
from app.main import _WorkItemPlan, get_model_gateway
from app.orchestrator import DurableTaskOrchestrator

async def run_worker():
    if settings.app_env == "production":
        validate_production_environment()
    tenant=os.getenv("WORKFORCE_WORKER_TENANT_ID")
    user=os.getenv("WORKFORCE_WORKER_USER_ID","rungrow-worker")
    if not tenant:
        raise RuntimeError("WORKFORCE_WORKER_TENANT_ID is required")
    products=frozenset(x.strip() for x in os.getenv("WORKFORCE_WORKER_PRODUCT_IDS","").split(",") if x.strip())
    scopes=frozenset(x.strip() for x in os.getenv("WORKFORCE_WORKER_SCOPES","work.assign,agent.run").split(",") if x.strip())
    principal=Principal(user,tenant,"workforce-worker",scopes,actor_type="agent",product_ids=products)
    worker_id=os.getenv("WORKFORCE_WORKER_ID",f"worker-{uuid.uuid4()}")
    poll=max(0.25,float(os.getenv("WORKFORCE_WORKER_POLL_SECONDS","1")))
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for durable worker")
    factory=session_factory()
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try: loop.add_signal_handler(sig,stop.set)
        except NotImplementedError: pass

    async def beat(status,processed=0,dead=0):
        async with factory() as session:
            async with PostgresWorkforceRepository(session,principal) as repo:
                await repo.heartbeat(worker_id,status,processed,dead)

    await beat("STARTING")
    while not stop.is_set():
        task=None
        processed=dead=0
        status="IDLE"
        try:
            async with factory() as session:
                async with PostgresWorkforceRepository(session,principal) as repo:
                    orchestrator=DurableTaskOrchestrator(repo,worker_id)
                    task=await orchestrator.claim()
                    if task:
                        status="BUSY"
                        await repo.heartbeat(worker_id,"BUSY")
                        try:
                            gateway=get_model_gateway(repo)
                            plan,telemetry=await gateway.generate_structured(
                                messages=[{"role":"user","content":("SANDBOX MODE: simulate only. " if task.get("is_sandbox") else "")+"Create an execution plan only; do not perform side effects. Task: "+task["title"]+" "+task.get("description","")}],
                                output_model=_WorkItemPlan,tenant_id=tenant,product_id=task.get("product_id"),
                                employee_id=task["assignee_role_id"],run_id=str(task["id"]),is_sandbox=bool(task.get("is_sandbox",False)))
                            if plan.requires_approval:
                                await repo.pause_task_for_approval(str(task["id"]),plan.action_type or "task.sensitive_action",
                                    plan.action_payload or {"task_id":str(task["id"]),"summary":plan.summary})
                            else:
                                result=plan.model_dump() if hasattr(plan,"model_dump") else plan.dict()
                                result["telemetry"]=telemetry
                                await repo.finish_task(str(task["id"]),worker_id,result)
                            processed=1
                        except Exception as exc:
                            try:
                                outcome=await repo.fail_task(str(task["id"]),worker_id,str(exc),
                                    min(300,2**max(1,task.get("attempt_count",1))),task.get("max_attempts",5))
                                processed=1
                                if str((outcome or {}).get("status","")).upper()=="DEAD_LETTER" or task.get("attempt_count",0)+1>=task.get("max_attempts",5):
                                    dead=1
                            except Exception:
                                status="ERROR"
                    await repo.heartbeat(worker_id,status,processed,dead)
        except Exception:
            await asyncio.sleep(min(poll,2.0))
        if not task and not stop.is_set():
            try: await asyncio.wait_for(stop.wait(),timeout=poll)
            except asyncio.TimeoutError: pass
    await beat("STOPPING")

if __name__=="__main__":
    asyncio.run(run_worker())
