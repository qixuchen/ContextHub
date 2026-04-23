from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.skills.intertrajectory.linker import InterTrajectoryLinker

pytestmark = pytest.mark.unit


def test_intertrajectory_linker_writes_links_from_retrieved_scores() -> None:
    class _FakePoolBuilder:
        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            return SimpleNamespace(
                retrieved_trajectory_scores=[
                    {
                        "trajectory_id": "traj-b",
                        "total_score": 0.82,
                        "semantic_score": 0.71,
                        "graph_match_score": 0.91,
                    },
                    {
                        "trajectory_id": "traj-c",
                        "total_score": 0.65,
                        "semantic_score": 0.66,
                        "graph_match_score": 0.64,
                    },
                ],
                warnings=["w1"],
            )

    calls: list[dict] = []

    class _FakeStateStore:
        def upsert_link_and_pending(self, **kwargs):  # noqa: ANN001
            calls.append(dict(kwargs))
            return {
                "edge_rel_type": "INTERTRAJ_SIMILAR",
                "pending_rel_type": "INTERTRAJ_ACTIVATED_PENDING",
            }

    linker = InterTrajectoryLinker(
        pool_builder=_FakePoolBuilder(),
        state_store=_FakeStateStore(),
        edge_threshold=0.7,
        max_neighbors_per_commit=16,
    )
    out = linker.on_commit(
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space="agent",
        trajectory_id="traj-a",
    )

    assert out["enabled"] is True
    assert out["linked_count"] == 1
    assert out["retrieved_candidate_count"] == 2
    assert calls[0]["src_trajectory_id"] == "traj-a"
    assert calls[0]["dst_trajectory_id"] == "traj-b"
    assert calls[0]["score_total"] == 0.82
    assert out["warnings"] == ["w1"]
