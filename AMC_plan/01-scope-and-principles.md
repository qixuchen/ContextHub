# 01 — AMC 当前范围、边界与原则

本文档只保留**当前仓库已实现行为**，用于统一团队对 AMC 职责边界的理解。

## 1.1 AMC 当前职责

### 1.1.1 Commit

- 输入：上层传入的 trajectory 步骤序列。
- 处理：步骤配对、raw/clean 图构建、L0/L1 摘要、可选 LLM 依赖提取。
- 输出：
  - 本地轨迹包（`trajectory.json`、`raw_graph.json`、`clean_graph.json`、`meta.json`、摘要等）
  - 可选 Neo4j clean 图写入
  - 可选 pgvector 索引写入

### 1.1.2 Retrieve

- 输入：`task_description` + 可选 `partial_trajectory` + 上下文（`account_id/agent_id`）。
- 处理：
  - 语义召回（必选路径）
  - 图召回（当 `partial_trajectory` 与 clean graph loader 可用时）
  - 融合排序 + 可见性过滤
- 输出：
  - trajectory 命中项（分数、证据、摘要）
  - 可选 skill 命中项

### 1.1.3 Skill Route / Evolve

- `route`：判定 `update/create` 并执行分支。
- `evolve`：统一执行 skill 内容生成（结构化输出）与 `SKILL.md` 渲染。
- 已接入 commit 驱动的 inter-trajectory 自动触发。

### 1.1.4 Replay / Promote

- `replay`：按 `trajectory_id` 回放已存轨迹包内容。
- `promote`：将 agent 私有轨迹提升到 team scope，并刷新向量索引。

## 1.2 当前边界（做什么 / 不做什么）

### 做什么

- 提供轨迹记忆核心链路：commit、retrieve、replay、promote、skill route/evolve。
- 维护跨轨迹激活关系并支持阈值触发 route。
- 将关键操作写入 JSONL 审计日志。

### 不做什么（当前实现中未覆盖）

- 不做执行编排与任务调度（AMC 仅负责记忆与检索）。
- 不提供完整 ACL 策略引擎（`infra/security/acl_engine.py` 仍是占位）。
- 不提供跨租户组织级复杂授权模型（当前以 account context + scope 规则为主）。

## 1.3 当前隔离口径（实现事实）

- API 强制 `X-Account-Id`（commit/retrieve/promote）。
- `scope/owner_space` 在 commit 入参层校验：
  - `scope=agent` 时 `owner_space` 必须等于 agent；
  - 其他 scope 必须显式提供 `owner_space`。
- retrieve 侧目前使用内置可见性规则（非完整 ACL）：
  - `agent/user` 仅 owner 可见
  - `team/datalake` 当前按 account 内共享处理

## 1.4 设计原则（保留）

1. **实现优先可观测**：每条主链路要有可调试输出与审计记录。  
2. **结构化优先**：关键模型（轨迹图、skill 文档）优先结构化，再渲染文本。  
3. **低耦合集成**：对上层 Agent 保持 API 边界清晰。  
4. **默认 account 隔离**：跨 account 访问默认不允许。  
5. **文档随代码演进**：plan 以“当前行为”描述为主，不保留过期方案。

