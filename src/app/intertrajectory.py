"""Wiring helper for commit-time inter-trajectory linker."""

from __future__ import annotations

from app.config import AppSettings
from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveService
from core.skills.evolve.trajectory_pool_builder import TrajectoryPoolBuilder
from core.skills.intertrajectory.linker import InterTrajectoryLinker
from core.skills.intertrajectory.neo4j_state_store import Neo4jInterTrajectoryStateStore
from core.skills.intertrajectory.trigger import InterTrajectoryTrigger
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository


def build_intertrajectory_linker(
    *,
    settings: AppSettings,
    repo: LocalFSTrajectoryRepository,
    graph_store,  # noqa: ANN001
    vector_store,  # noqa: ANN001
) -> InterTrajectoryLinker | None:
    if not bool(settings.intertrajectory_enabled):
        return None
    if str(settings.intertrajectory_backend).strip().lower() != "neo4j":
        return None
    if graph_store is None:
        return None
    if not hasattr(graph_store, "load_clean_graph"):
        return None
    if vector_store is None:
        return None
    if settings.embedding_provider.lower() != "openai" or not settings.openai_api_key:
        return None

    semantic = SemanticRecall(
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        api_key=settings.openai_api_key,
        embedder_base_url=settings.model_endpoints.embedder_base_url or None,
        embedding_mode=settings.embedding_mode,
    )
    retrieve_service = RetrieveService(
        semantic_recall=semantic,
        skill_retriever=None,
        clean_graph_loader=(
            lambda trajectory_id: graph_store.load_clean_graph(trajectory_id=trajectory_id)
        ),  # type: ignore[attr-defined]
        query_dataflow_extractor=None,
        query_temporal_fallback_edge=settings.commit.temporal_fallback_edge,
        query_reasoning_min_confidence=settings.commit.reasoning_min_confidence,
    )
    pool_builder = TrajectoryPoolBuilder(repo=repo, retrieve_service=retrieve_service)
    state_store = Neo4jInterTrajectoryStateStore(
        graph_writer=graph_store,
        edge_rel_type=settings.intertrajectory_edge_rel_type,
        pending_rel_type=settings.intertrajectory_pending_rel_type,
    )
    return InterTrajectoryLinker(
        pool_builder=pool_builder,
        state_store=state_store,
        edge_threshold=float(settings.intertrajectory_edge_threshold),
        max_neighbors_per_commit=max(1, int(settings.intertrajectory_max_neighbors_per_commit)),
    )


def build_intertrajectory_trigger(
    *,
    settings: AppSettings,
    graph_store,  # noqa: ANN001
) -> InterTrajectoryTrigger | None:
    if not bool(settings.intertrajectory_enabled):
        return None
    if str(settings.intertrajectory_backend).strip().lower() != "neo4j":
        return None
    if graph_store is None:
        return None
    if not hasattr(graph_store, "list_intertrajectory_pending_neighbors"):
        return None
    if not hasattr(graph_store, "clear_intertrajectory_pending_neighbors"):
        return None

    state_store = Neo4jInterTrajectoryStateStore(
        graph_writer=graph_store,
        edge_rel_type=settings.intertrajectory_edge_rel_type,
        pending_rel_type=settings.intertrajectory_pending_rel_type,
    )

    def _run_route_with_candidates(**kwargs):  # noqa: ANN003
        from cli.route_skill import run_route_with_candidates

        return run_route_with_candidates(
            anchor_trajectory_id=kwargs["anchor_trajectory_id"],
            candidate_trajectory_ids=list(kwargs["candidate_trajectory_ids"]),
            candidate_trajectory_scores=dict(kwargs.get("candidate_trajectory_scores") or {}),
            account_id=kwargs["account_id"],
            agent_id=kwargs["agent_id"],
            support=max(1, int(kwargs["support"])),
            config_path=None,
            dry_run=False,
        )

    return InterTrajectoryTrigger(
        state_store=state_store,
        route_runner=_run_route_with_candidates,
        trigger_threshold=max(1, int(settings.intertrajectory_trigger_threshold)),
        pending_limit=max(1, int(settings.intertrajectory_max_neighbors_per_commit)),
        route_support=4,
    )
