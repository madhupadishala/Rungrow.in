"""Repository mixin for append-only release evidence and worker telemetry."""
from __future__ import annotations
import json
from uuid import uuid4
from sqlalchemy import text

class ReleaseRepositoryMixin:
    async def heartbeat(self, worker_id: str, status: str, processed_increment: int = 0,
                        dead_letter_increment: int = 0):
        if status not in {"STARTING","IDLE","BUSY","STOPPING","ERROR"}:
            raise ValueError("Invalid worker heartbeat status")
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
        return await self._rows("""
          SELECT worker_id,status,last_heartbeat_at,processed_count,dead_letter_count,
                 (last_heartbeat_at >= now() - make_interval(secs => :stale)) AS is_fresh
          FROM worker_heartbeats WHERE tenant_id=:tenant ORDER BY last_heartbeat_at DESC
        """, {"tenant":self.principal.tenant_id,"stale":stale_after_seconds})

    async def record_release_gate_signoff(self, *, release_layer: str, gate_name: str,
            status: str, evidence: dict, verifier_type: str, verifier_id: str,
            candidate_sha: str | None, environment: str):
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
        item=await self._one("""
          INSERT INTO release_gate_signoffs
            (id,tenant_id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
             candidate_sha,environment,occurred_at)
          VALUES (:id,:tenant,:layer,:gate,:status,CAST(:evidence AS jsonb),:type,:verifier,
                  :sha,:environment,now()) RETURNING *
        """, {"id":str(uuid4()),"tenant":self.principal.tenant_id,"layer":release_layer,
              "gate":gate_name,"status":status,"evidence":json.dumps(evidence),
              "type":verifier_type,"verifier":verifier_id,"sha":candidate_sha,
              "environment":environment})
        return item

    async def list_release_gate_signoffs(self):
        return await self._rows("""
          SELECT id,release_layer,gate_name,status,evidence,verifier_type,verifier_id,
                 candidate_sha,environment,occurred_at
          FROM release_gate_signoffs WHERE tenant_id=:tenant ORDER BY occurred_at DESC
        """, {"tenant":self.principal.tenant_id})
