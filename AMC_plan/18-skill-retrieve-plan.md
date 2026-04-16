# 18 — AMC Skill Recall（Retrieve）实现计划

## 18.1 背景与目标

目标是在 AMC 的 `retrieve` 链路中新增一条 **skill recall** 分支，与现有 trajectory recall 并行工作：

1. 保留现有 trajectory（语义 + 可选图匹配）召回；
2. 新增 skill 向量召回（先按 `description`）；
3. 把命中 skill 作为 retrieve 返回结果的一部分。

本期约束（按当前需求）：
- Skill 存储目录：`data/skill/{skill_name}/`（全局 skill 池，不做 account/agent 隔离）
- 每个 skill 一个目录，最少包含 `SKILL.md`
- 先使用 `description` 生成向量并召回
- 不在本期实现 skill 执行器，只实现 skill 发现与返回

---

## 18.2 Skill 定义对齐（外部规范 -> AMC 口径）

结合 Claude 官方说明与 Agent Skills 开放规范，AMC 对 skill 采用以下最小兼容口径：

1. skill 是一个文件夹，核心文件为 `SKILL.md`；
2. `SKILL.md` 头部包含 frontmatter 元数据；
3. 至少有 `name` 与 `description` 两个核心字段；
4. `description` 同时描述 “做什么” 和 “何时使用”；
5. 目录内可包含 `scripts/`、`references/`、`assets/` 等扩展内容。

AMC 本期仅使用：
- `name`
- `description`
- skill 目录路径（用于回放与定位）

---

## 18.3 数据模型与目录约定

### 18.3.1 存储路径

```
data/skill/{skill_name}/
  ├── SKILL.md
  ├── scripts/        (optional)
  ├── references/     (optional)
  └── assets/         (optional)
```

### 18.3.2 内部对象（建议）

`SkillDoc`：
- `skill_name: str`
- `description: str`
- `root_path: str`
- `skill_md_path: str`
- `content_sha256: str`（基于 `description`）
- `updated_at: str`（索引时戳）

### 18.3.3 向量索引对象（建议）

向量记录（单文档）：
- `id = skill::{skill_name}::description`
- `content = description`
- `metadata`：
  - `source_type=skill`
  - `skill_name`
  - `uri=ctx://skills/{skill_name}`
  - `content_sha256`

---

## 18.4 双回路总览（离线构建 + 在线检索）

按最新约束，skill 检索采用“双回路”：

1. **离线构建回路**（脚本触发）
   - 命令：`amc-compute-skill-embedding`
   - 扫描 `data/skill/`，比较 hash，重算变更 skill embedding，写入 skill 向量索引。
2. **在线 retrieve 回路**
   - 只对 query 计算向量；
   - 直接检索“已存储”的 skill embedding；
   - 不在 retrieve 请求内重算任何 skill embedding。

---

## 18.5 离线脚本：`amc-compute-skill-embedding`

### 18.5.1 目标

提供一个可重复执行的离线脚本，负责维护 skill 向量索引增量更新。

### 18.5.2 执行流程

```text
scan data/skill/*
  -> parse SKILL.md (name + description)
  -> compute content_sha256(description)
  -> compare with previous state snapshot
  -> changed/new/deleted set
  -> parallel embed changed/new descriptions
  -> upsert changed/new skill vectors
  -> delete removed skill vectors
  -> persist new state snapshot
```

### 18.5.3 差异判断

建议维护状态文件（例如 `data/index/skill_embedding_state.json`），记录：
- `skill_name`
- `content_sha256`
- `last_indexed_at`
- `vector_id`

判定规则：
- 新 skill：无历史记录 -> 需要 embedding；
- 更新 skill：hash 变化 -> 需要 embedding；
- 删除 skill：目录不存在 -> 删除向量记录与状态。

### 18.5.4 并行策略

脚本支持并行 embedding（线程池）：
- `--max-workers`（默认 4 或按 CPU/限流配置）
- 对 embedding API 的限流/重试复用现有 runtime 策略

建议参数：
- `--full-rebuild`：忽略状态全量重建
- `--dry-run`：仅输出变更集，不写向量库
- `--skill-root`：默认 `data/skill`

---

## 18.6 在线 Retrieve 集成（只读 skill 索引）

```text
retrieve(query)
  -> existing trajectory recall branch
  -> skill branch:
       embed(query_text)
       vector search in skill index only
       post-filter + threshold
  -> merge response:
       trajectories + skills + warnings/summary
```

关键约束：
1. retrieve 不触发 skill 目录扫描；
2. retrieve 不触发 skill embedding 重算；
3. 若 skill 索引为空，只返回空 `skills`，不影响 trajectory。

### 18.6.1 查询文本构造

复用已有 query 归一化逻辑：
- `task_description`
- `constraints`（可序列化部分）
- 可选 `partial_trajectory` 摘要文本

### 18.6.2 过滤与排序

本期默认全局 skill 池（无 account/agent 过滤）：
- `skill_score = semantic_score`
- `top_k + score_threshold` 双阈裁剪

---

## 18.7 与 Retrieve API 的集成

在现有 retrieve 响应增加字段（向后兼容）：
- `skills: []`
- `skill_retrieval_summary`（可选）

`skills[]` item（建议）：
- `skill_name`
- `description`
- `score`
- `uri`
- `path`

---

## 18.8 代码改造清单（建议）

### 新增模块

- `src/core/skills/skill_loader.py`
  - 扫描 `data/skill/`
  - 解析 `SKILL.md` frontmatter
  - 产出 `SkillDoc`

- `src/core/skills/skill_embedding_builder.py`
  - hash diff 计算
  - 并行 embedding
  - 向量 upsert/delete
  - 状态快照读写

- `src/core/retrieve/skill_retriever.py`
  - 检索 skill 向量索引
  - 返回标准化 skill hits

- `src/cli/compute_skill_embedding.py`
  - CLI 入口（`amc-compute-skill-embedding`）

### 修改模块

- `pyproject.toml`
  - 注册脚本：`amc-compute-skill-embedding`

- `src/core/retrieve/service.py`
  - 并入 skill 分支（只查不构建）

- `src/api/schemas/retrieve.py`
  - 扩展 `skills` 响应字段

- `src/api/routes/retrieve.py`
  - 返回 skill 命中结果

- `scripts/reset_amc_storage.py`
  - 默认仅重置 trajectory embedding
  - 增加可选参数用于重置 skill embedding

---

## 18.9 Skill 与 Trajectory embedding 隔离策略

结论：**不需要在 pg 新开一个 database**，优先采用“同库分表/分集合”。

推荐方案：
1. trajectory embedding：继续使用现有表（如 `amc_trajectory_index`）；
2. skill embedding：新增独立表（如 `amc_skill_index`）；
3. 两者 metadata 都保留 `source_type`，并在各自检索路径中固定表名。

原因：
- 运维简单（连接、迁移、备份策略复用）；
- 逻辑隔离已足够（物理表隔离 + 查询入口隔离）；
- 避免额外 DB 实例/权限配置复杂度。

仅在后续容量/权限需求显著增长时，再评估单独 database。

---

## 18.10 `reset_amc_storage.py` 行为约定

默认行为（保持当前习惯）：
- 重置内容文件与 trajectory 向量索引；
- **不重置 skill 向量索引**。

新增可选开关（建议）：
- `--include-skill-embedding`：额外清空 skill 向量索引；
- `--only-skill-embedding`：仅清空 skill 向量索引。

---

## 18.11 配置项扩展建议

```yaml
skills:
  root: data/skill
  embedding:
    enabled: true
    index_table: amc_skill_index
    state_file: data/index/skill_embedding_state.json
    max_workers: 4

retrieve:
  skills:
    enabled: true
    top_k: 3
    score_threshold: 0.2
```

环境变量覆盖（示例）：
- `AMC_SKILLS_ROOT`
- `AMC_SKILL_EMBEDDING_ENABLED`
- `AMC_SKILL_EMBEDDING_INDEX_TABLE`
- `AMC_SKILL_EMBEDDING_STATE_FILE`
- `AMC_SKILL_EMBEDDING_MAX_WORKERS`
- `AMC_RETRIEVE_SKILLS_ENABLED`
- `AMC_RETRIEVE_SKILLS_TOP_K`
- `AMC_RETRIEVE_SKILLS_SCORE_THRESHOLD`

---

## 18.12 分阶段实施

### Phase A（最小可用）
- `amc-compute-skill-embedding` 脚本落地
- hash diff + 并行 embedding + upsert
- retrieve 增加 `skills[]`（仅查 skill index）
- `reset_amc_storage.py` 默认不清 skill index

### Phase B（稳定性）
- skill 删除同步（delete）
- dry-run/full-rebuild/summary 指标完善
- 脚本审计日志与失败重试增强

### Phase C（增强）
- 名称命中/反馈信号重排
- 多作用域与 ACL
- skill 与 trajectory 联合上下文编排

---

## 18.13 测试与验收口径

### 功能验收

1. 初次执行脚本后，`data/skill/{skill_name}` 能被 retrieve 命中；
2. 修改 description 后再次执行脚本，召回语义更新；
3. 删除 skill 后执行脚本，skill 不再被召回；
4. 不执行脚本时，retrieve 不会自动更新 skill embedding；
5. trajectory 检索不回归，skill/trajectory 结果互不串表。

### 非功能验收

1. 脚本在 N 个 skill 上可并行完成，吞吐优于串行；
2. retrieve 延迟增量可控（主要增加一次 skill top-k 查询）；
3. skill 索引故障时降级为空 `skills`，主链路可用。

---

## 18.14 风险与缓解

1. **frontmatter 非法导致脚本失败**
   - 缓解：单 skill 容错跳过 + 汇总错误报告。

2. **增量状态文件损坏**
   - 缓解：提供 `--full-rebuild` 一键重建。

3. **skill 与 trajectory 索引误混**
   - 缓解：独立表 + 独立 retriever + 测试覆盖跨类型查询。

4. **embedding API 限流**
   - 缓解：并发上限 + 重试退避 + 批量分片。

