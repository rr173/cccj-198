# 床位日报：发布、核账与装载（bedreport / bedload）

`fhist bedreport` 从指定 cut 与一份开床日历（CSV）计算「本地日期 × 病区」
日事实分区，产出一份自包含、可整体移动的**发布目录**；同一发布系列的后
续**更正版**只交付精确的行级差分。`fhist bedload` 是独立于原仓的下游装
载命令，只读取发布目录，把分区原子地落入 SQLite。

只处理离线合成资料：不连接任何医院系统，不做诊疗分析。

## 1. 概念

* **发布系列（series）**：固定 `(source_server, tenant, timezone,
  date_from, date_to)`。时区支持 `UTC` 与 `America/New_York`；日期范围
  是左闭右开 `[date_from, date_to)` 的本地日期序列。
* **发布（release）**：系列内按序编号（1, 2, 3…）的一批交付。系列第一
  批是 `initial`（完整分区）；之后每批是 `correction`（对照某个此前发
  布的版本交付差分）。更正的 cut 可以相同（仅日历改动）或更晚。
* **日事实行**：`(local_date, ward_code)` → `open_seconds,
  empty_seconds, single_seconds, conflict_seconds`。只含业务键与度量，
  不含任何版本坐标；后三项之和恒等于开床秒数。只要有开床秒数就有一行
  （无人入住也有行）。

## 2. 计算口径

* 全部按绝对时间（epoch 秒）计算，按报告时区的日界切段；日历区间、
  占床区间、交接时刻、午夜切换一律左闭右开 `[start, end)`。
* 在选定 cut 逐一取 Encounter 当时的非墓碑版本。只统计
  `Encounter.location.status` 为 `active` / `completed` 的条目，其他状
  态不形成占床（计入 `location_entries_ignored_status`）。
* `completed` 必须同时有起止；`active` 缺止时间视为开放到报表窗口结
  束。坏时间戳、反向或空区间、不可定位的引用都作为**逐项异常**保留在
  `evidence/exceptions.ndjson`，不计入床位事实。
* 位置引用仅接受指向本来源本租户当前 Location 的**无版本**
  `Location/id`（相对形式，或仓库已配置规范基址/等价别名的绝对形
  式）。外站、版本化（`Location/id/_history/v`）、已删除或从不存在的
  目标一律成为异常，绝不凭尾部 id 迁就。
* 日历内同床同病区的开床区间取并集；同床**不同**病区区间重叠则拒绝
  整份日历。日历用到的床必须在该 cut 能定位（非墓碑），否则拒绝发
  布——不把它报成空床。
* 同一 Encounter 在同一床的多条重叠记录不自相冲突（按不同 Encounter
  去重计数）；两个及以上不同 Encounter 同时占一床记为冲突秒数，不任
  选一条当真。
* 占床落在该床无开床分配的时段，按床与时间区间留诊断
  （`evidence/unassigned.ndjson`），不归入任何病区、不吞掉。

## 3. 命令用法

### 3.1 发布

```bash
# 初版（系列第一批）：完整分区
bin/fhist --repo REPO bedreport publish \
  --source-server https://fhir.example.org/a --tenant tenant-ward \
  --cut 10 --timezone UTC --from-date 2026-03-10 --to-date 2026-03-11 \
  --calendar calendars/headline-v1.csv

# 更正版：对照此前某一批（目录、seq 或 release id 均可）
bin/fhist --repo REPO bedreport publish \
  --source-server ... --tenant ... --cut 11 \
  --timezone UTC --from-date 2026-03-10 --to-date 2026-03-11 \
  --calendar calendars/headline-v2.csv \
  --base-release REPO/bedreports/series-<id16>/rel-0001-<id12>
```

* 默认交付根目录是 `<repo>/bedreports`，可用 `--reports-root` 改道。
* 相同发布输入重跑得到同样的内容与标识，直接折叠（`status: folded`），
  不产生新批次；已发布的目录永不被改写。
* 输出 JSON 含 `release_dir`、`release_id`、`facts_sha256`、
  `fact_rows`、差分行数与汇总计数。

### 3.2 浏览与核账定位

```bash
bin/fhist --repo REPO bedreport list                 # 全部系列与批次
bin/fhist bedreport show   --release REL_DIR         # 打印清单
bin/fhist bedreport verify --release REL_DIR         # 重算清单全部摘要
bin/fhist bedreport evidence --release REL_DIR \
  --section slices --ward W1 --date 2026-03-10 --kind conflict
bin/fhist bedreport evidence --release REL_DIR --section exceptions
bin/fhist bedreport evidence --release REL_DIR --section unassigned --bed bed-5
```

`evidence` 的 `--section` 可取 `slices`（组成秒数的床位时间片，含日历
行号与 Encounter 版本/location 条目）、`exceptions`（逐项异常）、
`unassigned`（日历外占用诊断）、`facts`（本版完整事实）。`show` /
`verify` / `evidence` 只读发布目录，不需要 `--repo`。

### 3.3 装载（独立于原仓）

```bash
bin/fhist bedload --db warehouse.sqlite3 --release REL_DIR
bin/fhist bedload --db warehouse.sqlite3 --status
```

* 整批（首装或更正）在**一个事务**内可见；失败整体回滚。
* 装更正前重算本地分区身份，必须等于清单里 `base.facts_sha256`——不是
  只看 cut 或批号；不符（跳批、错系列、本地前态被改）即拒绝。
* 应用后再次重算，必须等于 `target.facts_sha256` 与 `fact_rows`，否则
  回滚（`target_mismatch`）。
* 重放已成功装载的同一批：`already_loaded`，不重复修改、不倒退。
* 损坏/缺失文件、被改写的清单、错系列一律拒绝，旧表不会变成半更新。

## 4. 发布目录合同

```
<reports-root>/series-<series_id前16位>/
└── rel-<NNNN>-<release_id前12位>/
    ├── manifest.json            # 机器可验证清单（见下）
    ├── delivery/                # 交给消费端的全部内容
    │   ├── facts-full.ndjson    #   仅 initial：完整分区
    │   ├── facts-upsert.ndjson  #   仅 correction：新增/改变的整行
    │   └── facts-retract.ndjson #   仅 correction：消失的键（只有键）
    └── evidence/                # 本版核账证据（装载不读）
        ├── facts-full.ndjson    #   本版完整目标事实（差分对照的基准）
        ├── slices.ndjson        #   组成秒数的床位时间片
        ├── exceptions.ndjson    #   逐项异常
        ├── unassigned.ndjson    #   日历外占用诊断
        ├── calendar.csv         #   固定下来的日历输入
        └── inputs.json          #   固定下来的发布参数与摘要
```

发布目录内不存任何绝对路径，整体 `mv`/`cp` 后 `verify` 与 `bedload`
照常工作。发布先写 `.tmp-*` 临时目录再原子 rename；中止留下的临时目
录在下一次发布时清理，重算安全。

### 4.1 manifest.json

```json
{
  "format": "fhist-bedreport/1",
  "series":  {"source_server", "tenant", "timezone",
              "date_from", "date_to", "series_id"},
  "release": {
    "seq": 2, "release_id", "kind": "correction",
    "cut_seq": 11,
    "calendar": {"sha256", "rows", "beds"},
    "base":   {"release_id", "seq", "facts_sha256"},
    "target": {"facts_sha256", "fact_rows"},
    "window": {"start_utc", "end_utc", "days"}
  },
  "delivery": {"kind": "correction",
               "files": [{"path", "sha256", "rows", "role"}],
               "upsert_rows": 1, "retract_rows": 1},
  "evidence": {"files": [{"path", "sha256", "rows", "role"}]},
  "summary":  {"encounters_scanned", "location_entries", "exceptions",
               "open_seconds", "empty_seconds", "single_seconds",
               "conflict_seconds", "unassigned_seconds", ...}
}
```

* `initial` 的 `release.base` 为 `null`，delivery 只有一个
  `role=full` 的文件；`correction` 必有 `role=upsert` 与
  `role=retract` 两个文件（允许 0 行）。
* `rows` 对 NDJSON 文件是行数；`calendar.csv` 是数据行数；
  `inputs.json` 为 `null`。

### 4.2 身份与摘要公式

* `series_id` = sha256(canonical-json `{format, source_server, tenant,
  timezone, date_from, date_to}`)，canonical-json 指 sort_keys 且无空
  格的 JSON 序列化。
* **事实身份** `facts_sha256`：把全部事实行按 `(local_date,
  ward_code)` 排序，逐行写
  `date|ward|open|empty|single|conflict\n`（竖线分隔、十进制整数），
  对连接结果取 sha256。发布端与装载端用同一公式，装载端在事务内对
  本地表重算。
* `release_id` = sha256(canonical-json(manifest 去掉 `release.seq` 与
  `release.release_id`))。它绑定系列、cut、日历摘要、前一批与目标事
  实身份、全部交付/证据文件的摘要与行数——改动清单里任何字节都会
  使重算不符。

### 4.3 行格式

事实行（`facts-full` / `facts-upsert`，每行一个 JSON 对象，键序排
列，文件内按 `(local_date, ward_code)` 排序）：

```json
{"local_date":"2026-03-10","ward_code":"W1","open_seconds":86400,
 "empty_seconds":72000,"single_seconds":10800,"conflict_seconds":3600}
```

撤回行（`facts-retract`）只有业务键：

```json
{"local_date":"2026-03-10","ward_code":"W1"}
```

时间片（`slices`）携带核账证据：

```json
{"local_date":"2026-03-10","ward_code":"W1","bed_id":"bed-1",
 "start_utc":"2026-03-10T03:00:00+00:00","end_utc":"2026-03-10T04:00:00+00:00",
 "seconds":3600,"kind":"conflict","calendar_lines":[2],
 "occupants":[{"encounter_id":"E1","version_id":"v1","seq":10,
   "entries":[{"location_index":0,"status":"completed",
   "period_start_utc":"2026-03-10T01:00:00+00:00",
   "period_end_utc":"2026-03-10T04:00:00+00:00"}]}, ...]}
```

异常（`exceptions`）：`encounter_id, version_id, seq, location_index,
kind, detail[, ref_text]`；`kind` 取值：`location_ref_external` /
`location_ref_invalid` / `location_ref_not_location` /
`location_ref_versioned` / `location_unresolvable` /
`location_entry_invalid` / `period_missing_start` /
`period_missing_end` / `period_bad_timestamp` /
`period_reversed_or_empty`。

日历外占用（`unassigned`）：`bed_id, local_date, start_utc, end_utc,
seconds, reason="occupancy_outside_open_calendar", occupants`（结构同
slices）。

## 5. 装载端数据库

```sql
bedload_series(series_id PK, facts_sha256, fact_rows,
               head_release_id, head_release_seq, updated_at)
bedload_releases(series_id, release_seq, release_id,
                 target_facts_sha256, loaded_at, PK(series_id, release_id))
bed_facts(series_id, local_date, ward_code, open_seconds, empty_seconds,
          single_seconds, conflict_seconds,
          PK(series_id, local_date, ward_code))
```

装载器只写这三张表；一个数据库可以装多个系列的分区（按 `series_id`
隔离）。

## 6. 状态串与退出码

发布（退出码 2 为操作/用法错误）：`published` / `folded` /
`bad_calendar`（结构错误、反向区间、同床跨病区重叠）/
`calendar_bed_missing` / `cut_not_queryable` / `unknown_release` /
`series_mismatch` / `bad_release`（对照批损坏）/ `usage_error`。

装载：`loaded` / `already_loaded` / `bad_manifest` / `bad_delivery` /
`base_mismatch` / `local_state_mismatch` / `target_mismatch` /
`partition_not_empty`。校验类命令：`ok` / `corrupt`。

## 7. 内存边界

每次发布都重扫全窗口（不做增量计算），但 Encounter 版本、时间片与
差分都经过发布目录内临时的 SQLite 暂存与有序 NDJSON 流式处理：任一
时刻只有单张床的时间片在内存中，全窗口对象、全部时间片与整份差分
不会同时装进内存。大历史与大日历由 `tools/make_bed_fixtures.py` 现场
生成到 `build/`（不提交 Git）。

## 8. 演练

```bash
python3 -I tools/make_bed_fixtures.py build/bed-fixtures
python3 -I tools/check_bedreport.py build
```

核对器不 import 正式实现：它用标准库自实现日历并集、切日、扫掠与
差分，从交接包原始文件重算全部预期，再与发布物、证据和装载结果双
向比对；固定场景（全天开床、重复登记、日中换病区、本地日界、纽约
春秋换时、无开床时段、来源外同名床）另有人工数字表。
