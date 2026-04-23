"""Commit orchestrator: ACL/audit-friendly boundary for API layer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from core.commit.service import CommitCommand, CommitResult, CommitService
from core.indexing.base import TrajectoryIndexer
from core.skills.intertrajectory.linker import InterTrajectoryLinker
from core.skills.intertrajectory.trigger import InterTrajectoryTrigger
from infra.audit.audit_logger import JsonlAuditLogger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.base import GraphStoreWriter


@dataclass
class PreparedCommitOutcome:
    command: CommitCommand
    result: CommitResult | None
    error: Exception | None = None


class CommitOrchestrator:
    def __init__(
        self,
        *,
        commit_service: CommitService,
        repo: LocalFSTrajectoryRepository,
        audit: JsonlAuditLogger,
        graph_store: GraphStoreWriter | None = None,
        vector_indexer: TrajectoryIndexer | None = None,
        intertrajectory_linker: InterTrajectoryLinker | None = None,
        intertrajectory_trigger: InterTrajectoryTrigger | None = None,
        intertrajectory_batch_trigger_mode: str = "end_of_batch",
        idempotency_enabled: bool = False,
    ) -> None:
        self.commit_service = commit_service
        self.repo = repo
        self.audit = audit
        self.graph_store = graph_store
        self.vector_indexer = vector_indexer
        self.intertrajectory_linker = intertrajectory_linker
        self.intertrajectory_trigger = intertrajectory_trigger
        self.intertrajectory_batch_trigger_mode = str(intertrajectory_batch_trigger_mode or "end_of_batch")
        self.idempotency_enabled = idempotency_enabled

    def _persist_prepared_commit(
        self,
        command: CommitCommand,
        result: CommitResult,
        *,
        enable_intertrajectory_trigger: bool,
    ) -> CommitResult:
        # Persist prepared result into graph/fs/vector backends.
        account_id = command.resolved_account_id()
        scope = command.resolved_scope()
        owner_space = command.resolved_owner_space()
        existing_id = (
            self.repo.find_trajectory_id_by_idempotency_key(result.idempotency_key)
            if self.idempotency_enabled
            else None
        )
        if existing_id:
            # Duplicate commit: return stable result without rewriting storage.
            bundle = self.repo.load_trajectory(existing_id)
            nodes = int((bundle or {}).get("meta", {}).get("nodes", 0))
            edges = int((bundle or {}).get("meta", {}).get("edges", 0))
            vector_summary: dict[str, Any] = {"enabled": False}
            if self.vector_indexer is not None and bundle and bundle.get("base_path"):
                try:
                    vector_summary = self.vector_indexer.index_trajectory(
                        agent_id=command.agent_id,
                        account_id=account_id,
                        scope=scope,
                        owner_space=owner_space,
                        trajectory_id=existing_id,
                        base_path=str(bundle["base_path"]),
                        lifecycle_status="active",
                        stale_flag=False,
                    )
                except Exception as exc:
                    vector_summary = {
                        "enabled": True,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
            out = CommitResult(
                trajectory_id=existing_id,
                idempotency_key=result.idempotency_key,
                status="idempotent",
                nodes=nodes,
                edges=edges,
                warnings=["duplicate commit skipped via idempotency key"],
                summary_l0=(bundle or {}).get("abstract", result.summary_l0),
                summary_l1=(bundle or {}).get("overview", result.summary_l1),
                payload=result.payload,
            )
            out.payload["vector_index_summary"] = vector_summary
            out.payload["intertrajectory_summary"] = {
                "enabled": False,
                "reason": "idempotent_commit",
            }
            out.payload["intertrajectory_trigger_summary"] = {
                "enabled": False,
                "reason": "idempotent_commit",
            }
            self.audit.write(
                action="commit",
                result="idempotent",
                details={
                    "agent_id": command.agent_id,
                    "account_id": account_id,
                    "scope": scope,
                    "owner_space": owner_space,
                    "task_id": command.task_id,
                    "trajectory_id": existing_id,
                },
            )
            return out

        # First-seen commit: persist trajectory + raw/clean graph artifacts.
        neo4j_summary: dict[str, Any] | None = None
        if self.graph_store is not None:
            neo4j_summary = self.graph_store.upsert_trajectory_graphs(
                agent_id=command.agent_id,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=result.trajectory_id,
                raw_graph=result.payload["raw_graph"],
                clean_graph=result.payload["clean_graph"],
            )
        result.payload["neo4j_summary"] = neo4j_summary or {"enabled": False}
        base_path = self.repo.save_bundle(
            agent_id=command.agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=result.trajectory_id,
            idempotency_key=result.idempotency_key,
            payload=result.payload,
            visualize_graph_png=command.visualize_graph_png,
        )
        vector_summary: dict[str, Any] = {"enabled": False}
        if self.vector_indexer is not None:
            try:
                vector_summary = self.vector_indexer.index_trajectory(
                    agent_id=command.agent_id,
                    account_id=account_id,
                    scope=scope,
                    owner_space=owner_space,
                    trajectory_id=result.trajectory_id,
                    base_path=base_path,
                    lifecycle_status="active",
                    stale_flag=False,
                )
            except Exception as exc:
                result.warnings.append(f"vector indexing skipped: {type(exc).__name__}")
                vector_summary = {"enabled": True, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        result.payload["vector_index_summary"] = vector_summary
        intertraj_summary: dict[str, Any] = {"enabled": False}
        if self.intertrajectory_linker is not None:
            try:
                intertraj_summary = self.intertrajectory_linker.on_commit(
                    account_id=account_id,
                    agent_id=command.agent_id,
                    scope=scope,
                    owner_space=owner_space,
                    trajectory_id=result.trajectory_id,
                )
            except Exception as exc:
                result.warnings.append(f"intertrajectory linking skipped: {type(exc).__name__}")
                intertraj_summary = {
                    "enabled": True,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        result.payload["intertrajectory_summary"] = intertraj_summary
        intertraj_trigger_summary: dict[str, Any] = {"enabled": False}
        if enable_intertrajectory_trigger:
            if self.intertrajectory_trigger is None:
                intertraj_trigger_summary = {
                    "enabled": False,
                    "reason": "trigger_not_configured",
                }
            else:
                try:
                    intertraj_trigger_summary = self.intertrajectory_trigger.on_commit(
                        account_id=account_id,
                        agent_id=command.agent_id,
                        scope=scope,
                        owner_space=owner_space,
                        trajectory_id=result.trajectory_id,
                    )
                except Exception as exc:
                    result.warnings.append(f"intertrajectory trigger skipped: {type(exc).__name__}")
                    intertraj_trigger_summary = {
                        "enabled": True,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        else:
            intertraj_trigger_summary = {
                "enabled": False,
                "reason": "disabled_for_batch_or_manual_persist",
            }
        result.payload["intertrajectory_trigger_summary"] = intertraj_trigger_summary
        self.audit.write(
            action="commit",
            result="accepted" if self.idempotency_enabled else "accepted_idempotency_disabled",
            details={
                "agent_id": command.agent_id,
                "account_id": account_id,
                "scope": scope,
                "owner_space": owner_space,
                "task_id": command.task_id,
                "trajectory_id": result.trajectory_id,
                "nodes": result.nodes,
                "edges": result.edges,
                "idempotency_enabled": self.idempotency_enabled,
            },
        )
        return result

    def prepare_commit(self, command: CommitCommand) -> CommitResult:
        # Service computes graph + summaries and returns deterministic idempotency key.
        return self.commit_service.run(command)

    def prepare_commits(
        self, commands: list[CommitCommand], *, max_workers: int = 1
    ) -> list[PreparedCommitOutcome]:
        if not commands:
            return []
        workers = max(1, int(max_workers))
        if workers == 1:
            outcomes: list[PreparedCommitOutcome] = []
            for command in commands:
                try:
                    outcomes.append(
                        PreparedCommitOutcome(
                            command=command,
                            result=self.prepare_commit(command),
                            error=None,
                        )
                    )
                except Exception as exc:
                    outcomes.append(
                        PreparedCommitOutcome(command=command, result=None, error=exc)
                    )
            return outcomes

        outcomes_by_idx: dict[int, PreparedCommitOutcome] = {}
        with ThreadPoolExecutor(max_workers=min(workers, len(commands))) as pool:
            future_to_idx = {
                pool.submit(self.prepare_commit, command): idx for idx, command in enumerate(commands)
            }
            for fut in as_completed(future_to_idx):
                idx = future_to_idx[fut]
                command = commands[idx]
                try:
                    outcomes_by_idx[idx] = PreparedCommitOutcome(
                        command=command,
                        result=fut.result(),
                        error=None,
                    )
                except Exception as exc:
                    outcomes_by_idx[idx] = PreparedCommitOutcome(
                        command=command, result=None, error=exc
                    )
        return [outcomes_by_idx[i] for i in range(len(commands))]

    def commit(self, command: CommitCommand) -> CommitResult:
        prepared = self.prepare_commit(command)
        return self._persist_prepared_commit(
            command,
            prepared,
            enable_intertrajectory_trigger=True,
        )

    def commit_prepared(self, command: CommitCommand, result: CommitResult) -> CommitResult:
        return self._persist_prepared_commit(
            command,
            result,
            enable_intertrajectory_trigger=False,
        )

    def replay(self, trajectory_id: str) -> dict[str, Any] | None:
        # Thin pass-through for API replay endpoint.
        return self.repo.load_trajectory(trajectory_id)

    def trigger_intertrajectory_batch(
        self,
        *,
        account_id: str,
        agent_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
    ) -> dict[str, Any]:
        dedup_ids = list(dict.fromkeys(str(x).strip() for x in trajectory_ids if str(x).strip()))
        if not dedup_ids:
            return {
                "enabled": False,
                "reason": "no_accepted_trajectories",
                "triggered_count": 0,
                "evaluated_count": 0,
                "items": [],
            }
        mode = self.intertrajectory_batch_trigger_mode.strip().lower()
        if mode != "end_of_batch":
            return {
                "enabled": False,
                "reason": f"batch_trigger_mode={mode}",
                "triggered_count": 0,
                "evaluated_count": len(dedup_ids),
                "items": [],
            }
        if self.intertrajectory_trigger is None:
            return {
                "enabled": False,
                "reason": "trigger_not_configured",
                "triggered_count": 0,
                "evaluated_count": len(dedup_ids),
                "items": [],
            }

        expanded_ids = list(dedup_ids)
        expand_fn = getattr(self.intertrajectory_trigger, "expand_batch_anchor_candidates", None)
        if callable(expand_fn):
            try:
                expanded_ids = list(
                    dict.fromkeys(
                        str(x).strip()
                        for x in expand_fn(
                            account_id=account_id,
                            scope=scope,
                            owner_space=owner_space,
                            trajectory_ids=dedup_ids,
                        )
                        if str(x).strip()
                    )
                )
            except Exception:
                expanded_ids = list(dedup_ids)

        items: list[dict[str, Any]] = []
        triggered_count = 0
        for trajectory_id in expanded_ids:
            try:
                out = self.intertrajectory_trigger.on_commit(
                    account_id=account_id,
                    agent_id=agent_id,
                    scope=scope,
                    owner_space=owner_space,
                    trajectory_id=trajectory_id,
                )
                if bool(out.get("triggered")):
                    triggered_count += 1
                items.append({"trajectory_id": trajectory_id, **dict(out or {})})
            except Exception as exc:
                items.append(
                    {
                        "trajectory_id": trajectory_id,
                        "enabled": True,
                        "triggered": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {
            "enabled": True,
            "mode": "end_of_batch",
            "seed_count": len(dedup_ids),
            "triggered_count": triggered_count,
            "evaluated_count": len(expanded_ids),
            "items": items,
        }
