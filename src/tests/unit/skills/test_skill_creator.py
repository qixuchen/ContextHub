from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.skills.create.skill_creator import (
    create_skill_from_trajectories,
    slugify_skill_name,
    unique_skill_name,
)
from core.skills.evolve.types import TrajectoryContext

pytestmark = pytest.mark.unit


def _extract_frontmatter(markdown: str) -> dict:
    lines = markdown.splitlines()
    assert lines and lines[0].strip() == "---"
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    assert end is not None
    raw = "\n".join(lines[1:end])
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict)
    return loaded


def _pool() -> list[TrajectoryContext]:
    return [
        TrajectoryContext(
            trajectory_id="traj-1",
            task_id="spreadsheet_fix_formula",
            abstract="fix formulas and verify outputs",
            overview="tool sequence",
            trajectory=[],
        ),
        TrajectoryContext(
            trajectory_id="traj-2",
            task_id="spreadsheet_fix_formula",
            abstract="another successful run",
            overview="tool sequence 2",
            trajectory=[],
        ),
    ]


def test_slugify_and_unique_name() -> None:
    assert slugify_skill_name("Spreadsheet Fix Formula!") == "skill_spreadsheet_fix_formula"
    assert unique_skill_name("Spreadsheet Fix Formula!", existing_names={"skill_spreadsheet_fix_formula"}) == (
        "skill_spreadsheet_fix_formula_2"
    )


def test_create_skill_from_trajectories_writes_skill_md(tmp_path: Path) -> None:
    out = create_skill_from_trajectories(
        skill_root=str(tmp_path),
        task_type_summary="spreadsheet formula repair",
        pool=_pool(),
        existing_names=set(),
        dry_run=False,
    )
    path = Path(out.skill_md_path)
    assert out.created is True
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "name:" in text
    assert "## Evidence Trajectories" in text
    assert "`traj-1`" in text
    frontmatter = _extract_frontmatter(text)
    assert frontmatter["name"] == out.skill_name
    assert isinstance(frontmatter.get("description"), str)


def test_create_skill_frontmatter_is_yaml_safe_with_colons(tmp_path: Path) -> None:
    out = create_skill_from_trajectories(
        skill_root=str(tmp_path),
        task_type_summary="alfworld: kitchen > put apple on table",
        pool=_pool(),
        existing_names=set(),
        dry_run=False,
    )
    text = Path(out.skill_md_path).read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(text)
    assert frontmatter["name"] == out.skill_name
    assert "Focuses on:" in frontmatter["description"]


def test_create_skill_description_not_truncated(tmp_path: Path) -> None:
    task_summary = (
        "alfworld kitchen cleanup and object relocation with explicit navigation checkpoints, "
        "container open/close verification, object pickup retries, and final placement validation "
        "on requested receptacle"
    )
    out = create_skill_from_trajectories(
        skill_root=str(tmp_path),
        task_type_summary=task_summary,
        pool=_pool(),
        existing_names=set(),
        dry_run=False,
    )
    text = Path(out.skill_md_path).read_text(encoding="utf-8")
    frontmatter = _extract_frontmatter(text)
    assert task_summary in frontmatter["description"]

