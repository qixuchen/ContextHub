"""Shared data types for skill evolve pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class TrajectoryContext:
    trajectory_id: str
    task_id: str
    abstract: str
    overview: str
    trajectory: list[dict[str, Any]]


@dataclass
class SuccessPatch:
    trajectory_id: str
    title: str
    sop: str
    checklist: list[str]
    evidence: str = ""


@dataclass
class MergedSuccessRule:
    title: str
    sop: str
    checklist: list[str]
    evidence_trajectory_ids: list[str]
    support_count: int

