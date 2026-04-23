"""Neo4j-backed state store for inter-trajectory graph links."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Neo4jInterTrajectoryStateStore:
    """Thin adapter to persist inter-trajectory links via graph writer."""

    graph_writer: Any
    edge_rel_type: str = "INTERTRAJ_SIMILAR"
    pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING"

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
    ) -> dict[str, Any]:
        if not hasattr(self.graph_writer, "upsert_intertrajectory_relationship"):
            raise RuntimeError("graph writer does not support intertrajectory relationships")
        return self.graph_writer.upsert_intertrajectory_relationship(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            src_trajectory_id=src_trajectory_id,
            dst_trajectory_id=dst_trajectory_id,
            score_total=score_total,
            score_semantic=score_semantic,
            score_graph=score_graph,
            edge_rel_type=self.edge_rel_type,
            pending_rel_type=self.pending_rel_type,
        )

    def list_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        if not hasattr(self.graph_writer, "list_intertrajectory_pending_neighbors"):
            raise RuntimeError("graph writer does not support pending neighbor listing")
        return self.graph_writer.list_intertrajectory_pending_neighbors(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
            pending_rel_type=self.pending_rel_type,
            edge_rel_type=self.edge_rel_type,
            limit=limit,
        )

    def clear_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> int:
        if not hasattr(self.graph_writer, "clear_intertrajectory_pending_neighbors"):
            raise RuntimeError("graph writer does not support pending neighbor cleanup")
        return int(
            self.graph_writer.clear_intertrajectory_pending_neighbors(
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
                pending_rel_type=self.pending_rel_type,
            )
            or 0
        )

    def list_related_anchors_for_seeds(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
        limit: int = 2048,
    ) -> list[str]:
        if not hasattr(self.graph_writer, "list_intertrajectory_pending_related_anchors"):
            raise RuntimeError("graph writer does not support related anchor discovery")
        rows = self.graph_writer.list_intertrajectory_pending_related_anchors(
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_ids=[str(x).strip() for x in trajectory_ids if str(x).strip()],
            pending_rel_type=self.pending_rel_type,
            limit=max(1, int(limit)),
        )
        out: list[str] = []
        for row in rows:
            if isinstance(row, dict):
                tid = str(row.get("trajectory_id") or "").strip()
            else:
                tid = str(row or "").strip()
            if tid:
                out.append(tid)
        return list(dict.fromkeys(out))
