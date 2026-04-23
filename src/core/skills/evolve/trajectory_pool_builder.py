"""Build evolve trajectory pool from an anchor trajectory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.retrieve.service import RetrieveCommand, RetrieveService
from core.skills.evolve.types import TrajectoryContext
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _tool_name_from_action(action: str) -> str:
    raw = _safe_text(action)
    if not raw:
        return ""
    idx = raw.find("(")
    if idx <= 0:
        return raw
    return raw[:idx].strip()


def _build_anchor_query(bundle: dict[str, Any]) -> dict[str, Any]:
    meta = bundle.get("meta") if isinstance(bundle.get("meta"), dict) else {}
    task_id = _safe_text(meta.get("task_id"))
    abstract = _safe_text(bundle.get("abstract"))
    overview = _safe_text(bundle.get("overview"))
    trajectory = bundle.get("trajectory")
    steps = trajectory if isinstance(trajectory, list) else []
    partial = steps[: min(len(steps), 12)]

    tools: list[str] = []
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            continue
        name = _tool_name_from_action(_safe_text(step.get("Action")))
        if not name or name in seen:
            continue
        tools.append(name)
        seen.add(name)
        if len(tools) >= 8:
            break

    parts = [x for x in [task_id, abstract, overview] if x]
    task_description = " | ".join(parts) or f"trajectory {meta.get('trajectory_id') or ''}".strip()
    return {
        "task_description": task_description,
        "partial_trajectory": partial,
        "constraints": {"tool_whitelist": tools},
    }


def _bundle_to_context(bundle: dict[str, Any]) -> TrajectoryContext:
    meta = bundle.get("meta") if isinstance(bundle.get("meta"), dict) else {}
    trajectory = bundle.get("trajectory")
    steps = trajectory if isinstance(trajectory, list) else []
    return TrajectoryContext(
        trajectory_id=_safe_text(meta.get("trajectory_id")),
        task_id=_safe_text(meta.get("task_id")),
        abstract=_safe_text(bundle.get("abstract")),
        overview=_safe_text(bundle.get("overview")),
        trajectory=steps,
        outcome_label=_safe_text(meta.get("trajectory_outcome_label")) or None,
    )


@dataclass
class TrajectoryPoolBuildResult:
    anchor: TrajectoryContext
    neighbors: list[TrajectoryContext]
    pool: list[TrajectoryContext]
    retrieved_trajectory_ids: list[str]
    retrieved_trajectory_scores: list[dict[str, Any]]
    query_payload: dict[str, Any]
    warnings: list[str]


@dataclass
class TrajectoryPoolBuilder:
    repo: LocalFSTrajectoryRepository
    retrieve_service: RetrieveService

    def build_success_pool(
        self,
        *,
        account_id: str,
        agent_id: str,
        anchor_trajectory_id: str,
        top_k: int,
        trajectory_min_score: float = 0.7,
        include_anchor: bool = True,
    ) -> TrajectoryPoolBuildResult:
        anchor_bundle = self.repo.load_trajectory(anchor_trajectory_id)
        if not anchor_bundle:
            raise FileNotFoundError(f"anchor trajectory not found: {anchor_trajectory_id}")
        anchor = _bundle_to_context(anchor_bundle)
        query_payload = _build_anchor_query(anchor_bundle)

        retrieve_out = self.retrieve_service.run(
            RetrieveCommand(
                account_id=account_id,
                agent_id=agent_id,
                query=query_payload,
                top_k=max(1, int(top_k)),
                include_full_clean_graph=False,
            )
        )
        warnings = list(retrieve_out.warnings or [])

        neighbor_ids: list[str] = []
        neighbor_scores: list[dict[str, Any]] = []
        seen: set[str] = {anchor_trajectory_id}
        score_threshold = float(trajectory_min_score)
        for item in retrieve_out.items:
            tid = _safe_text(item.get("trajectory_id"))
            if not tid or tid in seen:
                continue
            total_score = item.get("total_score")
            try:
                total_score_f = float(total_score)
            except Exception:
                total_score_f = 0.0
            if total_score_f < score_threshold:
                warnings.append(
                    f"trajectory skipped by min score: {tid} total_score={total_score_f:.3f} < {score_threshold:.3f}"
                )
                continue
            neighbor_ids.append(tid)
            neighbor_scores.append(
                {
                    "trajectory_id": tid,
                    "total_score": total_score,
                    "semantic_score": item.get("semantic_score"),
                    "graph_match_score": item.get("graph_match_score"),
                }
            )
            seen.add(tid)
            if len(neighbor_ids) >= max(1, int(top_k)):
                break

        neighbors: list[TrajectoryContext] = []
        for tid in neighbor_ids:
            bundle = self.repo.load_trajectory(tid)
            if not bundle:
                warnings.append(f"trajectory skipped: replay not found for {tid}")
                continue
            neighbors.append(_bundle_to_context(bundle))

        pool = [anchor, *neighbors] if include_anchor else list(neighbors)
        return TrajectoryPoolBuildResult(
            anchor=anchor,
            neighbors=neighbors,
            pool=pool,
            retrieved_trajectory_ids=neighbor_ids,
            retrieved_trajectory_scores=neighbor_scores,
            query_payload=query_payload,
            warnings=warnings,
        )

