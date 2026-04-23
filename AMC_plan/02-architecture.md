# 02 — AMC 系统架构（现行实现对齐版）

本文档描述当前真实可运行架构，不再把未实现模块写进主链路。

## 2.1 当前运行架构（逻辑）

```text
Upper Agent / CLI
  -> AMC HTTP API
       - /commit
       - /commit/batch
       - /retrieve
       - /promote
       - /replay
  -> Orchestrators
       - CommitOrchestrator
       - RetrieveOrchestrator
       - PromoteOrchestrator
  -> Core Services
       - CommitService (graph build + summary)
       - RetrieveService (semantic recall + optional graph match + skill recall)
       - InterTrajectoryLinker / Trigger (commit后自动路由)
  -> Storage / Infra
       - LocalFS trajectory repo (事实存储)
       - Neo4j graph store (raw/clean graph + intertrajectory关系)
       - Vector store (pgvector/chroma, trajectory + skill 索引)
       - JSONL audit logger
```

## 2.2 API 与 CLI 的职责边界（当前）

### 2.2.1 HTTP API

当前 `create_app()` 只挂载以下路由：

- `commit`
- `retrieve`
- `promote`
- `replay`

`feedback` 路由文件存在，但未接入 app（仍是 placeholder）。

### 2.2.2 CLI

除了 HTTP 主链路，技能相关流程主要通过 CLI 落地：

- `amc-route-skill`
- `amc-evolve-skill`
- `amc-compute-skill-embedding`
- `amc-visualize-intertrajectory`

## 2.3 核心数据流（当前）

### 2.3.1 Commit

`CommitService` 完成：

1. 轨迹校验与规范化；
2. AI/Tool 配对；
3. raw/clean graph 构建；
4. L0/L1 摘要（LLM 优先，失败回退规则）；
5. 返回 `CommitResult` 给 orchestrator 持久化。

`CommitOrchestrator` 完成：

- 写 Neo4j（若启用）；
- 写 LocalFS bundle；
- 轨迹向量索引（若启用）；
- intertrajectory 建边 + 触发；
- 审计落盘。

### 2.3.2 Retrieve

`RetrieveService` 完成：

1. query 解析；
2. trajectory 语义召回；
3. 可选图匹配重打分（partial_trajectory 存在时）；
4. ACL 可见性过滤（当前内置简化规则）；
5. 可选 skill 召回。

`RetrieveOrchestrator` 再补：

- 从 LocalFS 回填 `abstract/overview`；
- 从图后端回填 clean graph（全量或紧凑版）；
- 写审计。

## 2.4 当前真实后端组合

- 内容主存：`LocalFS`（`trajectory.json/.abstract.md/.overview.md/...`）
- 图后端：`Neo4j`（raw/clean + intertrajectory 边）
- 向量后端：`pgvector`（也支持 chroma 适配）
- 审计：`JsonlAuditLogger`

设计上仍保持“FS 事实源 + 图/向量检索副本”。

## 2.5 当前未接入的架构层（明确）

以下目录存在但仍是 placeholder，不应纳入“已实现架构”：

- `core/feedback/*`、`api/routes/feedback.py`
- `core/propagation/*`
- `core/workflow/*`
- `infra/security/*`（ACL/Mask 的独立引擎尚未接入）
- `infra/storage/event/*`（事件总线未落地）

## 2.6 与其它文档分工

- `03`：commit 细节
- `04`：retrieve 细节
- `19`：skill route/evolve 与 intertrajectory 自动触发后的技能演进
- `08`：本文件维护当前架构分层与“已实现/占位”映射

