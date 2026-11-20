# 跨来源患者身份维表（identity）

本文件说明 `fhist identity` 子命令的接口、语义、存储与演练。全部数据均为
离线合成资料，身份只来自**人工给出的裁定**，绝不进行患者实体匹配；原仓
的摄取、资源查询、审计与床位日报完全照旧，`references` 仍按原有来源规则
解析（`Type/id` 只在同一 `(source_server, tenant)` 内解析，跨来源绝对 URL
一律 `external`）。

## 1. 为什么单独一张维表

原仓的事实身份仍是来源本地的
`(source_server, tenant, resourceType, id)`；各院区独立编发编号，同名编号
可能毫无关系。跨院区事实已经在入库时带着自己的来源键，因此**不重建事实**，
只增加一张“身份维表”：用“维度发布号 + 业务日期 + 来源键”查归属，用统一
键反查成员，并能解释某条裁定在某版某日是否参与。

维表是**附加表**（`db.py` 的 `IDENTITY_SCHEMA`）：老仓在第一次使用时通过
`ensure_identity_schema` 懒加载，不影响既有表与查询路径。

## 2. 持久模型

| 表 | 内容 | 不可变性 |
|---|---|---|
| `identity_release` | 发布号、父发布号、cut 向量（JSON）、日期范围、原始更正批、请求摘要、统计 | 只插入 |
| `idr_source` | 该版每个参与来源的 cut | 只插入 |
| `idr_patient` | 该版每个 `Patient/<id>` 在 cut 时的快照：`available/deleted/absent`、versionId、seq、ordinal | 只插入 |
| `idr_decision` | 该版**有效**裁定（已应用更正后的全量），含 revision、种类、两端来源键、区间 | 只插入 |
| `idr_decision_change` | 该版相对父版**变更过**的裁定号（`revision=0` 表示撤回） | 只插入 |
| `idr_group` | 每业务日每组的 `group_key`、`unified_key`、`conflict` | 只插入 |
| `idr_member` | 每业务日每个可用键的归属、父版坐标、是否本版重算 | 只插入 |
| `idr_day` / `idr_worklog` | 每业务日与每来源的处理统计 | 只插入 |

所有发布内容都在**一个 `BEGIN IMMEDIATE` 写事务**里一起提交：裁定、区间
归属、反查索引与所用来源坐标要么同时可见，要么完全不可见。中断（进程在
COMMIT 前死亡）由 WAL 整体丢弃，不会留下半版。

## 3. 发布语义

### 3.1 发布号与血缘

* 发布号由调用者给定，全仓唯一。
* **首版**：`--parent` 省略。它扫描全部参与来源（首次建版必然全量）。
* **后续版**：`--parent` 必须是某条血缘**当前的头部**（该父版尚未有任何
  后继）。两个同时从同一父版发出的不同发布，只有一个能成为后继；另一个
  整体拒绝。这同时避免“基于过期父号发布”。
* 同一血缘固定参与来源集合与业务日期范围 `[start, end)`；每个来源的 cut
  可独立推进或保持，但不得回退。
* 重复发布号且**语义输入相同**（cut 向量、父号、日期范围、更正批按裁定号
  排序后一致）返回原版（`status: folded`），即使头部已经向前推进；重用发布
  号改内容整体拒绝。

### 3.2 cut 向量

`--cut SOURCE|TENANT|CUT` 重复给出，每次都必须是该来源**已发布**的 cut，
且来源必须是仓内已登记来源。一次查询使用指定发布里的**整个** cut 向量。

### 3.3 更正批（NDJSON 逐行）

```json
{"decision_id":"d-ab","revision":2,"op":"set","kind":"same",
 "left":{"source_server":"https://fhir.example.org/alpha","tenant":"tenant-care","id":"a1"},
 "right":{"source_server":"https://fhir.example.org/bravo","tenant":"tenant-care","id":"b1"},
 "interval":{"start":"2026-01-01","end":"2026-02-01"}}
{"decision_id":"d-ac","revision":2,"op":"retract"}
```

* `decision_id` 在批内唯一；`revision` 是严格递增的正整数。
* `set` 给两个**不同**且属于参与来源的完整键、`same` 或 `different`、以及
  位于维表范围内的非空左闭右开 `interval`。整条替换旧修订：端点、种类、区间
  都可改，旧区间的旧修订随之失效（该裁定的旧区间不再参与任何一日）。
* `retract` 只给裁定号与修订号，必须针对父版**存在**的有效裁定。
* 一个批要么全部有效、要么整体拒绝：旧修订、重复裁定号、非法/越界区间、
  未知来源、未发布 cut、过期父号、重用发布号改内容，都会以
  `release_rejected` 整体失败，不留下任何可见痕迹。

## 4. 归属规则

### 4.1 可用性

某来源键在某版 cut 的可用性**只取仓内 `resource_version`**：最后一个事实是
PUT → `available`，DELETE → `deleted`，从未出现 → `absent`。三者在查询结果
里可区分。一个 `available` 的键参与该版**所有业务日期**的匹配（不做日期
前推）；`deleted`/`absent` 不参与。

### 4.2 分组与统一键

对某一业务日：

1. 该日有效的 `same` 裁定把两端**都可用**的键连成无向连通组；孤立可用键
   自成一组。
2. 若该日任一有效 `different` 裁定连接了**同一组内**两个键，整组标为
   `conflict`，所有成员都不取得统一键（`unified_key: null`）。组间
   `different` 可以并存。
3. 无冲突组取成员中按 UTF-8 字节逐字段排序最小的完整来源键，编码为三元素
   JSON 数组 `["<source_server>","<tenant>","<id>"]`。该编码单射，不同组绝不
   共享同一编码。

拆分后含原最小成员的组保留该键，其余组按各自最小成员重算；因此同一统一键
在不同发布里可能指向不同成员——**每个发布号保留自己的归属**，旧统一键在新
发布里按新发布的含义返回。

### 4.3 端点暂不可用（待生效）

裁定两端至少一端在该版不可用时，结果报 `pending`，并给出**两端各自**的
可用性、版本坐标（versionId、seq、tx_ordinal、cut）与已有归属。后续发布里
端点恢复为可用后，仍有效的裁定**自动**重新参与，无需重抄裁定（`idr_decision`
在每版全量复制有效裁定）。

### 4.4 小改动的工作集

* **来源变化**：只有 cut 推进的来源需要重扫；cut 保持的来源直接复制父版快照。
* **裁定变化**：只有变更裁定涉及的端点键进入触发集合。
* **每日分组**：从触发键出发，经活动裁定与父版组扩张得到“被触及组件”；
  被触及的组按活动裁定重新计算（因此成员被墓碑、桥被撤掉都能正确拆开或
  合并），**与变动无连接的历史组整组复制父版结果**，不重算。
* 统计写出 `recomputed_keys` 与 `reused_keys`，可检查小改动没有触发全表重算；
  `sources[].seen_versions` 是本次发布检查的该来源新增 Patient 版本数（cut
  未变则为 0），`sources[].changed_keys` 是可用性快照发生变化（含新增/墓碑）
  的键数，`active_versions` 是该 cut 下可用的 Patient 数。

实测（`docs/IDENTITY_DRILL.md`）：200 患者来源撤回一条桥时
`recomputed_keys=5, reused_keys=197`；无更正发布为 `0 / 202`。

## 5. 命令与输出

```bash
# 发布
bin/fhist --repo REPO identity publish --release 1 \
  --from-date 2026-01-01 --to-date 2026-02-01 \
  --cut 'https://fhir.example.org/alpha|tenant-care|10' \
  --cut 'https://fhir.example.org/bravo|tenant-care|20' \
  --cut 'https://fhir.example.org/charlie|tenant-care|30' \
  --corrections batch.ndjson
bin/fhist --repo REPO identity publish --release 2 --parent 1 ...  # 后续版

# 列发布 / 看一版
bin/fhist --repo REPO identity list
bin/fhist --repo REPO identity show --release 2

# 按来源键查某日归属
bin/fhist --repo REPO identity assign --release 2 --date 2026-01-15 \
  --key 'https://fhir.example.org/alpha|tenant-care|a1'

# 按统一键列某日成员（反查）
bin/fhist --repo REPO identity members --release 2 --date 2026-01-14 \
  --unified-key '["https://fhir.example.org/alpha","tenant-care","a1"]'

# 按来源键列一段日期内的状态与归属区间（只按状态与统一键切段、相邻同结果合并）
bin/fhist --repo REPO identity status --release 2 \
  --key 'https://fhir.example.org/charlie|tenant-care|c1' \
  --from-date 2026-01-08 --to-date 2026-01-22

# 某裁定在某日是否参与
bin/fhist --repo REPO identity decision --release 2 --decision-id d-bc \
  --date 2026-01-15

# 两源键某日的查证（same 最短连边路径 / different 证据）
bin/fhist --repo REPO identity verify --release 2 --date 2026-01-15 \
  --left  'https://fhir.example.org/alpha|tenant-care|a1' \
  --right 'https://fhir.example.org/charlie|tenant-care|c1'

# 每版处理统计
bin/fhist --repo REPO identity stats --release 2
```

全部输出是 stdout 上的 JSON，供脚本消费。

### 5.1 关键输出字段

`assign`：

```json
{"status":"assigned","availability":"available",
 "key":{...},"group_key":"[...]","unified_key":"[...]",
 "version":{"version_id":"v1","seq":10,"tx_ordinal":0,"method":"PUT"}}
```

`status` ∈ `assigned` / `conflict` / `deleted` / `absent`。
`members` 的 `status` ∈ `ok` / `conflict`（该键是冲突组的 group key）/
`missing`；`ok` 时枚举**完整**成员。

`status` 的 `runs` 只按 `(status, unified_key)` 切段并合并相邻同结果，区间为
左闭右开：`{"status","unified_key","from","to"}`。

`verify`：

* `same`：`path` 是**最短**连边路径（边数最少；等长按沿途 `decision_id`
  序列的 UTF-8 字节字典序取首条），每跳带裁定号、修订号，以及两端在该版
  实际使用的来源、cut、Patient versionId/seq/tx_ordinal。
* `conflict`：`evidence.decision` 是组内字典序最小的 `different` 裁定及其两端，
  `evidence.contradicting_same_path` 是两端的矛盾 `same` 最短路径。
* `different`：两端不在同组。
* `pending`：至少一端不可用，`items` 给出两端各自可用性与坐标。

### 5.2 状态串与退出码

退出码 0 = 成功，2 = 操作/用法错误。可区分的状态串：

| 状态 | 含义 |
|---|---|
| `published` / `folded` | 新发布 / 相同输入折叠到原版 |
| `release_rejected` | 发布整体被拒（旧修订、过期父号、非法区间、未知来源、未发布 cut…） |
| `unknown_release` | 查询的发布号不存在 |
| `unknown_source` | 来源未登记 |
| `key_not_participating` | 该来源不参与此发布 |
| `date_out_of_range` | 日期超出该发布的业务日期范围 |
| `unknown_decision` | 该裁定在该版无效（被撤回或从未存在） |
| `assigned` / `conflict` / `deleted` / `absent` | `assign` 的四种归属 |
| `ok` / `conflict` / `missing` | `members` 的三种反查结果 |

## 6. 一致性、并发与可重试

* 每个读命令在一个 SQLite 只读事务（`snapshot_tx`）里完成，读标记固定；摄取
  或新发布并发提交不会改变在途查询看到的内容（演练中由 `--hold-open` 与并发
  发布实证）。因此“查询只给发布号与日期”即可定位归属，即使源仓随后推进，
  查询期间也读到一致的版本。
* 旧版永不被改写：新发布只**增行**。重开进程后所有完整旧版照常可查。
* 同一发布重试安全：完整版重发折叠；中断未提交的版重发即为首次发布。

## 7. 内存边界

* 每版的患者快照按来源逐条写库；cut 未变的来源直接 `INSERT … SELECT` 复制。
* 每日分组按日计算、当日即写 `idr_group` / `idr_member` / `idr_day`，任何时刻
  只有**一个业务日**的分组与成员在内存中；全范围日期不逐日展开常驻。
* 中途退出重开：全部状态在 SQLite，无需重放。

## 8. 演练

```bash
python3 -I tools/make_identity_fixtures.py build/identity-fixtures
python3 -I tools/check_identity.py build
```

核对器 `tools/check_identity.py` **不导入** `fhist`：它只用 CLI 驱动正式库，
自己重读交接包 NDJSON 得到可用性，用约 120 行的参考引擎（普通标签传播，不
用图库、不调用正式区间/图遍历）对短日期窗逐日重算，并对多版结果逐键比对。
固定业务演练含：三源 `a<b<c`、`same(a,b)` 整月、`same(b,c)` 十天、
`different(a,c)` 三天；第二版替换 `same(b,c)`；第三版撤回 `different`；
推进 `b` 到墓碑再恢复；同名来源键、平行边、等长路径、独立数据；发布中断、
抢同一父版、查询期间推进。详见 `docs/IDENTITY_DRILL.md`。
