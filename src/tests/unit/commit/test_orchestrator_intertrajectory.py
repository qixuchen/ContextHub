from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.orchestrators.commit_orchestrator import CommitOrchestrator
from core.commit.service import CommitCommand, CommitResult

pytestmark = pytest.mark.unit


class _FakeCommitService:
    def run(self, cmd: CommitCommand) -> CommitResult:
        return CommitResult(
            trajectory_id=f"traj-{cmd.task_id}",
            idempotency_key=f"idem-{cmd.task_id}",
            status="accepted",
            nodes=1,
            edges=0,
            warnings=[],
            summary_l0="l0",
            summary_l1="l1",
            payload={"raw_graph": {"nodes": [], "edges": []}, "clean_graph": {"nodes": [], "edges": []}},
        )


class _FakeRepo:
    def __init__(self) -> None:
        self._id_map: dict[str, str] = {}

    def find_trajectory_id_by_idempotency_key(self, key: str) -> str | None:
        return self._id_map.get(key)

    def save_bundle(self, **kwargs: Any) -> str:
        trajectory_id = str(kwargs["trajectory_id"])
        idempotency_key = str(kwargs["idempotency_key"])
        self._id_map[idempotency_key] = trajectory_id
        return str(Path("/tmp/amc-test"))

    def load_trajectory(self, trajectory_id: str) -> dict[str, Any] | None:
        return {"meta": {"nodes": 1, "edges": 0}, "base_path": "/tmp/amc-test"}


class _FakeAudit:
    def write(self, **kwargs: Any) -> None:
        del kwargs


def _command(task_id: str) -> CommitCommand:
    return CommitCommand(
        agent_id="agent-test",
        account_id="account-test",
        scope="agent",
        owner_space="agent-test",
        session_id="session-test",
        task_id=task_id,
        trajectory=[{"Step": 1, "meta": {"role": "AIMessage"}}],
        labels={},
        is_incremental=False,
    )


def test_orchestrator_calls_intertrajectory_linker_on_accepted_commit() -> None:
    calls: list[dict[str, Any]] = []
    trigger_calls: list[dict[str, Any]] = []

    class _FakeLinker:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {"enabled": True, "linked_count": 2}

    class _FakeTrigger:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            trigger_calls.append(dict(kwargs))
            return {"enabled": True, "triggered": False, "reason": "below_trigger_threshold"}

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=_FakeLinker(),
        intertrajectory_trigger=_FakeTrigger(),
        idempotency_enabled=False,
    )
    out = orchestrator.commit(_command("task-a"))
    assert calls and calls[0]["trajectory_id"] == "traj-task-a"
    assert trigger_calls and trigger_calls[0]["trajectory_id"] == "traj-task-a"
    assert out.payload["intertrajectory_summary"]["enabled"] is True
    assert out.payload["intertrajectory_trigger_summary"]["enabled"] is True


def test_orchestrator_idempotent_commit_skips_intertrajectory_linker() -> None:
    calls: list[dict[str, Any]] = []
    trigger_calls: list[dict[str, Any]] = []

    class _FakeLinker:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {"enabled": True, "linked_count": 2}

    class _FakeTrigger:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            trigger_calls.append(dict(kwargs))
            return {"enabled": True, "triggered": True}

    repo = _FakeRepo()
    existing_task = "task-b"
    existing_trajectory = f"traj-{existing_task}"
    repo._id_map[f"idem-{existing_task}"] = existing_trajectory

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=repo,
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=_FakeLinker(),
        intertrajectory_trigger=_FakeTrigger(),
        idempotency_enabled=True,
    )
    out = orchestrator.commit(_command(existing_task))
    assert out.status == "idempotent"
    assert calls == []
    assert trigger_calls == []
    assert out.payload["intertrajectory_summary"]["reason"] == "idempotent_commit"
    assert out.payload["intertrajectory_trigger_summary"]["reason"] == "idempotent_commit"


def test_orchestrator_commit_prepared_disables_trigger_for_batch_flow() -> None:
    calls: list[dict[str, Any]] = []
    trigger_calls: list[dict[str, Any]] = []

    class _FakeLinker:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return {"enabled": True, "linked_count": 1}

    class _FakeTrigger:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            trigger_calls.append(dict(kwargs))
            return {"enabled": True, "triggered": True}

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=_FakeLinker(),
        intertrajectory_trigger=_FakeTrigger(),
        idempotency_enabled=False,
    )
    prepared = orchestrator.prepare_commit(_command("task-c"))
    out = orchestrator.commit_prepared(_command("task-c"), prepared)
    assert calls and calls[0]["trajectory_id"] == "traj-task-c"
    assert trigger_calls == []
    assert out.payload["intertrajectory_trigger_summary"]["reason"] == "disabled_for_batch_or_manual_persist"


def test_orchestrator_batch_trigger_runs_at_end_of_batch() -> None:
    trigger_calls: list[dict[str, Any]] = []

    class _FakeTrigger:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            trigger_calls.append(dict(kwargs))
            return {"enabled": True, "triggered": kwargs["trajectory_id"] == "traj-a"}

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=None,
        intertrajectory_trigger=_FakeTrigger(),
        intertrajectory_batch_trigger_mode="end_of_batch",
        idempotency_enabled=False,
    )
    out = orchestrator.trigger_intertrajectory_batch(
        account_id="account-test",
        agent_id="agent-test",
        scope="agent",
        owner_space="agent-test",
        trajectory_ids=["traj-a", "traj-b", "traj-a"],
    )
    assert out["enabled"] is True
    assert out["mode"] == "end_of_batch"
    assert out["evaluated_count"] == 2
    assert out["triggered_count"] == 1
    assert [x["trajectory_id"] for x in trigger_calls] == ["traj-a", "traj-b"]


def test_orchestrator_batch_trigger_respects_mode() -> None:
    class _FakeTrigger:
        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("should not run when mode is not end_of_batch")

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=None,
        intertrajectory_trigger=_FakeTrigger(),
        intertrajectory_batch_trigger_mode="disabled",
        idempotency_enabled=False,
    )
    out = orchestrator.trigger_intertrajectory_batch(
        account_id="account-test",
        agent_id="agent-test",
        scope="agent",
        owner_space="agent-test",
        trajectory_ids=["traj-a"],
    )
    assert out["enabled"] is False
    assert "batch_trigger_mode=disabled" in out["reason"]


def test_orchestrator_batch_trigger_expands_old_anchors() -> None:
    trigger_calls: list[str] = []

    class _FakeTrigger:
        def expand_batch_anchor_candidates(self, **kwargs: Any) -> list[str]:  # noqa: ANN401
            seeds = list(kwargs["trajectory_ids"])
            return [*seeds, "traj-old"]

        def on_commit(self, **kwargs: Any) -> dict[str, Any]:
            trigger_calls.append(str(kwargs["trajectory_id"]))
            return {"enabled": True, "triggered": kwargs["trajectory_id"] == "traj-old"}

    orchestrator = CommitOrchestrator(
        commit_service=_FakeCommitService(),
        repo=_FakeRepo(),
        audit=_FakeAudit(),
        graph_store=None,
        vector_indexer=None,
        intertrajectory_linker=None,
        intertrajectory_trigger=_FakeTrigger(),
        intertrajectory_batch_trigger_mode="end_of_batch",
        idempotency_enabled=False,
    )
    out = orchestrator.trigger_intertrajectory_batch(
        account_id="account-test",
        agent_id="agent-test",
        scope="agent",
        owner_space="agent-test",
        trajectory_ids=["traj-a", "traj-b"],
    )
    assert out["enabled"] is True
    assert out["seed_count"] == 2
    assert out["evaluated_count"] == 3
    assert out["triggered_count"] == 1
    assert trigger_calls == ["traj-a", "traj-b", "traj-old"]
