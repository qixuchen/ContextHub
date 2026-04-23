# 03 — 轨迹信息模型（当前实现）

本文档以当前代码行为为准，描述 commit 后真实落盘结构与图模型口径。

## 3.1 输入轨迹格式（当前）

commit 接口接收 `trajectory: list[dict]`，常见字段：

- `Step`
- `Thinking`
- `Action`
- `Action_result`
- `Response`
- `meta.role`（如 `AIMessage`、`ToolMessage`）

处理时会先做：

1. 结构校验（`validate_raw_steps`）
2. tool 输出截断（`max_action_result_chars`）
3. AI/Tool 步骤配对（`pair_ai_tool_steps`）

## 3.2 图模型（当前）

每条 trajectory 会构建两版图：

- `raw_graph`：原始依赖图
- `clean_graph`：清洗后图

节点字段（代码口径）重点包括：

- `node_id`
- `trajectory_id`
- `ai_step` / `tool_step`
- `tool_name` / `tool_args` / `tool_output`
- `thinking`
- `output_status` / `pending_output`
- `quality_flags`

边字段重点包括：

- `edge_id`
- `src` / `dst`
- `dep_type`（`dataflow|reasoning|retry|temporal`）
- `signal`
- `confidence`
- `signal_detail`

## 3.3 依赖边语义（当前）

- `dataflow`：后续步骤显式消费前序输出。
- `reasoning`：后续 thinking 参考前序执行结果。
- `retry`：失败后修正链路。
- `temporal`：兜底时序边（避免图断裂）。

说明：`dataflow` 与 `reasoning` 可并存于同一对节点。

## 3.4 持久化模型（当前）

### 2.4.1 LocalFS 轨迹包（主落盘）

默认根路径：`storage.localfs_root`（`config/config.yaml` 默认 `./data/content`）。

单条轨迹目录：

`accounts/{account_id}/scope/{scope}/{owner_space}/memories/trajectories/{trajectory_id}/`

当前实际文件：

- `trajectory.json`
- `raw_graph.json`
- `clean_graph.json`
- `graph_pointer.json`
- `.abstract.md`
- `.overview.md`
- `meta.json`
- （可选）`raw_graph.png` / `clean_graph.png`
- （可选）`llm_extraction/*.json`

说明：`_index.json`、`_uri_index.json`、`_idempotency.json` 在 root 级维护检索与幂等映射。

### 2.4.2 Graph 后端（可选）

- 当 Neo4j 可用时，commit 会写入轨迹节点与 clean/raw 图关系。
- retrieve 的 graph recall 依赖 graph 后端 `clean_graph_loader`。
- 因此当前是“LocalFS 必有 + Graph 后端可选增强”，不是“仅图后端存图”。

### 2.4.3 Vector 后端（可选）

- trajectory 向量索引默认 pgvector（配置可禁用/不可用时退化）。
- skill 索引单独使用 `amc_skill_index` 表。

## 3.5 URI 与作用域口径（当前）

轨迹 URI 生成规则：

`ctx://{scope}/{owner_space}/memories/trajectories/{trajectory_id}`

其中：

- `scope`：`agent|team|datalake|user`
- `owner_space`：scope 归属（如 agent_id 或 team path）

## 3.6 摘要模型（当前）

每条 trajectory 落盘时都会生成：

- `abstract`（L0）
- `overview`（L1）

当前检索主要使用 trajectory-level 摘要，不生成 node-level 摘要索引。

