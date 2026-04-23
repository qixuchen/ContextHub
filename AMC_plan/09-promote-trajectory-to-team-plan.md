# 09 — Promote 到 Team 方案（含当前实现状态）

本文档保留 promote 设计方案，同时明确标注当前哪些能力已实现、哪些仍是待实现项。

## 9.1 当前实现状态（先看这个）

### 9.1.1 已实现（当前代码）

当前 AMC 已有可用的 promote 主链路：

- 已有路由：`POST /api/v1/amc/promote`
- 基本输入：`X-Account-Id`、`X-Agent-Id`、`trajectory_id`、`target_team`
- 结果形态：返回 `source_uri/target_uri/scope/owner_space/status`
- 持久化行为：将 agent 侧轨迹提升为 team 侧记录（由 repo 处理）
- 检索联动：promote 后会触发 team 侧向量索引刷新
- 审计：写 `promote_trajectory` 审计日志

### 9.1.2 暂未实现或未完全实现

以下仍属于设计目标，不应视为已落地：

- 独立 ACL 引擎的 team 写权限校验链路（`infra/security/*` 仍是 placeholder）
- “仅团队成员可见”的完整 team membership ACL 闭环
- 基于 `derived_from` 的传播联动（如 stale 传播）
- promote 事件总线化（`infra/storage/event/*` 仍是 placeholder）
- team -> datalake、跨 account、审批流等高级 promote 形态

---

## 9.2 目标与范围（设计方案）

目标：在 AMC 中提供“将 agent 私有 trajectory/workflow 提升到 team 空间”的能力，行为口径尽量对齐 main 分支 `POST /api/v1/memories/promote`。

范围（设计）：

- 支持 `scope=agent -> scope=team` promote；
- 仅支持提升“当前调用 agent 自己提交的轨迹”；
- 提升后可被团队成员在 retrieve 中召回；
- 记录 `derived_from` 血缘与审计日志。

非范围（后续）：

- team -> datalake promote；
- 跨 account promote；
- 基于审批流的延迟生效。

> 状态：**部分实现（基础 promote 已有；ACL/血缘传播等未完整实现）**

---

## 9.3 接口设计（方案）

路由：

`POST /api/v1/amc/promote`

请求头（与 main 对齐）：

- `X-Account-Id`
- `X-Agent-Id`

请求体（建议）：

```json
{
  "trajectory_id": "traj_xxx",
  "target_team": "engineering",
  "reason": "promote reusable workflow"
}
```

返回体（建议）：

```json
{
  "source_uri": "ctx://agent/query-agent/memories/trajectories/traj_xxx",
  "target_uri": "ctx://team/engineering/memories/trajectories/traj_xxx",
  "scope": "team",
  "owner_space": "engineering",
  "derived_from": "ctx://agent/query-agent/memories/trajectories/traj_xxx",
  "status": "promoted"
}
```

> 状态：**接口主形态已实现，`derived_from` 的统一返回/联动语义仍按实现细节收敛中**

---

## 9.4 处理流程（设计口径，对齐 main）

1. 读取源 trajectory 元信息（不存在则 `404`）
2. 类型校验（非 trajectory 则 `400`）
3. 所有权校验（非本人 agent 私有则 `403`）
4. 目标写权限 ACL 校验（无权限则 `403`）
5. 构造 `target_uri`（`ctx://team/{target_team}/...`）
6. 写 team 侧 promote 记录（冲突 `409`）
7. 写 `derived_from` 血缘关系
8. 写审计日志与变更事件
9. 更新索引一致性（FS/Graph/Vector）

> 状态：**当前已实现其中核心路径（读源 -> promote -> 向量刷新 -> 审计），ACL 与事件化/血缘传播仍未完整实现**

---

## 9.5 存储层建议（设计方案）

推荐“轻拷贝”策略：

1. 源数据不变（agent 侧保留）；
2. team 侧新增 promote 记录；
3. 图资产通过 pointer 复用；
4. 向量索引按 team 视图写入可检索记录。

> 状态：**核心思路与当前实现方向一致；图层/血缘联动细节仍可继续完善**

---

## 9.6 错误码与行为约定（设计口径）

- `404 Not Found`：source trajectory 不存在或不可用；
- `400 Bad Request`：参数非法/类型不支持；
- `403 Forbidden`：所有权不满足或无 target_team 写权限；
- `409 Conflict`：target 已存在；
- `201 Created`：promote 成功。

> 状态：**当前接口已覆盖主要错误分支；ACL 细粒度口径仍待补齐**

---

## 9.7 与 retrieve/ACL 的联动要求（设计方案）

promote 后 retrieve 目标行为：

1. 过滤层可命中 team promoted 记录；
2. 应用层保留兜底过滤；
3. 最终 ACL 只允许 team 成员可见；
4. 非成员不可见（越权返回率=0）。

> 状态：**promote 后可检索已实现；“team 成员闭环 ACL”未完整实现**

---

## 9.8 测试建议（方案）

### 单元测试

1. promote 成功：agent 私有 -> team；
2. source 不存在 -> 404；
3. source 非 agent 私有 -> 403；
4. 无 team 写权限 -> 403；
5. 重复 promote -> 409。

### 集成测试

1. Agent A promote 到 `team=engineering`；
2. 团队成员可命中；
3. 非成员不可命中；
4. 审计日志包含 `source_uri/target_uri/target_team/actor`。

> 状态：**可作为后续补齐 ACL/可见性闭环的验收清单**

---

## 9.9 分阶段落地建议（保留方案）

1. **P1（最小可用）**：API + promote registry + 审计（当前已基本落地）
2. **P2（检索与权限闭环）**：team membership ACL + 检索可见性严格化
3. **P3（一致性增强）**：`derived_from` 传播联动、事件总线、幂等返回策略

验收口径：

- 团队内可复用命中率提升；
- 越权可见率 = 0；
- promote 接口 P95 延迟可控（目标 < 300ms，不含重建 embedding）。
