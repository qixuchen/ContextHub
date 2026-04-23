"""Visualize inter-trajectory graph nodes/edges and activate counts."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from app.config import load_settings
from infra.storage.graph.factory import build_graph_store_writer


def _render_intertrajectory_png(
    *,
    graph_summary: dict[str, Any],
    output_path: str,
    title: str,
    dpi: int,
) -> str:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import networkx as nx
    except Exception as exc:  # pragma: no cover - depends on runtime environment
        raise RuntimeError("PNG visualization requires matplotlib and networkx.") from exc

    output = Path(output_path).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output.parent.mkdir(parents=True, exist_ok=True)

    nodes = list(graph_summary.get("nodes") or [])
    edges = list(graph_summary.get("edges") or [])
    graph = nx.Graph()

    for node in nodes:
        trajectory_id = str(node.get("trajectory_id") or "").strip()
        if not trajectory_id:
            continue
        activate_count = int(node.get("activate_count", 0) or 0)
        graph.add_node(trajectory_id, activate_count=activate_count)

    for edge in edges:
        src = str(edge.get("src_trajectory_id") or "").strip()
        dst = str(edge.get("dst_trajectory_id") or "").strip()
        if not src or not dst:
            continue
        score_total = float(edge.get("score_total", 0.0) or 0.0)
        graph.add_edge(src, dst, score_total=score_total)

    fig, ax = plt.subplots(figsize=(12, 9))
    if graph.number_of_nodes() == 0:
        ax.set_title(title)
        ax.text(0.5, 0.5, "No inter-trajectory nodes", ha="center", va="center")
        ax.axis("off")
        fig.savefig(output, dpi=max(72, int(dpi)), bbox_inches="tight")
        plt.close(fig)
        return str(output)

    # Layout each connected component independently so dense subgraphs do not get squeezed
    # by far-away isolated nodes in one global spring layout.
    pos: dict[str, Any] = {}
    components = sorted((set(c) for c in nx.connected_components(graph)), key=len, reverse=True)
    comp_count = max(1, len(components))
    cols = max(1, int(math.ceil(math.sqrt(comp_count))))
    slot_gap = 10.0
    for idx, comp_nodes in enumerate(components):
        row = idx // cols
        col = idx % cols
        center_x = float(col) * slot_gap
        center_y = -float(row) * slot_gap
        sub = graph.subgraph(comp_nodes).copy()
        node_count = sub.number_of_nodes()
        if node_count <= 1:
            only_node = next(iter(comp_nodes))
            pos[only_node] = (center_x, center_y)
            continue

        density = float(nx.density(sub) or 0.0)
        if node_count <= 8 or density >= 0.55:
            local_scale = max(2.8, 0.9 + (0.7 * float(node_count)))
            local_pos = nx.circular_layout(sub, scale=local_scale)
        else:
            spring_k = 2.6 / max(1.0, float(node_count) ** 0.5)
            local_scale = max(3.0, float(node_count) * 0.55)
            local_pos = nx.spring_layout(sub, seed=42, k=spring_k, iterations=220, scale=local_scale)
        for n, xy in local_pos.items():
            pos[n] = (float(xy[0]) + center_x, float(xy[1]) + center_y)
    activate_counts = [int(graph.nodes[n].get("activate_count", 0) or 0) for n in graph.nodes]
    node_sizes = [300 + (50 * max(0, c)) for c in activate_counts]
    edge_widths = [
        1.0 + (3.0 * max(0.0, float(graph[u][v].get("score_total", 0.0) or 0.0)))
        for (u, v) in graph.edges
    ]

    nx.draw_networkx_edges(graph, pos, ax=ax, width=edge_widths, edge_color="#4b5563", alpha=0.9)
    max_activate = max(activate_counts) if activate_counts else 0
    node_artist = nx.draw_networkx_nodes(
        graph,
        pos,
        ax=ax,
        node_size=node_sizes,
        node_color=activate_counts,
        cmap="YlOrRd",
        vmin=0,
        vmax=max(1, max_activate),
        edgecolors="#1f2937",
        linewidths=1.3,
    )
    node_labels = {n: f"{n[-8:]}\na={int(graph.nodes[n].get('activate_count', 0) or 0)}" for n in graph.nodes}
    nx.draw_networkx_labels(graph, pos, labels=node_labels, font_size=8, font_weight="bold", ax=ax)
    edge_labels = {(u, v): f"{float(graph[u][v].get('score_total', 0.0) or 0.0):.2f}" for (u, v) in graph.edges}
    nx.draw_networkx_edge_labels(
        graph,
        pos,
        edge_labels=edge_labels,
        font_size=6,
        font_color="#111827",
        label_pos=0.58,
        bbox={"facecolor": "none", "edgecolor": "none", "alpha": 0.0, "pad": 0.0},
        ax=ax,
        rotate=False,
    )
    cbar = fig.colorbar(node_artist, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label("activate_count", rotation=90)

    ax.set_title(
        f"{title}\nNodes={graph.number_of_nodes()}  Edges={graph.number_of_edges()}",
        fontsize=13,
    )
    ax.axis("off")
    fig.savefig(output, dpi=max(72, int(dpi)), bbox_inches="tight")
    plt.close(fig)
    return str(output)


def _resolve_scope_owner_space(*, scope: str, owner_space: str | None, agent_id: str) -> tuple[str, str]:
    resolved_scope = str(scope or "agent").strip().lower() or "agent"
    if resolved_scope not in {"agent", "team", "datalake", "user"}:
        raise ValueError(f"invalid scope: {resolved_scope}")
    resolved_owner_space = str(owner_space or "").strip()
    if not resolved_owner_space:
        if resolved_scope == "agent":
            resolved_owner_space = str(agent_id).strip()
        else:
            raise ValueError("owner_space is required when scope is not agent")
    return resolved_scope, resolved_owner_space


def run_visualize(
    *,
    account_id: str,
    agent_id: str,
    scope: str,
    owner_space: str | None,
    node_limit: int,
    edge_limit_per_node: int,
    config_path: str | None,
    png_output: str | None = None,
    png_title: str = "AMC Inter-Trajectory Graph",
    png_dpi: int = 180,
) -> dict[str, Any]:
    settings = load_settings(config_path=config_path)
    resolved_scope, resolved_owner_space = _resolve_scope_owner_space(
        scope=scope,
        owner_space=owner_space,
        agent_id=agent_id,
    )
    graph_writer = build_graph_store_writer(settings)
    if graph_writer is None:
        raise RuntimeError("graph backend is unavailable; cannot visualize intertrajectory graph")
    close_fn = getattr(graph_writer, "close", None)
    try:
        if not hasattr(graph_writer, "describe_intertrajectory_graph"):
            raise RuntimeError("configured graph writer does not support intertrajectory visualization")
        graph_summary = graph_writer.describe_intertrajectory_graph(
            account_id=account_id,
            scope=resolved_scope,
            owner_space=resolved_owner_space,
            edge_rel_type=settings.intertrajectory_edge_rel_type,
            pending_rel_type=settings.intertrajectory_pending_rel_type,
            node_limit=max(1, int(node_limit)),
            edge_limit_per_node=max(1, int(edge_limit_per_node)),
        )
    finally:
        if callable(close_fn):
            close_fn()
    out = {
        "status": "ok",
        "account_id": account_id,
        "agent_id": agent_id,
        "scope": resolved_scope,
        "owner_space": resolved_owner_space,
        "edge_rel_type": settings.intertrajectory_edge_rel_type,
        "pending_rel_type": settings.intertrajectory_pending_rel_type,
        "graph": graph_summary,
    }
    if png_output:
        out["png_output"] = _render_intertrajectory_png(
            graph_summary=graph_summary,
            output_path=png_output,
            title=png_title,
            dpi=png_dpi,
        )
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amc-visualize-intertrajectory",
        description=(
            "Print inter-trajectory graph nodes/edges and optionally export a PNG."
        ),
    )
    parser.add_argument("--account-id", default="account-local", help="Account identifier")
    parser.add_argument("--agent-id", default="agent-local", help="Agent identifier")
    parser.add_argument(
        "--scope",
        default="agent",
        choices=["agent", "team", "datalake", "user"],
        help="Visibility scope for trajectory nodes.",
    )
    parser.add_argument("--owner-space", default=None, help="Owner space under the selected scope.")
    parser.add_argument("--node-limit", type=int, default=200, help="Maximum number of nodes to display.")
    parser.add_argument(
        "--edge-limit-per-node",
        type=int,
        default=64,
        help="Maximum neighbor edges shown for each node.",
    )
    parser.add_argument("--config-path", default=None, help="Optional path to config YAML.")
    parser.add_argument(
        "--png-output",
        default=None,
        help="Optional output PNG path, e.g. ./tmp/intertrajectory.png",
    )
    parser.add_argument(
        "--png-title",
        default="AMC Inter-Trajectory Graph",
        help="Title shown on PNG when --png-output is enabled.",
    )
    parser.add_argument(
        "--png-dpi",
        type=int,
        default=180,
        help="DPI for PNG output when --png-output is enabled.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    out = run_visualize(
        account_id=args.account_id,
        agent_id=args.agent_id,
        scope=args.scope,
        owner_space=args.owner_space,
        node_limit=args.node_limit,
        edge_limit_per_node=args.edge_limit_per_node,
        config_path=args.config_path,
        png_output=args.png_output,
        png_title=args.png_title,
        png_dpi=args.png_dpi,
    )
    if args.pretty:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

