from __future__ import annotations

import pytest

from core.retrieve.skill_retriever import SkillHit
from core.skills.evolve.types import TrajectoryContext
from core.skills.routing.skill_router import SkillRouter

pytestmark = pytest.mark.unit


def _pool() -> list[TrajectoryContext]:
    return [
        TrajectoryContext(
            trajectory_id="traj-1",
            task_id="spreadsheet_fix_formula",
            abstract="fix formula and validate output",
            overview="use openpyxl, then verify expected cells",
            trajectory=[],
        )
    ]


def test_router_returns_create_when_no_candidate() -> None:
    router = SkillRouter(model="dummy", api_key="")
    out = router.decide(candidate_skill=None, pool=_pool())
    assert out.decision == "create"
    assert out.scope_match_level == "low"


def test_router_hard_guard_forces_create_when_scope_not_high() -> None:
    candidate = SkillHit(
        skill_name="skill_sheet",
        description="spreadsheet edits",
        uri="ctx://skills/skill_sheet",
        path="",
        score=0.91,
    )
    router = SkillRouter(
        model="dummy",
        api_key="",
        decision_fn=lambda c, scope, pool: {  # noqa: ARG005
            "decision": "update",
            "confidence": 0.99,
            "scope_match_level": "medium",
            "reasoning": "forced by test",
            "task_type_summary": "x",
        },
    )
    out = router.decide(candidate_skill=candidate, pool=_pool())
    assert out.decision == "create"
    assert "hard-guard forced create" in out.reasoning


def test_router_allows_update_when_high_and_confident() -> None:
    candidate = SkillHit(
        skill_name="skill_sheet",
        description="spreadsheet edits",
        uri="ctx://skills/skill_sheet",
        path="",
        score=0.91,
    )
    router = SkillRouter(
        model="dummy",
        api_key="",
        confidence_threshold=0.7,
        decision_fn=lambda c, scope, pool: {  # noqa: ARG005
            "decision": "update",
            "confidence": 0.9,
            "scope_match_level": "high",
            "reasoning": "aligned",
            "task_type_summary": "sheet",
        },
    )
    out = router.decide(candidate_skill=candidate, pool=_pool())
    assert out.decision == "update"
    assert out.scope_match_level == "high"

