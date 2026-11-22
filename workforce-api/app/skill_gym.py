"""Isolated, rubric-driven skill evaluation and promotion engine.

Sandbox agents receive only immutable skill/scenario data. They receive no
repository, database, network, messaging, publishing, payment or adapter handles.
Only benchmark evidence is persisted by this service.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from uuid import uuid4

from sqlalchemy import text

from .skill_registry import FORTUNE50_SKILLS

AI_DISCLOSURE = "I am an AI assistant acting on behalf of RunGrow."
RUBRIC_WEIGHTS = {
    "clarity": 0.20,
    "problem_first_value": 0.20,
    "indian_smb_relevance": 0.20,
    "ai_disclosure_compliance": 0.25,
    "conversion_cta": 0.15,
}
FORBIDDEN_ACTION = re.compile(
    r"\b(published|posted|sent (?:the )?(?:message|post|campaign)|"
    r"charged your card|payment processed|campaign is live|"
    r"provisioned your account|successfully launched)\b",
    re.I,
)


class SkillGymError(Exception):
    pass


class SkillGymEngine:
    def __init__(self, repo, principal, sandbox_agent=None):
        self.repo = repo
        self.principal = principal
        self.sandbox_agent = sandbox_agent or self._sandbox_agent

    @staticmethod
    def _hash(skill):
        raw = json.dumps(
            {k: skill[k] for k in (
                "skill_id", "department", "role_id", "category",
                "skill_name", "instructions", "rubric_json"
            )},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _validate_rubric(rubric):
        if not isinstance(rubric, dict):
            raise ValueError("rubric_json must be an object")
        threshold = float(rubric.get("minimum_overall_score", 0.85))
        if not math.isfinite(threshold) or threshold != 0.85:
            raise ValueError("Skill promotion threshold is fixed at 0.85")
        weights = {key: float(rubric.get(key, default)) for key, default in RUBRIC_WEIGHTS.items()}
        if any(not math.isfinite(value) or value < 0 for value in weights.values()):
            raise ValueError("Rubric weights must be finite and non-negative")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6):
            raise ValueError("Rubric weights must sum to 1.0")
        return {**weights, "minimum_overall_score": 0.85}

    async def seed_catalog(self):
        """Idempotently materialise the curated catalog without demoting unchanged skills."""
        for skill in FORTUNE50_SKILLS:
            await self.build_or_update_skill(**{
                k: skill[k] for k in (
                    "skill_id", "department", "role_id", "category",
                    "skill_name", "instructions", "rubric_json"
                )
            })
        return {"seeded": len(FORTUNE50_SKILLS), "catalog_count": len(FORTUNE50_SKILLS)}

    async def build_or_update_skill(
        self, *, skill_id, department, role_id, category, skill_name,
        instructions, rubric_json=None, created_by=None,
    ):
        if not all(isinstance(x, str) and x.strip() for x in (
            skill_id, department, role_id, category, skill_name, instructions
        )):
            raise ValueError("All skill identity and instruction fields are required")
        if len(skill_id) > 120 or len(instructions) > 12000 or len(skill_name) > 180:
            raise ValueError("Skill definition exceeds supported limits")
        rubric = self._validate_rubric(rubric_json or {
            **RUBRIC_WEIGHTS, "minimum_overall_score": 0.85
        })
        skill = {
            "skill_id": skill_id, "department": department, "role_id": role_id,
            "category": category, "skill_name": skill_name,
            "instructions": instructions, "rubric_json": rubric,
        }
        version_hash = self._hash(skill)
        await self.repo.session.execute(text("""
          INSERT INTO agent_skills
            (id,tenant_id,skill_id,department,role_id,category,skill_name,instructions,
             rubric_json,status,version_hash,version_number,created_by,created_at,updated_at)
          VALUES (:id,:tenant,:skill,:department,:role,:category,:name,:instructions,
             CAST(:rubric AS jsonb),'DRAFT',:hash,1,:actor,now(),now())
          ON CONFLICT (tenant_id,skill_id) DO UPDATE SET
            department=EXCLUDED.department, role_id=EXCLUDED.role_id,
            category=EXCLUDED.category, skill_name=EXCLUDED.skill_name,
            instructions=EXCLUDED.instructions, rubric_json=EXCLUDED.rubric_json,
            version_number=CASE
              WHEN agent_skills.version_hash IS DISTINCT FROM EXCLUDED.version_hash
              THEN agent_skills.version_number + 1 ELSE agent_skills.version_number END,
            status=CASE
              WHEN agent_skills.version_hash IS DISTINCT FROM EXCLUDED.version_hash
              THEN 'DRAFT' ELSE agent_skills.status END,
            latest_score=CASE
              WHEN agent_skills.version_hash IS DISTINCT FROM EXCLUDED.version_hash
              THEN NULL ELSE agent_skills.latest_score END,
            version_hash=EXCLUDED.version_hash, updated_at=now()
        """), {
            "id": str(uuid4()), "tenant": self.principal.tenant_id, "skill": skill_id,
            "department": department, "role": role_id, "category": category,
            "name": skill_name, "instructions": instructions,
            "rubric": json.dumps(rubric), "hash": version_hash,
            "actor": created_by or self.principal.user_id,
        })
        stored = await self.repo._one(
            "SELECT status,version_number,latest_score FROM agent_skills "
            "WHERE tenant_id=:tenant AND skill_id=:skill",
            {"tenant": self.principal.tenant_id, "skill": skill_id},
        )
        return {
            **skill, "status": stored["status"], "version_number": stored["version_number"],
            "latest_score": stored["latest_score"], "version_hash": version_hash,
        }

    @staticmethod
    def _sandbox_agent(skill, scenario):
        """Deterministic local agent adapter; no production capabilities are injected."""
        problem = str(scenario.get("problem") or scenario.get("input") or
                      "A small business is losing time to manual follow-up.")
        vertical = str(scenario.get("vertical") or "Indian SMB")
        offer = str(scenario.get("verified_offer") or "a guided product demonstration")
        return (
            f"{problem}\nFor an {vertical}, {skill['skill_name']} addresses this "
            "operational friction with a practical, evidence-led workflow. "
            "RunGrow can assess fit using verified product capabilities. "
            f"{AI_DISCLOSURE} Would you like to arrange {offer}?"
        )

    @staticmethod
    def _evaluate(output, scenario, rubric_json=None):
        value = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        value = value[:4000]
        lower = value.lower()
        disclosed = bool(re.search(r"\bai\b|artificial intelligence", value, re.I))
        identified = bool(re.search(r"\brungrow\b", value, re.I))
        compliance = disclosed and identified and not bool(FORBIDDEN_ACTION.search(value))
        problem = str(scenario.get("problem") or scenario.get("input") or "").strip().lower()
        problem_first = bool(problem and lower.startswith(problem[: min(45, len(problem))]))
        scores = {
            "clarity": 0.95 if 80 <= len(value.strip()) <= 1800 else 0.45,
            "problem_first_value": 0.95 if problem_first or re.search(
                r"problem|manual|delay|cost|time|challenge|losing|pain", lower
            ) else 0.40,
            "indian_smb_relevance": 0.95 if re.search(
                r"indian|india|smb|small business|clinic|studio|rupee|inr", lower
            ) else 0.40,
            "ai_disclosure_compliance": 1.0 if compliance else 0.0,
            "conversion_cta": 0.95 if re.search(
                r"\b(would you|shall we|book|demo|demonstration|reply|next step|like to)\b", lower
            ) else 0.35,
        }
        weights = {key: float((rubric_json or {}).get(key, default))
                   for key, default in RUBRIC_WEIGHTS.items()}
        overall = round(sum(scores[key] * weights[key] for key in RUBRIC_WEIGHTS), 4)
        return scores, overall, compliance

    async def run_realtime_skill_test(self, skill_id, scenarios):
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 30:
            raise ValueError("Provide between 1 and 30 benchmark scenarios")
        skill = await self.repo._one("""
            SELECT * FROM agent_skills
            WHERE tenant_id=:tenant AND skill_id=:skill FOR UPDATE
        """, {"tenant": self.principal.tenant_id, "skill": skill_id})
        if not skill:
            raise LookupError("Skill is not available in this tenant")
        await self.repo.session.execute(text("""
            UPDATE agent_skills SET status='SANDBOX_TESTING', updated_at=now()
            WHERE tenant_id=:tenant AND skill_id=:skill
        """), {"tenant": self.principal.tenant_id, "skill": skill_id})

        results, scores = [], []
        any_compliance_failure = False
        safe_skill = {
            key: skill[key] for key in (
                "skill_id", "department", "role_id", "category",
                "skill_name", "instructions", "rubric_json"
            )
        }
        for raw_scenario in scenarios:
            if not isinstance(raw_scenario, dict):
                raise ValueError("Each scenario must be a JSON object")
            scenario = json.loads(json.dumps(raw_scenario, ensure_ascii=False))
            scenario.pop("tenant_id", None)
            scenario.pop("tool_calls", None)
            scenario.pop("external_actions", None)
            if len(json.dumps(scenario, ensure_ascii=False)) > 8000:
                raise ValueError("Scenario exceeds the 8 KB sandbox input limit")

            output = self.sandbox_agent(safe_skill, scenario)
            if hasattr(output, "__await__"):
                output = await output
            if not isinstance(output, (str, dict, list)):
                output = str(output)
            rubric_scores, score, compliant = self._evaluate(
                output, scenario, skill["rubric_json"]
            )
            passed = score >= 0.85 and compliant
            any_compliance_failure |= not compliant
            scores.append(score)
            result = {
                "scenario_input": scenario, "agent_output": output,
                "rubric_scores": rubric_scores, "overall_score": score,
                "passed": passed,
            }
            results.append(result)
            await self.repo.session.execute(text("""
              INSERT INTO skill_test_runs(test_run_id,tenant_id,skill_id,scenario_input,
                agent_output,rubric_scores,overall_score,passed,executed_at)
              VALUES (:id,:tenant,:skill,CAST(:scenario AS jsonb),CAST(:output AS jsonb),
                CAST(:rubric AS jsonb),:score,:passed,now())
            """), {
                "id": str(uuid4()), "tenant": self.principal.tenant_id, "skill": skill_id,
                "scenario": json.dumps(scenario, ensure_ascii=False),
                "output": json.dumps(output, ensure_ascii=False, default=str),
                "rubric": json.dumps(rubric_scores), "score": score, "passed": passed,
            })

        overall = round(sum(scores) / len(scores), 4)
        promoted = overall >= 0.85 and all(row["passed"] for row in results) and not any_compliance_failure
        status = "PROMOTED" if promoted else "REJECTED"
        await self.repo.session.execute(text("""
            UPDATE agent_skills SET status=:status, latest_score=:score, updated_at=now()
            WHERE tenant_id=:tenant AND skill_id=:skill
        """), {"status": status, "score": overall,
               "tenant": self.principal.tenant_id, "skill": skill_id})
        return {
            "skill_id": skill_id, "status": status, "overall_score": overall,
            "passed": promoted, "threshold": 0.85, "scenario_count": len(results),
            "compliance_failure": any_compliance_failure, "results": results,
        }

    async def list_test_runs(self, skill_id):
        exists = await self.repo._one(
            "SELECT skill_id FROM agent_skills WHERE tenant_id=:tenant AND skill_id=:skill",
            {"tenant": self.principal.tenant_id, "skill": skill_id},
        )
        if not exists:
            raise LookupError("Skill is not available in this tenant")
        return await self.repo._rows("""
            SELECT test_run_id,skill_id,scenario_input,agent_output,rubric_scores,
                   overall_score,passed,executed_at
            FROM skill_test_runs WHERE tenant_id=:tenant AND skill_id=:skill
            ORDER BY executed_at DESC
        """, {"tenant": self.principal.tenant_id, "skill": skill_id})
