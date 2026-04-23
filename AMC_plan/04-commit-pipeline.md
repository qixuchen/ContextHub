# 04 — Commit Pipeline（现行实现对齐版）

本文档描述当前代码中已经落地的 commit 主链路：输入契约、单条/批量执行路径、commit 后联动，以及当前空白点。

## 4.1 接口契约（当前）

### 4.1.1 单条提交

- 路由：`POST /api/v1/amc/commit`
- 上下文来源：
  - `X-Account-Id`、`X-Agent-Id` 为主
  - `body.account_id`、`body.agent_id` 为兼容回退
- 关键 body 字段：
  - `scope`（默认 `agent`）
  - `owner_space`（`scope=agent` 时必须等于 agent；其他 scope 必填）
  - `session_id`、`task_id`、`trajectory`
  - `labels`、`is_incremental`（保留字段）、`trajectory_id`、`visualize_graph_png`
- 关键返回字段：
  - `trajectory_id`、`idempotency_key`、`status`
  - `nodes`、`edges`、`warnings`
  - `summary_l0`、`summary_l1`
  - `neo4j_summary`、`vector_index_summary`

### 4.1.2 批量提交

- 路由：`POST /api/v1/amc/commit/batch`
- 请求结构：
  - 顶层：`batch_id`、上下文字段、`scope`、`owner_space`、`options`、`items`
  - `items[]`：单条 commit 的任务字段集合
  - `options`：`fail_fast`、`llm_batch_size_hint`、`llm_max_items_per_batch`、`llm_token_usage_ratio`、`llm_max_context_tokens_fallback`、`persist_batch_size`（保留）
- 关键返回字段：
  - `batch_id`、`status`
  - `summary`（`total/accepted/idempotent/failed/skipped`）
  - `items[]`
  - `intertrajectory_batch_trigger_summary`

## 4.2 单条 Commit 执行链路

入口：`CommitOrchestrator.commit()` -> `CommitService.run()` -> 持久化阶段。

### 4.2.1 Prepare 阶段（CommitService）

1. **Validate**
   - `trajectory` 非空
   - `Step` 严格递增
   - `meta.role` 必须是 `AIMessage` 或 `ToolMessage`
2. **Normalize**
   - `Action_result` 按 `max_action_result_chars` 截断（默认 12000）
   - 截断只记 warning，不阻断
3. **ID / 幂等键**
   - `trajectory_id`：外部传入优先，否则自动 `traj_{hash[:16]}`
   - `idempotency_key = hash(account_id + task_id + normalized_trajectory)`
4. **Pair AI/Tool**
   - `pair_ai_tool_steps()` 配对 action 与 tool result
   - 支持非严格交替
5. **Raw Graph**
   - 节点：`GraphNode`
   - 边：`dataflow/reasoning/temporal/retry`
   - dataflow 提取：`rule_based` 或 `llm`
   - LLM 模式下 dataflow/reasoning 两路并行，trace 写入 `llm_extraction_traces`
6. **Clean Graph**
   - 去除被后续同工具同 `file_path` 成功步骤覆盖的失败节点
7. **Trajectory Summary**
   - 仅 trajectory-level L0/L1
   - LLM 优先，失败回退规则法

### 4.2.2 Persist 阶段（CommitOrchestrator）

- 幂等命中且开关开启：
  - 返回 `status=idempotent`
  - 不重写图/文件
  - 可按现有 bundle 刷新向量索引
- 首次提交：
  - 写图（若启用）：`upsert_trajectory_graphs(raw_graph, clean_graph)`
  - 写本地 bundle：`trajectory/raw_graph/clean_graph/graph_pointer/.abstract/.overview/meta`
  - 写向量（若启用）：索引 `.abstract.md` + `.overview.md`
  - 写审计：`commit`

## 4.3 批量 Commit 执行链路

### 4.3.1 `fail_fast=true`

- 逐项执行：`prepare_commit` -> `commit_prepared`
- 首个失败后，剩余项标记 `skipped`

### 4.3.2 `fail_fast=false`（默认）

- 先用 `plan_prepare_micro_batches()` 按 token 预算切分 prepare 批
- 每个 micro-batch 内并行 `prepare_commits(max_workers=N)`
- prepare 完成后逐项 `commit_prepared` 落盘

## 4.4 Commit 后联动（当前已落地）

当前已落地的是 intertrajectory 激活链路（不是通用 propagation 子系统）：

1. commit 成功后，`InterTrajectoryLinker` 召回相似轨迹；
2. 对阈值内邻居写 Neo4j 关系：
   - `INTERTRAJ_SIMILAR`
   - `INTERTRAJ_ACTIVATED_PENDING`
3. `InterTrajectoryTrigger` 依据 `pending_count` 与阈值判断是否触发 route；
4. 达阈值时调用 route（内部 `run_route_with_candidates`）；
5. route 进入终态（`update/create/support_insufficient`）后清理 pending。

### 4.4.1 单条与批量触发差异

- 单条 commit：提交后即时触发检查；
- 批量 commit：批末 `trigger_intertrajectory_batch(...)` 统一触发，默认 `end_of_batch`，可扩展覆盖被本批激活的旧 anchor。

## 4.5 存储与路径

- 本地根目录：`storage.localfs_root`（默认 `./data/content`）
- 轨迹目录：

```text
data/content/accounts/{account_id}/scope/{scope}/{owner_space}/memories/trajectories/{trajectory_id}/
```

- 轨迹 URI：

```text
ctx://{scope}/{owner_space}/memories/trajectories/{trajectory_id}
```

## 4.6 返回与观测摘要

- 单条 commit 关键摘要：
  - `neo4j_summary`
  - `vector_index_summary`
  - `intertrajectory_summary`
  - `intertrajectory_trigger_summary`
- 批量 commit 关键摘要：
  - 每 item 的 `status/error`
  - `intertrajectory_batch_trigger_summary`

## 4.7 当前保留项与未落地能力

### 4.7.1 保留字段（已入参，未完整生效）

- `is_incremental`：未实现“局部追加 + 图版本演进”
- `persist_batch_size`：当前主流程未使用

### 4.7.2 propagation / feedback 方向空白

- `core/propagation/*`、`app/orchestrators/propagation_orchestrator.py` 为 placeholder
- `api/routes/feedback.py`、`api/schemas/feedback.py`、`core/feedback/*` 为 placeholder
- `core/commit/deps_extractor.py` 为 placeholder
- `infra/storage/event/*` 为 placeholder（当前落地的是 audit JSONL，不是事件总线）

---

后续若恢复增量 commit、依赖登记、事件发布或 feedback 回写，请继续在本文件按“已落地行为 + 开关 + 返回字段变化”更新。
