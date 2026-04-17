from __future__ import annotations

import pytest

from core.skills.evolve.trajectory_pool_builder import TrajectoryPoolBuilder

pytestmark = pytest.mark.unit


class _FakeRepo:
    def __init__(self, bundles: dict[str, dict]) -> None:
        self._bundles = bundles

    def load_trajectory(self, trajectory_id: str):  # noqa: ANN001
        return self._bundles.get(trajectory_id)


class _FakeRetrieveService:
    def run(self, command):  # noqa: ANN001
        assert command.top_k == 3
        return type(
            "_Result",
            (),
            {
                "items": [
                    {"trajectory_id": "anchor", "total_score": 0.99, "semantic_score": 0.91, "graph_match_score": 0.9},
                    {"trajectory_id": "n1", "total_score": 0.88, "semantic_score": 0.83, "graph_match_score": 0.7},
                    {"trajectory_id": "n2", "total_score": 0.55, "semantic_score": 0.72, "graph_match_score": 0.5},
                ],
                "warnings": ["w1"],
            },
        )()


def _bundle(tid: str, task_id: str) -> dict:
    return {
        "meta": {"trajectory_id": tid, "task_id": task_id},
        "abstract": f"abstract {tid}",
        "overview": f"overview {tid}",
        "trajectory": [{"Step": 1, "Action": "local_db_sql(...)"}],
    }


def test_build_success_pool_includes_anchor_and_neighbors() -> None:
    repo = _FakeRepo(
        bundles={
            "anchor": _bundle("anchor", "task-anchor"),
            "n1": _bundle("n1", "task-1"),
            "n2": _bundle("n2", "task-2"),
        }
    )
    builder = TrajectoryPoolBuilder(repo=repo, retrieve_service=_FakeRetrieveService())  # type: ignore[arg-type]
    out = builder.build_success_pool(
        account_id="acc",
        agent_id="agent",
        anchor_trajectory_id="anchor",
        top_k=3,
        trajectory_min_score=0.7,
        include_anchor=True,
    )
    assert out.anchor.trajectory_id == "anchor"
    assert [x.trajectory_id for x in out.neighbors] == ["n1"]
    assert [x.trajectory_id for x in out.pool] == ["anchor", "n1"]
    assert out.retrieved_trajectory_scores == [
        {"trajectory_id": "n1", "total_score": 0.88, "semantic_score": 0.83, "graph_match_score": 0.7},
    ]
    assert out.warnings and "w1" in out.warnings
    assert any("skipped by threshold" in w for w in out.warnings)
    assert out.query_payload.get("task_description")

