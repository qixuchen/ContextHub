from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.route_skill import build_parser, run_route, run_route_with_candidates
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
        retrieved_trajectory_ids=["traj-1", "traj-2", "traj-3", "traj-4"],
        retrieved_trajectory_scores=[
            {"trajectory_id": "traj-1", "total_score": 0.91},
            {"trajectory_id": "traj-2", "total_score": 0.88},
            {"trajectory_id": "traj-3", "total_score": 0.84},
            {"trajectory_id": "traj-4", "total_score": 0.81},
        ],
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
    assert args.trajectory_min_score == 0.7
    assert args.support == 4
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
                suggested_skill_name="spreadsheet_formula_repair",
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
        trajectory_min_score=0.7,
        support=4,
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
    assert out["decision"]["suggested_skill_name"] == "spreadsheet_formula_repair"
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
                suggested_skill_name="spreadsheet_formula_repair",
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
        trajectory_min_score=0.7,
        support=4,
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
    assert out["decision"]["suggested_skill_name"] == "spreadsheet_formula_repair"
    assert out["updated_skill_name"] is None
    assert out["created_skill_name"] == "skill_auto_test"


def test_run_route_skips_when_support_insufficient(monkeypatch) -> None:
    monkeypatch.setattr("cli.route_skill.load_settings", lambda config_path=None: _fake_settings())
    monkeypatch.setattr("cli.route_skill._build_retrieve_service", lambda settings: (object(), []))
    monkeypatch.setattr("cli.route_skill.LocalFSTrajectoryRepository", lambda root: object())

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            out = _fake_pool_result()
            out.retrieved_trajectory_ids = ["traj-1", "traj-2", "traj-3"]
            out.retrieved_trajectory_scores = [
                {"trajectory_id": "traj-1", "total_score": 0.91},
                {"trajectory_id": "traj-2", "total_score": 0.88},
                {"trajectory_id": "traj-3", "total_score": 0.84},
            ]
            return out

    monkeypatch.setattr("cli.route_skill.TrajectoryPoolBuilder", _FakePoolBuilder)

    out = run_route(
        anchor_trajectory_id="traj-anchor",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        support=4,
        skill_top_k=1,
        include_anchor=True,
        merge_batch_size=8,
        max_parallel_analysts=4,
        confidence_threshold=0.7,
        force_mode="auto",
        dry_run=True,
        config_path=None,
    )

    assert out["decision"]["decision"] == "support_insufficient"
    assert out["branch_result"]["status"] == "skipped"
    assert out["updated_skill_name"] is None
    assert out["created_skill_name"] is None
    assert out["trajectory_pool"]["qualified_support_count"] == 3
    assert out["trajectory_pool"]["required_support"] == 4


def test_run_route_support_insufficient_short_circuits_router_and_skill_retriever(monkeypatch) -> None:
    monkeypatch.setattr("cli.route_skill.load_settings", lambda config_path=None: _fake_settings())
    monkeypatch.setattr("cli.route_skill._build_retrieve_service", lambda settings: (object(), []))
    monkeypatch.setattr("cli.route_skill.LocalFSTrajectoryRepository", lambda root: object())

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            out = _fake_pool_result()
            out.retrieved_trajectory_ids = ["traj-1", "traj-2", "traj-3"]
            out.retrieved_trajectory_scores = [
                {"trajectory_id": "traj-1", "total_score": 0.91},
                {"trajectory_id": "traj-2", "total_score": 0.88},
                {"trajectory_id": "traj-3", "total_score": 0.84},
            ]
            return out

    monkeypatch.setattr("cli.route_skill.TrajectoryPoolBuilder", _FakePoolBuilder)
    monkeypatch.setattr(
        "cli.route_skill._build_skill_retriever",
        lambda settings: (_ for _ in ()).throw(AssertionError("skill retriever should not run")),
    )
    monkeypatch.setattr(
        "cli.route_skill.SkillRouter",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("router should not run")),
    )

    out = run_route(
        anchor_trajectory_id="traj-anchor",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        support=4,
        skill_top_k=1,
        include_anchor=True,
        merge_batch_size=8,
        max_parallel_analysts=4,
        confidence_threshold=0.7,
        force_mode="auto",
        dry_run=True,
        config_path=None,
    )
    assert out["decision"]["decision"] == "support_insufficient"


def test_run_route_create_branch_refreshes_embedding_when_not_dry_run(monkeypatch) -> None:
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
                suggested_skill_name="spreadsheet_formula_repair",
            )

    monkeypatch.setattr("cli.route_skill.SkillRouter", _FakeRouter)
    monkeypatch.setattr("cli.route_skill.load_skills", lambda root: ([], []))
    monkeypatch.setattr(
        "cli.route_skill.create_skill_from_trajectories",
        lambda **kwargs: SimpleNamespace(  # noqa: ARG005
            skill_name="skill_auto_test",
            skill_md_path="data/skill/skill_auto_test/SKILL.md",
            created=True,
            description="desc",
        ),
    )
    monkeypatch.setattr(
        "cli.route_skill._refresh_skill_embedding",
        lambda **kwargs: {"refreshed": True},  # noqa: ARG005
    )

    out = run_route(
        anchor_trajectory_id="traj-anchor",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        support=4,
        skill_top_k=1,
        include_anchor=True,
        merge_batch_size=8,
        max_parallel_analysts=4,
        confidence_threshold=0.7,
        force_mode="auto",
        dry_run=False,
        config_path=None,
    )
    assert out["decision"]["decision"] == "create"
    assert out["embedding_refresh_summary"] == {"refreshed": True}


def test_run_route_with_candidates_uses_direct_pool(monkeypatch) -> None:
    monkeypatch.setattr("cli.route_skill.load_settings", lambda config_path=None: _fake_settings())

    class _FakeRepo:
        def __init__(self, root):  # noqa: ANN001
            del root

        def load_trajectory(self, trajectory_id):  # noqa: ANN001
            return {
                "meta": {"trajectory_id": trajectory_id, "task_id": f"task-{trajectory_id}"},
                "abstract": f"abstract-{trajectory_id}",
                "overview": f"overview-{trajectory_id}",
                "trajectory": [],
            }

    monkeypatch.setattr("cli.route_skill.LocalFSTrajectoryRepository", _FakeRepo)
    monkeypatch.setattr("cli.route_skill._build_skill_retriever", lambda settings: (None, []))

    class _FakeRouter:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def summarize_task_type(self, pool):  # noqa: ANN001
            return "summary"

        def decide(self, *, candidate_skill, pool):  # noqa: ANN001
            del candidate_skill, pool
            return SimpleNamespace(
                decision="create",
                confidence=0.95,
                scope_match_level="low",
                reasoning="no match",
                task_type_summary="summary",
                suggested_skill_name="skill_name",
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

    out = run_route_with_candidates(
        anchor_trajectory_id="traj-a",
        candidate_trajectory_ids=["traj-a", "traj-b", "traj-c"],
        candidate_trajectory_scores={
            "traj-b": {"total_score": 0.9, "semantic_score": 0.8, "graph_match_score": 1.0},
            "traj-c": {"total_score": 0.85, "semantic_score": 0.75, "graph_match_score": 0.95},
        },
        account_id="acc",
        agent_id="agent",
        support=2,
        dry_run=True,
    )
    assert out["decision"]["decision"] == "create"
    assert out["trajectory_pool"]["anchor"] == "traj-a"
    assert out["trajectory_pool"]["neighbor_count"] == 2
    assert out["trajectory_pool"]["trajectory_min_score"] is None

