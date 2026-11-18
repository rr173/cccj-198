# FHIR R4 离线历史对象仓（fhist）

面向医疗数据交换团队的**离线**复查工具：接收完整基线导出与随后分批交付的
历史事务，构建一个可复制、可整体移动、可在任意时点（cut）重开查询的
FHIR R4 历史对象仓。

* 只处理合成医疗资料；不连接任何医院系统；不做诊疗判断；不做患者实体匹配。
* 身份是四元组 `(source_server, tenant, resourceType, id)`；`versionId`
  只在同一身份内比较相等，绝不跨来源比较。
* 提交顺序只由正整数 `seq` 决定，与 FHIR `versionId`、`lastUpdated`、
  文件到达顺序无关。

## 1. 目录结构

```
fhir_hist_repo/
├── src/fhist/            # 正式实现（摄取、存储、解析、查询、CLI）
│   ├── cli.py            # 命令入口：init/stage/advance/status/resource/
│   │                     #         references/trace-path/verify
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
│   └── status.py         # 状态汇总
├── bin/fhist             # python -I 启动壳
├── tools/
│   ├── make_fixtures.py      # 合成夹具生成（主演练 + 负例）
│   ├── make_followup.py      # seq13 后续包（并发演练）
│   ├── make_conflict_case.py # conflict 解析状态可见性夹具
│   ├── large_history.py      # 大历史流式生成器（单资源可大、整包不常驻）
│   ├── check.py              # 独立核对器（不 import 正式解析器）
│   └── rebuild.py            # 从归档包全量重建并逐 cut 比对
└── docs/
    ├── ARCHITECTURE.md
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

## 3. 一键演练

```bash
python3 -I tools/make_fixtures.py build/fixtures
python3 -I tools/check.py build
```

核对器**不导入** `fhist` 解析代码：它仅通过 CLI 驱动正式库，自己用标准库
重读 NDJSON、自实现一份 cut 语义，并维护人工版本/边期望值表进行双向比对。
