from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.commit.summary_llm import LLMTrajectorySummarizer

pytestmark = pytest.mark.unit


def test_summary_prompt_requires_detailed_long_overview() -> None:
    prompt = LLMTrajectorySummarizer._summary_prompt()
    assert "1200-1800" in prompt
    assert "step-level details" in prompt
    assert "tool/action used" in prompt
    assert "task goal and completion status" in prompt


def test_summarize_sends_detailed_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_chat_completion_with_retry(**kwargs):  # noqa: ANN003
        captured["messages"] = kwargs["messages"]
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"l0":"a","l1":"b"}'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )
        return resp, 0

    monkeypatch.setattr("core.commit.summary_llm.chat_completion_with_retry", _fake_chat_completion_with_retry)
    summarizer = LLMTrajectorySummarizer(api_key="dummy", model="dummy-model")
    l0, l1, outcome = summarizer.summarize(
        [{"Step": 1, "meta": {"role": "AIMessage"}, "Action": "x()"}]
    )
    assert l0 == "a"
    assert l1 == "b"
    assert outcome["label"] is None
    messages = captured["messages"]
    assert isinstance(messages, list) and messages
    system_prompt = str(messages[0]["content"])
    assert "1200-1800" in system_prompt
    assert "step-level details" in system_prompt
    assert "tool/action used" in system_prompt
    assert "task goal and completion status" in system_prompt

