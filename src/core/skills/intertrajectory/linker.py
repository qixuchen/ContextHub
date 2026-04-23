"""Build inter-trajectory graph links from newly committed trajectories."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


class TrajectoryPoolBuilderProtocol(Protocol):
    def build_success_pool(
        self,
        *,
        account_id: str,
        agent_id: str,
        anchor_trajectory_id: str,
        top_k: int,
        trajectory_min_score: float = 0.7,
        include_anchor: bool = True,
    ) -> Any: ...


class InterTrajectoryStateStoreProtocol(Protocol):
    def upsert_link_and_pending(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        src_trajectory_id: str,
        dst_trajectory_id: str,
        score_total: float,
        score_semantic: float | None,
        score_graph: float | None,
    ) -> dict[str, Any]: ...


@dataclass
class InterTrajectoryLinker:
    """Link a committed trajectory to existing similar trajectories."""

    pool_builder: TrajectoryPoolBuilderProtocol
    state_store: InterTrajectoryStateStoreProtocol
    edge_threshold: float = 0.7
    max_neighbors_per_commit: int = 32

    def on_commit(
        self,
        *,
        account_id: str,
        agent_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> dict[str, Any]:
        edge_threshold = float(self.edge_threshold)
        top_k = max(1, int(self.max_neighbors_per_commit))
        pool_result = self.pool_builder.build_success_pool(
            account_id=account_id,
            agent_id=agent_id,
            anchor_trajectory_id=trajectory_id,
            top_k=top_k,
            trajectory_min_score=edge_threshold,
            include_anchor=False,
        )

        links: list[dict[str, Any]] = []
        for score_item in list(pool_result.retrieved_trajectory_scores or []):
            neighbor_id = _safe_text(score_item.get("trajectory_id"))
            if not neighbor_id or neighbor_id == trajectory_id:
                continue
            score_total = _safe_float(score_item.get("total_score"))
            if score_total is None or score_total < edge_threshold:
                continue
            row = self.state_store.upsert_link_and_pending(
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                src_trajectory_id=trajectory_id,
                dst_trajectory_id=neighbor_id,
                score_total=score_total,
                score_semantic=_safe_float(score_item.get("semantic_score")),
                score_graph=_safe_float(score_item.get("graph_match_score")),
            )
            links.append(
                {
                    "src_trajectory_id": trajectory_id,
                    "dst_trajectory_id": neighbor_id,
                    "score_total": score_total,
                    "score_semantic": _safe_float(score_item.get("semantic_score")),
                    "score_graph": _safe_float(score_item.get("graph_match_score")),
                    "edge_rel_type": _safe_text(row.get("edge_rel_type")),
                    "pending_rel_type": _safe_text(row.get("pending_rel_type")),
                }
            )

        return {
            "enabled": True,
            "trajectory_id": trajectory_id,
            "edge_threshold": edge_threshold,
            "max_neighbors_per_commit": top_k,
            "retrieved_candidate_count": len(pool_result.retrieved_trajectory_scores or []),
            "linked_count": len(links),
            "links": links,
            "warnings": list(pool_result.warnings or []),
        }
