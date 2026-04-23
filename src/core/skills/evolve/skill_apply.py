"""Apply merged success rules to skill directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from core.skills.evolve.types import MergedSuccessRule

BLOCK_START = "<!-- AMC_EVOLVE_SUCCESS_START -->"
BLOCK_END = "<!-- AMC_EVOLVE_SUCCESS_END -->"
REFERENCE_REL_PATH = "references/amc_evolved_success.md"


def _safe_text(value: str) -> str:
    return str(value or "").strip()


def _fallback_parse_frontmatter(yaml_text: str) -> dict:
    meta: dict[str, str] = {}
    for raw_line in yaml_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        k = key.strip()
        v = value.strip()
        if not k:
            continue
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        meta[k] = v
    return meta


def _split_frontmatter(raw: str) -> tuple[dict, str, bool]:
    text = raw.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text, False
    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text, False
    yaml_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        meta = yaml.safe_load(yaml_text) or {}
    except Exception:
        meta = _fallback_parse_frontmatter(yaml_text)
    if not isinstance(meta, dict):
        meta = _fallback_parse_frontmatter(yaml_text)
    return meta, body, True


def _compose_description(old_desc: str, rules: list[MergedSuccessRule]) -> str:
    base = _safe_text(old_desc)
    if "| Evolved focus:" in base:
        base = base.split("| Evolved focus:", 1)[0].strip()
    if not rules:
        return base
    titles = []
    seen = set()
    for rule in rules:
        title = _safe_text(rule.title).replace("|", " ").replace("\n", " ")
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
        if len(titles) >= 3:
            break
    if not titles:
        return base
    focus = ", ".join(titles)
    if base:
        merged = f"{base} | Evolved focus: {focus}".strip()
    else:
        merged = f"Evolved focus: {focus}".strip()
    return merged


def _render_success_block(*, rules: list[MergedSuccessRule], generated_at: str) -> str:
    lines: list[str] = []
    lines.append(BLOCK_START)
    lines.append("## AMC Evolved Success Patterns")
    lines.append("")
    lines.append(f"_Generated at: {generated_at}_")
    lines.append("")
    lines.append(f"Reference details: `{REFERENCE_REL_PATH}`")
    lines.append("")
    if not rules:
        lines.append("- No stable success pattern was extracted in this run.")
    else:
        for idx, rule in enumerate(rules, start=1):
            lines.append(f"### {idx}. {rule.title} (support={rule.support_count})")
            lines.append(f"- SOP: {rule.sop}")
            if rule.checklist:
                lines.append("- Checklist:")
                for item in rule.checklist[:4]:
                    lines.append(f"  - {item}")
            if rule.evidence_trajectory_ids:
                ids = ", ".join(f"`{x}`" for x in rule.evidence_trajectory_ids[:8])
                lines.append(f"- Evidence trajectories: {ids}")
            lines.append("")
    lines.append(BLOCK_END)
    return "\n".join(lines).strip() + "\n"


def _replace_or_append_block(body: str, block: str) -> tuple[str, bool]:
    start = body.find(BLOCK_START)
    end = body.find(BLOCK_END)
    if start != -1 and end != -1 and end > start:
        end_idx = end + len(BLOCK_END)
        next_body = body[:start].rstrip() + "\n\n" + block + body[end_idx:].lstrip()
        return next_body, True
    suffix = "" if body.endswith("\n") else "\n"
    next_body = body + suffix + "\n" + block
    return next_body, False


def _render_reference_doc(*, rules: list[MergedSuccessRule], generated_at: str) -> str:
    lines = [
        "# AMC Evolved Success Rules",
        "",
        f"- Generated at: {generated_at}",
        f"- Rule count: {len(rules)}",
        "",
    ]
    for idx, rule in enumerate(rules, start=1):
        lines.append(f"## {idx}. {rule.title}")
        lines.append("")
        lines.append(f"**SOP**: {rule.sop}")
        lines.append("")
        lines.append(f"**Support**: {rule.support_count}")
        if rule.evidence_trajectory_ids:
            lines.append("")
            lines.append("**Evidence trajectories**:")
            for tid in rule.evidence_trajectory_ids:
                lines.append(f"- `{tid}`")
        if rule.checklist:
            lines.append("")
            lines.append("**Checklist**:")
            for item in rule.checklist:
                lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


@dataclass
class ApplyResult:
    skill_md_path: str
    reference_path: str
    rule_count: int
    replaced_existing_block: bool
    description_updated: bool
    generated_at: str


def apply_merged_success_rules(
    *,
    skill_md_path: Path,
    rules: list[MergedSuccessRule],
    dry_run: bool = False,
) -> ApplyResult:
    raw = skill_md_path.read_text(encoding="utf-8")
    meta, body, has_frontmatter = _split_frontmatter(raw)
    old_desc = _safe_text(meta.get("description"))
    if not _safe_text(meta.get("name")):
        meta["name"] = skill_md_path.parent.name
    new_desc = _compose_description(old_desc, rules)
    description_updated = bool(new_desc and new_desc != old_desc)
    if description_updated:
        meta["description"] = new_desc

    generated_at = datetime.now(UTC).isoformat()
    block = _render_success_block(rules=rules, generated_at=generated_at)
    next_body, replaced = _replace_or_append_block(body, block)
    if has_frontmatter or meta:
        meta_yaml = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
        next_markdown = f"---\n{meta_yaml}\n---\n{next_body.lstrip()}"
    else:
        next_markdown = next_body

    ref_path = skill_md_path.parent / REFERENCE_REL_PATH
    ref_markdown = _render_reference_doc(rules=rules, generated_at=generated_at)

    if not dry_run:
        skill_md_path.write_text(next_markdown, encoding="utf-8")
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(ref_markdown, encoding="utf-8")

    return ApplyResult(
        skill_md_path=str(skill_md_path),
        reference_path=str(ref_path),
        rule_count=len(rules),
        replaced_existing_block=replaced,
        description_updated=description_updated,
        generated_at=generated_at,
    )

