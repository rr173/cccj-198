# Profile 合规审计（本地实施指南检查）

本功能回答一个问题：**某个已发布 cut 中现存的资源，是否符合某个本地登记的
实施指南（IG）**。它不重做事务完整性校验，也不做引用查询；它产出可复核的
逐资源 finding 和按 cut 汇总的质量报告。

设计上有两类不可变事实，互不混入：

* **资源事实**：`resource_version` 行只插入不修改；同一资源版本可出现在
  多个 cut 的成员集合中。
* **指南事实**：profile 包以内容 digest 标识，不可变；同一包的多个版本
  （如 `synth-ig@1.0.0` 与 `synth-ig@2.0.0`）可并存，各自独立被审计引用。

因此"资源内容变化"与"指南版本变化"造成的合规差异天然可分：换 cut 看同一
包，或换包看同一 cut。

## 1. Profile 包登记

包是一个可移动目录，内含：

```
mypackage/
├── package.json                 # {"name": ..., "version": ..., "dependencies": {"dep": "1.0.0"}}
├── StructureDefinition-*.json   # 带 url + version 的 R4 资源
├── ValueSet-*.json
└── CodeSystem-*.json
```

```bash
bin/fhist --repo R profile register --package-dir /path/to/mypackage
bin/fhist --repo R profile list
bin/fhist --repo R profile show --package synth-ig:1.0.0   # 或 64 位 digest
```

登记时校验（任一失败即整体拒绝，稳定 `status`）：

| 校验 | 失败 status |
|---|---|
| package.json 身份（name/version 形态）、文件 JSON 可解析、资源类型仅限 SD/VS/CS、每个资源有 url+version | `profile_package_error` |
| 依赖的包必须已按 `name@version` 登记（只读本地，不联网解析） | `profile_dependency_error` |
| 依赖图无环（含自依赖） | `profile_dependency_error` |
| 包 `name+version` 不得与已登记的不同内容冲突 | `profile_conflict` |
| 每个 `canonical|version` 不得与已登记的不同内容冲突 | `profile_conflict` |

包 digest = 对排序后的 `(相对路径, 文件sha256)` 列表再取 sha256。登记的确切
文件字节全部写入仓库内容寻址 blob 存储（`blobs/`），交付目录之后可移动或
删除。相同内容的重复登记折叠为 `already_registered`；同名同版本不同内容是
明确冲突，绝不静默覆盖。

## 2. 运行审计

```bash
bin/fhist --repo R audit run \
  --source-server https://fhir.example.org/ig --tenant tenant-ig \
  --cut 30 --cut 31 --cut 32 \
  --package synth-ig:1.0.0 \
  [--root-profile 'http://synth.example.org/StructureDefinition/synth-patient|1.0.0']
bin/fhist --repo R audit list
bin/fhist --repo R audit status  --audit-id <id>
bin/fhist --repo R audit findings --audit-id <id> [--cut 30] [--result fail]
bin/fhist --repo R audit report  --audit-id <id> [--out report.json]
```

* 审计请求 = `{source, tenant, cuts, package_digest, root_profiles}` 的规范
  JSON；其 sha256 即 `audit_id`。**相同请求幂等**：已完成的重跑返回
  `already_complete` 并复用存储的汇总。
* 每次审计固定：profile 包 digest、cut 集合、仓库事实边界
  （`frontier_at_start`，作为证据记录；cut ≤ 已发布前沿的事实不可变，
  之后的前沿推进不会改变旧 cut 的报告）。
* 每个 cut 只评估**当时最后一个非删除版本**：墓碑不计为资源（计入
  `deleted`），cut 之后的版本不可见。
* 评估发现按 `(包digest, 根profile集合, 资源版本, body digest)` 键控缓存
  （`eval_cache`）：同一资源版本出现在多个 cut 只评估一次，但每个 cut 的
  成员与汇总独立准确（报告中 `memberships` 与 `unique_evaluations` 可见
  复用程度）。
* 运行状态持久化（`audit_runs/audit_members/audit_deleted/
  audit_cut_progress`）：进程中断后重跑同一请求即续跑；全部报告都从持久
  化状态重建，临时输出不是唯一证据。

## 3. Profile 选择优先级

1. **显式根 profile**（`--root-profile`，可重复）：调用者指定的根覆盖
   `meta.profile`。根在审计创建时即解析校验；资源的 `resourceType` 与根的
   SD `type` 不匹配时该根不适用——若所有根都不适用，资源结果为
   `no_profile`。
2. **`meta.profile` 驱动**（默认）：逐条解析资源 `meta.profile`：
   * `canonical|version`：须在包闭包中精确命中，否则 `profile_unknown`；
   * 裸 `canonical`：闭包中恰有一个版本则选中；多个版本 →
     `profile_ambiguous`；零个 → `profile_unknown`；
   * 命中的 SD `type` 与资源类型不同 → `profile_type_mismatch`；
   * 无 `meta.profile` → `no_profile`。

包闭包 = 被钉住的包 + 其已登记依赖的传递闭包（全部不可变），因此选择结果
与之后登记的新包无关。

资源结果：`fail`（任一 error finding）> `unsupported`（任一 unsupported
finding 或选择问题）> `pass`。选择类结果（`no_profile`、
`profile_unknown`、`profile_ambiguous`、`profile_type_mismatch`）与
`unsupported` 都**不是 pass**——缺 profile、歧义、未支持的约束全部可见。

## 4. 支持的约束子集

| 约束 | 说明 | 违规 code |
|---|---|---|
| `baseDefinition` 继承 | 沿本地已登记祖先链合并：子声明的键覆盖，未声明的继承；外部基（如 FHIR 核心）为终点，只评包内声明 | — |
| 元素路径 / choice 类型 | `value[x]` 按前缀匹配实例键，类型须在所列 `type` 内 | `choice_type_not_allowed`、`type_mismatch` |
| min/max cardinality | 按路径实例计数（缺失中间元素按 0 计） | `min_cardinality`、`max_cardinality` |
| `fixed[x]` | JSON 深相等 | `fixed_mismatch` |
| `pattern[x]` | 递归包含：对象逐键、数组逐项存在匹配 | `pattern_mismatch` |
| slicing | 单个 `value` 或 `type` 鉴别器；`closed` 时无匹配切片即违规，`open` 时未匹配项合法（仍受切片元素本身约束）；切片 min/max；切片内子元素约束 | `slice_unmatched`、`slice_min`、`slice_max` |
| required binding | `CodeableConcept/Coding/code/uri` 取值对展开集合逐一核对 | `code_not_in_valueset` |
| ValueSet compose | `include`/`exclude` 的 `system`+`concept`、`system`（枚举本地 CodeSystem 概念树）、`valueSet` 嵌套引用；按包依赖闭包解析 | （解析失败见下表） |

**明确不支持**（出现时精确报告并使资源结果为 `unsupported`，绝不误报
pass）：

| 构造 | finding code |
|---|---|
| FHIRPath `constraint.expression` | `unsupported_fhirpath_constraint` |
| Reference `targetProfile` / 类型 `profile` | `unsupported_target_profile` |
| ValueSet `filter`（需术语推理） | `unsupported_valueset_filter` |
| 未登记的 ValueSet/CodeSystem、裸 canonical 多版本歧义、compose 环、空 compose | `unsupported_valueset_unresolved`、`unsupported_codesystem_unresolved`、`unsupported_valueset_ambiguous`、`unsupported_codesystem_ambiguous`、`unsupported_valueset_cycle`、`unsupported_valueset_compose` |
| 多鉴别器 / `exists`/`pattern`/`profile` 鉴别器 / 切片缺鉴别器元素 / 嵌套切片 / 无切片定义 | `unsupported_slicing_discriminator`、`unsupported_slicing_nested`、`unsupported_slicing_missing_base` |
| 未知 FHIR 类型代码 | `unsupported_type_code` |
| SD 自身无法解释（base 环、基版本未登记、缺 type） | `unsupported_profile_unresolvable` |

`extensible/preferred/example` 绑定不强制（只执行 `required`）。网络
terminology expansion 不在范围内：ValueSet 若引用未登记的 CodeSystem 且未
显式列出 concept，即 `unsupported_codesystem_unresolved`。

## 5. Finding 与报告

确定性 JSON（排序键、确定顺序、无墙钟字段），两次导出字节一致。每个
finding 含：

* 来源坐标（`source_server`/`tenant`，在所属资源条目的 `identity` 中）、
  `cut_seq`、资源 `resourceType`/`id`/`versionId`/`body_digest`；
* 实际选中的 profile（`profile`，形如 `canonical|version`）；
* 规则位置（`element_id` + 实例内 `path`，如 `$.component[1]`）；
* 稳定 `code`、`severity`（`error`/`unsupported`）；
* 可复核的 `expected`/`actual`（如 fixed 的期望值/实际值、绑定的实际
  coding 与目标 ValueSet）。

汇总含每 cut 与总计的 `pass/fail/unsupported/no_profile/profile_unknown/
profile_ambiguous/profile_type_mismatch/deleted` 计数、`by_finding_code`
与 `(profile, element_id, code)` 级 `by_rule` 计数、`memberships` 与
`unique_evaluations`。

## 6. 状态恢复与退出码

* `audit run` 逐 cut 在写事务内落盘成员；`--max-evaluations N`（测试钩子）
  处理 N 个新评估后以退出码 **3** 中断（`status: interrupted`）。重跑同一
  请求即续跑；已完成则 `already_complete`。
* 退出码：**0** = 成功（含审计完成但有 fail finding——finding 是数据）；
  **2** = 操作/用法错误（JSON 中带稳定 `status`）；**3** = 审计中断、可
  恢复。

## 7. 独立核对

`tools/check_audit.py` 不导入 `fhist`：夹具由
`tools/make_ig_fixtures.py` 运行时生成到 `build/ig-fixtures`，预期 finding
表为手工维护，与正式 profile 解析/匹配实现相互独立。
