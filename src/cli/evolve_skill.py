"""Evolve skill in update/create modes with LLM rewrite."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
import yaml
from openai import OpenAI

from app.config import load_settings
from cli.compute_skill_embedding import run as run_compute_skill_embedding
from core.commit.llm_runtime import chat_completion_with_retry, provider_key
from core.retrieve.semantic_recall import SemanticRecall
from core.retrieve.service import RetrieveService
from core.skills.evolve.analyst_error import ErrorAnalyst
from core.skills.evolve.analyst_success import SuccessAnalyst
from core.skills.evolve.patch_merge import hierarchical_merge_success_patches
from core.skills.create.skill_creator import unique_skill_name
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


_FAILURE_TOKENS = (
    "fail",
    "failed",
    "failure",
    "error",
    "exception",
    "traceback",
    "timeout",
    "cannot",
    "unable",
    "not found",
    "invalid",
)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _looks_like_failure(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    return any(tok in raw for tok in _FAILURE_TOKENS)


def _is_failure_trajectory(item) -> bool:  # noqa: ANN001
    outcome = str(getattr(item, "outcome_label", "") or "").strip().lower()
    if outcome in {"fail", "failed", "error"}:
        return True
    if outcome in {"success", "ok", "passed"}:
        return False
    text_parts = [str(getattr(item, "abstract", "") or ""), str(getattr(item, "overview", "") or "")]
    for step in list(getattr(item, "trajectory", []) or [])[-8:]:
        if not isinstance(step, dict):
            continue
        for key in ("Observation", "observation", "Result", "result"):
            val = step.get(key)
            if val:
                text_parts.append(str(val))
    return _looks_like_failure("\n".join(text_parts))


def _split_trajectory_pool(pool: list[Any]) -> tuple[list[Any], list[Any]]:
    success_pool: list[Any] = []
    error_pool: list[Any] = []
    for item in pool:
        if _is_failure_trajectory(item):
            error_pool.append(item)
        else:
            success_pool.append(item)
    return success_pool, error_pool


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = _safe_text(text)
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return {}
    return {}


def _pool_task_type_summary(pool: list[Any]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in pool:
        for value in (
            _safe_text(getattr(item, "task_id", "")),
            _safe_text(getattr(item, "abstract", "")),
            _safe_text(getattr(item, "overview", "")),
        ):
            if not value or value in seen:
                continue
            seen.add(value)
            parts.append(value)
            break
    return " | ".join(parts) or "trajectory task type summary unavailable"


class SkillRewriteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str
    description: str
    scope: str
    when_to_use: list[str]
    when_not_to_use: list[str]
    workflow: list[str]
    checklist: list[str]
    evidence_trajectories: list[str]


def _skill_rewrite_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "amc_skill_rewrite_output",
            "strict": True,
            "schema": SkillRewriteOutput.model_json_schema(),
        },
    }


def _is_response_format_unsupported(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "response_format" in text or "json_schema" in text


def _parse_skill_rewrite_output(raw_text: str) -> SkillRewriteOutput:
    data = _extract_json_payload(raw_text)
    try:
        return SkillRewriteOutput.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"invalid structured output: {exc}") from exc


def _clean_list_items(items: list[str], *, field_name: str, min_items: int) -> list[str]:
    cleaned = [_safe_text(x).replace("\n", " ") for x in items if _safe_text(x)]
    if len(cleaned) < min_items:
        raise ValueError(f"{field_name} requires at least {min_items} non-empty item(s)")
    return cleaned


def _compose_skill_markdown(*, parsed: SkillRewriteOutput, skill_name: str, description: str) -> str:
    scope = _safe_text(parsed.scope)
    if not scope:
        raise ValueError("scope must be non-empty")
    when_to_use = _clean_list_items(parsed.when_to_use, field_name="when_to_use", min_items=1)
    when_not_to_use = _clean_list_items(parsed.when_not_to_use, field_name="when_not_to_use", min_items=1)
    workflow = _clean_list_items(parsed.workflow, field_name="workflow", min_items=2)
    checklist = _clean_list_items(parsed.checklist, field_name="checklist", min_items=2)
    evidence = _clean_list_items(parsed.evidence_trajectories, field_name="evidence_trajectories", min_items=1)

    meta = {
        "name": skill_name,
        "description": description,
    }
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    lines: list[str] = [
        "---",
        frontmatter,
        "---",
        "",
        f"# {skill_name}",
        "",
        "## Scope",
        scope,
        "",
        "## When to use",
    ]
    lines.extend([f"- {item}" for item in when_to_use])
    lines.extend(["", "## When not to use"])
    lines.extend([f"- {item}" for item in when_not_to_use])
    lines.extend(["", "## Workflow"])
    lines.extend([f"{idx}. {item}" for idx, item in enumerate(workflow, start=1)])
    lines.extend(["", "## Checklist"])
    lines.extend([f"- [ ] {item}" for item in checklist])
    lines.extend(["", "## Evidence Trajectories"])
    lines.extend([f"- {item}" for item in evidence])
    lines.append("")
    return "\n".join(lines)


def _llm_rewrite_skill_markdown(
    *,
    settings,  # noqa: ANN001
    mode: str,
    target_skill_name: str,
    current_skill_markdown: str,
    task_type_summary: str,
    pool: list[Any],
    merged_rules,  # noqa: ANN001
    existing_names: set[str],
) -> dict[str, Any]:
    api_key = _safe_text(settings.openai_api_key)
    if not api_key:
        raise ValueError("openai_api_key is required for run_evolve LLM rewrite")
    client = OpenAI(
        api_key=api_key,
        base_url=settings.model_endpoints.llm_base_url or None,
    )
    if mode == "update":
        mode_prompt = (
            "Mode=update. Revise the existing candidate skill based on trajectory evidence and merged rules. "
            "The skill name MUST stay exactly equal to target_skill_name."
        )
    else:
        mode_prompt = (
            "Mode=create. Create a brand-new skill from trajectory evidence and merged rules. "
            "Output a concrete, task-oriented skill_name in snake_case and prefixed with 'skill_'."
        )
    prompt_base = (
        "You are an expert skill author.\n"
        f"{mode_prompt}\n"
        "Return JSON only using the schema fields exactly.\n"
        "Do NOT return markdown, frontmatter, or any extra keys.\n"
        "description and scope must be concise and specific.\n"
        "when_to_use / when_not_to_use / workflow / checklist / evidence_trajectories must be concrete lists.\n"
    )
    payload = {
        "mode": mode,
        "target_skill_name": target_skill_name,
        "task_type_summary": task_type_summary,
        "current_skill_markdown": current_skill_markdown,
        "merged_rules": [
            {
                "title": _safe_text(getattr(rule, "title", "")),
                "sop": _safe_text(getattr(rule, "sop", "")),
                "checklist": list(getattr(rule, "checklist", []) or []),
                "support_count": int(getattr(rule, "support_count", 0) or 0),
                "evidence_trajectory_ids": list(getattr(rule, "evidence_trajectory_ids", []) or []),
            }
            for rule in list(merged_rules or [])
        ],
        "trajectory_pool": [
            {
                "trajectory_id": _safe_text(getattr(t, "trajectory_id", "")),
                "task_id": _safe_text(getattr(t, "task_id", "")),
                "abstract": _safe_text(getattr(t, "abstract", "")),
                "overview": _safe_text(getattr(t, "overview", "")),
                "outcome_label": _safe_text(getattr(t, "outcome_label", "")),
                "trajectory": list(getattr(t, "trajectory", []) or []),
            }
            for t in pool
        ],
    }
    max_attempts = 3
    last_error = "unknown error"
    last_raw_preview = ""
    use_response_format = True
    for attempt in range(1, max_attempts + 1):
        prompt = prompt_base
        attempt_raw_preview = ""
        if attempt > 1:
            prompt = (
                f"{prompt_base}\n"
                f"Previous output format error: {last_error}\n"
                "Fix the format and retry. Return JSON only."
            )
        try:
            response_format = _skill_rewrite_response_format() if use_response_format else None
            resp, _ = chat_completion_with_retry(
                client=client,
                provider_id=provider_key(
                    base_url=settings.model_endpoints.llm_base_url or None,
                    model=settings.llm_model,
                ),
                model=settings.llm_model,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format=response_format,
            )
            raw = _safe_text(resp.choices[0].message.content)
            attempt_raw_preview = raw[:1600]
            parsed = _parse_skill_rewrite_output(raw)

            proposed_name = (
                _safe_text(parsed.skill_name)
                or target_skill_name
                or task_type_summary
            )
            if mode == "update":
                final_name = target_skill_name
            else:
                final_name = unique_skill_name(proposed_name, existing_names=existing_names)
            description = _safe_text(parsed.description)
            if not description:
                raise ValueError("missing description in llm output")
            markdown = _compose_skill_markdown(
                parsed=parsed,
                skill_name=final_name,
                description=description,
            )
            return {
                "skill_name": final_name,
                "description": description,
                "skill_markdown": markdown,
            }
        except Exception as exc:
            if use_response_format and _is_response_format_unsupported(exc):
                # Some providers don't support response_format=json_schema.
                use_response_format = False
            last_raw_preview = attempt_raw_preview
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt_raw_preview:
                last_error = f"{last_error} | raw_preview={attempt_raw_preview!r}"
    raise ValueError(
        f"llm rewrite output invalid after {max_attempts} attempts: {last_error}"
        f" | last_raw_preview={last_raw_preview!r}"
    )


def run_evolve(
    *,
    skill_name: str | None,
    anchor_trajectory_id: str,
    account_id: str,
    agent_id: str,
    top_k: int,
    trajectory_min_score: float,
    include_anchor: bool,
    mode: str = "update",
    analyst_mode: str,
    merge_batch_size: int,
    max_parallel_analysts: int,
    dry_run: bool,
    config_path: str | None,
    pool_override=None,  # noqa: ANN001
) -> dict[str, Any]:
    valid_evolve_modes = {"update", "create"}
    if mode not in valid_evolve_modes:
        raise ValueError(f"unsupported mode={mode}; expected one of {sorted(valid_evolve_modes)}")
    valid_modes = {"success_only", "error_only", "combined"}
    if analyst_mode not in valid_modes:
        raise ValueError(f"unsupported analyst_mode={analyst_mode}; expected one of {sorted(valid_modes)}")
    if mode == "update" and not _safe_text(skill_name):
        raise ValueError("skill_name is required when mode=update")

    settings = load_settings(config_path=config_path)
    t0 = time.perf_counter()

    skill_loader_warnings: list[str] = []
    skill_doc: SkillDoc | None = None
    if mode == "update":
        skill_doc, skill_loader_warnings = _load_skill_doc(
            skill_root=settings.skill_root,
            skill_name=_safe_text(skill_name),
        )
        base_skill_markdown = Path(skill_doc.skill_md_path).read_text(encoding="utf-8")
        target_skill_name = _safe_text(skill_doc.skill_name)
    else:
        loaded_docs, load_warn = load_skills(root=settings.skill_root)
        skill_loader_warnings.extend(load_warn)
        skill_doc = None
        base_skill_markdown = ""
        target_skill_name = _safe_text(skill_name)
        if not target_skill_name:
            target_skill_name = "skill_auto"

    loaded_docs, extra_load_warnings = load_skills(root=settings.skill_root)
    skill_loader_warnings.extend(extra_load_warnings)
    existing_skill_names = {str(d.skill_name).strip() for d in loaded_docs if str(d.skill_name).strip()}

    retrieve_warnings: list[str] = []
    if pool_override is None:
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
    else:
        pool_result = pool_override
        pool_seconds = 0.0

    task_type_summary = _pool_task_type_summary(list(pool_result.pool or []))
    success_pool, error_pool = _split_trajectory_pool(list(pool_result.pool or []))
    analyst_warnings: list[str] = []
    patches = []
    success_patch_count = 0
    error_patch_count = 0

    t_ana0 = time.perf_counter()
    if analyst_mode in {"success_only", "combined"}:
        if not success_pool:
            analyst_warnings.append("success analyst skipped: no success-like trajectory in pool")
        else:
            success_analyst = SuccessAnalyst(
                model=settings.llm_model,
                api_key=settings.openai_api_key,
                base_url=settings.model_endpoints.llm_base_url or None,
                temperature=0.0,
            )
            success_patches, success_warnings = success_analyst.propose_many(
                skill_markdown=base_skill_markdown,
                trajectories=success_pool,
                max_parallel_analysts=max(1, int(max_parallel_analysts)),
            )
            patches.extend(success_patches)
            analyst_warnings.extend(success_warnings)
            success_patch_count = len(success_patches)

    if analyst_mode in {"error_only", "combined"}:
        if not error_pool:
            error_pool = list(pool_result.pool or [])
            analyst_warnings.append(
                "no explicit failure trajectory detected; error analyst fallback to full trajectory pool"
            )
        error_analyst = ErrorAnalyst(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            base_url=settings.model_endpoints.llm_base_url or None,
            temperature=0.0,
        )
        error_patches, error_warnings = error_analyst.propose_many(
            skill_markdown=base_skill_markdown,
            trajectories=error_pool,
            max_parallel_analysts=max(1, int(max_parallel_analysts)),
        )
        patches.extend(error_patches)
        analyst_warnings.extend(error_warnings)
        error_patch_count = len(error_patches)

    analyst_seconds = time.perf_counter() - t_ana0

    t_merge0 = time.perf_counter()
    merge_result = hierarchical_merge_success_patches(
        patches,
        merge_batch_size=max(1, int(merge_batch_size)),
    )
    merge_seconds = time.perf_counter() - t_merge0

    t_apply0 = time.perf_counter()
    rewrite_result = _llm_rewrite_skill_markdown(
        settings=settings,
        mode=mode,
        target_skill_name=target_skill_name,
        current_skill_markdown=base_skill_markdown,
        task_type_summary=task_type_summary,
        pool=list(pool_result.pool or []),
        merged_rules=merge_result.rules,
        existing_names=existing_skill_names,
    )
    final_skill_name = _safe_text(rewrite_result["skill_name"])
    if mode == "update":
        skill_md_path = Path(skill_doc.skill_md_path) if skill_doc is not None else (Path(settings.skill_root) / final_skill_name / "SKILL.md")
        created = False
    else:
        skill_md_path = Path(settings.skill_root) / final_skill_name / "SKILL.md"
        created = not skill_md_path.exists()
    if not dry_run:
        skill_md_path.parent.mkdir(parents=True, exist_ok=True)
        skill_md_path.write_text(_safe_text(rewrite_result["skill_markdown"]), encoding="utf-8")
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
        "mode": mode,
        "analyst_mode": analyst_mode,
        "dry_run": bool(dry_run),
        "skill_name": final_skill_name,
        "anchor_trajectory_id": anchor_trajectory_id,
        "trajectory_pool": {
            "anchor": pool_result.anchor.trajectory_id,
            "neighbor_count": len(pool_result.neighbors),
            "pool_count": len(pool_result.pool),
            "trajectory_min_score": (
                float(trajectory_min_score) if trajectory_min_score is not None else None
            ),
            "retrieved_trajectory_ids": pool_result.retrieved_trajectory_ids,
            "retrieved_trajectory_scores": pool_result.retrieved_trajectory_scores,
        },
        "patch_summary": {
            "proposed": len(patches),
            "success_proposed": success_patch_count,
            "error_proposed": error_patch_count,
            "merged_rules": merge_result.merged_rule_count,
            "merge_levels": merge_result.levels,
            "merge_batch_size": merge_result.merge_batch_size,
        },
        "apply_summary": {
            "skill_md_path": str(skill_md_path),
            "reference_path": None,
            "rule_count": len(merge_result.rules),
            "description_updated": True,
            "replaced_existing_block": mode == "update",
            "created": bool(created and not dry_run),
            "description": rewrite_result["description"],
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
        description="Skill evolve in update/create mode from an anchor trajectory.",
    )
    p.add_argument(
        "--mode",
        default="update",
        choices=["update", "create"],
        help="Evolve mode: update existing skill or create a new skill",
    )
    p.add_argument(
        "--skill-name",
        required=False,
        default=None,
        help="Target skill name for update mode, or optional name seed for create mode",
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
        "--exclude-anchor",
        action="store_true",
        help="Exclude anchor trajectory from analyst pool",
    )
    p.add_argument(
        "--analyst-mode",
        default="success_only",
        choices=["success_only", "error_only", "combined"],
        help="Choose analysts used during patch proposal",
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
        mode=args.mode,
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

