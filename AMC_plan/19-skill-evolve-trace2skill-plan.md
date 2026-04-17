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
  -> build evolve pool (anchor + neighbors)
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

## 19.5.1 轨迹池构建（依赖现有 retrieve）

为复现“broad execution experience”，建议：
- 默认 `top_k=8` 控制成本；可按效果逐步提高到 12/16（上限再视成本评估）；
- 返回结果保留相似度分数与证据字段；
- 对 highly-duplicate 轨迹做去重（如同一 task、同一 idempotency_key）。

### 19.5.2 Success / Error 分析器

Success analyst 输入：
- trajectory 原文
- 当前 skill 目录快照
- 目标：抽取可泛化 SOP（不是 task-specific trick）

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
7. 不影响已有 `amc-evolve-skill --skill-name ...` 与 retrieve/commit 主链路。

