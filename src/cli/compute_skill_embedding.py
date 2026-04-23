"""Compute and refresh skill embeddings incrementally."""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import load_settings
from core.skills.skill_embedding_builder import (
    SkillEmbeddingBuilder,
    load_skill_embedding_state,
    save_skill_embedding_state,
)
from core.skills.skill_loader import load_skills
from infra.storage.vector.factory import build_skill_vector_store_adapter


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings(config_path=args.config_path)
    skill_root = str(args.skill_root or settings.skill_root).strip()
    state_file = str(args.state_file or settings.skill_embedding_state_file).strip()
    max_workers = int(args.max_workers or settings.skill_embedding_max_workers)
    dry_run = bool(args.dry_run)
    full_rebuild = bool(args.full_rebuild)

    docs, loader_warnings = load_skills(root=skill_root)
    previous_state = load_skill_embedding_state(state_file)

    vector_store = build_skill_vector_store_adapter(settings)
    if vector_store is None:
        raise RuntimeError("skill vector store is not configured")
    if not settings.openai_api_key:
        raise RuntimeError("AMC_OPENAI_API_KEY is required for embedding")
    if settings.embedding_provider.lower() != "openai":
        raise RuntimeError("only embedding_provider=openai is supported in this phase")

    builder = SkillEmbeddingBuilder(
        vector_store=vector_store,
        embedding_model=settings.embedding_model,
        api_key=settings.openai_api_key,
        embedder_base_url=settings.model_endpoints.embedder_base_url or None,
        embedding_mode=settings.embedding_mode,
    )
    outcome = builder.build_incremental(
        docs=docs,
        previous_state=previous_state,
        max_workers=max_workers,
        dry_run=dry_run,
        full_rebuild=full_rebuild,
    )
    if not dry_run:
        save_skill_embedding_state(state_file, outcome["state"])

    return {
        "ok": len(outcome.get("errors") or []) == 0,
        "dry_run": dry_run,
        "full_rebuild": full_rebuild,
        "skill_root": skill_root,
        "state_file": state_file,
        "max_workers": max_workers,
        "loader_warnings": loader_warnings,
        "summary": {
            "scanned_skills": outcome.get("scanned_skills"),
            "changed_skills": outcome.get("changed_skills"),
            "removed_skills": outcome.get("removed_skills"),
            "upserted_vectors": outcome.get("upserted_vectors"),
            "deleted_vectors": outcome.get("deleted_vectors"),
            "removed_skill_names": outcome.get("removed_skill_names"),
        },
        "errors": outcome.get("errors") or [],
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amc-compute-skill-embedding",
        description="Incrementally compute skill embeddings and sync skill vector index.",
    )
    p.add_argument("--config-path", default=None, help="Optional config YAML path")
    p.add_argument("--skill-root", default=None, help="Override skill root directory")
    p.add_argument("--state-file", default=None, help="Override skill embedding state file")
    p.add_argument("--max-workers", type=int, default=None, help="Parallel workers for embedding")
    p.add_argument("--full-rebuild", action="store_true", help="Force full rebuild ignoring hash state")
    p.add_argument("--dry-run", action="store_true", help="Compute change-set only, do not write index")
    p.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    return p


def main() -> int:
    args = build_parser().parse_args()
    out = run(args)
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

