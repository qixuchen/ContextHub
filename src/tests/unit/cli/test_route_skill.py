from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.route_skill import build_parser, run_route
from core.retrieve.skill_retriever import SkillHit
from core.skills.evolve.types import TrajectoryContext

pytestmark = pytest.mark.unit


def _fake_pool_result() -> SimpleNamespace:
    anchor = TrajectoryContext(
        trajectory_id="traj-anchor",
        task_id="task-anchor",
        abstract="abs",
        overview="ov",
        trajectory=[],
    )
    return SimpleNamespace(
        anchor=anchor,
        neighbors=[],
        pool=[anchor],
        retrieved_trajectory_ids=[],
        query_payload={"task_description": "task"},
        warnings=[],
    )


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        skill_root="data/skill",
        skill_embedding_state_file="data/index/skill_embedding_state.json",
        skill_embedding_max_workers=4,
        embedding_provider="openai",
        embedding_model="dummy-embedding",
        openai_api_key="dummy-key",
        llm_model="dummy-llm",
        storage=SimpleNamespace(localfs_root="data/content"),
        model_endpoints=SimpleNamespace(embedder_base_url="", llm_base_url=""),
        commit=SimpleNamespace(temporal_fallback_edge=True, reasoning_min_confidence=0.55),
    )


def test_build_parser_defaults_for_route_phase_a1() -> None:
    args = build_parser().parse_args(["--anchor-trajectory-id", "traj-1"])
    assert args.top_k == 8
    assert args.skill_top_k == 1
    assert args.merge_batch_size == 8
    assert args.force_mode == "auto"


def test_run_route_auto_update_branch(monkeypatch) -> None:
    monkeypatch.setattr("cli.route_skill.load_settings", lambda config_path=None: _fake_settings())
    monkeypatch.setattr("cli.route_skill._build_retrieve_service", lambda settings: (object(), []))
    monkeypatch.setattr("cli.route_skill.LocalFSTrajectoryRepository", lambda root: object())

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            return _fake_pool_result()

    monkeypatch.setattr("cli.route_skill.TrajectoryPoolBuilder", _FakePoolBuilder)

    class _FakeSkillRetriever:
        def recall(self, **kwargs):  # noqa: ANN001
            del kwargs
            return [
                SkillHit(
                    skill_name="skill_creator",
                    description="desc",
                    uri="ctx://skills/skill_creator",
                    path="data/skill/skill_creator",
                    score=0.9,
                )
            ]

    monkeypatch.setattr("cli.route_skill._build_skill_retriever", lambda settings: (_FakeSkillRetriever(), []))

    class _FakeRouter:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def summarize_task_type(self, pool):  # noqa: ANN001
            return "summary"

        def decide(self, *, candidate_skill, pool):  # noqa: ANN001
            return SimpleNamespace(
                decision="update",
                confidence=0.9,
                scope_match_level="high",
                reasoning="aligned",
                task_type_summary="summary",
            )

    monkeypatch.setattr("cli.route_skill.SkillRouter", _FakeRouter)
    monkeypatch.setattr(
        "cli.route_skill.run_evolve",
        lambda **kwargs: {"status": "ok", "embedding_refresh_summary": {"ok": True}},  # noqa: ARG005
    )

    out = run_route(
        anchor_trajectory_id="traj-anchor",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        skill_top_k=1,
        include_anchor=True,
        merge_batch_size=8,
        max_parallel_analysts=4,
        confidence_threshold=0.7,
        force_mode="auto",
        dry_run=True,
        config_path=None,
    )
    assert out["decision"]["decision"] == "update"
    assert out["updated_skill_name"] == "skill_creator"
    assert out["created_skill_name"] is None


def test_run_route_auto_create_branch(monkeypatch) -> None:
    monkeypatch.setattr("cli.route_skill.load_settings", lambda config_path=None: _fake_settings())
    monkeypatch.setattr("cli.route_skill._build_retrieve_service", lambda settings: (object(), []))
    monkeypatch.setattr("cli.route_skill.LocalFSTrajectoryRepository", lambda root: object())

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            return _fake_pool_result()

    monkeypatch.setattr("cli.route_skill.TrajectoryPoolBuilder", _FakePoolBuilder)
    monkeypatch.setattr("cli.route_skill._build_skill_retriever", lambda settings: (None, []))

    class _FakeRouter:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def summarize_task_type(self, pool):  # noqa: ANN001
            return "summary"

        def decide(self, *, candidate_skill, pool):  # noqa: ANN001
            return SimpleNamespace(
                decision="create",
                confidence=0.95,
                scope_match_level="low",
                reasoning="no match",
                task_type_summary="summary",
            )

    monkeypatch.setattr("cli.route_skill.SkillRouter", _FakeRouter)
    monkeypatch.setattr("cli.route_skill.load_skills", lambda root: ([], []))
    monkeypatch.setattr(
        "cli.route_skill.create_skill_from_trajectories",
        lambda **kwargs: SimpleNamespace(  # noqa: ARG005
            skill_name="skill_auto_test",
            skill_md_path="data/skill/skill_auto_test/SKILL.md",
            created=False,
            description="desc",
        ),
    )

    out = run_route(
        anchor_trajectory_id="traj-anchor",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        skill_top_k=1,
        include_anchor=True,
        merge_batch_size=8,
        max_parallel_analysts=4,
        confidence_threshold=0.7,
        force_mode="auto",
        dry_run=True,
        config_path=None,
    )
    assert out["decision"]["decision"] == "create"
    assert out["updated_skill_name"] is None
    assert out["created_skill_name"] == "skill_auto_test"

