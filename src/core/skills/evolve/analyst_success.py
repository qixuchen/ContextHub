"""Success-only analyst for Phase A skill evolve."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from openai import OpenAI

from core.commit.llm_runtime import chat_completion_with_retry, provider_key
from core.skills.evolve.types import SuccessPatch, TrajectoryContext


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _tool_name_from_action(action: str) -> str:
    raw = _safe_text(action)
    if not raw:
        return ""
    idx = raw.find("(")
    if idx <= 0:
        return raw
    return raw[:idx].strip()


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


ProposalFn = Callable[[str, TrajectoryContext], SuccessPatch]


@dataclass
class SuccessAnalyst:
    model: str
    api_key: str
    base_url: str | None = None
    temperature: float = 0.0
    proposal_fn: ProposalFn | None = None

    def _client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    def _heuristic_patch(self, *, skill_markdown: str, trajectory: TrajectoryContext) -> SuccessPatch:
        del skill_markdown
        tools: list[str] = []
        seen: set[str] = set()
        for step in trajectory.trajectory:
            if not isinstance(step, dict):
                continue
            name = _tool_name_from_action(_safe_text(step.get("Action")))
            if not name or name in seen:
                continue
            tools.append(name)
            seen.add(name)
            if len(tools) >= 4:
                break
        checklist = ["完成关键步骤后执行一次显式结果校验。"]
        if tools:
            checklist.append(f"优先按既有成功轨迹顺序使用工具：{', '.join(tools)}。")
        if trajectory.overview:
            checklist.append("将高频稳定步骤固化为可复用 SOP，避免任务特例化。")
        sop_source = trajectory.overview or trajectory.abstract or f"trajectory {trajectory.trajectory_id}"
        return SuccessPatch(
            trajectory_id=trajectory.trajectory_id,
            title=f"Success pattern from {trajectory.trajectory_id}",
            sop=f"复用该轨迹中的稳定执行路径：{sop_source[:240]}",
            checklist=checklist[:4],
            evidence=(trajectory.abstract or trajectory.overview)[:240],
        )

    def _llm_patch(self, *, skill_markdown: str, trajectory: TrajectoryContext) -> SuccessPatch:
        prompt = (
            "You are a Success Analyst for skill evolution.\n"
            "Given one successful trajectory and current SKILL.md, distill ONE generalizable SOP.\n"
            "Requirements:\n"
            "- The SOP must be reusable across similar tasks, not task-specific.\n"
            "- Keep it concise, actionable, and checklist-oriented.\n"
            "- Output JSON only with keys: title, sop, checklist, evidence.\n"
            "- checklist must be a list of 2-4 short items."
        )
        user_payload = {
            "trajectory_id": trajectory.trajectory_id,
            "task_id": trajectory.task_id,
            "abstract": trajectory.abstract,
            "overview": trajectory.overview,
            "trajectory": trajectory.trajectory[:14],
            "skill_markdown": skill_markdown[:6000],
        }
        client = self._client()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        resp, _ = chat_completion_with_retry(
            client=client,
            provider_id=provider_key(base_url=self.base_url, model=self.model),
            model=self.model,
            temperature=self.temperature,
            messages=messages,
        )
        content = _safe_text(resp.choices[0].message.content)
        data = _extract_json_payload(content)
        title = _safe_text(data.get("title")) or f"Success pattern from {trajectory.trajectory_id}"
        sop = _safe_text(data.get("sop"))
        checklist_raw = data.get("checklist")
        checklist = []
        if isinstance(checklist_raw, list):
            checklist = [_safe_text(x) for x in checklist_raw if _safe_text(x)]
        evidence = _safe_text(data.get("evidence"))
        if not sop:
            raise ValueError("missing sop field in success analyst output")
        if not checklist:
            checklist = ["执行后进行输出验证，确保结果一致。"]
        return SuccessPatch(
            trajectory_id=trajectory.trajectory_id,
            title=title,
            sop=sop,
            checklist=checklist[:4],
            evidence=evidence[:280],
        )

    def propose_patch(self, *, skill_markdown: str, trajectory: TrajectoryContext) -> SuccessPatch:
        if self.proposal_fn is not None:
            return self.proposal_fn(skill_markdown, trajectory)
        if self.api_key.strip():
            try:
                return self._llm_patch(skill_markdown=skill_markdown, trajectory=trajectory)
            except Exception:
                return self._heuristic_patch(skill_markdown=skill_markdown, trajectory=trajectory)
        return self._heuristic_patch(skill_markdown=skill_markdown, trajectory=trajectory)

    def propose_many(
        self,
        *,
        skill_markdown: str,
        trajectories: list[TrajectoryContext],
        max_parallel_analysts: int,
    ) -> tuple[list[SuccessPatch], list[str]]:
        if not trajectories:
            return [], []
        patches: list[SuccessPatch] = []
        warnings: list[str] = []
        workers = max(1, min(int(max_parallel_analysts), len(trajectories)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map = {
                pool.submit(self.propose_patch, skill_markdown=skill_markdown, trajectory=t): t
                for t in trajectories
            }
            for fut in as_completed(fut_map):
                trajectory = fut_map[fut]
                try:
                    patches.append(fut.result())
                except Exception as exc:
                    warnings.append(
                        f"success analyst failed for {trajectory.trajectory_id}: {type(exc).__name__}: {exc}"
                    )
        patches.sort(key=lambda p: p.trajectory_id)
        return patches, warnings

