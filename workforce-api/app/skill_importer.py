"""Secure importer for the open Agent Skills SKILL.md convention.

The parser accepts YAML frontmatter plus Markdown instructions. Imported skills
are treated as untrusted data and are never executed as shell, Python or tools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

MAX_SKILL_BYTES = 64 * 1024
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
BLOCKED_PATTERNS = (
    re.compile(r"(?i)\b(?:run|execute|invoke)\s+(?:any|arbitrary|untrusted)\s+(?:shell|bash|zsh|powershell|terminal|command)"),
    re.compile(r"(?i)\b(?:execute|run|launch)\b[^\n]{0,80}\b(?:shell|terminal|command line|bash|powershell)\b"),
    re.compile(r"(?i)\b(?:subprocess\.(?:run|Popen|call)|os\.system|child_process\.exec|shell=true|bash\s+-c|sh\s+-c|powershell\s+-command|sudo\s+|rm\s+-rf)\b"),
    re.compile(r"(?i)\b(?:curl|wget|nc|netcat|ssh|scp)\s+[^\n]*(?:\||>|/dev/tcp)"),
    re.compile(r"(?i)\b(?:exfiltrat\w*|steal|dump|upload|forward)\b[^\n]{0,100}\b(?:secret|credential|api.?key|access.?token|private.?key|password)"),
    re.compile(r"(?i)\b(?:read|print|reveal|send|upload|expose)\b[^\n]{0,100}\b(?:\.env|environment variables|credentials|api keys|private keys)"),
    re.compile(r"(?i)\b(?:bypass|solve|defeat|circumvent|ignore|disable)\b[^\n]{0,80}\b(?:captcha|login|authentication|mfa|two.factor|access control)\b"),
    re.compile(r"(?i)\b(?:ignore|override|disable|bypass)\b[^\n]{0,100}\b(?:growtoys|discovery.only|product eligibility|pricing approval)"),
    re.compile(r"(?i)\b(?:growtoys)\b[^\n]{0,140}\b(?:sell|quote|price|discount|checkout|convert|commercial|production|live product)\b"),
    re.compile(r"(?i)\b(?:change|promote|treat|mark)\b[^\n]{0,100}\b(?:growtoys)\b[^\n]{0,100}\b(?:commercial|production|live)\b"),
)

@dataclass(frozen=True)
class ImportedSkill:
    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: dict[str, Any]
    allowed_tools: tuple[str, ...]
    instructions: str
    raw_frontmatter: dict[str, Any]

class SkillImportError(ValueError):
    pass

def parse_skill_md(source: str) -> ImportedSkill:
    if not isinstance(source, str) or not source.strip():
        raise SkillImportError("SKILL.md content is required")
    if len(source.encode("utf-8")) > MAX_SKILL_BYTES:
        raise SkillImportError("SKILL.md exceeds the 64 KiB import limit")
    if source.startswith("\ufeff"):
        source = source.lstrip("\ufeff")
    lines = source.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillImportError("SKILL.md must begin with YAML frontmatter")
    try:
        closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise SkillImportError("YAML frontmatter closing delimiter is missing") from exc
    try:
        header = yaml.safe_load("\n".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        raise SkillImportError(f"Invalid SKILL.md YAML frontmatter: {exc}") from exc
    if not isinstance(header, dict):
        raise SkillImportError("SKILL.md frontmatter must be a YAML mapping")
    name = header.get("name")
    description = header.get("description")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not NAME_RE.fullmatch(name):
        raise SkillImportError("name must be lowercase hyphenated, 1–64 characters")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        raise SkillImportError("description must contain 1–1024 characters")
    for optional in ("license", "compatibility"):
        value = header.get(optional)
        if value is not None and (not isinstance(value, str) or len(value) > (500 if optional == "compatibility" else 1024)):
            raise SkillImportError(f"{optional} must be a string no longer than 1024 characters")
    metadata = header.get("metadata", {})
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or len(metadata) > 32 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items()):
        raise SkillImportError("metadata must be a string-to-string mapping with at most 32 entries")
    allowed = header.get("allowed-tools", [])
    if isinstance(allowed, str):
        allowed = allowed.split()
    if not isinstance(allowed, list) or len(allowed) > 64 or any(
        not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", item)
        for item in allowed
    ):
        raise SkillImportError("allowed-tools must be a list of safe tool identifiers")
    instructions = "\n".join(lines[closing + 1:]).strip()
    if not instructions:
        raise SkillImportError("Markdown instruction body is required")
    for pattern in BLOCKED_PATTERNS:
        match = pattern.search(instructions)
        if match:
            raise SkillImportError(f"Governance validation blocked unsafe instruction: {match.group(0)[:100]}")
    # Shell/code fences are allowed as explanatory documentation only when they
    # contain no executable credential or bypass directives; the sandbox never runs them.
    return ImportedSkill(
        name=name, description=description, license=header.get("license"),
        compatibility=header.get("compatibility"), metadata=metadata,
        allowed_tools=tuple(allowed), instructions=instructions,
        raw_frontmatter=header,
    )

def skill_to_gym_payload(skill: ImportedSkill, *, department: str, role_id: str) -> dict[str, Any]:
    return {
        "skill_id": "imported." + skill.name,
        "department": department,
        "role_id": role_id,
        "category": "agent_skills_import",
        "skill_name": skill.name.replace("-", " ").title(),
        "instructions": (
            f"{skill.description}\n\n"
            f"Imported open-standard skill. License: {skill.license or 'unspecified'}. "
            f"Compatibility: {skill.compatibility or 'unspecified'}. "
            f"Declared tools (not automatically granted): {', '.join(skill.allowed_tools) or 'none'}.\n\n"
            f"{skill.instructions}"
        ),
        "rubric_json": None,
    }
