from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.visualize_intertrajectory import build_parser, run_visualize

pytestmark = pytest.mark.unit


def _fake_settings() -> SimpleNamespace:
    return SimpleNamespace(
        intertrajectory_edge_rel_type="INTERTRAJ_SIMILAR",
        intertrajectory_pending_rel_type="INTERTRAJ_ACTIVATED_PENDING",
    )


def test_build_parser_defaults() -> None:
    args = build_parser().parse_args([])
    assert args.account_id == "account-local"
    assert args.agent_id == "agent-local"
    assert args.scope == "agent"
    assert args.node_limit == 200
    assert args.edge_limit_per_node == 64
    assert args.png_output is None
    assert args.png_title == "AMC Inter-Trajectory Graph"
    assert args.png_dpi == 180


def test_run_visualize_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli.visualize_intertrajectory.load_settings",
        lambda config_path=None: _fake_settings(),
    )

    class _FakeWriter:
        def __init__(self) -> None:
            self.closed = False
            self.kwargs = None

        def describe_intertrajectory_graph(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs
            return {"node_count": 1, "edge_count": 0, "nodes": [], "edges": []}

        def close(self) -> None:
            self.closed = True

    fake_writer = _FakeWriter()
    monkeypatch.setattr(
        "cli.visualize_intertrajectory.build_graph_store_writer",
        lambda settings: fake_writer,
    )

    out = run_visualize(
        account_id="acc",
        agent_id="agent-a",
        scope="agent",
        owner_space=None,
        node_limit=100,
        edge_limit_per_node=10,
        config_path=None,
    )
    assert out["status"] == "ok"
    assert out["owner_space"] == "agent-a"
    assert out["graph"]["node_count"] == 1
    assert fake_writer.kwargs["account_id"] == "acc"
    assert fake_writer.kwargs["scope"] == "agent"
    assert fake_writer.kwargs["owner_space"] == "agent-a"
    assert fake_writer.kwargs["node_limit"] == 100
    assert fake_writer.kwargs["edge_limit_per_node"] == 10
    assert fake_writer.closed is True


def test_run_visualize_renders_png_when_requested(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli.visualize_intertrajectory.load_settings",
        lambda config_path=None: _fake_settings(),
    )

    class _FakeWriter:
        def describe_intertrajectory_graph(self, **kwargs):  # noqa: ANN003
            return {"node_count": 1, "edge_count": 0, "nodes": [], "edges": []}

    monkeypatch.setattr(
        "cli.visualize_intertrajectory.build_graph_store_writer",
        lambda settings: _FakeWriter(),
    )

    captured: dict[str, object] = {}

    def _fake_render_intertrajectory_png(*, graph_summary, output_path, title, dpi):  # noqa: ANN001, ANN202
        captured["graph_summary"] = graph_summary
        captured["output_path"] = output_path
        captured["title"] = title
        captured["dpi"] = dpi
        return "/tmp/intertrajectory.png"

    monkeypatch.setattr("cli.visualize_intertrajectory._render_intertrajectory_png", _fake_render_intertrajectory_png)

    out = run_visualize(
        account_id="acc",
        agent_id="agent-a",
        scope="agent",
        owner_space=None,
        node_limit=100,
        edge_limit_per_node=10,
        config_path=None,
        png_output="./intertrajectory.png",
        png_title="test title",
        png_dpi=144,
    )
    assert out["png_output"] == "/tmp/intertrajectory.png"
    assert captured["output_path"] == "./intertrajectory.png"
    assert captured["title"] == "test title"
    assert captured["dpi"] == 144


def test_run_visualize_requires_owner_space_for_non_agent_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli.visualize_intertrajectory.load_settings",
        lambda config_path=None: _fake_settings(),
    )
    monkeypatch.setattr(
        "cli.visualize_intertrajectory.build_graph_store_writer",
        lambda settings: object(),
    )
    with pytest.raises(ValueError, match="owner_space is required"):
        run_visualize(
            account_id="acc",
            agent_id="agent-a",
            scope="team",
            owner_space=None,
            node_limit=100,
            edge_limit_per_node=10,
            config_path=None,
        )

