"""Neo4j graph writer for AMC trajectory raw/clean graphs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from dataclasses import asdict, is_dataclass
from typing import Any

from neo4j import GraphDatabase


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(_to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def _json_load(text: Any) -> Any:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


class Neo4jGraphWriter:
    """
    Persist trajectory graphs in Neo4j with explicit labels and relation types.

    Labels:
    - :AMCTrajectory
    - :AMCNode + :RawNode / :CleanNode

    Relationship types:
    - :DATAFLOW | :REASONING | :TEMPORAL | :RETRY
    """

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        database: str = "neo4j",
    ) -> None:
        self.database = database
        self.driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self.driver.close()

    def verify_connectivity(self) -> None:
        self.driver.verify_connectivity()

    def upsert_trajectory_graphs(
        self,
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        raw_graph: dict[str, Any],
        clean_graph: dict[str, Any],
    ) -> dict[str, Any]:
        raw = _to_jsonable(raw_graph)
        clean = _to_jsonable(clean_graph)
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._clear_trajectory_graph,
                trajectory_id=trajectory_id,
            )
            session.execute_write(
                self._merge_trajectory_node,
                agent_id=agent_id,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
            )
            self._write_graph_kind(session, trajectory_id=trajectory_id, graph_kind="raw", graph=raw)
            self._write_graph_kind(session, trajectory_id=trajectory_id, graph_kind="clean", graph=clean)
        return self._build_summary(
            agent_id=agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
            database=self.database,
            raw_graph=raw,
            clean_graph=clean,
        )

    def load_clean_graph(self, *, trajectory_id: str) -> dict[str, Any] | None:
        with self.driver.session(database=self.database) as session:
            nodes_result = session.run(
                """
                MATCH (n:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                RETURN n
                ORDER BY n.ai_step, n.tool_step, n.node_id
                """,
                trajectory_id=trajectory_id,
            )
            nodes: list[dict[str, Any]] = []
            for rec in nodes_result:
                n = rec.get("n")
                if n is None:
                    continue
                item = dict(n)
                nodes.append(
                    {
                        "node_id": item.get("node_id"),
                        "trajectory_id": item.get("trajectory_id"),
                        "ai_step": item.get("ai_step"),
                        "tool_step": item.get("tool_step"),
                        "thinking": item.get("thinking") or "",
                        "tool_name": item.get("tool_name"),
                        "tool_args": _json_load(item.get("tool_args_json")),
                        "tool_output": _json_load(item.get("tool_output_json")),
                        "output_status": item.get("output_status"),
                        "pending_output": bool(item.get("pending_output")),
                        "quality_flags": item.get("quality_flags") or [],
                    }
                )

            edges_result = session.run(
                """
                MATCH (src:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                      -[r]->
                      (dst:AMCNode:CleanNode {trajectory_id:$trajectory_id, graph_kind:'clean'})
                RETURN src.node_id AS src_node_id, dst.node_id AS dst_node_id, r
                ORDER BY r.edge_id
                """,
                trajectory_id=trajectory_id,
            )
            edges: list[dict[str, Any]] = []
            for rec in edges_result:
                r = rec.get("r")
                if r is None:
                    continue
                rel = dict(r)
                edges.append(
                    {
                        "edge_id": rel.get("edge_id"),
                        "src": rec.get("src_node_id"),
                        "dst": rec.get("dst_node_id"),
                        "dep_type": rel.get("dep_type"),
                        "signal": rel.get("signal"),
                        "confidence": rel.get("confidence"),
                        "signal_detail": _json_load(rel.get("signal_detail_json")),
                    }
                )

        if not nodes and not edges:
            return None
        return {"nodes": nodes, "edges": edges}

    def upsert_intertrajectory_relationship(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        src_trajectory_id: str,
        dst_trajectory_id: str,
        score_total: float,
        score_semantic: float | None,
        score_graph: float | None,
        edge_rel_type: str = "INTERTRAJ_SIMILAR",
        pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING",
    ) -> dict[str, Any]:
        src = str(src_trajectory_id or "").strip()
        dst = str(dst_trajectory_id or "").strip()
        if not src or not dst or src == dst:
            raise ValueError("src_trajectory_id and dst_trajectory_id must be non-empty and different")
        a_id, b_id = sorted([src, dst])
        edge_rel = self._sanitize_relationship_type(edge_rel_type)
        pending_rel = self._sanitize_relationship_type(pending_rel_type)
        now = datetime.now(UTC).isoformat()
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._merge_intertrajectory_relationship,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                src_trajectory_id=src,
                dst_trajectory_id=dst,
                edge_src_trajectory_id=a_id,
                edge_dst_trajectory_id=b_id,
                score_total=float(score_total),
                score_semantic=score_semantic,
                score_graph=score_graph,
                now=now,
                edge_rel_type=edge_rel,
                pending_rel_type=pending_rel,
            )
        return {
            "ok": True,
            "edge_src_trajectory_id": a_id,
            "edge_dst_trajectory_id": b_id,
            "edge_rel_type": edge_rel,
            "pending_rel_type": pending_rel,
            "score_total": float(score_total),
            "score_semantic": score_semantic,
            "score_graph": score_graph,
        }

    def list_intertrajectory_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING",
        edge_rel_type: str = "INTERTRAJ_SIMILAR",
        limit: int = 512,
    ) -> list[dict[str, Any]]:
        pending_rel = self._sanitize_relationship_type(pending_rel_type)
        edge_rel = self._sanitize_relationship_type(edge_rel_type)
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                self._read_intertrajectory_pending_neighbors,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
                pending_rel_type=pending_rel,
                edge_rel_type=edge_rel,
                limit=max(1, int(limit)),
            )

    def clear_intertrajectory_pending_neighbors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING",
    ) -> int:
        pending_rel = self._sanitize_relationship_type(pending_rel_type)
        with self.driver.session(database=self.database) as session:
            return session.execute_write(
                self._delete_intertrajectory_pending_neighbors,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_id=trajectory_id,
                pending_rel_type=pending_rel,
            )

    def list_intertrajectory_pending_related_anchors(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
        pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING",
        limit: int = 2048,
    ) -> list[dict[str, Any]]:
        pending_rel = self._sanitize_relationship_type(pending_rel_type)
        dedup_ids = list(dict.fromkeys(str(x).strip() for x in trajectory_ids if str(x).strip()))
        if not dedup_ids:
            return []
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                self._read_intertrajectory_pending_related_anchors,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                trajectory_ids=dedup_ids,
                pending_rel_type=pending_rel,
                limit=max(1, int(limit)),
            )

    def describe_intertrajectory_graph(
        self,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        edge_rel_type: str = "INTERTRAJ_SIMILAR",
        pending_rel_type: str = "INTERTRAJ_ACTIVATED_PENDING",
        node_limit: int = 200,
        edge_limit_per_node: int = 64,
    ) -> dict[str, Any]:
        edge_rel = self._sanitize_relationship_type(edge_rel_type)
        pending_rel = self._sanitize_relationship_type(pending_rel_type)
        with self.driver.session(database=self.database) as session:
            return session.execute_read(
                self._read_intertrajectory_graph_snapshot,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                edge_rel_type=edge_rel,
                pending_rel_type=pending_rel,
                node_limit=max(1, int(node_limit)),
                edge_limit_per_node=max(1, int(edge_limit_per_node)),
            )

    @staticmethod
    def _build_summary(
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        database: str,
        raw_graph: dict[str, Any],
        clean_graph: dict[str, Any],
    ) -> dict[str, Any]:
        raw_edges = [e for e in (raw_graph.get("edges") or []) if isinstance(e, dict)]
        clean_edges = [e for e in (clean_graph.get("edges") or []) if isinstance(e, dict)]
        raw_rel_counts: dict[str, int] = {}
        clean_rel_counts: dict[str, int] = {}
        for edge in raw_edges:
            rel_type = Neo4jGraphWriter._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            raw_rel_counts[rel_type] = raw_rel_counts.get(rel_type, 0) + 1
        for edge in clean_edges:
            rel_type = Neo4jGraphWriter._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            clean_rel_counts[rel_type] = clean_rel_counts.get(rel_type, 0) + 1
        return {
            "enabled": True,
            "agent_id": agent_id,
            "account_id": account_id,
            "scope": scope,
            "owner_space": owner_space,
            "trajectory_id": trajectory_id,
            "database": database,
            "raw_nodes": len(raw_graph.get("nodes") or []),
            "raw_edges": len(raw_edges),
            "raw_edge_type_counts": raw_rel_counts,
            "clean_nodes": len(clean_graph.get("nodes") or []),
            "clean_edges": len(clean_edges),
            "clean_edge_type_counts": clean_rel_counts,
        }

    @staticmethod
    def _clear_trajectory_graph(tx: Any, *, trajectory_id: str) -> None:
        tx.run(
            """
            MATCH (n:AMCNode {trajectory_id: $trajectory_id})
            DETACH DELETE n
            """,
            trajectory_id=trajectory_id,
        )

    @staticmethod
    def _merge_trajectory_node(
        tx: Any,
        *,
        agent_id: str,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
    ) -> None:
        tx.run(
            """
            MERGE (t:AMCTrajectory {trajectory_id: $trajectory_id})
            SET t.agent_id = $agent_id,
                t.account_id = $account_id,
                t.scope = $scope,
                t.owner_space = $owner_space
            """,
            trajectory_id=trajectory_id,
            agent_id=agent_id,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
        )

    def _write_graph_kind(
        self,
        session: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        graph: dict[str, Any],
    ) -> None:
        node_label = "RawNode" if graph_kind == "raw" else "CleanNode"
        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            session.execute_write(
                self._merge_node,
                trajectory_id=trajectory_id,
                graph_kind=graph_kind,
                node_label=node_label,
                node=node,
            )
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            rel_type = self._relationship_type(str(edge.get("dep_type") or "TEMPORAL"))
            session.execute_write(
                self._merge_edge,
                trajectory_id=trajectory_id,
                graph_kind=graph_kind,
                rel_type=rel_type,
                edge=edge,
            )

    @staticmethod
    def _relationship_type(dep_type: str) -> str:
        key = dep_type.strip().lower()
        if key == "dataflow":
            return "DATAFLOW"
        if key == "reasoning":
            return "REASONING"
        if key == "retry":
            return "RETRY"
        if key == "controlflow":
            # Backward compatibility for older graphs.
            return "RETRY"
        return "TEMPORAL"

    @staticmethod
    def _sanitize_relationship_type(raw: str) -> str:
        text = str(raw or "").strip().upper()
        if not text:
            raise ValueError("relationship type must be non-empty")
        if not all(ch.isalnum() or ch == "_" for ch in text):
            raise ValueError(f"invalid relationship type: {raw}")
        return text

    @staticmethod
    def _merge_node(
        tx: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        node_label: str,
        node: dict[str, Any],
    ) -> None:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            return
        graph_node_id = f"{trajectory_id}:{graph_kind}:{node_id}"
        query = f"""
        MATCH (t:AMCTrajectory {{trajectory_id: $trajectory_id}})
        MERGE (n:AMCNode:{node_label} {{graph_node_id: $graph_node_id}})
        SET n.trajectory_id = $trajectory_id,
            n.graph_kind = $graph_kind,
            n.node_id = $node_id,
            n.ai_step = $ai_step,
            n.tool_step = $tool_step,
            n.tool_name = $tool_name,
            n.output_status = $output_status,
            n.pending_output = $pending_output,
            n.thinking = $thinking,
            n.tool_args_json = $tool_args_json,
            n.tool_output_json = $tool_output_json,
            n.quality_flags = $quality_flags
        MERGE (t)-[:HAS_NODE {{graph_kind: $graph_kind}}]->(n)
        """
        tx.run(
            query,
            trajectory_id=trajectory_id,
            graph_node_id=graph_node_id,
            graph_kind=graph_kind,
            node_id=node_id,
            ai_step=int(node.get("ai_step") or 0),
            tool_step=node.get("tool_step"),
            tool_name=node.get("tool_name"),
            output_status=node.get("output_status"),
            pending_output=bool(node.get("pending_output")),
            thinking=str(node.get("thinking") or ""),
            tool_args_json=_json_text(node.get("tool_args")),
            tool_output_json=_json_text(node.get("tool_output")),
            quality_flags=[str(x) for x in (node.get("quality_flags") or [])],
        )

    @staticmethod
    def _merge_edge(
        tx: Any,
        *,
        trajectory_id: str,
        graph_kind: str,
        rel_type: str,
        edge: dict[str, Any],
    ) -> None:
        src = str(edge.get("src") or "")
        dst = str(edge.get("dst") or "")
        edge_id = str(edge.get("edge_id") or "")
        if not src or not dst or not edge_id:
            return
        src_graph_node_id = f"{trajectory_id}:{graph_kind}:{src}"
        dst_graph_node_id = f"{trajectory_id}:{graph_kind}:{dst}"
        graph_edge_id = f"{trajectory_id}:{graph_kind}:{edge_id}"
        query = f"""
        MATCH (src:AMCNode {{graph_node_id: $src_graph_node_id}})
        MATCH (dst:AMCNode {{graph_node_id: $dst_graph_node_id}})
        MERGE (src)-[r:{rel_type} {{graph_edge_id: $graph_edge_id}}]->(dst)
        SET r.trajectory_id = $trajectory_id,
            r.graph_kind = $graph_kind,
            r.edge_id = $edge_id,
            r.dep_type = $dep_type,
            r.signal = $signal,
            r.confidence = $confidence,
            r.signal_detail_json = $signal_detail_json
        """
        tx.run(
            query,
            src_graph_node_id=src_graph_node_id,
            dst_graph_node_id=dst_graph_node_id,
            graph_edge_id=graph_edge_id,
            trajectory_id=trajectory_id,
            graph_kind=graph_kind,
            edge_id=edge_id,
            dep_type=str(edge.get("dep_type") or ""),
            signal=edge.get("signal"),
            confidence=float(edge.get("confidence") or 0.0),
            signal_detail_json=_json_text(edge.get("signal_detail")),
        )

    @staticmethod
    def _merge_intertrajectory_relationship(
        tx: Any,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        src_trajectory_id: str,
        dst_trajectory_id: str,
        edge_src_trajectory_id: str,
        edge_dst_trajectory_id: str,
        score_total: float,
        score_semantic: float | None,
        score_graph: float | None,
        now: str,
        edge_rel_type: str,
        pending_rel_type: str,
    ) -> None:
        query = f"""
        MERGE (src:AMCTrajectory {{trajectory_id: $src_trajectory_id}})
        ON CREATE SET src.account_id = $account_id, src.scope = $scope, src.owner_space = $owner_space
        SET src.account_id = coalesce(src.account_id, $account_id),
            src.scope = coalesce(src.scope, $scope),
            src.owner_space = coalesce(src.owner_space, $owner_space)
        MERGE (dst:AMCTrajectory {{trajectory_id: $dst_trajectory_id}})
        ON CREATE SET dst.account_id = $account_id, dst.scope = $scope, dst.owner_space = $owner_space
        SET dst.account_id = coalesce(dst.account_id, $account_id),
            dst.scope = coalesce(dst.scope, $scope),
            dst.owner_space = coalesce(dst.owner_space, $owner_space)
        MERGE (a:AMCTrajectory {{trajectory_id: $edge_src_trajectory_id}})
        MERGE (b:AMCTrajectory {{trajectory_id: $edge_dst_trajectory_id}})
        MERGE (a)-[sim:{edge_rel_type}]->(b)
        ON CREATE SET sim.created_at = $now
        SET sim.score_total = $score_total,
            sim.score_semantic = $score_semantic,
            sim.score_graph = $score_graph,
            sim.updated_at = $now
        MERGE (src)-[p1:{pending_rel_type}]->(dst)
        ON CREATE SET p1.created_at = $now
        SET p1.updated_at = $now
        MERGE (dst)-[p2:{pending_rel_type}]->(src)
        ON CREATE SET p2.created_at = $now
        SET p2.updated_at = $now
        """
        tx.run(
            query,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            src_trajectory_id=src_trajectory_id,
            dst_trajectory_id=dst_trajectory_id,
            edge_src_trajectory_id=edge_src_trajectory_id,
            edge_dst_trajectory_id=edge_dst_trajectory_id,
            score_total=float(score_total),
            score_semantic=score_semantic,
            score_graph=score_graph,
            now=now,
        )

    @staticmethod
    def _read_intertrajectory_pending_neighbors(
        tx: Any,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        pending_rel_type: str,
        edge_rel_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = f"""
        MATCH (src:AMCTrajectory {{trajectory_id: $trajectory_id}})
        WHERE coalesce(src.account_id, $account_id) = $account_id
          AND coalesce(src.scope, $scope) = $scope
          AND coalesce(src.owner_space, $owner_space) = $owner_space
        MATCH (src)-[p:{pending_rel_type}]->(dst:AMCTrajectory)
        WHERE coalesce(dst.account_id, $account_id) = $account_id
          AND coalesce(dst.scope, $scope) = $scope
          AND coalesce(dst.owner_space, $owner_space) = $owner_space
        OPTIONAL MATCH (src)-[sim1:{edge_rel_type}]->(dst)
        OPTIONAL MATCH (dst)-[sim2:{edge_rel_type}]->(src)
        WITH dst, p, coalesce(sim1, sim2) AS sim
        RETURN dst.trajectory_id AS trajectory_id,
               sim.score_total AS score_total,
               sim.score_semantic AS score_semantic,
               sim.score_graph AS score_graph,
               p.updated_at AS pending_updated_at
        ORDER BY pending_updated_at DESC
        LIMIT $limit
        """
        rows = tx.run(
            query,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
            limit=int(limit),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "trajectory_id": row.get("trajectory_id"),
                    "score_total": row.get("score_total"),
                    "score_semantic": row.get("score_semantic"),
                    "score_graph": row.get("score_graph"),
                    "pending_updated_at": row.get("pending_updated_at"),
                }
            )
        return out

    @staticmethod
    def _delete_intertrajectory_pending_neighbors(
        tx: Any,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_id: str,
        pending_rel_type: str,
    ) -> int:
        query = f"""
        MATCH (src:AMCTrajectory {{trajectory_id: $trajectory_id}})
        WHERE coalesce(src.account_id, $account_id) = $account_id
          AND coalesce(src.scope, $scope) = $scope
          AND coalesce(src.owner_space, $owner_space) = $owner_space
        MATCH (src)-[p:{pending_rel_type}]->(:AMCTrajectory)
        WITH collect(p) AS rels
        FOREACH (r IN rels | DELETE r)
        RETURN size(rels) AS cleared
        """
        rec = tx.run(
            query,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_id=trajectory_id,
        ).single()
        return int((rec or {}).get("cleared") or 0)

    @staticmethod
    def _read_intertrajectory_pending_related_anchors(
        tx: Any,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        trajectory_ids: list[str],
        pending_rel_type: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        query = f"""
        MATCH (seed:AMCTrajectory)
        WHERE seed.trajectory_id IN $trajectory_ids
          AND coalesce(seed.account_id, $account_id) = $account_id
          AND coalesce(seed.scope, $scope) = $scope
          AND coalesce(seed.owner_space, $owner_space) = $owner_space
        OPTIONAL MATCH (seed)-[p:{pending_rel_type}]-(other:AMCTrajectory)
        WHERE coalesce(other.account_id, $account_id) = $account_id
          AND coalesce(other.scope, $scope) = $scope
          AND coalesce(other.owner_space, $owner_space) = $owner_space
        WITH collect(DISTINCT seed.trajectory_id) + collect(DISTINCT other.trajectory_id) AS ids
        UNWIND ids AS tid
        WITH DISTINCT tid
        WHERE tid IS NOT NULL AND tid <> ''
        RETURN tid AS trajectory_id
        ORDER BY trajectory_id
        LIMIT $limit
        """
        rows = tx.run(
            query,
            account_id=account_id,
            scope=scope,
            owner_space=owner_space,
            trajectory_ids=list(trajectory_ids),
            limit=int(limit),
        )
        return [{"trajectory_id": row.get("trajectory_id")} for row in rows]

    @staticmethod
    def _read_intertrajectory_graph_snapshot(
        tx: Any,
        *,
        account_id: str,
        scope: str,
        owner_space: str,
        edge_rel_type: str,
        pending_rel_type: str,
        node_limit: int,
        edge_limit_per_node: int,
    ) -> dict[str, Any]:
        node_query = f"""
        MATCH (n:AMCTrajectory)
        WHERE coalesce(n.account_id, $account_id) = $account_id
          AND coalesce(n.scope, $scope) = $scope
          AND coalesce(n.owner_space, $owner_space) = $owner_space
        WITH n
        ORDER BY n.trajectory_id
        LIMIT $node_limit
        OPTIONAL MATCH (n)-[p:{pending_rel_type}]->(pn:AMCTrajectory)
        WHERE coalesce(pn.account_id, $account_id) = $account_id
          AND coalesce(pn.scope, $scope) = $scope
          AND coalesce(pn.owner_space, $owner_space) = $owner_space
        RETURN n.trajectory_id AS trajectory_id,
               count(DISTINCT p) AS activate_count
        ORDER BY trajectory_id
        """
        node_rows = list(
            tx.run(
                node_query,
                account_id=account_id,
                scope=scope,
                owner_space=owner_space,
                node_limit=int(node_limit),
            )
        )
        trajectory_ids = [str(r.get("trajectory_id") or "").strip() for r in node_rows]
        trajectory_ids = [x for x in trajectory_ids if x]
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for row in node_rows:
            tid = str(row.get("trajectory_id") or "").strip()
            if not tid:
                continue
            nodes_by_id[tid] = {
                "trajectory_id": tid,
                "activate_count": int(row.get("activate_count") or 0),
                "edge_count": 0,
                "edges": [],
            }

        edge_rows: list[Any] = []
        if trajectory_ids:
            edge_query = f"""
            MATCH (src:AMCTrajectory)-[r:{edge_rel_type}]->(dst:AMCTrajectory)
            WHERE src.trajectory_id IN $trajectory_ids
              AND dst.trajectory_id IN $trajectory_ids
              AND coalesce(src.account_id, $account_id) = $account_id
              AND coalesce(src.scope, $scope) = $scope
              AND coalesce(src.owner_space, $owner_space) = $owner_space
              AND coalesce(dst.account_id, $account_id) = $account_id
              AND coalesce(dst.scope, $scope) = $scope
              AND coalesce(dst.owner_space, $owner_space) = $owner_space
            RETURN src.trajectory_id AS src_trajectory_id,
                   dst.trajectory_id AS dst_trajectory_id,
                   r.score_total AS score_total,
                   r.score_semantic AS score_semantic,
                   r.score_graph AS score_graph
            ORDER BY coalesce(r.score_total, -1.0) DESC, src_trajectory_id, dst_trajectory_id
            """
            edge_rows = list(
                tx.run(
                    edge_query,
                    trajectory_ids=trajectory_ids,
                    account_id=account_id,
                    scope=scope,
                    owner_space=owner_space,
                )
            )

        unique_edges: list[dict[str, Any]] = []
        edge_keys: set[tuple[str, str]] = set()
        for row in edge_rows:
            src_tid = str(row.get("src_trajectory_id") or "").strip()
            dst_tid = str(row.get("dst_trajectory_id") or "").strip()
            if not src_tid or not dst_tid:
                continue
            key = tuple(sorted((src_tid, dst_tid)))
            if key in edge_keys:
                continue
            edge_keys.add(key)
            edge_item = {
                "src_trajectory_id": key[0],
                "dst_trajectory_id": key[1],
                "score_total": row.get("score_total"),
                "score_semantic": row.get("score_semantic"),
                "score_graph": row.get("score_graph"),
            }
            unique_edges.append(edge_item)
            if key[0] in nodes_by_id:
                nodes_by_id[key[0]]["edges"].append(
                    {
                        "neighbor_trajectory_id": key[1],
                        "score_total": edge_item["score_total"],
                        "score_semantic": edge_item["score_semantic"],
                        "score_graph": edge_item["score_graph"],
                    }
                )
            if key[1] in nodes_by_id:
                nodes_by_id[key[1]]["edges"].append(
                    {
                        "neighbor_trajectory_id": key[0],
                        "score_total": edge_item["score_total"],
                        "score_semantic": edge_item["score_semantic"],
                        "score_graph": edge_item["score_graph"],
                    }
                )

        for node in nodes_by_id.values():
            edges = list(node.get("edges") or [])
            edges.sort(
                key=lambda x: (
                    -1.0 if x.get("score_total") is None else -float(x.get("score_total")),
                    str(x.get("neighbor_trajectory_id") or ""),
                )
            )
            node["edges"] = edges[: max(1, int(edge_limit_per_node))]
            node["edge_count"] = len(node["edges"])

        nodes = sorted(nodes_by_id.values(), key=lambda x: str(x.get("trajectory_id") or ""))
        return {
            "node_count": len(nodes),
            "edge_count": len(unique_edges),
            "nodes": nodes,
            "edges": unique_edges,
        }
