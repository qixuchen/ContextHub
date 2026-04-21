from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.intertrajectory import build_intertrajectory_linker

pytestmark = pytest.mark.unit


def test_build_intertrajectory_linker_wraps_clean_graph_loader() -> None:
    settings = SimpleNamespace(
        intertrajectory_enabled=True,
        intertrajectory_backend="neo4j",
        embedding_provider="openai",
        openai_api_key="dummy-key",
        embedding_model="dummy-embedding",
        model_endpoints=SimpleNamespace(embedder_base_url=""),
        embedding_mode="text",
        commit=SimpleNamespace(temporal_fallback_edge=True, reasoning_min_confidence=0.55),
        intertrajectory_edge_rel_type="INTERTRAJ_SIMILAR",
        intertrajectory_pending_rel_type="INTERTRAJ_ACTIVATED_PENDING",
        intertrajectory_edge_threshold=0.7,
        intertrajectory_max_neighbors_per_commit=32,
    )

    class _FakeGraphStore:
        def load_clean_graph(self, *, trajectory_id: str):  # noqa: ANN001
            return {"trajectory_id": trajectory_id, "nodes": [], "edges": []}

    linker = build_intertrajectory_linker(
        settings=settings,
        repo=object(),  # type: ignore[arg-type]
        graph_store=_FakeGraphStore(),
        vector_store=object(),
    )
    assert linker is not None
    clean_graph_loader = linker.pool_builder.retrieve_service.clean_graph_loader
    assert callable(clean_graph_loader)
    loaded = clean_graph_loader("traj-123")
    assert loaded["trajectory_id"] == "traj-123"

