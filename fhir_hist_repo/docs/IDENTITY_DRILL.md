# 身份维表演练报告

本报告对应的全部规定动作都由 `tools/make_identity_fixtures.py`、
`tools/make_identity_scale.py` 现场生成合成资料，`tools/check_identity.py`
独立核对。核对器**不导入** `fhist`：只用 CLI 驱动正式库，自己重读交接包
NDJSON 得到可用性，并用约 120 行参考引擎逐日重算、逐键比对。

复现：

```bash
python3 -I tools/make_identity_fixtures.py build/identity-fixtures
python3 -I tools/check_identity.py build
```

实测留档：`build/identity-drill-run.log`（245 PASS / 0 FAIL）。

## 1. 业务演练（人工期望表）

三个不同院区 `alpha < bravo < charlie`，三个键在首版 cut 均可用：

| 裁定 | 种类 | 区间 |
|---|---|---|
| `d-ab` | same | `[2026-01-01, 2026-02-01)` |
| `d-bc` | same | `[2026-01-10, 2026-01-20)` |
| `d-ac` | different | `[2026-01-15, 2026-01-18)` |

首版统一键 = 组内最小来源键（UTF-8 逐字段）：

| 日期 | alpha\|a1 | bravo\|b1 | charlie\|c1 | 说明 |
|---|---|---|---|---|
| 01-09 | alpha\|a1 | alpha\|a1 | charlie\|c1 | b 与 c 尚未同组 |
| 01-10 | alpha\|a1 | alpha\|a1 | alpha\|a1 | b–c 桥接 |
| **01-14** | alpha\|a1 | alpha\|a1 | alpha\|a1 | 三键归 `a` |
| **01-15..17** | conflict | conflict | conflict | 整组冲突，`unified=null` |
| **01-18** | alpha\|a1 | alpha\|a1 | alpha\|a1 | different 结束 |
| **01-19** | alpha\|a1 | alpha\|a1 | alpha\|a1 | 桥仍在 |
| **01-20** | alpha\|a1 | alpha\|a1 | charlie\|c1 | c 单列 |

核对器把这 9 天 × 3 键逐一代入 `identity assign` 比对，全部 PASS；
`identity members` 在 14 日枚举完整 3 成员，冲突组不可按统一键枚举；
`identity verify` 在 14 日返回 `same` 且路径恰为 `d-ab → d-bc`（两跳，
每跳带 versionId 与 seq），在 16 日返回 `conflict`，矛盾证据是最小
`different` 裁定 `d-ac`（revision 1），其两端最短 `same` 路径经过 `b`。

## 2. 第二版：替换 `same(b,c)`

`d-bc` revision 2 改为 `[2026-01-12, 2026-01-16)`：

| 日期 | 归属 | 关键点 |
|---|---|---|
| 01-10 | a-b 一组，c 单列 | 桥尚未生效 |
| 01-12 | 三键归 `a` | 新桥生效 |
| **01-15** | 整组冲突 | 桥仍在 + different 仍在 |
| **01-16** | a-b 一组，c 单列 | 新桥在 16 日结束 |
| 01-19 | a-b 一组，c 单列 | 旧桥区间失效 |

同时验证：首版（release 1）重查在 16 日仍为冲突——旧版不被改写；
`identity decision` 报 `revision=2`、`active_on_date` 随新端点正确变化；
旧统一键在新版按新版含义返回（16 日的 `alpha|a1` 组只剩 a、b）。

## 3. 撤回与独立血缘

* release 3 撤回 `d-ac`：16 日不再冲突，`decision` 对 `d-ac` 报
  `unknown_decision`；release 2 的 16 日仍冲突。
* release 4/5 是独立血缘（`alpha + delta`），验证同名键
  `alpha|a1` 与 `delta|a1` 永不按名字合并；`delta` 内 m/n/o/p 有等长
  两跳 `same(m,n,p)` 与 `same(m,o,p)`，按沿途裁定号序列字节序取
  `p1,p2`；再补一条平行边 `p0` 后，单跳平局取 `p0`。

## 4. 推进到墓碑、待生效与恢复

bravo 推进到 seq21（`b1` 墓碑）：

* release 6（父 3）报 `d-ab`、`d-bc` 均 `pending`，两端可用性为
  `available`/`deleted`，并带已有版本坐标；
* `b1` 的状态是 `deleted`，从未出现的键是 `absent`——两者可区分；
* `a1`、`c1` 各自独立成组（`different` 连接两个不同的无冲突组，可以并存）；
* `verify(a1,c1)` 报 `different`；`verify(a1,b1)` 报 `pending` 且 `items`
  给出两端可用性。

恢复到 seq23（`b1` 重新 PUT）：release 7（父 6）中 `d-ab`、`d-bc` 自动
重新参与，无需重抄裁定；14 日三键重新归 `a`，15 日冲突随 `d-bc` revision 2
的区间回来；`d-bc` 的修订号仍为 2（旧版坐标保留）。

## 5. 刷新计数（小改动不触发全表重算）

| 发布 | 操作 | recomputed_keys | reused_keys |
|---|---|---|---|
| scale 0→1 | 200 个 echo 患者 + alpha，全新 | 202 | 0 |
| scale 1→2 | 撤回一组 4 条桥中的 1 条 | **5** | **197** |
| scale 2→3 | 无更正 | **0** | **202** |
| headline 7→8 | 撤一条边 | 4 | 6 |
| headline 9→10 | 无更正 | 0 | 10 |

`identity stats` 还按来源给出 `seen_versions`（本版检查的新增来源版本数）
与 `changed_keys`，按日给出 `idr_day` 的 `recomputed_keys`/`reused_keys`。
核对器断言 release 7（bravo cut 20→23）`seen_versions=2`、`changed_keys=2`，
而 cut 未变的来源 `seen_versions=0`。`active_versions` 给出该 cut 下可用
Patient 数。
scale 演练另确认 10 个业务日的逐日统计确实落盘。

## 6. 发布中断、抢同一父版、查询期间推进

* **中断**：`--interrupt exit` 让进程在写完所有行、COMMIT 之前死亡
  （退出码 17）。随后 `identity show` 报 `unknown_release`，父版仍为头部；
  相同输入重试得到 `published`，中断的裁定正确生效——未提交事务被 WAL
  整体丢弃，没有半版。
* **抢同一父版**：release 10 与 11 同时以 9 为父，先者 `published`，
  后者 `release_rejected`（父已有后继）。
* **重复发布号**：相同语义输入折叠（`folded`），头部推进后仍折叠；
  重用发布号改内容拒绝。
* **查询期间推进**：读者以 `--hold-open` 持有 release 7 的只读快照，
  写者并发发布 release 13（新增 `same(a1,b2)`）。读者结束仍返回 release 7
  的结果；新查询看到 release 13 的桥；release 7 对 `b2` 的归属保持原样。

## 7. 整体拒绝

10 类非法发布全部 `release_rejected`，且 `identity show` 证明对应发布号
不存在：重用发布号改内容、过期父号、旧修订、批内重复裁定号、空区间、
越界区间、未知来源端点、未发布 cut、撤回父版不存在的裁定、首版带父号。

## 8. 独立参考引擎

`reference_engine` 不调用正式区间计算或图遍历：对每业务日，取两端可用且
区间覆盖该日的裁定，用普通标签传播合并 `same`，再标出被 `different` 连到
同组的组。对 release 1/2/3 的 31 天 × 6 键逐日逐键与 CLI 比对，全部一致。
独立核对器还从交接包 NDJSON 自行推导可用性，而不是读库。

## 9. 产物

```
build/identity-fixtures/     alpha/bravo/charlie/delta + bravo-advance
build/identity-repo/         演练库（12 个以上发布，含独立血缘）
build/identity-scale-repo/   200 患者规模演练库
build/identity-drill-run.log 245 项断言留档
```
