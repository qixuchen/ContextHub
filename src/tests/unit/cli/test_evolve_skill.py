from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.evolve_skill import build_parser, run_evolve
from core.skills.evolve.types import MergedSuccessRule, SuccessPatch, TrajectoryContext
from core.skills.skill_loader import SkillDoc

pytestmark = pytest.mark.unit


def test_build_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args(["--skill-name", "skill_a", "--anchor-trajectory-id", "traj-1"])
    assert args.mode == "update"
    assert args.top_k == 8
    assert args.merge_batch_size == 8
    assert args.analyst_mode == "success_only"


def _build_llm_json_output(*, name: str, description: str = "llm desc") -> str:
    return json.dumps(
        {
            "skill_name": name,
            "description": description,
            "scope": "scope",
            "when_to_use": ["when use"],
            "when_not_to_use": ["when not"],
            "workflow": ["step one", "step two"],
            "checklist": ["check one", "check two"],
            "evidence_trajectories": ["`traj-success`: evidence"],
        },
        ensure_ascii=False,
    )


def _setup_common(monkeypatch, tmp_path: Path, *, chat_completion_fn=None) -> Path:  # noqa: ANN001
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("---\nname: skill_a\ndescription: desc\n---\n# body\n", encoding="utf-8")
    skill_doc = SkillDoc(
        skill_name="skill_a",
        description="desc",
        root_path=str(tmp_path),
        skill_md_path=str(skill_md),
        content_sha256="sha",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    settings = SimpleNamespace(
        skill_root=str(tmp_path),
        storage=SimpleNamespace(localfs_root=str(tmp_path / "content")),
        llm_model="dummy-model",
        openai_api_key="dummy-key",
        model_endpoints=SimpleNamespace(llm_base_url="", embedder_base_url=""),
        commit=SimpleNamespace(temporal_fallback_edge=True, reasoning_min_confidence=0.55),
    )
    monkeypatch.setattr("cli.evolve_skill.load_settings", lambda config_path=None: settings)
    monkeypatch.setattr("cli.evolve_skill._load_skill_doc", lambda **kwargs: (skill_doc, []))
    monkeypatch.setattr("cli.evolve_skill._build_retrieve_service", lambda settings: (object(), []))

    class _FakePoolBuilder:
        def __init__(self, repo, retrieve_service):  # noqa: ANN001
            del repo, retrieve_service

        def build_success_pool(self, **kwargs):  # noqa: ANN001
            del kwargs
            success = TrajectoryContext(
                trajectory_id="traj-success",
                task_id="task-1",
                abstract="a",
                overview="b",
                trajectory=[],
                outcome_label="success",
            )
            fail = TrajectoryContext(
                trajectory_id="traj-fail",
                task_id="task-2",
                abstract="failed at step 2",
                overview="error: timeout",
                trajectory=[],
                outcome_label="fail",
            )
            return SimpleNamespace(
                anchor=success,
                neighbors=[fail],
                pool=[success, fail],
                retrieved_trajectory_ids=["traj-fail"],
                retrieved_trajectory_scores=[{"trajectory_id": "traj-fail", "total_score": 0.9}],
                warnings=[],
            )

    class _FakeSuccessAnalyst:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def propose_many(self, **kwargs):  # noqa: ANN001
            trajectories = kwargs["trajectories"]
            return (
                [
                    SuccessPatch(
                        trajectory_id=t.trajectory_id,
                        title=f"s-{t.trajectory_id}",
                        sop="do x",
                        checklist=["c1"],
                    )
                    for t in trajectories
                ],
                [],
            )

    class _FakeErrorAnalyst:
        def __init__(self, **kwargs):  # noqa: ANN001
            del kwargs

        def propose_many(self, **kwargs):  # noqa: ANN001
            trajectories = kwargs["trajectories"]
            return (
                [
                    SuccessPatch(
                        trajectory_id=t.trajectory_id,
                        title=f"e-{t.trajectory_id}",
                        sop="avoid y",
                        checklist=["c2"],
                    )
                    for t in trajectories
                ],
                [],
            )

    monkeypatch.setattr("cli.evolve_skill.TrajectoryPoolBuilder", _FakePoolBuilder)
    monkeypatch.setattr("cli.evolve_skill.SuccessAnalyst", _FakeSuccessAnalyst)
    monkeypatch.setattr("cli.evolve_skill.ErrorAnalyst", _FakeErrorAnalyst)
    monkeypatch.setattr(
        "cli.evolve_skill.hierarchical_merge_success_patches",
        lambda patches, merge_batch_size: SimpleNamespace(
            rules=[
                MergedSuccessRule(
                    title=str(p.title),
                    sop=str(p.sop),
                    checklist=list(p.checklist),
                    evidence_trajectory_ids=[p.trajectory_id],
                    support_count=1,
                )
                for p in patches
            ],
            merged_rule_count=len(patches),
            levels=1,
            merge_batch_size=merge_batch_size,
        ),
    )
    monkeypatch.setattr("cli.evolve_skill.OpenAI", lambda **kwargs: object())
    monkeypatch.setattr("cli.evolve_skill.provider_key", lambda **kwargs: "p")

    def _fake_chat_completion_with_retry(**kwargs):  # noqa: ANN003
        payload = json.loads(kwargs["messages"][1]["content"])
        mode = payload.get("mode")
        target_name = payload.get("target_skill_name") or "skill_auto"
        name = target_name if mode == "update" else "skill_created_by_llm"
        content = _build_llm_json_output(name=name, description="llm desc")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        return response, 0

    monkeypatch.setattr(
        "cli.evolve_skill.chat_completion_with_retry",
        chat_completion_fn or _fake_chat_completion_with_retry,
    )
    return skill_md


def test_run_evolve_supports_error_only(monkeypatch, tmp_path: Path) -> None:
    _setup_common(monkeypatch, tmp_path)
    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="update",
        analyst_mode="error_only",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert out["mode"] == "update"
    assert out["analyst_mode"] == "error_only"
    assert out["patch_summary"]["success_proposed"] == 0
    assert out["patch_summary"]["error_proposed"] == 1
    assert out["patch_summary"]["proposed"] == 1


def test_run_evolve_supports_combined(monkeypatch, tmp_path: Path) -> None:
    _setup_common(monkeypatch, tmp_path)
    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="update",
        analyst_mode="combined",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert out["mode"] == "update"
    assert out["analyst_mode"] == "combined"
    assert out["patch_summary"]["success_proposed"] == 1
    assert out["patch_summary"]["error_proposed"] == 1
    assert out["patch_summary"]["proposed"] == 2


def test_run_evolve_supports_create_mode(monkeypatch, tmp_path: Path) -> None:
    _setup_common(monkeypatch, tmp_path)
    out = run_evolve(
        skill_name=None,
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="create",
        analyst_mode="combined",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert out["mode"] == "create"
    assert out["analyst_mode"] == "combined"
    assert out["skill_name"] == "skill_created_by_llm"
    assert out["apply_summary"]["skill_md_path"].endswith("skill_created_by_llm/SKILL.md")
    assert out["patch_summary"]["proposed"] == 2


def test_run_evolve_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match="unsupported analyst_mode"):
        run_evolve(
            skill_name="skill_a",
            anchor_trajectory_id="traj-1",
            account_id="acc",
            agent_id="agent",
            top_k=8,
            trajectory_min_score=0.7,
            include_anchor=True,
            mode="update",
            analyst_mode="unknown_mode",
            merge_batch_size=8,
            max_parallel_analysts=4,
            dry_run=True,
            config_path=None,
        )


def test_run_evolve_rejects_invalid_evolve_mode() -> None:
    with pytest.raises(ValueError, match="unsupported mode"):
        run_evolve(
            skill_name="skill_a",
            anchor_trajectory_id="traj-1",
            account_id="acc",
            agent_id="agent",
            top_k=8,
            trajectory_min_score=0.7,
            include_anchor=True,
            mode="unknown_mode",
            analyst_mode="combined",
            merge_batch_size=8,
            max_parallel_analysts=4,
            dry_run=True,
            config_path=None,
        )


def test_run_evolve_retries_on_invalid_markdown_then_succeeds(monkeypatch, tmp_path: Path) -> None:
    state = {"calls": 0}

    def _flaky_chat_completion_with_retry(**kwargs):  # noqa: ANN003
        payload = json.loads(kwargs["messages"][1]["content"])
        mode = payload.get("mode")
        target_name = payload.get("target_skill_name") or "skill_auto"
        name = target_name if mode == "update" else "skill_created_by_llm"
        state["calls"] += 1
        if state["calls"] == 1:
            bad_content = json.dumps(
                {
                    "skill_name": name,
                    "description": "llm desc",
                    "scope": "scope",
                    "when_to_use": ["when use"],
                    "when_not_to_use": ["when not"],
                    "workflow": ["only one step"],
                    "checklist": ["only one check"],
                    "evidence_trajectories": [],
                },
                ensure_ascii=False,
            )
            response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=bad_content))])
            return response, 0
        good_content = _build_llm_json_output(name=name, description="llm desc")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=good_content))])
        return response, 0

    _setup_common(monkeypatch, tmp_path, chat_completion_fn=_flaky_chat_completion_with_retry)
    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="update",
        analyst_mode="combined",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert state["calls"] == 2


def test_run_evolve_raises_on_invalid_markdown_after_retries(monkeypatch, tmp_path: Path) -> None:
    state = {"calls": 0}

    def _always_bad_chat_completion_with_retry(**kwargs):  # noqa: ANN003
        payload = json.loads(kwargs["messages"][1]["content"])
        mode = payload.get("mode")
        target_name = payload.get("target_skill_name") or "skill_auto"
        name = target_name if mode == "update" else "skill_created_by_llm"
        state["calls"] += 1
        bad_content = json.dumps(
            {
                "skill_name": name,
                "description": "llm desc",
                "scope": "",
                "when_to_use": [],
                "when_not_to_use": [],
                "workflow": [],
                "checklist": [],
                "evidence_trajectories": [],
            },
            ensure_ascii=False,
        )
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=bad_content))])
        return response, 0

    _setup_common(monkeypatch, tmp_path, chat_completion_fn=_always_bad_chat_completion_with_retry)
    with pytest.raises(ValueError, match="llm rewrite output invalid after 3 attempts"):
        run_evolve(
            skill_name="skill_a",
            anchor_trajectory_id="traj-1",
            account_id="acc",
            agent_id="agent",
            top_k=8,
            trajectory_min_score=0.7,
            include_anchor=True,
            mode="update",
            analyst_mode="combined",
            merge_batch_size=8,
            max_parallel_analysts=4,
            dry_run=True,
            config_path=None,
        )
    assert state["calls"] == 3


def test_run_evolve_uses_structured_response_format(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def _chat_with_capture(**kwargs):  # noqa: ANN003
        captured["response_format"] = kwargs.get("response_format")
        payload = json.loads(kwargs["messages"][1]["content"])
        mode = payload.get("mode")
        target_name = payload.get("target_skill_name") or "skill_auto"
        name = target_name if mode == "update" else "skill_created_by_llm"
        content = _build_llm_json_output(name=name, description="llm desc")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        return response, 0

    _setup_common(monkeypatch, tmp_path, chat_completion_fn=_chat_with_capture)
    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="update",
        analyst_mode="combined",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert isinstance(captured.get("response_format"), dict)
    assert captured["response_format"]["type"] == "json_schema"


def test_run_evolve_falls_back_when_response_format_unsupported(monkeypatch, tmp_path: Path) -> None:
    state = {"calls": 0, "response_formats": []}

    def _chat_with_unsupported_then_ok(**kwargs):  # noqa: ANN003
        state["calls"] += 1
        state["response_formats"].append(kwargs.get("response_format"))
        if state["calls"] == 1:
            raise RuntimeError("response_format json_schema is not supported")
        payload = json.loads(kwargs["messages"][1]["content"])
        mode = payload.get("mode")
        target_name = payload.get("target_skill_name") or "skill_auto"
        name = target_name if mode == "update" else "skill_created_by_llm"
        content = _build_llm_json_output(name=name, description="llm desc")
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        return response, 0

    _setup_common(monkeypatch, tmp_path, chat_completion_fn=_chat_with_unsupported_then_ok)
    out = run_evolve(
        skill_name="skill_a",
        anchor_trajectory_id="traj-1",
        account_id="acc",
        agent_id="agent",
        top_k=8,
        trajectory_min_score=0.7,
        include_anchor=True,
        mode="update",
        analyst_mode="combined",
        merge_batch_size=8,
        max_parallel_analysts=4,
        dry_run=True,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert state["calls"] == 2
    assert isinstance(state["response_formats"][0], dict)
    assert state["response_formats"][1] is None

