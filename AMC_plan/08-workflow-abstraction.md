# 08 — 从具体轨迹到通用 Workflow 的抽象方案（含实现状态）

本文档保留 Workflow 设计方案，同时明确标注当前哪些能力尚未实现，避免和现行代码状态混淆。

## 8.1 当前实现状态（先看这个）

### 8.1.1 已实现到什么程度

当前 AMC **尚未落地**通用 Workflow 模板系统。`core/workflow` 仍是预留目录。

### 8.1.2 明确未实现项

以下能力当前均未进入运行链路：

- `core/workflow/cluster.py`
- `core/workflow/subpath_mining.py`
- `core/workflow/template_builder.py`
- `core/workflow/service.py`
- workflow 专用 API 路由
- workflow 持久化与版本管理
- retrieve 的 workflow template 召回分支

### 8.1.3 当前可替代的“复用层”

虽然 workflow template 未落地，但已有两类复用能力在运行：

1. trajectory 复用（见 `04`）  
2. skill 复用（`skill retrieve + route + evolve`，见 `19`）

---

## 8.2 Workflow 目标（设计方案）

给定同类任务（例如“sales analysis”）的 N 条轨迹，输出：

- 通用步骤骨架（阶段序列）；
- 每阶段推荐工具与参数模板；
- 常见失败分支与修复策略；
- 适用前提与不适用边界。

> 状态：**设计目标，暂未实现**

## 8.3 输入与产出（设计方案）

### 8.3.1 输入

- 一组高质量轨迹（按主题标签/工具骨架筛选）；
- 每条轨迹的反馈分与执行结果；
- 可选专家标注（关键步骤、禁止步骤）。

说明：

- 轨迹入口从 `ctx://{scope}/{owner_space}/memories/trajectories/...` 读取；
- 节点/边结构通过 `graph_pointer` 到图后端（如 Neo4j）拉取。

### 8.3.2 输出

```python
class WorkflowTemplate:
    workflow_id: str                   # 模板唯一 ID（版本管理主键）
    account_id: str                    # 账户隔离字段
    name: str                          # 模板名称
    tags: list[str]                    # 适用主题标签（用于组织与展示）
    stages: list[WorkflowStage]        # 主流程阶段定义（工具链骨架）
    failure_playbook: list[FailurePattern]  # 常见失败模式与修复策略
    confidence: float                  # 模板可信度（覆盖率/成功率/反馈综合）
    source_trajectories: list[str]     # 来源轨迹 ID 列表（可审计）
```

> 状态：**数据模型提案，暂未实现**

## 8.4 抽象方法（设计方案）

### Phase A：规则聚合（MVP+1）

- 按工具序列 + 关键节点语义聚类；
- 提取频繁子路径（frequent subpath mining）；
- 生成“主路径 + 分支路径”。

### Phase B：图模式挖掘

- 对轨迹图做 motif mining；
- 对齐不同轨迹中的同构子图；
- 提炼通用依赖关系（不仅是顺序，还包括数据依赖）。

### Phase C：LLM 辅助泛化

- 将候选路径转成可读 workflow 描述；
- 生成参数模板和检查清单；
- 由人审后发布到 team/datalake scope。

> 状态：**三阶段方法论，暂未实现**

## 8.5 质量门槛（设计方案）

仅当满足以下条件才允许发布 Workflow：

- 来源轨迹数量 >= M（如 10）；
- 覆盖率 >= C（如该类任务 60% 可套用）；
- 失败率不高于基线；
- 人工审阅通过（至少一名领域负责人）。

> 状态：**发布门禁提案，暂未实现**

## 8.6 与 Retrieve 的联动（设计方案）

retrieve 可先召回 workflow template，再补充具体轨迹证据：

```text
Workflow first -> trajectory evidence second
```

这样可同时给上层 Agent：

- 可复用“骨架”（高层策略）
- 可复制“片段”（底层操作细节）

> 状态：**联动策略提案，暂未实现**

## 8.7 风险与控制（设计方案）

- 过度泛化：模板看似通用但忽略边界；
- 误学习：把错误操作固化为模板；
- 版本漂移：底层 Skill/表结构变化导致模板失效。

控制手段：版本化、周期重评估、人工门禁。

> 状态：**治理建议，暂未实现**

## 8.8 与其他文档关系

- `03`：当前 commit 主链路与 intertrajectory 联动
- `04`：当前 trajectory retrieve
- `19`：当前 skill route/evolve
- `07`：workflow 的中长期设计与状态边界

