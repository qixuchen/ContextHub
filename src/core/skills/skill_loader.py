"""Load and validate skills from local skill root."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SkillDoc:
    skill_name: str
    description: str
    root_path: str
    skill_md_path: str
    content_sha256: str
    updated_at: str


def _parse_frontmatter(markdown: str) -> tuple[dict, str]:
    text = markdown.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    yaml_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, body


def load_skills(*, root: str) -> tuple[list[SkillDoc], list[str]]:
    root_path = Path(root)
    if not root_path.exists():
        return [], [f"skill root does not exist: {root_path}"]
    warnings: list[str] = []
    docs: list[SkillDoc] = []
    now = datetime.now(UTC).isoformat()
    for skill_dir in sorted([p for p in root_path.iterdir() if p.is_dir()]):
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            warnings.append(f"skip skill '{skill_dir.name}': SKILL.md not found")
            continue
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except Exception as exc:
            warnings.append(f"skip skill '{skill_dir.name}': failed reading SKILL.md ({type(exc).__name__})")
            continue
        meta, _ = _parse_frontmatter(raw)
        skill_name = str(meta.get("name") or skill_dir.name).strip()
        description = str(meta.get("description") or "").strip()
        if not skill_name:
            warnings.append(f"skip skill '{skill_dir.name}': missing name in frontmatter")
            continue
        if not description:
            warnings.append(f"skip skill '{skill_dir.name}': missing description in frontmatter")
            continue
        docs.append(
            SkillDoc(
                skill_name=skill_name,
                description=description,
                root_path=str(skill_dir),
                skill_md_path=str(skill_md),
                content_sha256=_sha256(description),
                updated_at=now,
            )
        )
    return docs, warnings

