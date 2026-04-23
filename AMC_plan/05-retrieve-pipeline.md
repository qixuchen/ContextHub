# 05 — Retrieve Pipeline（现行实现对齐版）

本文档仅描述当前代码已落地的 retrieve 行为；历史草案、未实现设计和伪代码已移除。

## 5.1 API（当前）

### 5.1.1 路由

- `POST /api/v1/amc/retrieve`

### 5.1.2 请求上下文与 body

- Header（当前硬要求）：
  - `X-Account-Id`（必须）
  - `X-Agent-Id`（推荐；可回退到 `body.agent_id`）
- Body（`RetrieveRequest`）：
  - `agent_id`（兼容字段，deprecated）
  - `query`：
    - `task_description: str`
    - `partial_trajectory: list[step] | null`
    - `constraints.tool_whitelist: list[str]`
  - `scope: list[str]`（可选过滤）
  - `owner_space: list[str]`（可选过滤）
  - `top_k: int`（1~100，默认 5）
  - `include_full_clean_graph: bool`（默认 false）

### 5.1.3 响应

- `items[]`：
  - `trajectory_id`
  - `score` / `total_score`
  - `semantic_score`
  - `graph_match_score`（无图匹配时可能为 null）
  - `scope` / `owner_space` / `uri`
  - `rationale[]`
  - `evidence`（`matched_uris` + 可选图匹配摘要）
  - `abstract` / `overview`
  - `clean_graph`（完整或紧凑版本）
- `skills[]`（可选）
- `skill_retrieval_summary`
- `warnings[]`

## 5.2 主流程（当前实现）

整体入口：`RetrieveOrchestrator.retrieve()` -> `RetrieveService.run()`。

```
Parse Query
  -> Semantic Recall (always when backend ready)
  -> Optional Graph Match (only if partial_trajectory and graph backend ready)
  -> Score Compose + ACL Filter + TopK
  -> Enrich from FS/Graph
  -> Optional Skill Recall
  -> Audit
```

## 5.3 Query 解析（当前）

`parse_retrieve_query()` 生成 `query_text`：

- 基础：`task_description`
- 可选拼接：`constraints.tool_whitelist`
- 可选拼接：从 `partial_trajectory` 的 `Action_result` 提取失败线索（error/failed/exception/traceback/syntax）

说明：

- `tool_whitelist` 当前只用于构造 query 文本提示，不是硬过滤器。
- `has_partial_trajectory` 用于决定是否尝试图匹配分支。

## 5.4 Semantic Recall（当前）

`SemanticRecall.recall()`：

1. 对 `query_text` 做 embedding（`text` 或 `multimodal` 模式）
2. 向量检索过滤条件：
   - `account_id`（强制）
   - `exclude_statuses=["deleted"]`
   - 可选 `scopes`（来自 `scope`）
   - 可选 `owner_spaces`（来自 `owner_space`）
3. 候选池大小：`max(top_k * 6, 20)`
4. 应用层二次过滤与聚合：
   - 再次校验 account/scope/owner_space/status
   - 按 `trajectory_id` 聚合，语义分取最大值
5. 返回 `top_k` 语义候选

语义分转换：

- `semantic_score = 1 / (1 + distance)`（distance 来自向量库）

## 5.5 图匹配分支（当前）

触发条件：

- `partial_trajectory` 非空
- `clean_graph_loader` 可用

### 5.5.1 Query Graph 构建

`build_query_graph()` 复用 commit 侧逻辑：

- `pair_ai_tool_steps()`
- `build_raw_graph()`
- `derive_clean_graph()`

当前 wiring 中 `query_dataflow_extractor=None`，因此 query graph 使用规则提取路径（非 LLM）。

### 5.5.2 候选范围

- 仅对**语义候选集合**做图匹配（不是独立图召回分支）

### 5.5.3 相似度算法

`recall_graph_candidates()` + `_mcs_match()`（networkx ISMAGS）：

- 节点匹配：`tool_name` 相等
- 边匹配：`dep_type` 相等
- 分数：
  - `graph_score = (matched_nodes + matched_edges) / max(1, query_nodes + query_edges)`

图证据会写入 `evidence.graph_match` 和 `evidence.matched_subgraph` 摘要。

## 5.6 排序与总分（当前）

- 基础排序：先按语义分降序（`rerank_semantic_only`）
- 最终分合成：
  - 有图分：`total_score = (0.45*semantic + 0.45*graph) / (0.45+0.45)`（即两者平均）
  - 无图分：`total_score = semantic_score`
- 返回前按 `score` 降序并截断到 `top_k`

说明：

- 当前无 `feedback_boost` 项。
- 当前无“语义候选 + 图候选 union”双分支融合；图匹配仅是语义候选上的附加打分。

## 5.7 ACL 与可见性（当前）

服务内 `_acl_filter_visible()`：

- `datalake`: 可见
- `agent` / `user`: `owner_space == agent_id`
- `team`: 当前视为 account 内共享可见（后续可接入更严格 team ACL）

如果 ACL 过滤了候选，会追加 warning：`acl filtered N invisible candidates`。

## 5.8 结果富化（当前）

`RetrieveOrchestrator` 在 service 结果上补充：

- 从 FS 按 `trajectory_id` 回源：
  - `abstract`
  - `overview`
- 从图后端回源 clean_graph：
  - `include_full_clean_graph=true`：返回完整 clean_graph
  - 否则返回紧凑版：
    - node: `node_id/thinking/tool_name/tool_args/tool_output`（字段值截断）
    - edge: `src/dst/dep_type`

## 5.9 Skill Retrieve（当前）

若 `skill_retriever` 已配置：

- 对同一个 `query_text` 做 skill 向量召回
- 过滤：`source_type=skill`
- 分组：按 `skill_name` 取最高分
- 阈值过滤：`score >= skill_score_threshold`
- 截断：`top_k = skill_top_k`

返回：

- `skills[]`（`skill_name/description/score/uri/path`）
- `skill_retrieval_summary`（`enabled/top_k/score_threshold/hit_count`）

## 5.10 审计（当前）

每次 retrieve 写审计：

- `account_id`
- `agent_id`
- `scope_filter`
- `owner_space_filter`
- `top_k`
- `hit_count`
- `skill_hit_count`

## 5.11 当前限制与未实现项

- 未实现 graph-only 独立候选分支；图匹配仅作用于语义候选
- 未实现 `feedback_boost` 融合项
- `tool_whitelist` 目前不是硬过滤（仅作为 query text 上下文）
- team 可见性暂为宽松策略（未接入 team-path 闭包 ACL）
- retrieve 端未实现专门的字段级脱敏策略模块

---

后续若新增图分支 union、反馈融合、严格 ACL 或在线脱敏，请按“已落地行为 + 开关 + 返回字段变化”同步更新本文件。
