# 架构与语义说明

## 1. 数据模型与不可变事实

所有持久化在一个 SQLite 库（WAL 模式）加一个内容寻址 blob 目录中完成。

### 1.1 表职责

| 表 | 职责 | 不可变性 |
|---|---|---|
| `sources` | 来源坐标、基线 seq、已发布前沿、阻断点 | 只追加状态字段 |
| `transactions` | 每个 seq 的暂存/发布/冲突状态与清单摘要 | 状态机单向 |
| `shards` | 分片期望/实际摘要、条数、验证状态 | 可由更新的交付补全，但已发布事实不回改 |
| `resource_version` | 每个 PUT/DELETE 事实（身份、versionId、seq、ordinal、墓碑标记、blob 指针） | **只插入，不更新不删除** |
| `ref_edges` | 每个资源版本发出的引用边（含原始 JSON 位置 `loc` 与结构分类） | 只插入 |
| `contained_resources` | 拥有资源版本内的 `#id` 名册（作用域=拥有者版本） | 只插入 |
| `cuts` | 已发布的完整 cut 序列 | 只插入 |
| `isolated_conflicts` | 被隔离的异载荷重传/坏事务载荷与原因 | 只插入 |
| `events` | 摄取时间线（审计） | 只追加 |
| `profile_packages` / `profile_package_files` / `profile_canonicals` | 已登记 IG 包身份、文件摘要与 canonical 注册表 | 只插入；冲突即拒绝 |
| `audit_runs` / `audit_members` / `audit_deleted` / `audit_cut_progress` | 审计运行状态与逐 cut 成员 | 运行单调推进，完成后不再变 |
| `eval_cache` | 按 `(包digest, 根profile, 资源版本, body digest)` 键控的评估发现 | 只插入 |

关键约束：

* `resource_version` 上对 `(source_id, resource_type, logical_id, version_id)`
  有唯一索引——versionId 仅在身份内有意义；不同身份（含另一 tenant 的同名
  `Patient/p`）天然分行，不存在全局资源命名空间。
* 同身份同版本的异载荷重传**无法**写入事实表（冲突时走
  `isolated_conflicts`），因此已发布事务与旧 cut 永不被覆盖。

### 1.2 内容寻址 blob

PUT 正文规范化序列化后按 sha256 存于 `blobs/xx/yy/<digest>`。相同正文自动
共享存储；blob 只追加、原地不变。数据库只保存**仓库相对 POSIX 路径**，
来源目录名是坐标的 sha256 前 16 位，不包含任何输入绝对路径，因此整体
`mv` 仓库目录后查询不受影响。

## 2. 摄取状态机

### 2.1 乱序重交付的状态不变量

测试中专门构造了"同名相对路径、不同字节"与"部分包后于完整包回放"的
交付序列，由此固化三条防护：

1. **内容寻址隔离**：每个交付落到 `packages/seq<baseline>-<完整manifest摘要>/`，
   两个字节不同的包不可能写到同一文件；数据库分片路径保存该包内相对路径。
2. **状态单调**：`shards` 行一旦为 `verified`，后到交付对同一分片给出的
   `missing/bad_*` 判定不得降级该行（只在 stage 结果里留告警）。新交付
   若带来新的期望摘要且自身 verified，则正常替换为新的已验证物料。
3. **发布前复核**：`advance` 在写事务之外按期望摘要重新计算字节摘要与
   条数，并按内容摘要在 packages 树中选取物料（不依赖目录遍历顺序），
   任何 staging 与 publication 之间的差异都阻断发布而不是产生部分 cut。

```
                    ┌──────────────┐
 manifest + shards │   staging    │  分片可乱序、先到先存
 ─────────────────> │ (per seq)    │  缺片=missing；摘要/条数错=bad_*
                    └──────┬───────┘
                           │ 全部分片 verified 且行级校验通过
                           ▼
                    ┌──────────────┐
                    │ publishable  │
                    └──────┬───────┘
        advance 仅取 published_seq+1 ── 不连续则停在当前前沿
                           ▼
                    ┌──────────────┐
                    │  published   │  单写事务：事实+边+cuts 原子提交
                    └──────────────┘
```

* **seq 严格连续**：前沿只接受 `published_seq + 1`。先到的后续分片（如
  先交 12 再交 11 的一半）只暂存，前沿保持不动。
* **全有或全无**：一个事务只有在 manifest 所列分片全部齐备、摘要与条数
  全部通过、每行信封/正文校验通过后才可发布。基线本身未验证时不产生
  任何 cut，坏输入不可能把完整 cut 替换成部分状态。
* **完全重传折叠**：已发布 seq 收到相同 manifest（摘要一致）直接折叠。
* **异载荷重传隔离**：不同 manifest 的重传逐行与已发布事实比对，差异行
  连同载荷复制进 `isolated_conflicts`，记事件，发布表零改动。
* **未发布事务内冲突**（同 seq/ordinal、同事务同资源多项、元数据与正文
  不一致）：事务置 `conflict`，来源 `blocked_seq` 阻断该 seq 起的可用前沿。
* 每个 seq 的发布是一个 `BEGIN IMMEDIATE` 写事务，事实行、引用边、
  `cuts` 行在同一事务内提交，cut 要么完整可查、要么不存在。

## 3. cut 与引用解析语义

cut N = 取每个身份 `seq <= N` 的最后一个 PUT 或 DELETE。墓碑使当前资源
不存在，但历史版本行全部保留可读。

### 3.1 无版本引用

`Type/id` 与匹配规范基址/别名的绝对 URL，在**所查询 cut 的资源集合**中
解析，而不是在引用发出时刻解析。实现上每次查询都对不可变版本行施加
`seq <= cut` 谓词，所以前沿推进不会让旧 cut 的无版本边偷偷改指新对象。

### 3.2 带版本引用

`Type/id/_history/versionId` 只在该 cut 已知（`seq <= cut`）的精确历史
版本中解析：

* 目标后来被 DELETE——仍 `resolved` 到旧版本；
* 版本只在更晚 seq 出现——`version_not_known_at_cut`，不能提前可见；
* 任何 cut 都不存在该版本——`missing`。

### 3.3 绝对 URL 与外部引用

绝对 URL 必须前缀匹配 `repo.json` 中该来源的规范基址或**显式配置的等价
别名**，否则记 `external`。绝不凭尾部 id 跨来源连接（sourceB 中指向
sourceA 基址的引用即 external）。

### 3.4 contained

`#id` 只在拥有资源的那个版本内有效，解析查 `contained_resources`
的 `(owner, owner_version, contained_id)` 精确四元组；两个资源各有 `#x`
互不合并。contained 资源再向外发出的引用与普通边走同一来源范围规则。

### 3.5 七种解析状态

| 状态 | 含义 |
|---|---|
| `resolved` | 命中 cut 可见的存活版本（含已删身份的历史精确版本） |
| `deleted` | 无版本引用的当前版本是墓碑 |
| `missing` | cut 时无任何事实（或版本在任何 cut 都不存在） |
| `version_not_known_at_cut` | 精确版本存在但属于更晚 seq |
| `external` | 绝对 URL 不属于配置基址/别名，或不可解析 scheme |
| `invalid` | 结构非法（空引用、错误相对形态、坏 fragment 等） |
| `conflict` | 目标版本存在被隔离的异载荷重传记录 |

每条结果都回带原始来源位置：`loc`（资源内 JSON 路径）、`container`
（拥有者或 `<owner>#<containedId>`）、`edge_id` 与原始 `ref_text`。

## 4. 并发与快照

* WAL + `busy_timeout=30s`：单写多读，写事务 `BEGIN IMMEDIATE` 串行化。
* 每个查询入口在一个 `BEGIN` 只读事务内完成 `check_cut` 与全部读取，
  SQLite 读标记在事务期间固定。摄取进程可同时提交下一事务，而在途查询
  始终看到同一 cut、同一完整事实集（演练中由 seq13 并发提交实证）。
* CLI 提供 `--hold-open N` 测试钩子，便于人工安排同步点。

## 5. trace-path 的循环处理

以"已解析版本三元组 `(type,id,version)`"为 visited 键做 DFS：再次到达
已访问版本时，该跳标注 `stopped=true, stop_reason="cycle: ..."` 并终止
该分支，循环边本身作为证据保留，不产生无限路径。`external/invalid/
missing/deleted/conflict` 同样终止分支并给出原因；`max_hops` 兜底。

## 6. 可重开、可迁移、可重建

* 所有状态在磁盘，CLI 无状态；中途退出后任意时刻重跑 `status/advance`
  即可继续，暂存与已发布内容都不丢。
* 数据库与清单中没有绝对路径；`mv` 整个仓库目录后按来源坐标直接复查。
* `tools/rebuild.py` 从归档的 manifest+分片全量重建新库，并逐来源比对
  cut 列表与逐 cut 读取结果——不需要任何增量算法。

## 7. 流式与内存边界

* NDJSON 逐行解析；摘要按 1 MiB 块计算。
* 大历史生成器与摄取管道都不把整包载入内存：单行可以很大（演练单资源
  约 120 KB 填充），但同时存活的只有当前行。
* blob 写入走临时文件 + 原子 rename。
