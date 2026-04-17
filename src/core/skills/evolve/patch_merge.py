"""Hierarchical merge for success patches."""

from __future__ import annotations

from dataclasses import dataclass

from core.skills.evolve.types import MergedSuccessRule, SuccessPatch


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _merge_title(a: str, b: str) -> str:
    if len(str(b or "").strip()) > len(str(a or "").strip()):
        return str(b or "").strip()
    return str(a or "").strip()


@dataclass
class _MergeNode:
    by_rule: dict[str, MergedSuccessRule]


@dataclass
class MergeResult:
    rules: list[MergedSuccessRule]
    levels: int
    input_patch_count: int
    merged_rule_count: int
    merge_batch_size: int


def _node_from_patch(patch: SuccessPatch) -> _MergeNode:
    key = _norm(patch.sop) or _norm(patch.title) or patch.trajectory_id
    rule = MergedSuccessRule(
        title=str(patch.title).strip() or f"Success pattern from {patch.trajectory_id}",
        sop=str(patch.sop).strip() or str(patch.evidence).strip() or "stable successful workflow",
        checklist=[str(x).strip() for x in patch.checklist if str(x).strip()],
        evidence_trajectory_ids=[patch.trajectory_id],
        support_count=1,
    )
    return _MergeNode(by_rule={key: rule})


def _merge_group(nodes: list[_MergeNode]) -> _MergeNode:
    merged: dict[str, MergedSuccessRule] = {}
    for node in nodes:
        for key, incoming in node.by_rule.items():
            current = merged.get(key)
            if current is None:
                merged[key] = MergedSuccessRule(
                    title=incoming.title,
                    sop=incoming.sop,
                    checklist=list(incoming.checklist),
                    evidence_trajectory_ids=list(incoming.evidence_trajectory_ids),
                    support_count=int(incoming.support_count),
                )
                continue
            current.title = _merge_title(current.title, incoming.title)
            current.sop = current.sop if len(current.sop) >= len(incoming.sop) else incoming.sop
            seen = set(current.checklist)
            for item in incoming.checklist:
                if item not in seen:
                    current.checklist.append(item)
                    seen.add(item)
            current_evidence = set(current.evidence_trajectory_ids)
            for tid in incoming.evidence_trajectory_ids:
                if tid not in current_evidence:
                    current.evidence_trajectory_ids.append(tid)
                    current_evidence.add(tid)
            current.support_count = len(current.evidence_trajectory_ids)
    return _MergeNode(by_rule=merged)


def hierarchical_merge_success_patches(
    patches: list[SuccessPatch],
    *,
    merge_batch_size: int = 8,
) -> MergeResult:
    if not patches:
        return MergeResult(
            rules=[],
            levels=0,
            input_patch_count=0,
            merged_rule_count=0,
            merge_batch_size=max(1, int(merge_batch_size)),
        )
    batch_size = max(1, int(merge_batch_size))
    level_nodes = [_node_from_patch(p) for p in patches]
    levels = 0
    while len(level_nodes) > 1:
        next_nodes: list[_MergeNode] = []
        for i in range(0, len(level_nodes), batch_size):
            group = level_nodes[i : i + batch_size]
            next_nodes.append(_merge_group(group))
        level_nodes = next_nodes
        levels += 1
    final_rules = list(level_nodes[0].by_rule.values())
    final_rules.sort(key=lambda x: (int(x.support_count), len(x.checklist)), reverse=True)
    return MergeResult(
        rules=final_rules,
        levels=max(1, levels),
        input_patch_count=len(patches),
        merged_rule_count=len(final_rules),
        merge_batch_size=batch_size,
    )

