from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.skills.evolve.analyst_success import SuccessAnalyst
from core.skills.evolve.patch_merge import hierarchical_merge_success_patches
from core.skills.evolve.skill_apply import apply_merged_success_rules
from core.skills.evolve.types import SuccessPatch, TrajectoryContext

pytestmark = pytest.mark.unit


def _traj(tid: str) -> TrajectoryContext:
    return TrajectoryContext(
        trajectory_id=tid,
        task_id=f"task-{tid}",
        abstract=f"abs-{tid}",
        overview=f"overview-{tid}",
        trajectory=[{"Step": 1, "Action": "local_db_sql(...)"}],
    )


def test_success_analyst_propose_many_with_custom_proposal_fn() -> None:
    def _proposal(_skill_markdown: str, trajectory: TrajectoryContext) -> SuccessPatch:
        return SuccessPatch(
            trajectory_id=trajectory.trajectory_id,
            title="Stable validation sequence",
            sop="Run query then verify output.",
            checklist=["run", "verify"],
            evidence=trajectory.abstract,
        )

    analyst = SuccessAnalyst(
        model="dummy",
        api_key="",
        proposal_fn=_proposal,
    )
    patches, warnings = analyst.propose_many(
        skill_markdown="# skill",
        trajectories=[_traj("a"), _traj("b")],
        max_parallel_analysts=4,
    )
    assert warnings == []
    assert len(patches) == 2
    assert {p.trajectory_id for p in patches} == {"a", "b"}


def test_merge_and_apply_success_rules_updates_skill_md_and_reference(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: skill-x\ndescription: original desc\n---\n# Skill X\n\nBody\n",
        encoding="utf-8",
    )
    patches = [
        SuccessPatch(
            trajectory_id="traj-1",
            title="Validation first",
            sop="Run output verification after each write.",
            checklist=["write", "verify"],
            evidence="ok",
        ),
        SuccessPatch(
            trajectory_id="traj-2",
            title="Validation first",
            sop="Run output verification after each write.",
            checklist=["verify", "recheck"],
            evidence="ok2",
        ),
    ]
    merged = hierarchical_merge_success_patches(patches, merge_batch_size=2)
    assert merged.merged_rule_count == 1
    assert merged.rules[0].support_count == 2

    out = apply_merged_success_rules(skill_md_path=skill_md, rules=merged.rules, dry_run=False)
    updated = skill_md.read_text(encoding="utf-8")
    assert "AMC Evolved Success Patterns" in updated
    assert "Evolved focus:" in updated
    assert out.rule_count == 1
    assert Path(out.reference_path).exists()


def test_apply_rewrites_invalid_frontmatter_to_yaml_safe(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        (
            "---\n"
            "name: skill-y\n"
            "description: Auto-created from trajectory evidence. Focuses on: bad yaml token\n"
            "---\n"
            "# Skill Y\n"
        ),
        encoding="utf-8",
    )
    rules = [
        SuccessPatch(
            trajectory_id="traj-3",
            title="Stable check",
            sop="Verify result format before answer.",
            checklist=["verify"],
            evidence="ok",
        )
    ]
    merged = hierarchical_merge_success_patches(rules, merge_batch_size=1)
    apply_merged_success_rules(skill_md_path=skill_md, rules=merged.rules, dry_run=False)

    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0].strip() == "---"
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    frontmatter = yaml.safe_load("\n".join(lines[1:end]))
    assert isinstance(frontmatter, dict)
    assert frontmatter.get("name") == "skill-y"
    assert isinstance(frontmatter.get("description"), str)

