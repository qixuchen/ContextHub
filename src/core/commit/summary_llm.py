"""LLM-based trajectory summarizer for L0/L1 outputs."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from core.commit.llm_runtime import chat_completion_with_retry, provider_key
from core.commit.summary_outcome import normalize_trajectory_outcome


@dataclass
class LLMTrajectorySummarizer:
    api_key: str
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    temperature: float = 0.0
    _thread_local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    @property
    def last_traces(self) -> list[dict[str, Any]]:
        value = getattr(self._thread_local, "last_traces", None)
        if isinstance(value, list):
            return value
        value = []
        self._thread_local.last_traces = value
        return value

    @last_traces.setter
    def last_traces(self, value: list[dict[str, Any]]) -> None:
        self._thread_local.last_traces = list(value)

    def _client(self) -> OpenAI:
        if self.base_url:
            return OpenAI(api_key=self.api_key, base_url=self.base_url)
        return OpenAI(api_key=self.api_key)

    def _extract_json(self, text: str) -> dict[str, Any]:
        raw = text.strip()
        try:
            return json.loads(raw)
        except Exception:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        return {}

    @staticmethod
    def _summary_prompt() -> str:
        return (
            "# ROLE\n"
            "You are an expert AI agent evaluator and summarizer. Your task is to analyze an agent's execution trajectory, "
            "evaluate its success strictly, and generate two Chinese summaries at different levels of detail.\n\n"
            "# OUTPUT FORMAT\n"
            "Return ONLY a valid JSON object. Do not include markdown code blocks (like ```json), greetings, or explanations "
            "outside the JSON.\n"
            "{\n"
            '  "l0": "string (Chinese) - Concise executive summary",\n'
            '  "l1": "string (Chinese) - Highly detailed step-by-step report",\n'
            '  "trajectory_outcome": {\n'
            '    "reason": "string (Chinese) - Step-by-step verification of sub-goals based on evidence",\n'
            '    "confidence": float (0.0 to 1.0),\n'
            '    "label": "success" | "fail"\n'
            "  }\n"
            "}\n\n"
            "# SUMMARIZATION GUIDELINES (CHINESE OUTPUT REQUIRED)\n\n"
            "## l0: Executive Summary\n"
            "- Length: A concise paragraph (approx. 100-180 Chinese characters).\n"
            "- Content: Must clearly state the original task goal, the core execution route taken, and the final outcome.\n\n"
            "## l1: Detailed Execution Report\n"
            "- Length: A highly detailed, comprehensive report (approx. 1200-1800 Chinese characters). Do NOT over-compress.\n"
            "- Structure: Use a structured narrative (e.g., numbered lists for key steps) to improve downstream skill extraction specificity.\n"
            "- Content Requirements:\n"
            "  1. Opening: Explicitly state the task goal and completion status at the very beginning.\n"
            "  2. Step-by-Step Details: For EVERY key step, explicitly state:\n"
            "     - step-level details are mandatory; do not provide only high-level summary.\n"
            "     (a) The tool/action used and its input.\n"
            "     (b) What was done or attempted.\n"
            "     (c) The key observation/result.\n"
            "  3. Error Handling: Include details of any failures, retries, or fixes when present, and explain how the agent found the final successful path.\n"
            "  4. Grounding: Keep all facts strictly grounded in the trajectory. Do NOT invent unseen details.\n\n"
            "# EVALUATION GUIDELINES (trajectory_outcome)\n\n"
            "- Be skeptical by default: Verify query completion with concrete evidence from trajectory steps/results, not assumptions.\n"
            "- Decompose & Verify (in reason): Decompose the user query into required sub-goals/constraints. Check them one by one against the trajectory evidence.\n"
            '- Strict Success: Return "success" ONLY when ALL required query sub-goals are clearly satisfied by evidence.\n'
            '- Strict Failure: If evidence is incomplete/ambiguous, or you cannot confidently verify completion, return "fail". '
            'Any partially completed task MUST be labeled "fail".'
        )

    def summarize(
        self,
        steps: list[dict[str, Any]],
        query: str | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        self.last_traces = []
        prompt = self._summary_prompt()
        client = self._client()
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(
                    {"query": str(query or ""), "trajectory": steps},
                    ensure_ascii=False,
                ),
            },
        ]
        resp, retries = chat_completion_with_retry(
            client=client,
            provider_id=provider_key(base_url=self.base_url, model=self.model),
            model=self.model,
            temperature=self.temperature,
            messages=messages,
        )
        content = resp.choices[0].message.content or "{}"
        data = self._extract_json(content)
        l0 = str(data.get("l0") or "").strip()
        l1 = str(data.get("l1") or "").strip()
        trajectory_outcome = normalize_trajectory_outcome(data.get("trajectory_outcome"))
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        self.last_traces.append(
            {
                "call_type": "summary",
                "model": self.model,
                "base_url": self.base_url or "",
                "temperature": self.temperature,
                "raw_response_text": content,
                "parsed_result": {
                    "l0": l0,
                    "l1": l1,
                    "trajectory_outcome": trajectory_outcome,
                },
                "error": "",
                "retry_count": int(retries),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }
        )
        if not l0 or not l1:
            raise ValueError("llm summary output missing l0/l1")
        return l0, l1, trajectory_outcome

