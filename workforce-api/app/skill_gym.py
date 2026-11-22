"""Isolated skill evaluation and promotion engine.

The sandbox renderer is pure and has no repository, network, messaging, publishing,
payment or production tool handles. Only rubric proof is persisted by this service.
"""
from __future__ import annotations
import hashlib, json, re
from uuid import uuid4
from sqlalchemy import text
from .skill_registry import FORTUNE50_SKILLS, SKILLS_BY_ID

AI_DISCLOSURE = "I am an AI assistant acting on behalf of RunGrow."
FORBIDDEN_ACTION = re.compile(r"\b(published|sent the message|charged your card|payment processed|campaign is live|posted successfully|provisioned your account)\b", re.I)

class SkillGymError(Exception): pass

class SkillGymEngine:
    def __init__(self, repo, principal, sandbox_agent=None):
        self.repo, self.principal = repo, principal
        self.sandbox_agent = sandbox_agent or self._sandbox_agent

    @staticmethod
    def _hash(skill):
        raw=json.dumps({k:skill[k] for k in ("skill_id","department","role_id","category","skill_name","instructions","rubric_json")},sort_keys=True,separators=(",",":"),ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def seed_catalog(self):
        """Idempotently materialise the curated catalog into this tenant."""
        for skill in FORTUNE50_SKILLS:
            await self.build_or_update_skill(**skill)
        return {"seeded":len(FORTUNE50_SKILLS),"catalog_count":len(FORTUNE50_SKILLS)}

    async def build_or_update_skill(self, *, skill_id, department, role_id, category,
                                    skill_name, instructions, rubric_json=None, created_by=None):
        if not all(isinstance(x,str) and x.strip() for x in
                   (skill_id,department,role_id,category,skill_name,instructions)):
            raise ValueError("All skill identity and instruction fields are required")
        if len(instructions)>12000 or len(skill_name)>180:
            raise ValueError("Skill definition exceeds supported limits")
        rubric=rubric_json or {"clarity":.20,"problem_first_value":.20,
            "indian_smb_relevance":.20,"ai_disclosure_compliance":.25,
            "conversion_cta":.15,"minimum_overall_score":.85}
        skill={"skill_id":skill_id,"department":department,"role_id":role_id,
               "category":category,"skill_name":skill_name,"instructions":instructions,
               "rubric_json":rubric}
        version_hash=self._hash(skill)
        await self.repo.session.execute(text("""
          INSERT INTO agent_skills
            (id,tenant_id,skill_id,department,role_id,category,skill_name,instructions,
             rubric_json,status,version_hash,created_by,created_at,updated_at)
          VALUES (:id,:tenant,:skill,:department,:role,:category,:name,:instructions,
             CAST(:rubric AS jsonb),'DRAFT',:hash,:actor,now(),now())
          ON CONFLICT (tenant_id,skill_id) DO UPDATE SET
            department=EXCLUDED.department, role_id=EXCLUDED.role_id,
            category=EXCLUDED.category, skill_name=EXCLUDED.skill_name,
            instructions=EXCLUDED.instructions, rubric_json=EXCLUDED.rubric_json,
            status='DRAFT', latest_score=NULL, version_hash=EXCLUDED.version_hash,
            updated_at=now()
        """),{"id":str(uuid4()),"tenant":self.principal.tenant_id,"skill":skill_id,
              "department":department,"role":role_id,"category":category,"name":skill_name,
              "instructions":instructions,"rubric":json.dumps(rubric),"hash":version_hash,
              "actor":created_by or self.principal.user_id})
        return {**skill,"status":"DRAFT","version_hash":version_hash}

    @staticmethod
    def _sandbox_agent(skill, scenario):
        problem=str(scenario.get("problem") or scenario.get("input") or "A small business is losing time to manual follow-up.")
        vertical=str(scenario.get("vertical") or "Indian SMB")
        offer=str(scenario.get("verified_offer") or "a guided product demonstration")
        return (f"{problem}\nFor an {vertical}, this creates avoidable operating effort. "
                f"RunGrow can help assess a practical workflow using verified capabilities. "
                f"{AI_DISCLOSURE} Would you like to arrange {offer}?")

    @staticmethod
    def _evaluate(output, scenario):
        value=output if isinstance(output,str) else json.dumps(output,ensure_ascii=False)
        lower=value.lower()
        compliance=bool(re.search(r"\bai\b|artificial intelligence",value,re.I)) and "rungrow" in lower
        compliance = compliance and not bool(FORBIDDEN_ACTION.search(value))
        scores={
          "clarity":0.95 if len(value.strip())>=80 and len(value)<=1800 else 0.45,
          "problem_first_value":0.95 if re.search(r"problem|manual|delay|cost|time|challenge|losing|pain",lower) else 0.40,
          "indian_smb_relevance":0.95 if re.search(r"indian|india|smb|small business|clinic|studio|rupee|inr",lower) else 0.40,
          "ai_disclosure_compliance":1.0 if compliance else 0.0,
          "conversion_cta":0.95 if re.search(r"\b(would you|shall we|book|demo|demonstration|reply|next step|like to)\b",lower) else 0.35,
        }
        overall=round(sum(scores[k]*w for k,w in {
          "clarity":.20,"problem_first_value":.20,"indian_smb_relevance":.20,
          "ai_disclosure_compliance":.25,"conversion_cta":.15}.items()),4)
        return scores,overall,compliance

    async def run_realtime_skill_test(self, skill_id, scenarios):
        if not isinstance(scenarios,list) or not 1<=len(scenarios)<=30:
            raise ValueError("Provide between 1 and 30 benchmark scenarios")
        skill=await self.repo._one("""SELECT * FROM agent_skills
            WHERE tenant_id=:tenant AND skill_id=:skill FOR UPDATE""",
            {"tenant":self.principal.tenant_id,"skill":skill_id})
        if not skill: raise LookupError("Skill is not available in this tenant")
        await self.repo.session.execute(text("""UPDATE agent_skills SET status='SANDBOX_TESTING',
            updated_at=now() WHERE tenant_id=:tenant AND skill_id=:skill""",
            ),{"tenant":self.principal.tenant_id,"skill":skill_id})
        results=[]; scores=[]; any_compliance_failure=False
        safe_skill={k:skill[k] for k in ("skill_id","department","role_id","category","skill_name","instructions","rubric_json")}
        for scenario in scenarios:
            if not isinstance(scenario,dict): raise ValueError("Each scenario must be a JSON object")
            # Sandbox input is bounded and contains no production tool or DB capability.
            scenario=json.loads(json.dumps(scenario,ensure_ascii=False))
            scenario.pop("tenant_id",None); scenario.pop("tool_calls",None)
            output=self.sandbox_agent(safe_skill,scenario)
            if hasattr(output,"__await__"): output=await output
            rubric,overall,compliance=self._evaluate(output,scenario)
            passed=overall>=float(skill["rubric_json"].get("minimum_overall_score",.85)) and compliance
            any_compliance_failure |= not compliance
            scores.append(overall)
            result={"scenario_input":scenario,"agent_output":output,"rubric_scores":rubric,
                    "overall_score":overall,"passed":passed}
            results.append(result)
            await self.repo.session.execute(text("""
              INSERT INTO skill_test_runs(test_run_id,tenant_id,skill_id,scenario_input,
                agent_output,rubric_scores,overall_score,passed,executed_at)
              VALUES (:id,:tenant,:skill,CAST(:scenario AS jsonb),CAST(:output AS jsonb),
                CAST(:rubric AS jsonb),:score,:passed,now())
            """),{"id":str(uuid4()),"tenant":self.principal.tenant_id,"skill":skill_id,
                  "scenario":json.dumps(scenario,ensure_ascii=False),
                  "output":json.dumps(output,ensure_ascii=False,default=str),
                  "rubric":json.dumps(rubric),"score":overall,"passed":passed})
        overall=round(sum(scores)/len(scores),4)
        passed=overall>=.85 and all(x["passed"] for x in results) and not any_compliance_failure
        status="PROMOTED" if passed else "REJECTED"
        await self.repo.session.execute(text("""UPDATE agent_skills SET status=:status,
            latest_score=:score,updated_at=now()
            WHERE tenant_id=:tenant AND skill_id=:skill"""),
            {"status":status,"score":overall,"tenant":self.principal.tenant_id,"skill":skill_id})
        return {"skill_id":skill_id,"status":status,"overall_score":overall,
                "passed":passed,"threshold":.85,"scenario_count":len(results),
                "compliance_failure":any_compliance_failure,"results":results}

    async def list_test_runs(self, skill_id):
        exists=await self.repo._one("SELECT skill_id FROM agent_skills WHERE tenant_id=:tenant AND skill_id=:skill",
            {"tenant":self.principal.tenant_id,"skill":skill_id})
        if not exists: raise LookupError("Skill is not available in this tenant")
        return await self.repo._rows("""SELECT test_run_id,skill_id,scenario_input,agent_output,
            rubric_scores,overall_score,passed,executed_at FROM skill_test_runs
            WHERE tenant_id=:tenant AND skill_id=:skill ORDER BY executed_at DESC""",
            {"tenant":self.principal.tenant_id,"skill":skill_id})
