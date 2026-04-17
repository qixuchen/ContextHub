"""Route between updating an existing skill and creating a new one."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from openai import OpenAI

from core.commit.llm_runtime import chat_completion_with_retry, provider_key
from core.retrieve.skill_retriever import SkillHit
from core.skills.evolve.types import TrajectoryContext

DecisionFn = Callable[[SkillHit | None, str, list[TrajectoryContext]], dict[str, Any]]


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


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


def _clamp_confidence(value: Any) -> float:
    try:
        score = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, score))


@dataclass
class SkillRouteDecision:
    decision: str
    confidence: float
    scope_match_level: str
    reasoning: str
    task_type_summary: str
    suggested_skill_name: str = ""


@dataclass
class SkillRouter:
    model: str
    api_key: str
    base_url: str | None = None
    confidence_threshold: float = 0.7
    decision_fn: DecisionFn | None = None

    def _client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    @staticmethod
    def summarize_task_type(pool: list[TrajectoryContext]) -> str:
        parts: list[str] = []
        for item in pool:
            if item.task_id:
                parts.append(item.task_id)
            elif item.abstract:
                parts.append(item.abstract)
            elif item.overview:
                parts.append(item.overview)
        return " | ".join(parts) or "trajectory task type summary unavailable"

    @staticmethod
    def _candidate_scope_text(candidate_skill: SkillHit | None, candidate_skill_md: str) -> str:
        if candidate_skill is None:
            return ""
        desc = _safe_text(candidate_skill.description)
        md = _safe_text(candidate_skill_md)
        return f"{desc}\n\n{md}".strip()

    def _heuristic_route(
        self,
        *,
        candidate_skill: SkillHit | None,
        candidate_scope_text: str,
        pool: list[TrajectoryContext],
    ) -> SkillRouteDecision:
        task_summary = self.summarize_task_type(pool)
        if candidate_skill is None:
            return SkillRouteDecision(
                decision="create",
                confidence=1.0,
                scope_match_level="low",
                reasoning="no candidate skill retrieved",
                task_type_summary=task_summary,
                suggested_skill_name=task_summary,
            )
        scope_tokens = {
            tok for tok in _safe_text(candidate_scope_text).lower().replace("\n", " ").split() if len(tok) >= 4
        }
        traj_tokens = {
            tok
            for tok in _safe_text(task_summary).lower().replace("\n", " ").split()
            if len(tok) >= 4
        }
        overlap = len(scope_tokens.intersection(traj_tokens))
        if overlap >= 6:
            level = "high"
            confidence = 0.72
            decision = "update"
        elif overlap >= 3:
            level = "medium"
            confidence = 0.58
            decision = "create"
        else:
            level = "low"
            confidence = 0.9
            decision = "create"
        return SkillRouteDecision(
            decision=decision,
            confidence=confidence,
            scope_match_level=level,
            reasoning=f"heuristic token overlap={overlap}",
            task_type_summary=task_summary,
            suggested_skill_name=task_summary,
        )

    def _llm_route(
        self,
        *,
        candidate_skill: SkillHit | None,
        candidate_scope_text: str,
        pool: list[TrajectoryContext],
    ) -> SkillRouteDecision:
        task_summary = self.summarize_task_type(pool)
        if candidate_skill is None:
            return SkillRouteDecision(
                decision="create",
                confidence=1.0,
                scope_match_level="low",
                reasoning="no candidate skill retrieved",
                task_type_summary=task_summary,
                suggested_skill_name=task_summary,
            )
        prompt = (
            "You are a skill routing judge.\n"
            "Given one candidate skill and a trajectory pool summary, decide UPDATE or CREATE.\n"
            "Hard rule:\n"
            "- Return update ONLY when the candidate skill scope is highly related to trajectory task type.\n"
            "- If relation is medium/low/uncertain, MUST return create.\n"
            "For task_type_summary, describe the concrete shared task solved by these trajectories.\n"
            "Prefer specific, operational details (goal, key objects/entities, mandatory tool/step sequence, success checks),\n"
            "and avoid generic summaries like domain-level labels.\n"
            "Also propose suggested_skill_name based on that concrete shared task (short phrase, task-oriented).\n"
            "Output JSON only with keys: decision, confidence, scope_match_level, reasoning, task_type_summary, suggested_skill_name."
        )
        payload = {
            "candidate_skill": {
                "skill_name": candidate_skill.skill_name,
                "description": candidate_skill.description,
                "scope_text": candidate_scope_text,
                "score": float(candidate_skill.score),
            },
            "trajectory_pool": [
                {
                    "trajectory_id": t.trajectory_id,
                    "task_id": t.task_id,
                    "abstract": t.abstract,
                    "overview": t.overview,
                }
                for t in pool
            ],
            "task_type_summary": task_summary,
        }
        client = self._client()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        resp, _ = chat_completion_with_retry(
            client=client,
            provider_id=provider_key(base_url=self.base_url, model=self.model),
            model=self.model,
            temperature=0.0,
            messages=messages,
        )
        data = _extract_json_payload(_safe_text(resp.choices[0].message.content))
        decision = _safe_text(data.get("decision")).lower()
        level = _safe_text(data.get("scope_match_level")).lower()
        confidence = _clamp_confidence(data.get("confidence"))
        reasoning = _safe_text(data.get("reasoning")) or "llm decision"
        out_summary = _safe_text(data.get("task_type_summary")) or task_summary
        suggested_name = _safe_text(data.get("suggested_skill_name")) or out_summary
        return SkillRouteDecision(
            decision=decision,
            confidence=confidence,
            scope_match_level=level,
            reasoning=reasoning,
            task_type_summary=out_summary,
            suggested_skill_name=suggested_name,
        )

    def _apply_hard_guard(self, decision: SkillRouteDecision, has_candidate: bool) -> SkillRouteDecision:
        raw_decision = decision.decision.lower().strip()
        level = decision.scope_match_level.lower().strip()
        if raw_decision not in {"update", "create"}:
            raw_decision = "create"
        if not has_candidate:
            raw_decision = "create"
            level = "low"
        if raw_decision == "update":
            if level != "high" or float(decision.confidence) < float(self.confidence_threshold):
                reason = decision.reasoning or ""
                suffix = "hard-guard forced create: scope not high/confidence too low"
                reason = f"{reason}; {suffix}" if reason else suffix
                return SkillRouteDecision(
                    decision="create",
                    confidence=decision.confidence,
                    scope_match_level=level or "low",
                    reasoning=reason,
                    task_type_summary=decision.task_type_summary,
                    suggested_skill_name=decision.suggested_skill_name,
                )
        return SkillRouteDecision(
            decision=raw_decision,
            confidence=decision.confidence,
            scope_match_level=level or "low",
            reasoning=decision.reasoning,
            task_type_summary=decision.task_type_summary,
            suggested_skill_name=decision.suggested_skill_name,
        )

    def decide(
        self,
        *,
        candidate_skill: SkillHit | None,
        pool: list[TrajectoryContext],
    ) -> SkillRouteDecision:
        candidate_md = ""
        if candidate_skill is not None and candidate_skill.path:
            skill_md = Path(candidate_skill.path) / "SKILL.md"
            if skill_md.exists():
                try:
                    candidate_md = skill_md.read_text(encoding="utf-8")
                except Exception:
                    candidate_md = ""
        scope_text = self._candidate_scope_text(candidate_skill, candidate_md)
        if self.decision_fn is not None:
            data = self.decision_fn(candidate_skill, scope_text, pool)
            raw = SkillRouteDecision(
                decision=_safe_text(data.get("decision")).lower(),
                confidence=_clamp_confidence(data.get("confidence")),
                scope_match_level=_safe_text(data.get("scope_match_level")).lower(),
                reasoning=_safe_text(data.get("reasoning")),
                task_type_summary=_safe_text(data.get("task_type_summary")) or self.summarize_task_type(pool),
                suggested_skill_name=_safe_text(data.get("suggested_skill_name")),
            )
            return self._apply_hard_guard(raw, has_candidate=candidate_skill is not None)
        if self.api_key.strip():
            try:
                raw = self._llm_route(
                    candidate_skill=candidate_skill,
                    candidate_scope_text=scope_text,
                    pool=pool,
                )
            except Exception:
                raw = self._heuristic_route(
                    candidate_skill=candidate_skill,
                    candidate_scope_text=scope_text,
                    pool=pool,
                )
        else:
            raw = self._heuristic_route(
                candidate_skill=candidate_skill,
                candidate_scope_text=scope_text,
                pool=pool,
            )
        return self._apply_hard_guard(raw, has_candidate=candidate_skill is not None)

