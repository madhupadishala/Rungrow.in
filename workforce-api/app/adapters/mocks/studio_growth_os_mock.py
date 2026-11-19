"""ASGI mock matching the standalone Studio Growth OS v2 HTTP contract."""
import hashlib, hmac, os, time
from fastapi import FastAPI, HTTPException, Query, Request
app=FastAPI(title="Synthetic Studio Growth OS ingress",version="2.0.0")
TENANT="studio-hyd-001"
LEADS=[
 {"id":"lead-hyd-001","tenant_id":TENANT,"contact_id":"contact-001","stage":"qualified","score":92,
  "event_date":"2026-12-12T00:00:00+05:30","budget_band":"INR 100000-150000",
  "source_first":"instagram","source_last":"whatsapp","campaign":"Hyderabad Wedding 2026","is_synthetic":True},
 {"id":"lead-hyd-002","tenant_id":TENANT,"contact_id":"contact-002","stage":"new","score":68,
  "event_date":"2026-12-20T00:00:00+05:30","budget_band":"INR 50000-75000",
  "source_first":"google","source_last":"website","campaign":"Dilsukhnagar Local","is_synthetic":True}]
QUOTES=[{"id":"quote-hyd-001","tenant_id":TENANT,"lead_id":"lead-hyd-001","revision":1,
 "status":"approved","pricebook_version":3,"totals_json":{"total_paise":12500000,"currency":"INR"},
 "is_synthetic":True}]
BOOKINGS=[{"id":"booking-hyd-001","tenant_id":TENANT,"lead_id":"lead-hyd-001","state":"confirmed",
 "event_date":"2026-12-12T00:00:00+05:30","is_synthetic":True}]

async def _tenant(request,tenant_id):
 if request.headers.get("X-RunGrow-Tenant-ID")!=tenant_id: raise HTTPException(403,"external tenant binding mismatch")
 org_id=request.headers.get("X-RunGrow-Org-ID","")
 if not org_id or org_id==tenant_id: raise HTTPException(403,"internal/external tenant separation required")
 if request.headers.get("X-RunGrow-Product-ID")!="photo-studio-growth-os": raise HTTPException(403,"product binding mismatch")
 secret=os.getenv("STUDIO_GROWTH_OS_SERVICE_SECRET","")
 timestamp=request.headers.get("X-RunGrow-Timestamp","")
 signature=request.headers.get("X-RunGrow-Signature","")
 try: issued=int(timestamp)
 except (TypeError,ValueError): raise HTTPException(401,"invalid service timestamp")
 if not secret or abs(time.time()-issued)>300: raise HTTPException(401,"service signature outside replay window")
 body=await request.body()
 binding=".".join((timestamp,tenant_id,org_id,"photo-studio-growth-os")).encode()
 expected="sha256="+hmac.new(secret.encode(),binding+b"."+body,hashlib.sha256).hexdigest()
 if not hmac.compare_digest(expected,signature): raise HTTPException(401,"invalid service signature")

@app.get("/api/v1/leads")
async def leads(request:Request,page:int=1,page_size:int=50,stage:str|None=None):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 rows=[x for x in LEADS if x["tenant_id"]==tenant and (not stage or x["stage"]==stage)]
 return {"leads":rows[:page_size],"total":len(rows),"page":page,"page_size":page_size,"is_synthetic":True}

@app.get("/api/v1/quotes")
async def quotes(request:Request,status:str|None=None,page:int=1,page_size:int=50):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 return [x for x in QUOTES if x["tenant_id"]==tenant and (not status or x["status"]==status)][:page_size]

@app.get("/api/v1/quotes/{quote_id}")
async def quote_detail(request:Request,quote_id:str):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 row=next((x for x in QUOTES if x["id"]==quote_id and x["tenant_id"]==tenant),None)
 if row is None: raise HTTPException(404,"Quote not found")
 return row

@app.get("/api/v1/bookings")
async def bookings(request:Request,state:str|None=None,page:int=1,page_size:int=50):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 return [x for x in BOOKINGS if x["tenant_id"]==tenant and (not state or x["state"]==state)][:page_size]

@app.get("/api/v1/conversations")
async def conversations(request:Request,lead_id:str=Query(...),include_messages:bool=True):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 if not any(x["id"]==lead_id and x["tenant_id"]==tenant for x in LEADS): raise HTTPException(404,"Lead not found")
 return {"lead_id":lead_id,"tenant_id":tenant,"channel":"whatsapp","status":"open",
  "messages":[{"direction":"inbound","content":"Please share wedding package details","is_synthetic":True}],
  "is_synthetic":True}

@app.get("/api/v1/analytics/dashboard")
async def summary(request:Request,range:str="30d"):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 return {"tenant_id":tenant,"range":range,"lead_count":len(LEADS),"quote_count":len(QUOTES),
  "booking_count":len(BOOKINGS),"recognised_revenue_inr":0,"is_synthetic":True}

@app.post("/api/v1/leads")
async def prospect_handoff(request:Request):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 payload=await request.json()
 if not payload.get("contact_id"): raise HTTPException(422,"contact_id required")
 return {"status":"accepted","tenant_id":tenant,"external_reference":"synthetic-handoff-001",
  "received_fields":sorted(payload),"is_synthetic":True}

@app.post("/api/v1/agent/execute-tool")
async def execute_tool(request:Request):
 tenant=request.headers.get("X-RunGrow-Tenant-ID",""); await _tenant(request,tenant)
 key=request.headers.get("Idempotency-Key")
 if not key: raise HTTPException(400,"Idempotency-Key required")
 payload=await request.json()
 if payload.get("tool")=="send_quote" and not payload.get("arguments",{}).get("quote_id"):
  raise HTTPException(422,"quote_id required for send_quote")
 return {"status":"accepted","tool":payload.get("tool"),"idempotency_key":key,"is_synthetic":True}
