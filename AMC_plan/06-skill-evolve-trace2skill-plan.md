# 06 — Skill Evolve / Route（当前代码对齐）

本文档仅记录仓库**当前已实现**的 skill evolve / route 行为。  
历史草案、已过期阶段计划、未落地设计已删除。

## 6.0 快速阅读指引

如果只想先理解“现在这套系统在做什么”，按这个顺序读：

1. `6.1`（功能总览：route / evolve 分别负责什么）
2. `6.1.1`（一条完整主链路：从 route 决策到 skill 落盘）
3. `6.3`（evolve 细节：结构化输出 + markdown 渲染）
4. `6.4`（route 细节：candidate 检查、hard guard、分支执行）
5. `6.5 ~ 6.7`（默认参数、自动触发、返回字段）

## 6.1 当前能力总览

- `amc-evolve-skill` 已统一支持 `mode=update|create`，并支持 `analyst_mode=success_only|error_only|combined`。
- `amc-route-skill` 已统一走 evolve 分支执行：
  - `update -> run_evolve(mode="update")`
  - `create -> run_evolve(mode="create")`
- route 已实现 stale candidate 清理：
  - 候选 skill 本地缺失时，清理向量索引与 embedding state；
  - 将 `candidate_skill` 置空后继续路由；
  - 返回 `stale_skill_cleanup_summary`。
- `candidate_skill=None` 语义已收敛为仅允许 `create`（router prompt + hard guard 双约束）。

### 6.1.1 Route / Evolve 分工（功能定位）

- `evolve` 的职责：
  - 输入：`skill(可选) + trajectory pool + analyst merge 结果`
  - 输出：一个满足统一格式约束的最终 `SKILL.md`（并在非 dry-run 时刷新 embedding）
  - 关注点：内容生成与文档结构正确性
- `route` 的职责：
  - 输入：anchor trajectory（以及召回邻居、候选 skill）
  - 输出：`update` 或 `create` 的分支决策，并调用 evolve 执行
  - 关注点：分支选择、门禁（support/confidence/hard guard）、stale candidate 清理

### 6.1.2 一条完整主链路（手动 route）

1. `amc-route-skill` 先构建 trajectory pool，并做 `support` 门禁  
2. 召回 top-1 candidate skill，若本地不存在则立即清理索引并置空 candidate  
3. `SkillRouter` 基于候选与轨迹池做 `update/create` 决策（含 hard guard）  
4. route 统一调用 evolve：
   - update -> `run_evolve(mode="update")`
   - create -> `run_evolve(mode="create")`
5. evolve 内部完成：
   - analyst 产 patch + merge
   - LLM 结构化输出（schema）
   - 程序渲染最终 `SKILL.md`
   - 非 dry-run 刷新 skill embedding
6. route 返回最终决策、分支结果、skill 名与审计字段

## 6.2 Skill 存储与索引

### 6.2.1 Skill 文件

- 路径：`data/skill/{skill_name}/SKILL.md`
- loader 约束：
  - frontmatter 必须包含 `name`、`description`；
  - 缺失则该 skill 被跳过并返回 warning。

### 6.2.2 Skill 向量索引

- 刷新入口：`amc-compute-skill-embedding`
- 默认索引表：`amc_skill_index`
- route create/update 完成后，`run_evolve` 在非 dry-run 下自动触发 embedding 刷新。

## 6.3 Evolve（当前实现）

入口：`amc-evolve-skill`

### 6.3.1 主流程

1. 读取 settings 与目标 skill（`mode=update` 必须提供 `skill_name`）。
2. 构建 trajectory pool（retrieve 召回或 `pool_override` 直传）。
3. 按 `analyst_mode` 运行 `SuccessAnalyst`/`ErrorAnalyst`，产出 patch 列表。
4. `hierarchical_merge_success_patches(...)` 合并 patch（作为后续 LLM 重写上下文）。
5. 调用 `_llm_rewrite_skill_markdown(...)` 生成结构化字段并渲染最终 `SKILL.md`。
6. 非 dry-run 时写回 skill 文件并刷新 embedding。

### 6.3.2 结构化输出与渲染

- `run_evolve` 不再要求 LLM 直接输出完整 markdown。
- LLM 输出 schema（`SkillRewriteOutput`）包含：
  - `skill_name`、`description`、`scope`
  - `when_to_use`、`when_not_to_use`
  - `workflow`、`checklist`、`evidence_trajectories`
- 代码侧统一渲染 `SKILL.md`：
  - frontmatter 始终写为 `name` / `description`
  - body 固定 section：
    - `## Scope`
    - `## When to use`
    - `## When not to use`
    - `## Workflow`
    - `## Checklist`
    - `## Evidence Trajectories`

### 6.3.3 输出约束与重试

- 优先使用 `response_format=json_schema`；若 provider 不支持则自动降级。
- 结构化解析或字段校验失败时最多重试 3 次。
- 重试失败会报错，并附带最后一次 `raw_preview`（便于定位模型返回格式问题）。

### 6.3.4 模式行为边界

- `mode=update`：skill 名固定为目标 skill，不改名。
- `mode=create`：允许新名字，最终经 `unique_skill_name(...)` 去重后落盘。
- 两个模式都会复用 analyst/merge 结果作为 LLM rewrite 的证据上下文。

## 6.4 Route（当前实现）

入口：`amc-route-skill`

### 6.4.1 主流程

1. 构建 trajectory pool（anchor + neighbors）。
2. support 门禁（默认 `support=4`）：
   - 不满足则返回 `support_insufficient`，停止 route 分支执行。
3. 候选 skill 召回（默认 top-1）并执行本地存在性校验。
4. 若候选 skill stale：
   - 删除向量记录 + embedding state；
   - `candidate_skill=None`；
   - 记录 `stale_skill_cleanup_summary`。
5. Router 决策 `update/create`（LLM 失败回退 heuristic）。
6. hard guard：
   - 无 candidate 时强制 `create`；
   - `update` 仅在高匹配且置信度达阈值时保留，否则强制 `create`。
7. 分支执行统一走 evolve：
   - `update -> run_evolve(mode="update", pool_override=...)`
   - `create -> run_evolve(mode="create", pool_override=...)`

### 6.4.2 当前语义说明

- `decision.task_type_summary` 在 `candidate_skill=None` 时来自 pool 的 task/摘要拼接。
- `decision.suggested_skill_name` 在无 candidate 场景下与 `task_type_summary` 同值。
- create 分支中 `suggested_skill_name` 仅作为 name seed；最终落地 skill 名由 evolve create 结果决定。

## 6.5 Route / Evolve 默认参数

### 6.5.1 `amc-route-skill`

- `top_k=8`
- `trajectory_min_score=0.7`
- `support=4`
- `skill_top_k=1`
- `merge_batch_size=8`
- `max_parallel_analysts=32`
- `analyst_mode=combined`
- `confidence_threshold=0.7`
- `force_mode=auto|update|create`

### 6.5.2 `amc-evolve-skill`

- `mode=update`（CLI 默认）
- `analyst_mode=success_only`（CLI 默认）
- `top_k=8`
- `trajectory_min_score=0.7`
- `merge_batch_size=8`
- `max_parallel_analysts=32`

## 6.6 Inter-Trajectory 自动触发（当前）

- 状态后端：Neo4j
- 关系类型：
  - `INTERTRAJ_SIMILAR`
  - `INTERTRAJ_ACTIVATED_PENDING`
- trigger 阈值（默认）：
  - `trigger_threshold=8`
  - `route_support=4`
- batch 触发模式：
  - `batch_trigger_mode=end_of_batch`
  - batch 末尾会检查“本批新节点 + 被本批激活到阈值的旧节点”。

## 6.7 关键返回字段（当前）

### 6.7.1 `amc-route-skill`

- `decision`
- `candidate_skill`
- `updated_skill_name` / `created_skill_name`
- `trajectory_pool`
- `branch_result`
- `stale_skill_cleanup_summary`
- `embedding_refresh_summary`
- `timing`
- `warnings`

### 6.7.2 `amc-evolve-skill`

- `mode` / `analyst_mode`
- `skill_name`
- `trajectory_pool`
- `patch_summary`
- `apply_summary`
- `embedding_refresh_summary`
- `timing`
- `warnings`

## 6.8 当前已知限制（仅保留现状）

1. `task_type_summary` 在无 candidate 场景下偏“ID 拼接”，语义摘要质量有限。
2. `mode=update` 当前不支持改名（skill name 固定）。
3. analyst 输入仍有截断策略（如 `trajectory[:14]`、`skill_markdown[:6000]`），用于控制上下文长度。

---

后续如调整 route 决策摘要策略、update 改名策略、或 evolve 返回契约，请同步更新本文件，避免文档与代码漂移。
