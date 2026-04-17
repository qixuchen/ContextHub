"""Evolve one skill from successful trajectory pool (Phase A: success_only)."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.config import load_settings
from cli.compute_skill_embedding import run as run_compute_skill_embedding
from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveService
from core.skills.evolve.analyst_success import SuccessAnalyst
from core.skills.evolve.patch_merge import hierarchical_merge_success_patches
from core.skills.evolve.skill_apply import apply_merged_success_rules
from core.skills.evolve.trajectory_pool_builder import TrajectoryPoolBuilder
from core.skills.skill_loader import SkillDoc, load_skills
from infra.storage.fs.trajectory_repo import LocalFSTrajectoryRepository
from infra.storage.graph.factory import build_graph_store_writer
from infra.storage.vector.factory import build_vector_store_adapter


def _load_skill_doc(*, skill_root: str, skill_name: str) -> tuple[SkillDoc, list[str]]:
    docs, warnings = load_skills(root=skill_root)
    for doc in docs:
        if str(doc.skill_name).strip() == str(skill_name).strip():
            return doc, warnings
    raise FileNotFoundError(f"skill not found under {skill_root}: {skill_name}")


def _build_retrieve_service(settings) -> tuple[RetrieveService, list[str]]:  # noqa: ANN001
    warnings: list[str] = []
    vector_store = build_vector_store_adapter(settings)
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


def run_evolve(
    *,
    skill_name: str,
    anchor_trajectory_id: str,
    account_id: str,
    agent_id: str,
    top_k: int,
    trajectory_min_score: float,
    include_anchor: bool,
    analyst_mode: str,
    merge_batch_size: int,
    max_parallel_analysts: int,
    dry_run: bool,
    config_path: str | None,
) -> dict[str, Any]:
    if analyst_mode != "success_only":
        raise ValueError("Phase A only supports analyst_mode=success_only")

    settings = load_settings(config_path=config_path)
    t0 = time.perf_counter()

    skill_doc, skill_loader_warnings = _load_skill_doc(skill_root=settings.skill_root, skill_name=skill_name)
    skill_markdown = Path(skill_doc.skill_md_path).read_text(encoding="utf-8")

    retrieve_service, retrieve_warnings = _build_retrieve_service(settings)
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

    analyst = SuccessAnalyst(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        base_url=settings.model_endpoints.llm_base_url or None,
        temperature=0.0,
    )
    t_ana0 = time.perf_counter()
    patches, analyst_warnings = analyst.propose_many(
        skill_markdown=skill_markdown,
        trajectories=pool_result.pool,
        max_parallel_analysts=max(1, int(max_parallel_analysts)),
    )
    analyst_seconds = time.perf_counter() - t_ana0

    t_merge0 = time.perf_counter()
    merge_result = hierarchical_merge_success_patches(
        patches,
        merge_batch_size=max(1, int(merge_batch_size)),
    )
    merge_seconds = time.perf_counter() - t_merge0

    t_apply0 = time.perf_counter()
    apply_result = apply_merged_success_rules(
        skill_md_path=Path(skill_doc.skill_md_path),
        rules=merge_result.rules,
        dry_run=bool(dry_run),
    )
    apply_seconds = time.perf_counter() - t_apply0

    embed_summary = None
    embed_seconds = 0.0
    if not dry_run:
        t_emb0 = time.perf_counter()
        embed_summary = _refresh_skill_embedding(config_path=config_path, settings=settings)
        embed_seconds = time.perf_counter() - t_emb0

    total_seconds = time.perf_counter() - t0
    warnings = [
        *skill_loader_warnings,
        *retrieve_warnings,
        *(pool_result.warnings or []),
        *analyst_warnings,
    ]
    return {
        "status": "ok",
        "phase": "A",
        "mode": "success_only",
        "dry_run": bool(dry_run),
        "skill_name": skill_doc.skill_name,
        "anchor_trajectory_id": anchor_trajectory_id,
        "trajectory_pool": {
            "anchor": pool_result.anchor.trajectory_id,
            "neighbor_count": len(pool_result.neighbors),
            "pool_count": len(pool_result.pool),
            "trajectory_min_score": float(trajectory_min_score),
            "retrieved_trajectory_ids": pool_result.retrieved_trajectory_ids,
            "retrieved_trajectory_scores": pool_result.retrieved_trajectory_scores,
        },
        "patch_summary": {
            "proposed": len(patches),
            "merged_rules": merge_result.merged_rule_count,
            "merge_levels": merge_result.levels,
            "merge_batch_size": merge_result.merge_batch_size,
        },
        "apply_summary": {
            "skill_md_path": apply_result.skill_md_path,
            "reference_path": apply_result.reference_path,
            "rule_count": apply_result.rule_count,
            "description_updated": apply_result.description_updated,
            "replaced_existing_block": apply_result.replaced_existing_block,
        },
        "embedding_refresh_summary": embed_summary,
        "timing": {
            "pool_seconds": round(pool_seconds, 3),
            "analyst_seconds": round(analyst_seconds, 3),
            "merge_seconds": round(merge_seconds, 3),
            "apply_seconds": round(apply_seconds, 3),
            "embedding_seconds": round(embed_seconds, 3),
            "total_seconds": round(total_seconds, 3),
        },
        "warnings": warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amc-evolve-skill",
        description="Phase A skill evolve (success_only) from an anchor trajectory.",
    )
    p.add_argument("--skill-name", required=True, help="Target skill name under data/skill")
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
        "--exclude-anchor",
        action="store_true",
        help="Exclude anchor trajectory from analyst pool",
    )
    p.add_argument(
        "--analyst-mode",
        default="success_only",
        choices=["success_only", "error_only", "combined"],
        help="Phase A only supports success_only",
    )
    p.add_argument("--merge-batch-size", type=int, default=8, help="Hierarchical merge batch size")
    p.add_argument("--max-parallel-analysts", type=int, default=32, help="Success analysts parallel workers")
    p.add_argument("--dry-run", action="store_true", help="Do not write skill files or embedding state")
    p.add_argument("--config-path", default=None, help="Optional config YAML path")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out = run_evolve(
        skill_name=args.skill_name,
        anchor_trajectory_id=args.anchor_trajectory_id,
        account_id=args.account_id,
        agent_id=args.agent_id,
        top_k=int(args.top_k),
        trajectory_min_score=float(args.trajectory_min_score),
        include_anchor=not bool(args.exclude_anchor),
        analyst_mode=args.analyst_mode,
        merge_batch_size=int(args.merge_batch_size),
        max_parallel_analysts=int(args.max_parallel_analysts),
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

