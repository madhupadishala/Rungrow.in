"""Repository mixin for append-only release evidence and worker telemetry."""
from __future__ import annotations
import json
from uuid import uuid4
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

class ReleaseRepositoryMixin:
    async def heartbeat(self, worker_id: str, status: str, processed_increment: int = 0,
                        dead_letter_increment: int = 0):
        if status not in {"STARTING","IDLE","BUSY","STOPPING","ERROR"}:
            raise ValueError("Invalid worker heartbeat status")
        if not hasattr(self, "session"):
            rows=getattr(self,"worker_heartbeats",None)
            if rows is None: self.worker_heartbeats=[]; rows=self.worker_heartbeats
            item=next((x for x in rows if x["worker_id"]==worker_id and x["tenant_id"]==self.principal.tenant_id),None)
            now=datetime.now(timezone.utc).isoformat()
            if item:
                item.update(status=status,last_heartbeat_at=now,processed_count=item["processed_count"]+processed_increment,
                    dead_letter_count=item["dead_letter_count"]+dead_letter_increment)
            else:
                rows.append({"worker_id":worker_id,"tenant_id":self.principal.tenant_id,"status":status,
                    "last_heartbeat_at":now,"processed_count":processed_increment,"dead_letter_count":dead_letter_increment})
            return
        await self.session.execute(text("""
          INSERT INTO worker_heartbeats(worker_id,tenant_id,status,last_heartbeat_at,processed_count,dead_letter_count)
          VALUES (:worker,:tenant,:status,now(),:processed,:dead)
          ON CONFLICT(worker_id) DO UPDATE SET
            tenant_id=EXCLUDED.tenant_id,status=EXCLUDED.status,last_heartbeat_at=now(),
            processed_count=worker_heartbeats.processed_count+EXCLUDED.processed_count,
            dead_letter_count=worker_heartbeats.dead_letter_count+EXCLUDED.dead_letter_count
          WHERE worker_heartbeats.tenant_id=EXCLUDED.tenant_id
        """), {"worker":worker_id,"tenant":self.principal.tenant_id,"status":status,
               "processed":processed_increment,"dead":dead_letter_increment})
        await self.session.flush()

    async def list_worker_heartbeats(self, stale_after_seconds: int = 90):
        if not hasattr(self, "session"):
            cutoff=datetime.now(timezone.utc)-timedelta(seconds=stale_after_seconds)
            return [{**x,"is_fresh":datetime.fromisoformat(x["last_heartbeat_at"])>=cutoff}
                for x in getattr(self,"worker_heartbeats",[]) if x["tenant_id"]==self.principal.tenant_id]
        return await self._rows("""
          SELECT worker_id,status,last_heartbeat_at,processed_count,dead_letter_count,
                 (last_heartbeat_at >= now() - make_interval(secs => :stale)) AS is_fresh
          FROM worker_heartbeats WHERE tenant_id=:tenant ORDER BY last_heartbeat_at DESC
        """, {"tenant":self.principal.tenant_id,"stale":stale_after_seconds})

    async def verify_migration_chain(self):
        if not hasattr(self, "session"):
            return {"ok":False,"missing":["PostgreSQL migration evidence"],"drift":[]}
        from .migrate import migration_files
        expected={v:(p.name,c) for v,p,c in migration_files()}
        rows=await self._rows("SELECT version,filename,checksum_sha256 FROM schema_migrations")
        actual={str(x["version"]):(x["filename"],x["checksum_sha256"]) for x in rows}
        missing=sorted(set(expected)-set(actual))
        drift=sorted(v for v in set(expected)&set(actual) if expected[v]!=actual[v])
        return {"ok":not missing and not drift and set(actual)==set(expected),"missing":missing,"drift":drift}

    async def record_release_gate_signoff(self, *, release_layer: str, gate_name: str,
            status: str, evidence: dict, verifier_type: str, verifier_id: str,
            candidate_sha: str | None, environment: str, cohort_id: str = "portfolio", scope_key: str = "cohort"):
        allowed_layers={"RELEASE_1_WORKFORCE","RELEASE_2_PRODUCTS","RELEASE_3_COMMERCIAL"}
        allowed_gates={"ENGINEERING","SECURITY","INTEGRATION","RELIABILITY","AI_GOVERNANCE","OPERATIONS","BUSINESS"}
        if release_layer not in allowed_layers or gate_name not in allowed_gates:
            raise ValueError("Invalid release layer or gate")
        if status not in {"PASS","BLOCKED","DEFERRED_WITH_ACCEPTANCE"}:
            raise ValueError("Invalid release signoff status")
        if verifier_type not in {"HUMAN","AUTOMATED"} or not verifier_id.strip():
            raise ValueError("Verifier identity is required")
        if not isinstance(evidence,dict) or not evidence:
            raise ValueError("Evidence object is required")
        import re
        if not candidate_sha or not re.fullmatch(r"[0-9a-fA-F]{40,64}",candidate_sha):
            raise ValueError("A full immutable Git commit SHA is required for release signoff")
        if not environment or not environment.strip():
            raise ValueError("Release environment is required")
        if cohort_id not in {"portfolio","flagship-production","portfolio-expansion"}:
            raise ValueError("Invalid release cohort")
        if not scope_key or len(scope_key)>200:
            raise ValueError("Release signoff scope key is required")
        if status=="DEFERRED_WITH_ACCEPTANCE":
            if release_layer!="RELEASE_2_PRODUCTS" or gate_name not in {"INTEGRATION","BUSINESS"}:
                raise ValueError("Only product integration/business risks may be deferred; shared security and reliability gates are non-waivable")
            required=("product_id","accepted_risk","owner","due_date","compensating_control","cohort_id")
            if any(not isinstance(evidence.get(key),str) or not evidence[key].strip() for key in required):
                raise ValueError("Product deferral requires product_id, accepted_risk, owner, due_date, compensating_control and cohort_id")
            if evidence.get("cohort_id")!="portfolio-expansion" or cohort_id!="portfolio-expansion":
                raise ValueError("Product deferrals must be recorded in portfolio-expansion cohort")
            if evidence["product_id"] not in {"photo-studio-billing","entrepreneur-instagram-tools","pharma-software-utilities","artists-business-tools","growtoys"}:
                raise ValueError("The flagship production products cannot be deferred")
            if evidence["product_id"]=="growtoys" and evidence.get("governance_mode")!="DISCOVERY_ONLY":
                raise ValueError("Growtoys deferral must preserve DISCOVERY_ONLY governance")
            if evidence["product_id"]!="growtoys" and evidence.get("repository_status")!="NO_REPO_YET":
                raise ValueError("Only NO_REPO_YET products may use product integration deferral")
        if not cohort_id or len(cohort_id) > 80 or not scope_key or len(scope_key) > 160:
            raise ValueError("Release cohort and scope key are required")
        if status == "DEFERRED_WITH_ACCEPTANCE":
            required = ("accepted_risk", "risk_rationale", "owner", "due_date", "compensating_control")
            absent = [key for key in required if not evidence.get(key)]
            if absent:
                raise ValueError("Deferred acceptance requires: " + ", ".join(absent))
            if gate_name in {"SECURITY", "RELIABILITY", "ENGINEERING", "INTEGRATION", "AI_GOVERNANCE", "OPERATIONS"}:
                raise ValueError("Security, reliability and control gates cannot be deferred")
            if not (release_layer == "RELEASE_2_PRODUCTS" and gate_name == "BUSINESS" and scope_key != "cohort"):
                raise ValueError("Only product-scoped Release 2 business acceptance may be deferred")
        if not hasattr(self, "session"):
            if not hasattr(self,"release_signoffs"): self.release_signoffs=[]
            item={"id":str(uuid4()),"tenant_id":self.principal.tenant_id,"release_layer":release_layer,
                "gate_name":gate_name,"status":status,"evidence":evidence,"verifier_type":verifier_type,
                "verifier_id":verifier_id,"candidate_sha":candidate_sha,"environment":environment,
                "cohort_id":cohort_id,"scope_key":scope_key,"occurred_at":datetime.now(timezone.utc).isoformat()}
            self.release_signoffs.insert(0,item)
            return item
        return await self._one("""
          INSERT INTO release_gate_signoffs
            (id,tenant_id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
             candidate_sha,environment,cohort_id,scope_key,occurred_at)
          VALUES (:id,:tenant,:layer,:gate,:status,CAST(:evidence AS jsonb),:type,:verifier,
                  :sha,:environment,:cohort,:scope,now()) RETURNING *
        """, {"id":str(uuid4()),"tenant":self.principal.tenant_id,"layer":release_layer,
              "gate":gate_name,"status":status,"evidence":json.dumps(evidence),
              "type":verifier_type,"verifier":verifier_id,"sha":candidate_sha,
              "environment":environment,"cohort":cohort_id,"scope":scope_key})

    async def list_release_gate_signoffs(self):
        if not hasattr(self, "session"):
            return [x for x in getattr(self,"release_signoffs",[]) if x["tenant_id"]==self.principal.tenant_id]
        return await self._rows("""
          SELECT id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
                 candidate_sha,environment,cohort_id,scope_key,occurred_at
          FROM release_gate_signoffs WHERE tenant_id=:tenant ORDER BY occurred_at DESC
        """, {"tenant":self.principal.tenant_id})
