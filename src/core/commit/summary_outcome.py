"""Helpers to normalize LLM trajectory outcome payload."""

from __future__ import annotations

from typing import Any


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_confidence(value: Any) -> float:
    try:
        score = float(value)
    except Exception:
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def normalize_trajectory_outcome(raw: Any) -> dict[str, Any]:
    """
    Normalize trajectory outcome payload from LLM summary.

    Contract:
    - label: "success" | "fail" | None (fallback must be null-like)
    - reason: string
    - confidence: float in [0, 1]
    """
    if not isinstance(raw, dict):
        return {"label": None, "reason": "", "confidence": 0.0}

    label_raw = _safe_text(raw.get("label")).lower()
    label: str | None
    if label_raw in {"success", "fail"}:
        label = label_raw
    else:
        label = None

    reason = _safe_text(raw.get("reason"))
    confidence = _safe_confidence(raw.get("confidence"))
    return {
        "label": label,
        "reason": reason,
        "confidence": confidence,
    }

