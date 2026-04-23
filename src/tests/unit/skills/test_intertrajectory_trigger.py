from __future__ import annotations

import pytest

from core.skills.intertrajectory.trigger import InterTrajectoryTrigger

pytestmark = pytest.mark.unit


def test_intertrajectory_trigger_skips_when_below_threshold() -> None:
    class _FakeStateStore:
        def list_pending_neighbors(self, **kwargs):  # noqa: ANN001
            del kwargs
            return [{"trajectory_id": "traj-b"}]

        def clear_pending_neighbors(self, **kwargs):  # noqa: ANN001
            raise AssertionError("should not clear when below threshold")

    trigger = InterTrajectoryTrigger(
        state_store=_FakeStateStore(),
        route_runner=lambda **kwargs: {},  # noqa: ARG005
        trigger_threshold=2,
        route_support=4,
    )
    out = trigger.on_commit(
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space="agent",
        trajectory_id="traj-a",
    )
    assert out["triggered"] is False
    assert out["reason"] == "below_trigger_threshold"


def test_intertrajectory_trigger_runs_route_and_clears_on_terminal_decision() -> None:
    called: dict[str, object] = {}

    class _FakeStateStore:
        def list_pending_neighbors(self, **kwargs):  # noqa: ANN001
            del kwargs
            return [
                {"trajectory_id": "traj-b", "score_total": 0.81},
                {"trajectory_id": "traj-c", "score_total": 0.79},
            ]

        def clear_pending_neighbors(self, **kwargs):  # noqa: ANN001
            called["cleared"] = True
            return 2

    def _runner(**kwargs):  # noqa: ANN003
        called["runner_kwargs"] = kwargs
        return {"decision": {"decision": "create"}}

    trigger = InterTrajectoryTrigger(
        state_store=_FakeStateStore(),
        route_runner=_runner,
        trigger_threshold=2,
        route_support=4,
    )
    out = trigger.on_commit(
        account_id="acc",
        agent_id="agent",
        scope="agent",
        owner_space="agent",
        trajectory_id="traj-a",
    )
    assert out["triggered"] is True
    assert out["decision"] == "create"
    assert out["pending_cleared"] is True
    assert out["cleared_count"] == 2
    assert called.get("cleared") is True
    runner_kwargs = called["runner_kwargs"]
    assert runner_kwargs["anchor_trajectory_id"] == "traj-a"
    assert set(runner_kwargs["candidate_trajectory_ids"]) == {"traj-a", "traj-b", "traj-c"}


def test_intertrajectory_trigger_keeps_pending_on_route_failure() -> None:
    class _FakeStateStore:
        def list_pending_neighbors(self, **kwargs):  # noqa: ANN001
            del kwargs
            return [{"trajectory_id": "traj-b"}]

        def clear_pending_neighbors(self, **kwargs):  # noqa: ANN001
            raise AssertionError("should not clear when route fails")

    def _runner(**kwargs):  # noqa: ANN003
        del kwargs
        raise RuntimeError("route failed")

    trigger = InterTrajectoryTrigger(
        state_store=_FakeStateStore(),
        route_runner=_runner,
        trigger_threshold=1,
        route_support=4,
    )
    with pytest.raises(RuntimeError):
        trigger.on_commit(
            account_id="acc",
            agent_id="agent",
            scope="agent",
            owner_space="agent",
            trajectory_id="traj-a",
        )


def test_expand_batch_anchor_candidates_includes_related_old_nodes() -> None:
    class _FakeStateStore:
        def list_related_anchors_for_seeds(self, **kwargs):  # noqa: ANN001
            assert kwargs["trajectory_ids"] == ["traj-new"]
            return ["traj-old", "traj-new", "traj-old-2"]

    trigger = InterTrajectoryTrigger(
        state_store=_FakeStateStore(),
        route_runner=lambda **kwargs: {},  # noqa: ARG005
        trigger_threshold=8,
        route_support=4,
    )
    out = trigger.expand_batch_anchor_candidates(
        account_id="acc",
        scope="agent",
        owner_space="agent",
        trajectory_ids=["traj-new"],
    )
    assert out == ["traj-new", "traj-old", "traj-old-2"]


def test_expand_batch_anchor_candidates_falls_back_when_state_store_not_supported() -> None:
    class _FakeStateStore:
        pass

    trigger = InterTrajectoryTrigger(
        state_store=_FakeStateStore(),
        route_runner=lambda **kwargs: {},  # noqa: ARG005
        trigger_threshold=8,
        route_support=4,
    )
    out = trigger.expand_batch_anchor_candidates(
        account_id="acc",
        scope="agent",
        owner_space="agent",
        trajectory_ids=["traj-a", "traj-a", "traj-b"],
    )
    assert out == ["traj-a", "traj-b"]
