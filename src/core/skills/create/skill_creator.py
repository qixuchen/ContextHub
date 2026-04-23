"""Create new skill folder from trajectory pool evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from core.skills.evolve.types import TrajectoryContext


def _safe_text(value: str) -> str:
    return str(value or "").strip()


def slugify_skill_name(raw: str) -> str:
    text = _safe_text(raw).lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    if not text:
        return "skill_auto"
    if not text.startswith("skill_"):
        text = f"skill_{text}"
    return text[:48]


def unique_skill_name(base_name: str, *, existing_names: set[str]) -> str:
    base = slugify_skill_name(base_name)
    if base not in existing_names:
        return base
    idx = 2
    while True:
        cand = f"{base}_{idx}"
        if cand not in existing_names:
            return cand
        idx += 1


def _infer_name_seed(task_type_summary: str, pool: list[TrajectoryContext]) -> str:
    summary = _safe_text(task_type_summary)
    if summary:
        return summary.split("|", 1)[0].strip()
    if pool and pool[0].task_id:
        return pool[0].task_id
    if pool and pool[0].abstract:
        return pool[0].abstract[:40]
    return "auto"


def _build_description(task_type_summary: str) -> str:
    summary = _safe_text(task_type_summary) or "related tasks"
    return (
        "Auto-created from trajectory evidence. "
        f"Focuses on: {summary[:180]}. "
        "Use this skill when task type and tool pattern are highly aligned."
    )[:320]


def _build_skill_markdown(
    *,
    skill_name: str,
    description: str,
    task_type_summary: str,
    pool: list[TrajectoryContext],
) -> str:
    frontmatter = yaml.safe_dump(
        {"name": skill_name, "description": description},
        allow_unicode=True,
        sort_keys=False,
    ).strip()
    lines: list[str] = [
        "---",
        frontmatter,
        "---",
        "",
        f"# {skill_name}",
        "",
        "## Scope",
        "- This skill is created from recent successful trajectories.",
        f"- Task type summary: {(_safe_text(task_type_summary) or 'N/A')[:240]}",
        "",
        "## When to use",
        "- The current task shares similar intent, tool usage, and output shape with evidence trajectories.",
        "",
        "## When not to use",
        "- The task objective or domain differs significantly from the evidence trajectories.",
        "- Required tools are unavailable or workflow assumptions do not hold.",
        "",
        "## Workflow",
        "1. Analyze task constraints and identify required tools.",
        "2. Follow a stable execution sequence observed in successful trajectories.",
        "3. Perform explicit output verification before final response.",
        "",
        "## Checklist",
        "- [ ] Confirm task scope matches this skill.",
        "- [ ] Execute key tool sequence in controlled order.",
        "- [ ] Verify final output with deterministic checks.",
        "",
        "## Evidence Trajectories",
    ]
    for item in pool[:8]:
        tid = item.trajectory_id or "unknown"
        snippet = _safe_text(item.abstract) or _safe_text(item.overview)
        if snippet:
            lines.append(f"- `{tid}`: {snippet[:200]}")
        else:
            lines.append(f"- `{tid}`")
    lines.append("")
    return "\n".join(lines)


@dataclass
class CreateSkillResult:
    skill_name: str
    skill_md_path: str
    created: bool
    description: str


def create_skill_from_trajectories(
    *,
    skill_root: str,
    task_type_summary: str,
    pool: list[TrajectoryContext],
    existing_names: set[str],
    name_seed: str | None = None,
    dry_run: bool = False,
) -> CreateSkillResult:
    seed = _safe_text(name_seed) or _infer_name_seed(task_type_summary, pool)
    skill_name = unique_skill_name(seed, existing_names=existing_names)
    description = _build_description(task_type_summary)
    root = Path(skill_root) / skill_name
    skill_md = root / "SKILL.md"
    markdown = _build_skill_markdown(
        skill_name=skill_name,
        description=description,
        task_type_summary=task_type_summary,
        pool=pool,
    )
    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(markdown, encoding="utf-8")
    return CreateSkillResult(
        skill_name=skill_name,
        skill_md_path=str(skill_md),
        created=not dry_run,
        description=description,
    )

