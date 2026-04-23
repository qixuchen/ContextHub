"""Trajectory-level L0/L1 summary helpers for Phase 1."""

from __future__ import annotations

from typing import Any, Callable


def _tool_name(action: str) -> str:
    raw = str(action or "").strip()
    if not raw:
        return "unknown_tool"
    idx = raw.find("(")
    if idx <= 0:
        return raw[:24]
    return raw[:idx].strip() or "unknown_tool"


def _short_text(text: str, limit: int = 160) -> str:
    s = str(text or "").replace("\n", " ").strip()
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


def _summarize_rule_based(steps: list[dict[str, Any]]) -> tuple[str, str]:
    # L0: short one-liner for quick preview/recall metadata.
    ai_actions = [str(s.get("Action") or "") for s in steps if (s.get("meta") or {}).get("role") == "AIMessage"]
    tool_results = [
        str(s.get("Action_result") or "") for s in steps if (s.get("meta") or {}).get("role") == "ToolMessage"
    ]

    tools: list[str] = []
    for action in ai_actions:
        if action.startswith("local_db_sql("):
            tools.append("local_db_sql")
        elif action:
            tools.append("unknown_tool")
    unique_tools = sorted(set(tools))
    l0 = f"Trajectory with {len(ai_actions)} actions, tools={','.join(unique_tools) or 'none'}."

    failures = sum(1 for r in tool_results if "'status': 'failed'" in r or '"status": "failed"' in r)
    successes = sum(1 for r in tool_results if "'status': 'success'" in r or '"status": "success"' in r)
    # L1: include explicit step-level tool/action/result details for downstream extraction.
    key_lines: list[str] = []
    ai_seq = 0
    max_lines = 12
    for idx, step in enumerate(steps):
        meta = step.get("meta") or {}
        if meta.get("role") != "AIMessage":
            continue
        ai_seq += 1
        action = str(step.get("Action") or "").strip()
        tool = _tool_name(action)
        result = ""
        for j in range(idx + 1, len(steps)):
            next_step = steps[j]
            next_meta = next_step.get("meta") or {}
            if next_meta.get("role") == "AIMessage":
                break
            if next_meta.get("role") == "ToolMessage":
                result = _short_text(str(next_step.get("Action_result") or ""))
                break
        detail = (
            f"{ai_seq}) tool={tool}; action={_short_text(action)}; "
            f"result={result or 'N/A'}"
        )
        key_lines.append(detail)
        if len(key_lines) >= max_lines:
            break
    key_steps = " | ".join(key_lines) if key_lines else "no step-level details extracted"
    l1 = (
        f"Steps={len(steps)}, AI actions={len(ai_actions)}, tool_results={len(tool_results)}, "
        f"successes={successes}, failures={failures}. "
        f"Key steps: {key_steps}. "
        "Rule-based detailed overview for commit/replay and downstream skill extraction."
    )
    return l0, l1


def summarize_trajectory(
    steps: list[dict[str, Any]],
    llm_summarizer: Callable[[list[dict[str, Any]]], tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """
    Prefer LLM summary when available; fall back to deterministic rule-based summary.
    """
    if llm_summarizer is not None:
        try:
            l0, l1 = llm_summarizer(steps)
            if str(l0).strip() and str(l1).strip():
                return str(l0), str(l1)
        except Exception:
            pass
    return _summarize_rule_based(steps)
