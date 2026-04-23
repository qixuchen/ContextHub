# AMC 规划索引（当前实现导向）

本文档是 AMC 规划文档入口，目标是“先能快速定位当前实现对应文档，再看细节设计”。  
`03 / 04 / 19` 已按当前代码实现完成对齐更新。

## AMC 当前主能力

- **Commit**：接收轨迹，构建 raw/clean 图，写入本地轨迹包，并按配置写 Neo4j/pgvector。
- **Retrieve**：语义召回 +（可选）图召回融合排序，返回轨迹证据与（可选）skill 命中。
- **Skill Route / Evolve**：按 `update/create` 路由，统一调用 evolve 生成或更新 `SKILL.md`。
- **Inter-trajectory Trigger**：commit 后在 Neo4j 维护跨轨迹激活关系并自动触发 route。
- **Promote**：将 agent 私有轨迹提升到 team scope（含向量索引刷新）。

## 推荐阅读顺序（按优先级）

### 第一层：总设计

1. [01-scope-and-principles.md](01-scope-and-principles.md)：系统边界、能力口径与原则  
2. [02-architecture.md](02-architecture.md)：当前可运行架构与模块分层

### 第二层：核心主功能

3. [03-trajectory-information-model.md](03-trajectory-information-model.md)：轨迹信息模型与落盘结构  
4. [04-commit-pipeline.md](04-commit-pipeline.md)：commit 主链路（单条/批量/联动）  
5. [05-retrieve-pipeline.md](05-retrieve-pipeline.md)：retrieve 主链路（trajectory + skill）  
6. [06-skill-evolve-trace2skill-plan.md](06-skill-evolve-trace2skill-plan.md)：skill route/evolve 当前实现

### 第三层：次一级功能

7. [07-multitenancy-and-access-control.md](07-multitenancy-and-access-control.md)：多租户与访问控制现状  
8. [08-workflow-abstraction.md](08-workflow-abstraction.md)：workflow 设计方案与实现边界  
9. [09-promote-trajectory-to-team-plan.md](09-promote-trajectory-to-team-plan.md)：promote 方案与实现状态

### 第四层：补充文档

10. [10-openclaw-plugin-integration-plan.md](10-openclaw-plugin-integration-plan.md)  
11. [11-configuration-spec.md](11-configuration-spec.md)

## 全量文档索引

- [01-scope-and-principles.md](01-scope-and-principles.md)
- [02-architecture.md](02-architecture.md)
- [03-trajectory-information-model.md](03-trajectory-information-model.md)
- [04-commit-pipeline.md](04-commit-pipeline.md)
- [05-retrieve-pipeline.md](05-retrieve-pipeline.md)
- [06-skill-evolve-trace2skill-plan.md](06-skill-evolve-trace2skill-plan.md)
- [07-multitenancy-and-access-control.md](07-multitenancy-and-access-control.md)
- [08-workflow-abstraction.md](08-workflow-abstraction.md)
- [09-promote-trajectory-to-team-plan.md](09-promote-trajectory-to-team-plan.md)
- [10-openclaw-plugin-integration-plan.md](10-openclaw-plugin-integration-plan.md)
- [11-configuration-spec.md](11-configuration-spec.md)

---

维护规则：文档与代码不一致时，以代码行为为准，及时在对应 plan 文件中修正“当前实现”章节。

