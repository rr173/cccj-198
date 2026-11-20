# FHIR R4 离线历史对象仓（fhist）

面向医疗数据交换团队的**离线**复查工具：接收完整基线导出与随后分批交付的
历史事务，构建一个可复制、可整体移动、可在任意时点（cut）重开查询的
FHIR R4 历史对象仓。

* 只处理合成医疗资料；不连接任何医院系统；不做诊疗判断；不做患者实体匹配。
* 事实身份是四元组 `(source_server, tenant, resourceType, id)`；`versionId`
  只在同一身份内比较相等，绝不跨来源比较。
* 跨院区同一人由**人工裁定**的独立身份维表表达（`fhist identity`），
  不改变事实身份与引用解析规则。
* 提交顺序只由正整数 `seq` 决定，与 FHIR `versionId`、`lastUpdated`、
  文件到达顺序无关。

## 1. 目录结构

```
fhir_hist_repo/
├── src/fhist/            # 正式实现（摄取、存储、解析、查询、CLI）
│   ├── cli.py            # 命令入口：init/stage/advance/status/resource/
│   │                     #         references/trace-path/verify/profile/audit
│   │                     #         bedreport/bedload/identity
│   ├── config.py         # 规范基址与显式等价别名
│   ├── paths.py          # 可重定位布局 + 内容寻址 blob 路径
│   ├── db.py             # SQLite schema、WAL、写事务/快照读事务
│   ├── manifest.py       # 清单结构校验
│   ├── jsonl.py          # NDJSON 流式读写与摘要
│   ├── validate.py       # 操作信封/正文一致性、事务内冲突规则
│   ├── blobs.py          # 内容寻址 blob 存储（sha256）
│   ├── intake.py         # 交接包复制、stage、advance、发布
│   ├── refs.py           # Reference 抽取与结构分类（含 contained 作用域）
│   ├── resolver.py       # cut 时点解析，七种状态
│   ├── query.py          # 当前/版本/历史读取、上下游、trace-path
│   ├── status.py         # 状态汇总
│   ├── igmodel.py        # IG 模型：SD 继承合并、ValueSet 展开（纯内存）
│   ├── profiles.py       # profile 包登记、canonical 唯一性、依赖闭包
│   ├── audit.py          # 合规审计：评估引擎、可恢复运行、确定性报告
│   ├── bedreport.py      # 床位日报：日历+cut → 日事实分区、更正差分、发布目录
│   ├── bedload.py        # 下游装载：发布目录 → SQLite（独立于原仓）
│   └── identity.py       # 跨来源患者身份维表：裁定发布、归属、反查、证据
├── bin/fhist             # python -I 启动壳
├── tools/
│   ├── make_fixtures.py      # 合成夹具生成（主演练 + 负例）
│   ├── make_followup.py      # seq13 后续包（并发演练）
│   ├── make_conflict_case.py # conflict 解析状态可见性夹具
│   ├── large_history.py      # 大历史流式生成器（单资源可大、整包不常驻）
│   ├── make_ig_fixtures.py   # profile 包 + 审计历史夹具（运行时生成）
│   ├── make_bed_fixtures.py  # 床位日报夹具 + 大历史/大日历生成器
│   ├── make_identity_fixtures.py # 身份维表夹具（三院区 + 独立血缘）
│   ├── make_identity_scale.py    # 200 患者规模夹具
│   ├── check_identity.py     # 身份维表独立核对器（自带逐日参考引擎）
│   ├── check.py              # 独立核对器（不 import 正式解析器）
│   ├── check_audit.py        # 审计独立核对器（手工预期 finding 表）
│   ├── check_bedreport.py    # 床位日报独立核对器（手工数字表 + 自实现口径）
│   └── rebuild.py            # 从归档包全量重建并逐 cut 比对
└── docs/
    ├── ARCHITECTURE.md
    ├── PROFILE_AUDIT.md      # 约束子集、选择优先级、审计边界、恢复语义
    ├── BED_REPORT.md         # 床位日报：发布/核账/装载命令与文件合同
    ├── IDENTITY_DIMENSION.md # 跨来源患者身份维表：接口与语义
    ├── IDENTITY_DRILL.md     # 身份维表演练报告
    └── DRILL_REPORT.md
```

## 2. 快速开始

```bash
# 1) 初始化仓库（声明各来源规范基址与别名）
cat > sources.json <<'JSON'
{"sources": [
  {"source_server": "https://fhir.example.org/a",
   "aliases": ["https://fhir-alias.example.net/a"]}
]}
JSON
bin/fhist --repo ./handoff init --sources-file sources.json

# 2) 暂存一个交接包（分片可乱序、可缺失；坏输入不会破坏已有 cut）
bin/fhist --repo ./handoff stage --manifest /path/to/pkg/manifest.json

# 3) 推进前沿：只发布严格连续且全部分片齐备的事务
bin/fhist --repo ./handoff advance \
  --source-server https://fhir.example.org/a --tenant tenant-main

# 4) 状态、读取、引用、路径
bin/fhist --repo ./handoff status
bin/fhist --repo ./handoff resource --source-server S --tenant T \
  --cut 11 --resource-type Patient --id p
bin/fhist --repo ./handoff resource ... --version-id vA      # 精确版本
bin/fhist --repo ./handoff resource ... --history            # 版本链
bin/fhist --repo ./handoff references ... --direction both   # 上/下游
bin/fhist --repo ./handoff trace-path ... --resource-type X --id y
```

所有输出均为 stdout 上的 JSON；失败带稳定 `status` 字符串与非零退出码，
适合脚本编排与中途退出后重开。

## 3. Profile 合规审计

对已发布 cut 中现存资源按本地登记的实施指南逐资源评估（详见
[docs/PROFILE_AUDIT.md](docs/PROFILE_AUDIT.md)）：

```bash
# 登记本地 profile 包（package.json + SD/ValueSet/CodeSystem，只读本地文件）
bin/fhist --repo ./handoff profile register --package-dir /path/to/ig-pkg
bin/fhist --repo ./handoff profile list

# 对一个或多个已发布 cut 运行/恢复审计（相同请求幂等复用）
bin/fhist --repo ./handoff audit run --source-server S --tenant T \
  --cut 10 --cut 11 --package synth-ig:1.0.0
bin/fhist --repo ./handoff audit findings --audit-id <id> --cut 11
bin/fhist --repo ./handoff audit report   --audit-id <id> --out report.json
```

退出码：0 = 成功（含审计完成但存在 fail finding）；2 = 操作/用法错误；
3 = 审计中断、可重跑恢复。

## 4. 床位日报发布与装载

对已发布 cut 与一份开床日历（CSV）生成「本地日期 × 病区」日事实分
区，产出可整体移动、带机器可验证清单的发布目录；更正版只交付精确行
级差分；下游用独立于原仓的 `bedload` 命令把分区原子装入 SQLite（详
见 [docs/BED_REPORT.md](docs/BED_REPORT.md)）：

```bash
bin/fhist --repo ./handoff bedreport publish --source-server S --tenant T \
  --cut 11 --timezone UTC --from-date 2026-03-10 --to-date 2026-03-11 \
  --calendar beds.csv [--base-release REL_DIR]
bin/fhist bedreport evidence --release REL_DIR --section slices --ward W1
bin/fhist bedload --db warehouse.sqlite3 --release REL_DIR
```

## 5. 跨来源患者身份维表

为已入库的跨院区事实增加一张**附加的**身份维表：用“身份维表发布号 +
业务日期 + 来源键”查归属，用统一键反查成员并解释归属。详见
[docs/IDENTITY_DIMENSION.md](docs/IDENTITY_DIMENSION.md)。

```bash
bin/fhist --repo REPO identity publish --release 1 \
  --from-date 2026-01-01 --to-date 2026-02-01 \
  --cut 'https://fhir.example.org/alpha|tenant-care|10' \
  --cut 'https://fhir.example.org/bravo|tenant-care|20' \
  --corrections batch.ndjson
bin/fhist --repo REPO identity assign  --release 1 --date 2026-01-15 \
  --key 'https://fhir.example.org/alpha|tenant-care|a1'
bin/fhist --repo REPO identity members --release 1 --date 2026-01-14 \
  --unified-key '["https://fhir.example.org/alpha","tenant-care","a1"]'
bin/fhist --repo REPO identity status  --release 1 \
  --key '...|charlie|tenant-care|c1' --from-date 2026-01-08 --to-date 2026-01-22
bin/fhist --repo REPO identity decision --release 1 --decision-id d-bc \
  --date 2026-01-15
bin/fhist --repo REPO identity verify  --release 1 --date 2026-01-15 \
  --left '...|alpha|tenant-care|a1' --right '...|charlie|tenant-care|c1'
```

每一版都是一次原子发布：裁定、区间归属、反查索引与所用来源坐标同时可见；
相同输入重试折叠，非法输入整体拒绝；旧版永不被改写。

## 6. 一键演练

```bash
python3 -I tools/make_fixtures.py build/fixtures
python3 -I tools/check.py build
python3 -I tools/check_audit.py build   # profile 审计演练（夹具运行时生成）
python3 -I tools/check_bedreport.py build  # 床位日报演练（夹具运行时生成）
python3 -I tools/check_identity.py build   # 身份维表演练（夹具运行时生成）
```

核对器**不导入** `fhist` 解析代码：它仅通过 CLI 驱动正式库，自己用标准库
重读 NDJSON、自实现一份 cut 语义，并维护人工版本/边期望值表进行双向比对。
