# 07 — 多租户与访问控制（当前实现）

本文档只记录当前代码中已落地的隔离与访问控制行为，并显式标注未实现能力。

## 7.1 当前作用域模型

当前 commit/retrieve 路由接受并传递以下 scope：

- `agent`
- `team`
- `datalake`
- `user`

轨迹 URI 规则：

`ctx://{scope}/{owner_space}/memories/trajectories/{trajectory_id}`

默认规则：

- `scope=agent` 时，`owner_space` 默认等于 `agent_id`；
- 非 `agent` scope 必须显式给 `owner_space`。

## 7.2 当前接口上下文约束

### 7.2.1 Commit / Batch Commit

- 必须提供 `X-Account-Id` 与 `X-Agent-Id`（或 body 兼容字段）。
- header/body 冲突会返回 `422`。
- `scope=agent` 时强制 `owner_space == agent_id`。

### 7.2.2 Retrieve

- 必须提供 `X-Account-Id`；
- `X-Agent-Id` 与 body.agent_id 二选一（header 优先）。

### 7.2.3 Promote

- 必须提供 `X-Account-Id` 与 `X-Agent-Id`。
- 仅允许“将该 agent 自己的 agent-scope 轨迹”提升到 `team`。

### 7.2.4 Replay

- 当前 `replay` 路由未接 header 级 account/agent 校验（仅按 `trajectory_id` 查本地索引）。
- 这是当前实现的安全缺口，需在后续版本补齐。

## 7.3 当前可见性控制（实现事实）

retrieve 使用内置可见性过滤（非完整 ACL 引擎）：

- `datalake`：对 account 内调用方可见；
- `agent` / `user`：仅 `owner_space == agent_id` 可见；
- `team`：当前按 account 内共享可见处理（未做 team path 继承闭包）。

说明：

- `infra/security/acl_engine.py` 目前仍为占位，deny-override 策略尚未接入主链路。
- 因此当前是“轻量可见性规则”，不是“完整 ACL 系统”。

## 7.4 当前审计能力

审计后端为 JSONL（`JsonlAuditLogger`），记录格式：

- `timestamp`
- `action`
- `result`
- `details`

已接入主要动作：

- `commit`
- `retrieve`
- `promote_trajectory`
- （以及其他链路中的 orchestrator 事件）

当前不包含策略版本、字段级脱敏命中等高级 ACL 审计字段。

## 7.5 当前未实现能力（避免误读）

以下能力在当前代码中**未实现或未完整实现**：

1. 通用 deny-override ACL 引擎（策略表达式 + 冲突裁决）。
2. 字段级动态脱敏（按调用方角色/团队裁剪返回字段）。
3. team 层级继承与组织级精细授权模型。
4. replay 路由的完整访问控制与审计上下文绑定。

## 7.6 当前风险与建议

1. replay 缺少 account/agent 上下文校验，建议优先补齐。  
2. team 可见性当前较宽松（account 内共享），建议在 ACL 引擎接入后收敛。  
3. 若要对外提供更强安全承诺，需先完成字段脱敏与策略审计扩展。  

