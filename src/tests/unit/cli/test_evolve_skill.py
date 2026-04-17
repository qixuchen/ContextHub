from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.evolve_skill import build_parser, run_evolve
from core.skills.evolve.types import MergedSuccessRule, SuccessPatch, TrajectoryContext
from core.skills.skill_loader import SkillDoc

pytestmark = pytest.mark.unit


def test_build_parser_defaults_for_phase_a() -> None:
    parser = build_parser()
    args = parser.parse_args(["--skill-name", "skill_a", "--anchor-trajectory-id", "traj-1"])
    assert args.top_k == 8
    assert args.trajectory_min_score == 0.7
    assert args.merge_batch_size == 8
    assert args.analyst_mode == "success_only"


def test_run_evolve_rejects_non_success_mode() -> None:
    with pytest.raises(ValueError, match="success_only"):
        run_evolve(
            skill_name="skill_a",
            anchor_trajectory_id="traj-1",
            account_id="acc",
            agent_id="agent",
            top_k=8,
            trajectory_min_score=0.7,
            include_anchor=True,
            analyst_mode="combined",
            merge_batch_size=8,
            max_parallel_analysts=4,
            dry_run=True,
            config_path=None,
        )


def test_run_evolve_dry_run_happy_path(monkeypatch, tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: skill_a\ndescription: desc\n---\n# body\n", encoding="utf-8")
    skill_doc = SkillDoc(
        skill_name="skill_a",
        description="desc",
        root_path=str(tmp_path),
        skill_md_path=str(skill_md),
        content_sha256="sha",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    settings = SimpleNamespace(
        skill_root=str(tmp_path),
        storage=SimpleNamespace(localfs_root=str(tmp_path / "content")),
        llm_model="dummy-model",
        openai_api_key="",
        model_endpoints=SimpleNamespace(llm_base_url="", embedder_base_url=""),
        commit=SimpleNamespace(temporal_fallback_edge=True, reasoning_min_confidence=0.55),
    )
    monkeypatch.setattr("cli.evolve_skill.load_settings", lambda config_path=None: settings)
    monkeypatch.setattr("cli.evolve_skill._load_skill_doc", lambda **kwargs: (skill_doc, []))
    monkeypatch.setattr("cli.evolve_skill._build_retrieve_service", lambda settings: (object(), []))

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            anchor = TrajectoryContext(
                trajectory_id="traj-1",
                task_id="task-1",
                abstract="a",
                overview="b",
                trajectory=[],
            )
            return SimpleNamespace(
                anchor=anchor,
                neighbors=[],
                pool=[anchor],
                retrieved_trajectory_ids=[],
                retrieved_trajectory_scores=[],
                warnings=[],
            )

    class _FakeSuccessAnalyst:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def propose_many(self, **kwargs):  # noqa: ANN001
            del kwargs
            return (
                [
                    SuccessPatch(
                        trajectory_id="traj-1",
                        title="rule",
                        sop="do x",
                        checklist=["c1"],
                    )
                ],
                [],
            )

    monkeypatch.setattr("cli.evolve_skill.TrajectoryPoolBuilder", _FakePoolBuilder)
    monkeypatch.setattr("cli.evolve_skill.SuccessAnalyst", _FakeSuccessAnalyst)
    monkeypatch.setattr(
        "cli.evolve_skill.hierarchical_merge_success_patches",
        lambda patches, merge_batch_size: SimpleNamespace(
            rules=[
                MergedSuccessRule(
                    title="rule",
                    sop="do x",
                    checklist=["c1"],
                    evidence_trajectory_ids=["traj-1"],
                    support_count=1,
                )
            ],
            merged_rule_count=1,
            levels=1,
            merge_batch_size=merge_batch_size,
        ),
    )
    monkeypatch.setattr(
        "cli.evolve_skill.apply_merged_success_rules",
        lambda **kwargs: SimpleNamespace(
            skill_md_path=str(skill_md),
            reference_path=str(tmp_path / "references" / "amc_evolved_success.md"),
            rule_count=1,
            description_updated=False,
            replaced_existing_block=False,
        ),
    )

    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
            trajectory_min_score=0.7,
        include_anchor=True,
        analyst_mode="success_only",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert out["mode"] == "success_only"
    assert out["patch_summary"]["proposed"] == 1
    assert out["patch_summary"]["merged_rules"] == 1

