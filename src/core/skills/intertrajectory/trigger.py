"""Commit-time trigger for automatic skill route execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

TerminalDecision = {"update", "create", "support_insufficient"}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


class InterTrajectoryStateStoreProtocol(Protocol):
    def list_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        limit: int = 512,
    ) -> list[dict[str, Any]]: ...

    def clear_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> int: ...

    def list_related_anchors_for_seeds(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
        limit: int = 2048,
    ) -> list[str]: ...


@dataclass
class InterTrajectoryTrigger:
    """Trigger route with activate candidates once pending reaches threshold."""

    state_store: InterTrajectoryStateStoreProtocol
    route_runner: Callable[..., dict[str, Any]]
    trigger_threshold: int = 8
    pending_limit: int = 512
    route_support: int = 4

    def expand_batch_anchor_candidates(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
    ) -> list[str]:
        seeds = list(dict.fromkeys(_safe_text(x) for x in trajectory_ids if _safe_text(x)))
        if not seeds:
            return []
        expand_fn = getattr(self.state_store, "list_related_anchors_for_seeds", None)
        if not callable(expand_fn):
            return seeds
        limit = max(int(self.pending_limit), len(seeds) * 16, 256)
        related = expand_fn(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_ids=seeds,
            limit=limit,
        )
        return list(dict.fromkeys([*seeds, *[_safe_text(x) for x in related if _safe_text(x)]]))

    def on_commit(
        self,
        *,
        account_id: str,
        agent_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> dict[str, Any]:
        pending = self.state_store.list_pending_neighbors(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
            limit=max(1, int(self.pending_limit)),
        )
        trigger_threshold = max(1, int(self.trigger_threshold))
        pending_count = len(pending)
        if pending_count < trigger_threshold:
            return {
                "enabled": True,
                "triggered": False,
                "trajectory_id": trajectory_id,
                "pending_count": pending_count,
                "trigger_threshold": trigger_threshold,
                "reason": "below_trigger_threshold",
            }

        candidate_ids = [trajectory_id]
        candidate_scores: dict[str, dict[str, Any]] = {}
        for row in pending:
            tid = _safe_text(row.get("trajectory_id"))
            if not tid or tid == trajectory_id:
                continue
            candidate_ids.append(tid)
            candidate_scores[tid] = {
                "total_score": row.get("score_total"),
                "semantic_score": row.get("score_semantic"),
                "graph_match_score": row.get("score_graph"),
            }
        dedup_ids = list(dict.fromkeys(candidate_ids))
        if len(dedup_ids) <= 1:
            return {
                "enabled": True,
                "triggered": False,
                "trajectory_id": trajectory_id,
                "pending_count": pending_count,
                "trigger_threshold": trigger_threshold,
                "reason": "no_valid_pending_candidates",
            }

        route_out = self.route_runner(
            anchor_trajectory_id=trajectory_id,
            candidate_trajectory_ids=dedup_ids,
            candidate_trajectory_scores=candidate_scores,
            account_id=account_id,
            agent_id=agent_id,
            support=max(1, int(self.route_support)),
        )
        decision = _safe_text(((route_out or {}).get("decision") or {}).get("decision")).lower()
        cleared_count = 0
        cleared = False
        if decision in TerminalDecision:
            cleared_count = self.state_store.clear_pending_neighbors(
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
            )
            cleared = True
        return {
            "enabled": True,
            "triggered": True,
            "trajectory_id": trajectory_id,
            "pending_count": pending_count,
            "trigger_threshold": trigger_threshold,
            "decision": decision,
            "pending_cleared": cleared,
            "cleared_count": cleared_count,
            "route_result": route_out,
        }
