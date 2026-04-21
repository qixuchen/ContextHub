"""Auto route between skill update and skill creation (Phase A1)."""

from __future__ import annotations

import argparse
import json
import time
from types import SimpleNamespace
from typing import Any

from app.config import load_settings
from cli.compute_skill_embedding import run as run_compute_skill_embedding
from cli.evolve_skill import run_evolve
from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveService
from core.retrieve.skill_retriever import SkillRetriever
from core.skills.create.skill_creator import create_skill_from_trajectories
from core.skills.evolve.trajectory_pool_builder import TrajectoryPoolBuilder
from core.skills.evolve.types import TrajectoryContext
from core.skills.routing.skill_router import SkillRouteDecision, SkillRouter
from core.skills.skill_loader import load_skills
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_skill_vector_store_adapter, build_vector_store_adapter


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _build_retrieve_service(settings) -> tuple[RetrieveService, list[str]]:  # noqa: ANN001
    warnings: list[str] = []
    vector_store = None
    try:
        vector_store = build_vector_store_adapter(settings)
    except Exception as exc:
        warnings.append(f"semantic backend unavailable: {type(exc).__name__}: {exc}")
    semantic = None
    if vector_store is not None and settings.embedding_provider.lower() == "openai" and settings.openai_api_key:
        semantic = SemanticRecall(
            vector_store=vector_store,
            embedding_model=settings.embedding_model,
            api_key=settings.openai_api_key,
            embedder_base_url=settings.model_endpoints.embedder_base_url or None,
            embedding_mode=settings.embedding_mode,
        )
    else:
        warnings.append(
            "semantic backend unavailable; trajectory pool will fallback to anchor-only if retrieval returns empty"
        )
    graph_store = build_graph_store_writer(settings)
    clean_graph_loader = None
    if graph_store is not None and hasattr(graph_store, "load_clean_graph"):
        clean_graph_loader = lambda trajectory_id: graph_store.load_clean_graph(trajectory_id=trajectory_id)  # type: ignore[attr-defined]
    service = RetrieveService(
        semantic_recall=semantic,
        skill_retriever=None,
        clean_graph_loader=clean_graph_loader,
        query_dataflow_extractor=None,
        query_temporal_fallback_edge=settings.commit.temporal_fallback_edge,
        query_reasoning_min_confidence=settings.commit.reasoning_min_confidence,
    )
    return service, warnings


def _build_skill_retriever(settings) -> tuple[SkillRetriever | None, list[str]]:  # noqa: ANN001
    warnings: list[str] = []
    vector_store = None
    try:
        vector_store = build_skill_vector_store_adapter(settings)
    except Exception as exc:
        warnings.append(f"skill vector backend unavailable: {type(exc).__name__}: {exc}")
    if vector_store is None:
        warnings.append("skill vector backend unavailable; candidate skill retrieval skipped")
        return None, warnings
    if settings.embedding_provider.lower() != "openai" or not settings.openai_api_key:
        warnings.append("skill retriever disabled: only openai embedding mode is supported")
        return None, warnings
    retriever = SkillRetriever(
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        api_key=settings.openai_api_key,
        embedder_base_url=settings.model_endpoints.embedder_base_url or None,
        embedding_mode=settings.embedding_mode,
    )
    return retriever, warnings


def _build_skill_query_text(pool_result) -> str:  # noqa: ANN001
    task_description = _safe_text((pool_result.query_payload or {}).get("task_description"))
    snippets: list[str] = []
    for item in pool_result.pool:
        text = _safe_text(item.abstract) or _safe_text(item.overview)
        if text:
            snippets.append(text)
    parts = [task_description] if task_description else []
    if snippets:
        parts.append(" | ".join(snippets))
    return " || ".join(parts) or "retrieve relevant skill"


def _refresh_skill_embedding(*, config_path: str | None, settings) -> dict[str, Any]:  # noqa: ANN001
    args = argparse.Namespace(
        config_path=config_path,
        skill_root=settings.skill_root,
        state_file=settings.skill_embedding_state_file,
        max_workers=settings.skill_embedding_max_workers,
        dry_run=False,
        full_rebuild=False,
        pretty=False,
    )
    return run_compute_skill_embedding(args)


def _forced_decision(mode: str, pool_summary: str) -> SkillRouteDecision:
    if mode == "update":
        return SkillRouteDecision(
            decision="update",
            confidence=1.0,
            scope_match_level="high",
            reasoning="force_mode=update",
            task_type_summary=pool_summary,
            suggested_skill_name=pool_summary,
        )
    return SkillRouteDecision(
        decision="create",
        confidence=1.0,
        scope_match_level="low",
        reasoning="force_mode=create",
        task_type_summary=pool_summary,
        suggested_skill_name=pool_summary,
    )


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
    )


def _pool_task_description(anchor: TrajectoryContext, neighbors: list[TrajectoryContext]) -> str:
    parts: list[str] = []
    for value in [anchor.task_id, anchor.abstract, anchor.overview]:
        text = _safe_text(value)
        if text:
            parts.append(text)
    if not parts:
        for item in neighbors:
            text = _safe_text(item.task_id) or _safe_text(item.abstract) or _safe_text(item.overview)
            if text:
                parts.append(text)
                break
    return " | ".join(parts) or f"trajectory {anchor.trajectory_id}"


def _build_pool_result_from_candidates(
    *,
    repo: LocalFSTrajectoryRepository,
    anchor_trajectory_id: str,
    candidate_trajectory_ids: list[str],
    candidate_trajectory_scores: dict[str, dict[str, Any]] | None,
    include_anchor: bool,
) -> tuple[Any, list[str]]:  # noqa: ANN401
    warnings: list[str] = []
    anchor_bundle = repo.load_trajectory(anchor_trajectory_id)
    if not anchor_bundle:
        raise FileNotFoundError(f"anchor trajectory not found: {anchor_trajectory_id}")
    anchor = _bundle_to_context(anchor_bundle)

    dedup_ids = list(dict.fromkeys([_safe_text(x) for x in candidate_trajectory_ids if _safe_text(x)]))
    if anchor_trajectory_id not in dedup_ids:
        dedup_ids.insert(0, anchor_trajectory_id)
    if len(dedup_ids) < 2:
        warnings.append("candidate trajectory list has no neighbors besides anchor")

    neighbors: list[TrajectoryContext] = []
    retrieved_ids: list[str] = []
    retrieved_scores: list[dict[str, Any]] = []
    score_map = candidate_trajectory_scores or {}
    for tid in dedup_ids:
        if tid == anchor_trajectory_id:
            continue
        bundle = repo.load_trajectory(tid)
        if not bundle:
            warnings.append(f"candidate trajectory skipped: replay not found for {tid}")
            continue
        neighbors.append(_bundle_to_context(bundle))
        retrieved_ids.append(tid)
        score_item = score_map.get(tid) or {}
        retrieved_scores.append(
            {
                "trajectory_id": tid,
                "total_score": score_item.get("total_score"),
                "semantic_score": score_item.get("semantic_score"),
                "graph_match_score": score_item.get("graph_match_score"),
            }
        )
    pool = [anchor, *neighbors] if include_anchor else list(neighbors)
    query_payload = {"task_description": _pool_task_description(anchor, neighbors)}
    pool_result = SimpleNamespace(
        anchor=anchor,
        neighbors=neighbors,
        pool=pool,
        retrieved_trajectory_ids=retrieved_ids,
        retrieved_trajectory_scores=retrieved_scores,
        query_payload=query_payload,
        warnings=warnings,
    )
    return pool_result, warnings


def _run_route_from_pool(
    *,
    settings,  # noqa: ANN001
    pool_result,  # noqa: ANN001
    anchor_trajectory_id: str,
    account_id: str,
    agent_id: str,
    top_k: int,
    trajectory_min_score: float | None,
    support: int,
    skill_top_k: int,
    include_anchor: bool,
    merge_batch_size: int,
    max_parallel_analysts: int,
    confidence_threshold: float,
    force_mode: str,
    dry_run: bool,
    config_path: str | None,
    warnings: list[str],
    pool_seconds: float,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    required_support = max(1, int(support))
    qualified_support_count = len(pool_result.retrieved_trajectory_scores)
    support_satisfied = qualified_support_count >= required_support
    if not support_satisfied:
        warnings.append(
            f"support insufficient: qualified trajectories={qualified_support_count} < required_support={required_support}"
        )
        total_seconds = time.perf_counter() - t0
        return {
            "status": "ok",
            "phase": "A1",
            "mode": "route",
            "decision": {
                "decision": "support_insufficient",
                "confidence": 0.0,
                "scope_match_level": "low",
                "reasoning": "support below minimum; skip route and skill write",
                "task_type_summary": _safe_text((pool_result.query_payload or {}).get("task_description")),
                "suggested_skill_name": "",
            },
            "candidate_skill": None,
            "updated_skill_name": None,
            "created_skill_name": None,
            "trajectory_pool": {
                "anchor": pool_result.anchor.trajectory_id,
                "neighbor_count": len(pool_result.neighbors),
                "pool_count": len(pool_result.pool),
                "trajectory_min_score": (
                    float(trajectory_min_score) if trajectory_min_score is not None else None
                ),
                "retrieved_trajectory_ids": pool_result.retrieved_trajectory_ids,
                "retrieved_trajectory_scores": pool_result.retrieved_trajectory_scores,
                "qualified_support_count": qualified_support_count,
                "required_support": required_support,
                "support_satisfied": support_satisfied,
            },
            "branch_result": {
                "status": "skipped",
                "reason": "support_insufficient",
                "qualified_support_count": qualified_support_count,
                "required_support": required_support,
            },
            "embedding_refresh_summary": None,
            "timing": {
                "pool_seconds": round(pool_seconds, 3),
                "route_seconds": 0.0,
                "total_seconds": round(total_seconds, 3),
            },
            "warnings": warnings,
        }

    skill_retriever, skill_warn = _build_skill_retriever(settings)
    warnings.extend(skill_warn)
    candidate_skill = None
    if skill_retriever is not None:
        query_text = _build_skill_query_text(pool_result)
        hits = skill_retriever.recall(
            query_text=query_text,
            top_k=max(1, int(skill_top_k)),
            score_threshold=0.0,
        )
        if hits:
            candidate_skill = hits[0]

    router = SkillRouter(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.model_endpoints.llm_base_url or None,
        confidence_threshold=float(confidence_threshold),
    )
    t_route0 = time.perf_counter()
    if force_mode in {"update", "create"}:
        decision = _forced_decision(force_mode, router.summarize_task_type(pool_result.pool))
        if force_mode == "update" and candidate_skill is None:
            decision = SkillRouteDecision(
                decision="create",
                confidence=1.0,
                scope_match_level="low",
                reasoning="force update requested but no candidate skill found; fallback create",
                task_type_summary=decision.task_type_summary,
                suggested_skill_name=decision.suggested_skill_name,
            )
            warnings.append("force update fallback to create: no candidate skill")
    else:
        decision = router.decide(candidate_skill=candidate_skill, pool=pool_result.pool)
    route_seconds = time.perf_counter() - t_route0

    branch = decision.decision
    branch_result: dict[str, Any]
    created_skill_name: str | None = None
    updated_skill_name: str | None = None
    embedding_refresh_summary: dict[str, Any] | None = None

    if branch == "update" and candidate_skill is not None:
        updated_skill_name = candidate_skill.skill_name
        branch_result = run_evolve(
            skill_name=updated_skill_name,
            anchor_trajectory_id=anchor_trajectory_id,
            account_id=account_id,
            agent_id=agent_id,
            top_k=max(1, int(top_k)),
            trajectory_min_score=(
                float(trajectory_min_score) if trajectory_min_score is not None else 0.0
            ),
            include_anchor=bool(include_anchor),
            analyst_mode="success_only",
            merge_batch_size=max(1, int(merge_batch_size)),
            max_parallel_analysts=max(1, int(max_parallel_analysts)),
            dry_run=bool(dry_run),
            config_path=config_path,
            pool_override=pool_result,
        )
        embedding_refresh_summary = branch_result.get("embedding_refresh_summary")
    else:
        branch = "create"
        docs, _ = load_skills(root=settings.skill_root)
        existing_names = {str(d.skill_name).strip() for d in docs if str(d.skill_name).strip()}
        t_create0 = time.perf_counter()
        create_result = create_skill_from_trajectories(
            skill_root=settings.skill_root,
            task_type_summary=decision.task_type_summary,
            pool=pool_result.pool,
            existing_names=existing_names,
            name_seed=decision.suggested_skill_name,
            dry_run=bool(dry_run),
        )
        create_seconds = time.perf_counter() - t_create0
        created_skill_name = create_result.skill_name
        if not dry_run:
            embedding_refresh_summary = _refresh_skill_embedding(config_path=config_path, settings=settings)
        branch_result = {
            "status": "ok",
            "phase": "A1-create",
            "mode": "create",
            "dry_run": bool(dry_run),
            "skill_name": create_result.skill_name,
            "apply_summary": {
                "skill_md_path": create_result.skill_md_path,
                "created": bool(create_result.created),
                "description": create_result.description,
            },
            "timing": {
                "create_seconds": round(create_seconds, 3),
            },
        }

    total_seconds = time.perf_counter() - t0
    return {
        "status": "ok",
        "phase": "A1",
        "mode": "route",
        "decision": {
            "decision": branch,
            "confidence": float(decision.confidence),
            "scope_match_level": decision.scope_match_level,
            "reasoning": decision.reasoning,
            "task_type_summary": decision.task_type_summary,
            "suggested_skill_name": decision.suggested_skill_name,
        },
        "candidate_skill": (
            {
                "skill_name": candidate_skill.skill_name,
                "description": candidate_skill.description,
                "score": float(candidate_skill.score),
                "uri": candidate_skill.uri,
                "path": candidate_skill.path,
            }
            if candidate_skill is not None
            else None
        ),
        "updated_skill_name": updated_skill_name,
        "created_skill_name": created_skill_name,
        "trajectory_pool": {
            "anchor": pool_result.anchor.trajectory_id,
            "neighbor_count": len(pool_result.neighbors),
            "pool_count": len(pool_result.pool),
            "trajectory_min_score": (
                float(trajectory_min_score) if trajectory_min_score is not None else None
            ),
            "retrieved_trajectory_ids": pool_result.retrieved_trajectory_ids,
            "retrieved_trajectory_scores": pool_result.retrieved_trajectory_scores,
            "qualified_support_count": qualified_support_count,
            "required_support": required_support,
            "support_satisfied": support_satisfied,
        },
        "branch_result": branch_result,
        "embedding_refresh_summary": embedding_refresh_summary,
        "timing": {
            "pool_seconds": round(pool_seconds, 3),
            "route_seconds": round(route_seconds, 3),
            "total_seconds": round(total_seconds, 3),
        },
        "warnings": warnings,
    }


def run_route(
    *,
    anchor_trajectory_id: str,
    account_id: str,
    agent_id: str,
    top_k: int,
    trajectory_min_score: float,
    support: int,
    skill_top_k: int,
    include_anchor: bool,
    merge_batch_size: int,
    max_parallel_analysts: int,
    confidence_threshold: float,
    force_mode: str,
    dry_run: bool,
    config_path: str | None,
) -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    warnings: list[str] = []

    retrieve_service, retrieve_warn = _build_retrieve_service(settings)
    warnings.extend(retrieve_warn)
    repo = LocalFSTrajectoryRepository(root=settings.storage.localfs_root)
    pool_builder = TrajectoryPoolBuilder(repo=repo, retrieve_service=retrieve_service)
    t_pool0 = time.perf_counter()
    pool_result = pool_builder.build_success_pool(
        account_id=account_id,
        agent_id=agent_id,
        anchor_trajectory_id=anchor_trajectory_id,
        top_k=max(1, int(top_k)),
        trajectory_min_score=float(trajectory_min_score),
        include_anchor=bool(include_anchor),
    )
    pool_seconds = time.perf_counter() - t_pool0
    warnings.extend(pool_result.warnings or [])
    return _run_route_from_pool(
        settings=settings,
        pool_result=pool_result,
        anchor_trajectory_id=anchor_trajectory_id,
        account_id=account_id,
        agent_id=agent_id,
        top_k=top_k,
        trajectory_min_score=float(trajectory_min_score),
        support=support,
        skill_top_k=skill_top_k,
        include_anchor=include_anchor,
        merge_batch_size=merge_batch_size,
        max_parallel_analysts=max_parallel_analysts,
        confidence_threshold=confidence_threshold,
        force_mode=force_mode,
        dry_run=dry_run,
        config_path=config_path,
        warnings=warnings,
        pool_seconds=pool_seconds,
    )


def run_route_with_candidates(
    *,
    anchor_trajectory_id: str,
    candidate_trajectory_ids: list[str],
    candidate_trajectory_scores: dict[str, dict[str, Any]] | None,
    account_id: str,
    agent_id: str,
    support: int,
    skill_top_k: int = 1,
    include_anchor: bool = True,
    merge_batch_size: int = 8,
    max_parallel_analysts: int = 32,
    confidence_threshold: float = 0.7,
    force_mode: str = "auto",
    dry_run: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    repo = LocalFSTrajectoryRepository(root=settings.storage.localfs_root)
    t_pool0 = time.perf_counter()
    pool_result, warnings = _build_pool_result_from_candidates(
        repo=repo,
        anchor_trajectory_id=anchor_trajectory_id,
        candidate_trajectory_ids=candidate_trajectory_ids,
        candidate_trajectory_scores=candidate_trajectory_scores,
        include_anchor=bool(include_anchor),
    )
    pool_seconds = time.perf_counter() - t_pool0
    return _run_route_from_pool(
        settings=settings,
        pool_result=pool_result,
        anchor_trajectory_id=anchor_trajectory_id,
        account_id=account_id,
        agent_id=agent_id,
        top_k=max(1, len(candidate_trajectory_ids)),
        trajectory_min_score=None,
        support=support,
        skill_top_k=skill_top_k,
        include_anchor=include_anchor,
        merge_batch_size=merge_batch_size,
        max_parallel_analysts=max_parallel_analysts,
        confidence_threshold=confidence_threshold,
        force_mode=force_mode,
        dry_run=dry_run,
        config_path=config_path,
        warnings=warnings,
        pool_seconds=pool_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amc-route-skill",
        description="Auto route between updating existing skill and creating new skill.",
    )
    p.add_argument("--anchor-trajectory-id", required=True, help="Anchor trajectory id")
    p.add_argument("--account-id", default="account-local", help="Account identifier")
    p.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    p.add_argument("--top-k", type=int, default=8, help="Neighbor trajectories to retrieve")
    p.add_argument(
        "--trajectory-min-score",
        type=float,
        default=0.7,
        help="Minimum total_score required to include a retrieved trajectory",
    )
    p.add_argument(
        "--support",
        type=int,
        default=4,
        help="Minimum number of trajectories passing threshold before route can update/create",
    )
    p.add_argument("--skill-top-k", type=int, default=1, help="Candidate skill top-k before routing")
    p.add_argument(
        "--exclude-anchor",
        action="store_true",
        help="Exclude anchor trajectory from analysis pool",
    )
    p.add_argument("--merge-batch-size", type=int, default=8, help="Merge batch size for update branch")
    p.add_argument("--max-parallel-analysts", type=int, default=32, help="Parallel workers for update branch")
    p.add_argument(
        "--confidence-threshold",
        type=float,
        default=0.7,
        help="Minimum confidence required by route guard for update",
    )
    p.add_argument(
        "--force-mode",
        choices=["auto", "update", "create"],
        default="auto",
        help="Route mode override for debugging",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not write skill files or embedding state")
    p.add_argument("--config-path", default=None, help="Optional config YAML path")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out = run_route(
        anchor_trajectory_id=args.anchor_trajectory_id,
        account_id=args.account_id,
        agent_id=args.agent_id,
        top_k=int(args.top_k),
        trajectory_min_score=float(args.trajectory_min_score),
        support=int(args.support),
        skill_top_k=int(args.skill_top_k),
        include_anchor=not bool(args.exclude_anchor),
        merge_batch_size=int(args.merge_batch_size),
        max_parallel_analysts=int(args.max_parallel_analysts),
        confidence_threshold=float(args.confidence_threshold),
        force_mode=args.force_mode,
        dry_run=bool(args.dry_run),
        config_path=args.config_path,
    )
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

