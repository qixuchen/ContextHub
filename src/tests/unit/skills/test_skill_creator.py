from __future__ import annotations

from pathlib import Path

import pytest

from core.skills.create.skill_creator import (
    create_skill_from_trajectories,
    slugify_skill_name,
    unique_skill_name,
)
from core.skills.evolve.types import TrajectoryContext

pytestmark = pytest.mark.unit


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

