# 19 — 基于 Trace2Skill 的 Skill Evolve / Create 自动路由实现计划

## 19.1 背景与目标

目标是复现并落地论文 *Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills* 的核心思想，
用于 AMC 的 skill 进化：

1. 先 commit 一批 trajectory；
2. 以一个“锚点 trajectory”为中心，按**向量相似 + 轨迹图相似**召回相似轨迹；
3. 对召回轨迹并行分析，提炼 trajectory-local lessons；
4. 将局部 patch 分层合并为统一 skill 更新（`SKILL.md + references/`）；
5. 更新 skill 后重新计算 skill embedding，供后续 retrieve 使用。

本计划与 `18-skill-retrieve-plan.md` 的关系：
- 18 负责“skill 检索与索引”；
- 19 负责“skill 内容进化（evolve）+ 自动判定更新/新建（route）”。

---

## 19.2 论文方法到 AMC 的映射

论文 Trace2Skill 三阶段：
- Stage 1: 轨迹池构建（成功/失败轨迹）；
- Stage 2: Success/Error analyst 并行提 patch；
- Stage 3: 分层冲突合并，得到单一一致 skill 更新。

AMC 映射：

1. **Stage 1（轨迹池）**  
   AMC 不额外在线 rollout，直接使用已 commit 轨迹池。  
   对用户指定锚点 trajectory，调用现有 retrieve（向量+图）召回 top-k 相似轨迹，形成 evolve pool。

2. **Stage 2（并行 patch proposal）**  
   并行 dispatch 分析器（LLM 子代理或任务线程）：
   - Success analyst：提炼“有效 SOP（Standard Operating Procedure，标准操作流程）、稳定步骤、校验清单”；
   - Error analyst：提炼“失败根因、防错规则、验证步骤”。

3. **Stage 3（hierarchical merge）**  
   对 patch 池做批次分层合并，加入冲突检测与格式校验，输出一个统一 skill 目录更新。

---

## 19.3 AMC-Evolve 工作流（以锚点 trajectory 为中心）

```text
input: skill_name, anchor_trajectory_id, top_k
  -> load anchor trajectory (replay)
  -> build retrieve query from anchor (task_description + partial_trajectory)
  -> retrieve top_k similar trajectories (semantic + graph hybrid)
  -> filter by min score threshold (default 0.6)
  -> build evolve pool (anchor + neighbors that pass threshold)
  -> parallel analyst patch proposal (Phase A: success_only)
  -> hierarchical merge + conflict checks
  -> apply edits to data/skill/{skill_name}/
  -> run skill format validation
  -> run amc-compute-skill-embedding (incremental)
output: evolved skill + evolve report
```

---

## 19.4 Evolve 输入/输出定义（建议）

### 19.4.1 输入

`SkillEvolveRequest`（建议）：
- `skill_name: str`
- `anchor_trajectory_id: str`
- `top_k: int`（默认 8）
- `trajectory_min_score: float`（默认 0.6；用于 trajectory pool 过滤）
- `include_anchor: bool`（默认 true）
- `analyst_mode: "combined" | "error_only" | "success_only"`（默认 success_only；Phase A 仅启用 success_only）
- `merge_batch_size: int`（默认 8；表示“每轮合并时一个 merge 节点最多同时处理多少个 patch”）
- `max_parallel_analysts: int`（默认 32）
- `dry_run: bool`（仅产出 patch 与报告，不写 skill）

### 19.4.2 输出

`SkillEvolveResult`（建议）：
- `skill_name`
- `anchor_trajectory_id`
- `retrieved_trajectory_ids`
- `patch_stats`（success/error/total/accepted/rejected）
- `merge_stats`（levels, conflicts, dedup）
- `applied_files`（SKILL.md、references/* 等）
- `embedding_refresh_summary`（调用 `amc-compute-skill-embedding` 的结果摘要）
- `warnings` / `errors`

---

## 19.5 关键实现细节

### 19.5.0 Commit 阶段 overview 细节增强（先提高输入质量）

当前 skill 提取过于泛化的一个根因是 overview 信息密度不足。需要先改 commit 侧摘要策略：

1. 放宽 overview 长度上限（相对现状提升约 2-3 倍）；
2. 明确要求 overview 包含“关键步骤级细节”：
   - 每个关键阶段用了什么工具；
   - 每步动作做了什么；
   - 关键观察/结果是什么；
   - 失败重试与修正（若存在）；
3. 允许较长但结构化输出，优先保证可用于后续 skill 提取，而不是过度压缩。

建议在 `LLMTrajectorySummarizer` prompt 中把 `l1` 改为“细节优先”的长摘要（例如 1200-1800 中文字符区间，按实践再调）。

### 19.5.1 轨迹池构建（依赖现有 retrieve）

为复现“broad execution experience”，建议：
- 默认 `top_k=8` 控制成本；可按效果逐步提高到 12/16（上限再视成本评估）；
- 返回结果保留相似度分数与证据字段；
- 对候选轨迹执行最小分数阈值过滤：`score >= trajectory_min_score`（默认 0.6）；
- 即使过滤后数量不足，也不回填低分轨迹（宁缺毋滥）；
- 对 highly-duplicate 轨迹做去重（如同一 task、同一 idempotency_key）。

分数口径：
- 优先使用 retrieve 的融合分数（vector+graph 的 `total_score`）；
- 当图分支不可用时，回退到 semantic score，但仍应用同一阈值并写入 warning。

### 19.5.2 Success / Error 分析器

Success analyst 输入：
- trajectory 原文
- 当前 skill 目录快照
- 目标：抽取“同类任务簇下可复用、但足够具体”的 SOP（specificity 优先，避免 domain 级泛化）

Error analyst 输入：
- trajectory + 失败迹象（若可得）
- 当前 skill 目录快照
- 目标：提出可验证根因与防错 patch

说明：
- AMC 当前未强制保存 ground truth；因此 Error analyst 先采用“弱监督错误信号”：
  - trajectory 中显式失败/重试模式；
  - tool output error 片段；
  - 既有反馈日志（若有）。
- 后续可扩展为“带标准答案/验收脚本”的强监督模式。

### 19.5.3 Patch 结构（建议）

每个 analyst 输出：
- `title`
- `type`: `success` / `error`
- `evidence_trajectory_id`
- `generalization_claim`
- `edits`: list of file edits（优先结构化 edit，不直接 free-form）

### 19.5.4 分层合并（Trace2Skill 核心）

采用分层 merge（batch size = `merge_batch_size`）：
- Level-0: trajectory-level patches
- Level-i: 上一层 patch 合并结果
- 直到得到单一 `final_patch`

`merge_batch_size` 含义补充：
- 若当前层有 `N` 个 patch，每个 merge 节点最多吃 `B=merge_batch_size` 个 patch；
- 下一层节点数约为 `ceil(N / B)`；
- 总层数约为 `ceil(log_B(N))`；
- `B` 越大：层数更少、单次上下文更长；`B` 越小：层数更多、每次更稳更省上下文。

确定性 guardrails（对齐论文）：
1. 引用不存在文件 -> reject；
2. 同文件同区间重叠 edit -> conflict；
3. create/link 原子对（例如新增 `references/x.md` 时必须在 `SKILL.md` 建立引用）；
4. 应用后做 skill format 校验。

### 19.5.5 prevalence bias（防过拟合）

merge 提示中显式要求：
- 高频重复 patch 优先；
- 单次出现且无支撑的 patch 降权；
- 将“广泛共识规则”写入 `SKILL.md`；
- 将“低频但有价值的边角情况”写入 `references/`。

---

## 19.6 代码改造清单（建议）

### 新增模块

- `src/core/skills/evolve/trajectory_pool_builder.py`
  - 根据锚点 trajectory 调用 retrieve，构建 evolve pool

- `src/core/skills/evolve/analyst_success.py`
- `src/core/skills/evolve/analyst_error.py`
  - 并行 patch proposal

- `src/core/skills/evolve/patch_merge.py`
  - 分层 merge + conflict/dedup/validation

- `src/core/skills/evolve/skill_apply.py`
  - 将 final patch 应用到 `data/skill/{skill_name}`

- `src/cli/evolve_skill.py`
  - CLI：`amc-evolve-skill`

### 修改模块

- `pyproject.toml`
  - 注册命令：`amc-evolve-skill`

- `src/core/skills/skill_loader.py`
  - 增加 evolve 所需读取辅助（references/scripts 索引）

- `src/cli/compute_skill_embedding.py`
  - 保持可被 evolve 结束后自动调用

---

## 19.7 与现有 AMC 链路耦合点

1. **Trajectory 检索**：复用现有 hybrid retrieve（语义 + 图）；
2. **Skill 存储**：复用 `data/skill/{skill_name}/`；
3. **Skill 索引**：复用 `amc-compute-skill-embedding`；
4. **重置策略**：保持 `reset_amc_storage.py` 默认不清 skill embedding。

---

## 19.8 分阶段实施建议

### Phase A（最小闭环）
- CLI `amc-evolve-skill`（支持 anchor + top_k）
- trajectory pool 构建
- success analyst 并行 patch proposal（`success_only`）
- 单层 merge + 基础冲突检测（先不引入 error patch）
- skill 落盘 + 调用 `amc-compute-skill-embedding`

### Phase B（论文关键能力增强）
- 引入 `error_only` 与 `combined` 模式（含弱监督失败信号）
- 分层 merge（多层）
- prevalence bias 强化
- create/link 原子约束
- references 分流策略

### Phase C（评估与稳定性）
- 引入 held-out 评估集，计算 evolve 前后成功率
- patch 贡献度分析（影响追踪）
- 无效 patch 自动回滚/降级

---

## 19.9 测试与验收口径

### 功能验收

1. 输入 `anchor_trajectory_id + top_k` 可生成 evolve pool；
2. 能产出并应用 final skill patch；
3. evolve 后自动触发 skill embedding 更新；
4. retrieve 可命中新版 skill（description 更新生效）。

### 方法一致性验收（对齐论文思想）

1. patch proposal 是并行执行，不是顺序单条更新；
2. merge 是 many-to-one（不是 online sequential edit）；
3. 输出是单一可移植 skill 目录（非运行时 memory bank）。

### 质量验收

1. 对同一 domain 的 held-out 任务，evolve 后成功率提升；
2. 对相邻 domain（OOD-lite）不显著退化；
3. 无明显 skill 膨胀（`SKILL.md` 与 references 可控增长）。

---

## 19.10 风险与缓解

1. **锚点检索集合噪声大，导致 patch 污染**
   - 缓解：设置最小分数阈值 + diversity 去重 + analyst 证据约束。

2. **Error analyst 无 ground truth 时误判根因**
   - 缓解：先用弱监督并标注可信度；后续接入可执行验证器。

3. **合并冲突导致 skill 结构损坏**
   - 缓解：分层合并 + 确定性 guardrails + apply 前验证。

4. **过拟合锚点任务**
   - 缓解：prevalence bias + references 分流 + held-out 回归测试。

---

## 19.11 论文关键 Prompt 与实现参数备忘（防遗忘）

以下内容用于后续实现时快速对齐论文，不追求逐字复刻，但保留核心约束。

### 19.11.1 Stage 1（Trajectory Generation）提示词要点

- 角色：领域专家代理（如 spreadsheet expert）。
- 输入：预加载 `S0` 的 `SKILL.md` 内容到 system prompt。
- 交互：ReAct（reasoning/tool/observation）多轮。
- 目标：产出可追踪轨迹（含推理、工具调用、观察、结果标签）。

### 19.11.2 Stage 2（Analyst）提示词要点

Success analyst（单次调用）：
- 提炼成功轨迹中的可泛化行为模式；
- 要求“广覆盖 + 频次优先 + 可操作”；
- 增加硬约束：**specificity 优先于 generality**；
- 禁止输出过宽泛技能（例如覆盖整个 domain 的大而空描述）；
- 优先输出“聚焦某一类任务簇”的 SOP（例如 `clean-and-place`、`search-then-relocate` 级别）；
- 输出紧凑 memory/patch，不写任务特例。

Error analyst（多轮 ReAct）：
- 任务：定位失败根因，并通过最小修复验证因果；
- 若无法得到可验证根因，则放弃该条 patch（质量门禁）；
- 输出 failure causes + generalizable failure memories。

Merge operator（合并器）：
- 去重、冲突消解、保留独特洞察；
- 偏好“多轨迹重复出现”的 patch（prevalence bias）；
- 强约束行级独立编辑；
- create/link 成对原子保留（references 文件与 `SKILL.md` 引用必须一起保留或一起丢弃）。

### 19.11.3 论文实现参数（可作为 AMC 初始参考）

- Stage 2 并行分析器：约 128 并发子代理（论文设置）
- merge batch size：32（论文设置）
- ReAct 分析回合预算：100 turns（论文设置）
- 合并层数：`L = ceil(log_B(|P|))`（`B` 为 merge batch size）

### 19.11.4 AMC 侧落地取舍（当前版本）

- 先做 `success_only`（因为当前轨迹池几乎全成功轨迹）；
- 默认 `top_k=8`，先保证稳定与成本，再逐步扩大；
- `merge_batch_size` 默认 8，后续通过离线评测决定是否升到 16/32；
- 先实现“可运行闭环”，再补 error analyst 与多层 merge 全量能力。

---

## 19.12 自动路由（不再手工指定 skill）

在 evolve 基础上新增自动路由能力：用户仅提供锚点 trajectory，不再手工传 `skill_name`。

路由目标：
1. 先检索最相似 skill（向量）与相似 trajectory 池（向量+图）；
2. 将候选 skill 与 trajectory 池交给 LLM 判定：
   - `update`：更新已有 skill（走 evolve）
   - `create`：创建新 skill（走 create）
3. 两条路径都在结束后刷新 skill embedding。

---

## 19.13 Route 决策规则（Prompt + 程序双重约束）

### 19.13.1 Prompt 硬约束（必须写死）

> 只有当“候选 skill 的 scope 与 trajectory 任务类型**非常相关**（highly related）”时才允许返回 `update`；  
> 只要是中等相关、弱相关或不确定，必须返回 `create`。

### 19.13.2 结构化输出（建议）

```json
{
  "decision": "update|create",
  "confidence": 0.0,
  "scope_match_level": "high|medium|low",
  "reasoning": "..."
}
```

### 19.13.3 程序侧 Hard Guard（防漂移）

1. `scope_match_level != high` -> 强制 `create`；
2. `scope_match_level == high` 但 `confidence < threshold`（如 0.7）-> 强制 `create`；
3. 候选 skill 为空 -> 直接 `create`；
4. 非法 JSON 或非法 `decision` -> fallback `create`。

---

## 19.14 双分支执行

### 19.14.1 `update` 分支（复用 evolve）

当路由结果为 `update`：
- 取 top-1 候选 skill 作为 `skill_name`；
- 调用现有 `amc-evolve-skill` 主流程（Phase A 先 `success_only`）；
- 完成后调用 `amc-compute-skill-embedding` 增量刷新。

### 19.14.2 `create` 分支（新增）

当路由结果为 `create`：
1. 生成新 skill 名称（slug）：
   - 小写、`[a-z0-9_]+`、长度受限、冲突自动加后缀；
2. 初始化目录：`data/skill/{new_skill_name}/SKILL.md`（按模板）；
3. 生成初始内容（frontmatter + Scope + When to use + Workflow + Checklist）；
4. 引用 trajectory 证据（来自本次 trajectory pool）；
5. 调用 `amc-compute-skill-embedding` 增量刷新。

---

## 19.15 接口与模块扩展建议

### 新增入口

建议新增统一 CLI：
- `amc-route-skill`
  - 输入：`anchor_trajectory_id`、`account_id`、`agent_id`、`top_k`
  - 行为：自动 `update/create` 路由并执行
  - 输出：统一 JSON（含 decision、candidate_skill、updated/created skill 名称、timing）

### 新增模块

- `src/core/skills/routing/skill_router.py`
  - skill 候选检索 + LLM 路由判定 + hard guard
- `src/core/skills/create/skill_creator.py`
  - 新 skill 初始化与草稿生成
- `src/cli/route_skill.py`
  - 统一路由入口

---

## 19.16 分阶段补充（在现有 A/B/C 上叠加）

### Phase A1（Route MVP）
- `amc-route-skill` 可跑通；
- skill top-1 + trajectory pool + LLM route；
- commit 摘要 prompt 升级（overview 更细，支持步骤级工具/动作细节）；
- trajectory pool 增加 `trajectory_min_score` 过滤（默认 0.6，且不回填低分）；
- success analyst prompt 增加 “specificity > generality” 硬约束；
- `update` 复用 evolve；
- `create` 走模板创建；
- 两分支都刷新 embedding。

### Phase A2（质量增强）
- create 分支由 trajectory 自动归纳 description/scope；
- route 决策审计（decision + evidence）；
- slug 命名与冲突策略完善。

### Phase B（鲁棒性）
- skill 候选从 top-1 扩展到 top-k 重排；
- 置信度阈值校准；
- route 回归测试集与自动评测。

---

## 19.17 Route 相关测试验收补充

1. 无候选 skill -> `create`；
2. 候选 skill 高相关且高置信 -> `update`；
3. 中/低相关或低置信 -> `create`（即使 LLM 给 `update`）；
4. `update` 路径会修改已有 skill；
5. `create` 路径会生成新 skill 目录；
6. 两路径结束后都刷新 skill embedding；
7. trajectory pool 过滤遵循 `trajectory_min_score`，低分轨迹不会被拼进同一 skill 提取；
8. 输出 skill 不应出现“过于宽泛”的 scope（需通过 specificity 检查规则）；
9. 不影响已有 `amc-evolve-skill --skill-name ...` 与 retrieve/commit 主链路。

---

## 19.18 新增链路：Commit -> InterTrajectory Graph -> Route（自动触发）

目标：在保留现有 `amc-evolve-skill` / `amc-route-skill` 手动链路的前提下，新增一条由 commit 驱动的自动链路。

核心思路：
1. 每条已 commit trajectory 是 inter-trajectory graph 的一个节点；
2. 新节点 `A` 入图后，和现有节点 `B` 计算融合分数（semantic + graph）；
3. 若 `score >= intertraj_edge_threshold`（默认 0.7），则建立 `A <-> B` 无向边；
4. 边建立后，`A` 与 `B` 互相加入对方 activate 列表（pending 累积）；
5. 当某节点 `X` 的 activate 数达到阈值 `activate_trigger_threshold`（默认 8），触发一次 skill route：
   - candidate trajectories = `X + activate(X)`；
   - 复用现有 route/update/create 逻辑；
   - route 结束后清空 `activate(X)`，重新累积。

说明：这里的“触发 route”是**候选集驱动**，不再从 retrieve 相似度检索候选轨迹；候选轨迹直接来自 activate 列表。

### 19.18.1 与现有 Route 门禁的关系

- 现有 route 门禁：`support`（默认 4）；
- 新增 activate 触发门槛：`activate_trigger_threshold`（默认 8）；
- 两者关系：先满足 activate 触发（>=8）才进入 route；进入 route 后仍保留 `support` 校验（双保险）。

---

## 19.19 设计细化（最小侵入，最大复用）

### 19.19.1 数据模型（新增，直接落 Neo4j）

建议新增 inter-trajectory graph 状态存储（按 account/scope/owner_space 隔离）：

- `(:Trajectory {trajectory_id, account_id, scope, owner_space, ...})`
- `(:Trajectory)-[:INTERTRAJ_SIMILAR {score_total, score_semantic, score_graph, created_at}]-(:Trajectory)`
  - 逻辑上无向边；存储上可统一为一条关系（`min_id -> max_id`）避免重复。
- `(:Trajectory)-[:INTERTRAJ_ACTIVATED_PENDING {created_at}]->(:Trajectory)`
  - 表示“源节点的 activate 列表中存在目标节点”。
- `(:Trajectory {last_intertraj_triggered_at})`
  - 记录最近一次触发 route 的时间，便于审计和重试。

结论：该状态层按你的建议，默认直接使用 Neo4j，不再以本地状态文件作为主实现。

### 19.19.2 Commit 后处理流程（新增）

挂载点：
- 单条 commit：`CommitOrchestrator._persist_prepared_commit()` 成功持久化后（`status=accepted`）；
- batch commit：`/commit/batch` 全部 item 完成后统一触发一次（见 19.19.3）。

流程：
1. 以新 trajectory `A` 构建 query（复用 `TrajectoryPoolBuilder` 的 anchor query 生成策略）；
2. 调用 retrieve（semantic + graph）获取候选旧节点；
3. 对每个候选 `B`：
   - 若 `total_score >= intertraj_edge_threshold`（默认 0.7）则建边 `A<->B`；
   - 同时 `A.pending += B`，`B.pending += A`（去重）；
4. 将 activate pending 写入 Neo4j（`A -> B` 与 `B -> A`）；
5. 单条 commit 模式下，检查 `A.pending` 是否达到 `activate_trigger_threshold`（默认 8）；
6. 若达到阈值，触发 route 执行（candidate trajectories = `A + A.pending`）。

备注：commit API 本身应保持低延迟，route 触发建议异步执行（后台 worker / 事件队列）。

### 19.19.3 Batch Commit 触发语义（新增）

按你的建议，batch commit 采用“整批结束后统一触发”：

1. batch 内每个成功 item 仍正常建边并更新 pending（写 Neo4j）；
2. 不在单个 item 完成时立即触发 route；
3. 等 batch 全部 item 处理完后，统一扫描“本批涉及节点”中 pending 达阈值的锚点；
4. 对达阈值锚点逐个触发 route（可串行或限流并行）；
5. 每个锚点 route 完成后清空其 pending，再处理下一个锚点。

这样能避免 batch 内重复触发、降低抖动，并保证同一批新增轨迹能共同参与一次更完整的候选集。

### 19.19.4 Route 复用方式（关键）

在 `src/cli/route_skill.py` 上增加“显式候选池入口”（内部函数或 CLI 参数）：

- 新增内部函数（建议）：
  - `run_route_with_candidates(anchor_trajectory_id, candidate_trajectory_ids, ...)`
- 行为：
  1. 跳过 `TrajectoryPoolBuilder.build_success_pool` 的相似检索；
  2. 直接加载 candidate trajectories 组装 `pool_result`；
  3. 后续完全复用现有 route 逻辑（candidate skill recall + LLM route + update/create 分支）。

这样不改动既有 `amc-route-skill` 默认行为，仅新增可复用入口供 commit 自动链路调用。

### 19.19.5 activate 清空策略

按需求：当节点 `X` 触发并完成一次 route 后，清空 `X.pending`。

实现建议：
- route 终态为 `update` / `create` / `support_insufficient` 时都清空 `X.pending`；
- 若是系统异常（例如依赖不可用）则不清空，保留重试机会，并记录 warning/audit。

---

## 19.20 配置项扩展（建议默认值）

在 `config` + `AppSettings` 增加：

- `skills.intertrajectory.enabled: true`
- `skills.intertrajectory.edge_threshold: 0.7`
- `skills.intertrajectory.trigger_threshold: 8`
- `skills.intertrajectory.max_neighbors_per_commit: 32`（防止单次 commit 扫描过大）
- `skills.intertrajectory.backend: neo4j`
- `skills.intertrajectory.edge_rel_type: INTERTRAJ_SIMILAR`
- `skills.intertrajectory.pending_rel_type: INTERTRAJ_ACTIVATED_PENDING`
- `skills.intertrajectory.async_trigger: true`
- `skills.intertrajectory.batch_trigger_mode: end_of_batch`

并复用现有 route 参数：
- `trajectory_min_score`（默认 0.7）
- `support`（默认 4）

---

## 19.21 代码改造增量（在 19.6 基础上叠加）

### 新增模块（建议）

- `src/core/skills/intertrajectory/neo4j_state_store.py`
  - 读写 inter-trajectory graph 状态（节点 pending、边信息，Neo4j 实现）
- `src/core/skills/intertrajectory/linker.py`
  - 处理“新节点入图 -> 建边 -> activate 累积”
- `src/core/skills/intertrajectory/trigger.py`
  - 达阈值后组装 candidate trajectories 并调用 route 入口

### 修改模块（建议）

- `src/app/orchestrators/commit_orchestrator.py`
  - 单条 commit 成功后发布 intertrajectory link/trigger 任务
- `src/api/routes/commit.py`
  - batch commit 在“整批结束”后统一触发 intertrajectory route 扫描
- `src/cli/route_skill.py`
  - 增加“候选轨迹直传”入口（复用 route 主逻辑）
- `src/app/config.py`
  - 增加 intertrajectory 配置项

---

## 19.22 测试与验收补充（commit->route 自动链路）

1. 新节点 `A` 与旧节点 `B` 分数>=0.7 时，建立无向边，且互相写入 pending activate；
2. 分数<0.7 不建边，pending 不增加；
3. 单条 commit 下，`pending(A)` 达到 8 时自动触发 route；
4. batch commit 下，必须等整批 commit 完成后才统一触发 route；
5. 自动触发 route 使用 `A + pending(A)` 作为候选集，不再做候选相似检索；
6. route 完成后仅清空触发节点的 pending 列表；
7. route 异常时 pending 不清空，并可重试；
8. 不影响手动 `amc-route-skill` / `amc-evolve-skill` 既有行为；
9. batch commit 场景下多节点并发入图，边与 pending 无丢失（含并发写一致性测试）。

---

## 19.23 统一实施 Phase（整合版）

为避免当前文档中 Phase A/B/C、A1/A2 并行描述造成歧义，后续按以下**5 个 Phase**推进（含新链路）：

### Phase 1：Evolve/Route 基线收敛（已实现能力固化）

状态：已完成（进入维护态，后续作为 Phase 2+ 的回归基线）。

范围：
- 固化 `amc-evolve-skill` + `amc-route-skill` 当前主流程；
- 固化 `trajectory_min_score`、`support`、hard guard、specificity 约束；
- 固化 create/update 双分支与 embedding 刷新闭环。

目标：
- 手动 route/evolve 行为稳定、可回归、可观测，作为 commit 自动触发链路的基础。

验收：
- 通过现有 route/evolve 单测与关键集成测试；
- 同一输入在多次运行下决策与输出差异可控。

### Phase 2：InterTrajectory Graph 基础层（Neo4j）

状态：已完成（建边与 pending 累积已接入 commit；自动触发 route 留到 Phase 3/4）。

范围：
- 在 Neo4j 中落地 `INTERTRAJ_SIMILAR` 与 `INTERTRAJ_ACTIVATED_PENDING`；
- 实现新节点入图后建边与 pending 累积（仅状态更新，不触发 route）；
- 增加幂等与去重规则（避免重复边、重复 pending）。

目标：
- 新链路的数据底座先稳定，写路径可独立验证。

验收：
- 给定 commit 序列，边与 pending 状态与阈值规则一致；
- 并发写入下无明显重复/丢失。

### Phase 3：单条 Commit 自动触发 Route

状态：已完成（单条 commit 达阈值后自动 route，终态清空 pending，异常不清空）。

范围：
- 在单条 commit 成功后检查 pending 阈值（默认 8）；
- 达阈值则调用 `run_route_with_candidates(...)`（候选集为 `A + pending(A)`）；
- route 终态（`update/create/support_insufficient`）清空触发节点 pending；
- 异常时保留 pending 并记录审计/告警。

目标：
- 跑通“commit -> graph -> route -> evolve/create”的单条自动闭环。

验收：
- 单条 commit 触发链路可稳定完成；
- 失败重试语义符合预期（异常不清空，成功清空）。

### Phase 4：Batch Commit 统一触发（end_of_batch）

状态：已完成（batch item 级触发关闭；改为 batch 结束后统一触发）。

范围：
- batch 内 item 仅负责建边与 pending 累积；
- 全 batch 完成后统一扫描并触发 route（不在 item 级别触发）；
- 增加批内去重与触发限流策略（避免同批抖动和风暴）。

目标：
- 满足“batch commit 完成后再统一 evolve/create”的业务约束。

验收：
- batch 场景 route 只在 end-of-batch 触发；
- 同批多个锚点触发时顺序/并发可控，最终状态一致。

### Phase 5：增强与评估（论文能力补齐）

范围：
- 引入 `error_only` / `combined` 分析器与多层 merge；
- top-k skill 候选重排、阈值校准、prevalence bias 强化；
- held-out 评估、回归集、回滚/降级策略完善。

目标：
- 从“可运行”提升到“高质量、可扩展、可评估”。

验收：
- 线下评估指标（成功率、退化率、膨胀控制）达标；
- 线上稳定性与回归指标满足门槛。

### 19.23.1 建议实施顺序

1. 先完成 Phase 2（仅图状态层）；
2. 再做 Phase 3（单条自动触发）；
3. 再做 Phase 4（batch end-of-batch 统一触发）；
4. 最后进入 Phase 5（质量增强）。

说明：Phase 1~4 已落地，后续以 Phase 5 为主线推进质量增强与评估。

