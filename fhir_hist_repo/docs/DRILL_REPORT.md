# 手工主演练与核验报告

本报告对应需求中的全部规定动作。所有资料均为合成数据，由
`tools/make_fixtures.py`、`tools/make_followup.py`、
`tools/make_conflict_case.py`、`tools/large_history.py` 临时生成；
无任何医院系统连接、无诊疗判断、无患者实体匹配。

复现：

```bash
python3 -I tools/make_fixtures.py build/fixtures
python3 -I tools/check.py build          # 102 项断言，独立核对器
python3 -I tools/rebuild.py \
    --from build/handoff-moved --to build/handoff-rebuilt
```

实测留档：`build/drill-run.log`（102 PASS / 0 FAIL）、
`build/rebuild-run.log`（11 个来源 cut 全对齐）。

## 1. 人工版本/边期望表（主演练，来源 A / tenant-main）

基线 seq10：`Patient/p`(vA)、`Observation/o`(oV1)，o 同时发出
`Patient/p`（无版本）与 `Patient/p/_history/vA`（带版本）。
事务 11：p → vB（拆两个分片）；事务 12：DELETE p → 墓碑 vC。

### 1.1 版本链（人工表）

| 身份 | seq10 | seq11 | seq12 |
|---|---|---|---|
| Patient/p | PUT vA | PUT vB | DELETE vC（墓碑） |
| Observation/o | PUT oV1 | — | — |

### 1.2 边状态（人工表）

| 发出者 | 引用 | cut10 | cut11 | cut12 |
|---|---|---|---|---|
| Observation/o | `Patient/p` | resolved→vA | resolved→**vB** | **deleted**（墓碑 vC） |
| Observation/o | `Patient/p/_history/vA` | resolved→vA | resolved→vA | **resolved→vA**（目标后删仍指旧版） |

核对器中以 `EXPECTED_VERSIONS / EXPECTED_EDGES_CUT10/11/12`
手工表硬编码上述期望，并逐字段比对 CLI 实际输出（全部 PASS）。
cut10 在前沿到达 12 后复查结果保持不变（旧 cut 不被改写）。

## 2. 乱序分片摄取（先交 12，再交 11 的一半）

`sourceA-partial` 包：事务 12 分片完整、事务 11 仅 part-0（part-1 缺失）。

实测（`build/drill-run.log` 标题节）：

* `frontier held: no cut queryable while 11 incomplete` — 前沿不动；
* `early-arriving txn 12 is staged ahead of its turn` — 先到的 12 只暂存；
* 补齐完整包后一次 `advance` 同时发布 `[11, 12]`，前沿=12；
* 缺失分片在 `shards` 表记 `missing`，坏摘要记 `bad_digest`，条数不符记
  `bad_count`，JSON 损坏记 `bad_format`，均不可发布。

## 3. 命名空间与引用形态核验

| 场景 | 夹具 | 实测结果 |
|---|---|---|
| 另一 tenant 同名 `Patient/p` | sourceB（基线 seq20，p=vX） | 独立身份，读到 vX 而非 vA/vB |
| 跨来源绝对 URL | sourceB 的 `Patient/other → https://fhir.example.org/a/Patient/p` | `external`，不凭尾部 id 连接 |
| 两个拥有者各含 `#x` | sourceA owner1/owner2 | 各自 `#x` 均 resolved 且作用域不合并，loc 保留 |
| 别名绝对 URL | org1 → `fhir-alias.example.net/a/Organization/shared` | 同来源解析，目标缺失=`missing` |
| 外站绝对 URL | ext1 → `elsewhere.example.com/...` | `external` |
| 未来版本引用 | probe → `Patient/p/_history/vFUTURE` | cut10/11/12 均 `version_not_known_at_cut` |
| 从不存在的版本 | case-missing-history → `vGHOST` | `missing` |
| 基线之前的 cut | cut9 | `cut_not_queryable`，明确声明无证据 |

## 4. 冲突、损坏与阻断

| 负例 | 注入方式 | 事务终态 | 前沿 |
|---|---|---|---|
| case-dup-ordinal | 同 seq 两行 ordinal=0 | conflict + blocked_seq | 保持 5 |
| case-body-mismatch | 信封 versionId=vLIAR，正文 meta=v9 | conflict（stage 期隔离） | 保持 5 |
| case-corrupt | 分片追加非法 JSON 字节、摘要失配 | staged 但分片 bad_digest/bad_format，不可发布 | 保持 5 |
| case-retransmit | 已发布 seq6 以同身份同 versionId 异体重传 | 差异行进 `isolated_conflicts`，原 cut 正文不变 | 6 |
| case-conflict-visibility | 观察资源指向被异载荷重传的 v2 | 两条边由 resolved 变 **conflict**，带原因与 loc；已发布正文 active 仍为 true | 6 |

所有负例后基线 cut5/6 仍可正常读取——坏输入从未替换完整 cut。

### 损坏基线阻断后由干净交付恢复（case-bad-baseline）

两个包对基线使用**相同相对路径但不同字节**（坏包追加非法 JSON 行并虚报
条数），这正是最容易让"坏交付覆盖好状态"的布局。实测顺序与结果：

1. 先交损坏包：stage `bad[]` 报 `bad_format/bad_count`；advance 被拒
   （`bad_shard`/`cut_not_queryable`），不产生任何 cut；
2. 再交干净包：以完整 manifest 摘要落到独立内容寻址目录，shards 行的
   期望摘要/条数被整体替换，状态翻转为 verified；
3. advance 成功导入基线 seq7，资源 `Patient/z` 正常读到 z1。

测试过程中据此加固了三条不变量（见 `docs/ARCHITECTURE.md` §2.1）：

* 包目录按**完整 manifest 摘要**唯一隔离，同名不同字节的分片互不覆盖；
* 分片/基线状态**单调**：已 verified 的行不会被后到的乱序交付降级为
  missing/bad（仅在 stage 结果中留告警）；
* 发布瞬间按期望摘要重新核对字节与条数，物料按内容摘要而非目录遍历
  顺序选取，防止读到同名坏文件。


## 5. 循环关系

case-cycle：cycA→cycB→cycC→cycA。`trace-path` 输出一条三跳路径，
最后一跳 `stopped=true`、`stop_reason="cycle: already-visited version"`，
循环边证据保留，深度有界（实测路径长度 ≤ 3 跳 + max_hops 兜底）。

## 6. 并发同步点（查询中提交下一事务）

`drill_concurrency` 的实际时序（见日志 NOTE）：

1. 读者进程在 cut12 打开 `references` 并以 `--hold-open 3.0` 持有 SQLite
   读快照；
2. **同步点**：`reader snapshot open at cut12, writer advancing seq13`；
3. 写者 stage+advance 提交 seq13（WAL 下不阻塞），前沿推进到 13；
4. 读者结束，输出仍是 cut12 事实：无版本边 `deleted`、版本边仍
   `resolved→vA`，rc=0；
5. 新查询 cut13 可见 `Patient/new1`；同一身份在冻结的 cut12 仍 `missing`。

证明一次查询固定同一 cut 与同一完整事实集，摄取并发提交不影响在途查询。

## 7. 中途退出重开与目录迁移

* 所有 CLI 无服务端状态；每次调用都是新进程打开同一库，`status`
  重开结果一致（核对器重复调用比对）。
* `verify` 重算全部 blob 摘要，0 个损坏。
* 整个仓库 `mv build/handoff → build/handoff-moved` 后，不依赖原绝对
  路径，cut12 墓碑读取、status 全部正常（日志 NOTE 留痕）。

## 8. 全量重建

`tools/rebuild.py` 从迁移库归档的 manifest/分片重建全新库：11 个来源
全部 `cuts_match=True`、`frontier_match=True`，逐 cut 的 `Patient/p`
当前/版本读取全部一致，`mismatches=[]`。重建器只回放物料齐备的包
（增量包允许基线在更早的包目录中），跳过原始部分包中从未到货的缺失
分片；配合分片状态单调守卫，乱序的部分/完整包并存也能重建出与原库
逐 cut 一致的结果，无需任何增量算法。

## 9. 大历史与内存边界

`large_history.py` 实测：2000 个基线资源 + 8 个事务、约 1.69 MB 分片
（每 250 个资源含约 120 KB 单资源正文），生成耗时 0.04s，全程逐行写盘；
摄取后前沿 100→108，抽查 cut100/104/108 的最新版本与独立引擎一致。
仓库内 36 个 NDJSON 交接文件随库保存，整体约 22 MB，可直接打包上传。

## 10. Profile 合规审计演练

夹具由 `tools/make_ig_fixtures.py` 运行时生成（`build/ig-fixtures/`），
核对器 `tools/check_audit.py` 不导入 `fhist`，预期 finding 表为手工维护。
复现：`python3 -I tools/check_audit.py build`（73 项断言，留档
`build/audit-drill-run.log`）。

### 10.1 包登记

* `synth-core@1.0.0`（CodeSystem×2 + 基 Patient SD）、`synth-ig@1.0.0`
  （依赖 synth-core，含继承/choice/closed+open slicing/required binding/
  fixed/pattern 与 unsupported 构造）、`synth-ig@2.0.0`（gender ValueSet
  改为嵌套 include 且不再 exclude `unknown`）、歧义包组
  （`synth-dup-a/b` 各持 `dup-sd` 的一个版本，`synth-ambig` 同时依赖）。
* 负例全部拒绝且 status 稳定：缺依赖与自依赖环 →
  `profile_dependency_error`；canonical 同版本异内容、包同名同版本异内容
  → `profile_conflict`；相同内容重登记 → `already_registered` 折叠。

### 10.2 主审计（synth-ig@1.0.0，cuts 30/31/32）

基线 14 个资源覆盖全部结果类别；seq31 修改 `p-ok`（gender→other），
seq32 删除 `p-del`。实测汇总与手工表逐项一致：

| cut | pass | fail | unsupported | no_profile | profile_unknown | profile_type_mismatch | deleted |
|---|---|---|---|---|---|---|---|
| 30 | 4 | 5 | 1 | 1 | 2 | 1 | 0 |
| 31 | 3 | 6 | 1 | 1 | 2 | 1 | 0 |
| 32 | 2 | 6 | 1 | 1 | 2 | 1 | 1 |

`memberships=41`、`unique_evaluations=15`：同一资源版本跨 cut 复用发现，
各 cut 汇总独立正确。`p-ok` 在 cut30 为 pass（v1）、cut31 起为 fail
（v2 内容变化）；旧 cut 报告在前沿推进后不变。

### 10.3 指南版本并存与选择规则

* `synth-ig@2.0.0` 登记后重跑 1.0.0 审计：`already_complete`，汇总逐字节
  不变；同一 cut31 在 2.0.0 下 `p-excl`（gender=unknown，1.0.0 被
  exclude）由 fail 变 pass——指南版本差异；`p-ok`（gender=other）两版
  均 fail——资源内容差异。
* 显式 `--root-profile`：只评估类型匹配的资源（Patient），无
  meta.profile 的 `p-noprof` 按调用者指定根评估为 pass，Observation 全部
  `no_profile`。
* 歧义包审计：裸 canonical 命中两个已登记版本 → `profile_ambiguous`；
  未登记 canonical → `profile_unknown`；均不计 pass。

### 10.4 中断恢复、幂等与可重建

* `--max-evaluations 3` 以退出码 3 中断（`status: interrupted`，
  `cuts_done=[]`）；重跑同一请求续跑至完成；再次重跑
  `already_complete` 复用存储汇总。
* 两次 `audit report` 导出字节一致；整库 `cp -a` 迁移后导出仍一致；
  运行中的审计也可导出（`status: running` 的部分报告）。

## 11. 产物清单

```
build/fixtures/                 合成交接包（含负例与乱序包）
build/handoff-moved/            演练后的可查询库（已迁移路径）
  repo.json                     规范基址/别名
  meta/history.sqlite3*         WAL 数据库
  blobs/xx/yy/<sha256>          内容寻址正文
  sources/<hash>/packages/...   按来源坐标归档的 manifest+分片
  conflicts/                    隔离载荷副本
build/handoff-rebuilt/          全量重建产物
build/ig-fixtures/              运行时生成的 profile 包与审计历史夹具
build/ig-handoff/               审计演练库
build/drill-run.log             102 项断言留档
build/audit-drill-run.log       73 项审计断言留档
build/rebuild-run.log           重建比对留档
```
