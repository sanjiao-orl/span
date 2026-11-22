# 寻章 Span — 使用者工作区 Agent 作战手册

本文件约束阅读、考据与拆书工作区中操作 `span` CLI 的 AI Agent。先看前 3 节即可直接上手，勿凭记忆猜参数，也勿用脚本直读原小说文件。

## 1. 进场路由决策树（0 秒选对起点）

```text
收到任务
 ├─ 一次性快查（找人名/查章号/核对某桥段）
 │    └─ 直接用 --text 跑 overview / find / read，2–4 步直接作答
 │       [禁建 research init · 禁写当前稿.md · 禁套五层长报告模板]
 ├─ 长篇对比 / 拆书 / 改稿 / 跨会话接续
 │    └─ 先 research init <任务目录> --as-of ...，后续把回执里的 run_directory 传给 --run
 └─ 遇到编码乱码 / 分章为 0 / 紧连标题 / 多层级 Markdown / EPUB 摸底
      └─ 立刻跑 span import <文件> --preview（兼容别名 import-txt）
         （返回可用编码提示、suggested_chapter_pattern、0 命中 line_samples、
          异常超长章 chapter_length_stats、Markdown 层级及 EPUB 目录/Spine 状态）
```

## 2. 硬契约速查卡（一屏兜住高频参数与上限）

| 维度 | 硬规则与常用上限 |
|---|---|
| **参数位置与输入** | `--text` / `--library` / `--run` / `--format` / `--output` / `--state-dir` 与 `--encoding` / `--chapter-pattern` / `--paragraphs` / `--heading-level` / `--single-chapter` **放在子命令前后均可**；业务参数（`--as-of`、`--from-chapter`、`--limit` 等）放子命令后，`--as-of` 与 `--from-chapter` 均支持 `C0002` 或整数 `2`。支持 TXT（`.txt`/`.text`/无后缀）、Markdown（`.md`/`.markdown`）与无 DRM 的 EPUB（`.epub`）；多层级 Markdown 须显式传 `--heading-level 1..6`；EPUB 不接受 `--encoding` / `--heading-level`（可用 `--chapter-pattern` 或 `--single-chapter` 覆盖）；无章界单篇文稿须显式传 `--single-chapter`（与 `--chapter-pattern` / `--heading-level` 互斥） |
| **目录与统计** | `overview` 默认 20 条/页；要字符数与段数时**直接传 `--limit 2000 --format json`**（上限 `2000` 受 400KB 体积保护，长篇 1–2 页拉完）；仅看章号用 `--toc`。**翻页用 `--after-chapter`**（非 `--offset`） |
| **检索与批查** | `find --query` 单次最多 **`32` 组**，支持 7 个键： `all`、`any`、`exclude`、`near`、`offset`、`distribution_offset`、`limit`。批量初筛可用 `--compact --examples 0 --limit 1`，保留每组总量和首个匹配项的回读坐标。 |
| **阅读与预算** | `read` 单个 `--chapter` 区间 `<= 100` 章、展开 `<= 32` 选区；顺读大章设 `--budget 12000–24000`；并读对比设 `--group-budget 2000 --budget 12000` |
| **并发与格式** | 1) **同一 `--run` 下所有命令（含 `read`/`find`/`compare`）必须严格串行执行**，勿并发发起多进程（用单条 `--query` / `--chapter` 批处理代替并发）；<br>2) **读正文优先用默认/显式 `text` 模式**（少对多章 `read` 强加 `--format json`）；<br>3) **写稿正式引用裸写 `[@C0001:4-6]`，严禁加反引号**（`` `[@C0001:4-6]` `` 会被 `resume` / `research index` 当代码示例跳过） |

## 3. 高效打法对照（DO vs DON'T）

- **优先复用页尾可执行命令。** `find` / `compare` / `overview` / 截断 `read` 页尾或 `--output` 回执打印的 `回读` / `扩读` / `续读` 命令已自动带齐快照绑定、`--state-dir`、输出格式与当前启动入口；直接复制执行即可，少手拼参数与坐标。
- **筛一批词，再选组回读。** PowerShell 用单引号包 JSON。比较各阶段总量时，按阶段传 `--from-chapter` 和 `--as-of`。
  ```powershell
  span --run $run find --compact --examples 0 --limit 1 --format json --query '{"all":["城门"]}' --query '{"all":["雾夜","钥匙"],"near":2}'
  ```
- **查词散落在哪些章。** 用 `span --run $run find '城门' --hit-chapters --limit 10`，每行有章号、匹配数和一处回读坐标。用 `--distribution-offset` 跳过已看的命中章，批查的翻页值填在对应查询的 `distribution_offset`。要核对含零值的逐章分布，文本用 `--distribution`，JSON 看 `distribution`。
- **查另一个叫法补出了哪些章。** 用 `span --run $run find --any '铜钥匙' --any '青铜匙'`。仅查两个 OR 词时，结果提供各词独有章与两词同章的章数，以及独有章的首处回读坐标。各词次数独立计。
- **跨章取例并记录选法。** 用 `find --spread --examples 3` 按命中章等距取例。保存结果后用 `read --from-result 结果.txt --example 1` 回读可沿用选法。运行内用 `research index` 查看材料的选材来源。
- **Markdown 文稿、EPUB 电子书与单章文档**：
  - **DO**：对 `.md` / `.markdown` 先跑 `import 文稿.md --preview` 看 `markdown_heading_levels`；仅有 1 个 ATX 层级可直接读，多层级（如 `#` 分卷 + `##` 分章）显式传 `--heading-level N`；剥离 `#` 存标题后直接兼容 `--as-of 第三章`，正文保 Markdown 原样，开头 YAML front matter 归 `章前文字`、围栏代码块内 `#` 不误切。对 `.epub` 先 `import 书.epub --preview` 看 `chapter_source`、`skipped_spine_items` 与章数；中短篇可直接 `--text 书.epub`，数 MB 以上长篇或多轮分析建议先 `span --library 书库 import 书.epub` 入库，再以 `--library` 或 `research init --library ...` 建 `--run`，避免每次调用重复解压解析全书。无章界单篇同人/设定/大纲显式用 `--single-chapter`。
- **脏 TXT 体检、外部预处理与防剧透隔离**：
  - **DO**：先看 `import --preview`（兼容别名 `import-txt`）返回的样例标题用字（繁体/异体如 `門/將`）、0 命中 `line_samples` 与超长章 `chapter_length_stats`；在 PowerShell 把长正则存进 `$pat` 变量复用（`--chapter-pattern $pat`）。若需外部繁转简/转码/清杂乱（`opencc -c tw2s`、`charset-normalizer`、离线 `novel-processor`；**勿对中文小说直跑 `ftfy` 默认 CLI**），**未读完作品须在阅读分析会话之外**定版输出新 UTF-8 文件并保留原件，再 `research init` 并把工具与参数写入 `--version-note`（详规见 `span help troubleshooting`）。
- **遇到原书章号重号（如多卷从第一章重新编号）**：
  - **DO**：看报错列出的当前范围内冲突 `Cxxxx`（如 `C0080`），确认顺序后改传 `--as-of C0080`。
  - **DON'T**：盲猜章号或违背用户进度要求去翻未授权后文。
- **获取精确句段引用**：整章 `read` 只在章头给出整章区间；写稿需要局部精确段坐标时，用 `find` 命中片段的 `read_spans`。
- **长期研究与 `run_directory`**：`research init` 可带 `--purpose` / `--audience` / `--evaluation-framework` / `--external-policy` / `--version-note`；后续用回执中的 `run_directory`（`<任务目录>/runs/run_...`），扩界须显式 `research expand`。
- **离线内置专题**：`span help quickstart | method | json | run | citations | troubleshooting`。

## 4. 范围红线、分析五层与核稿规范

- **严守允许范围**：正文、标题与统计只取当前授权范围（`--as-of`、`--from-chapter`、默认过滤外围章或显式 `--include-peripheral`）。**绝不**凭模型记忆补写、剧透、还原屏蔽词或混入他版剧情。
- **不把存储或坐标当已读**：`--output` 落盘、`find` 坐标、`compare` 词频、`research index` 计数均不代表正文已进入上下文；见 `未完成` / `未开始` / `本轮未续` 须按提示续读或如实说明（`complete=true` 仅指本次选区完结）。
- **原书只读**：不改写用户原 TXT / Markdown / EPUB；需要清洗转码时另存新 UTF-8 文件并保留原文件。
- **何时先问**：先继承已知进度、用途与约束；仅当缺失信息会实质改变**阅读范围、评价框架或投入尺度**时先简短确认，复杂长篇可先交一小段对齐方向。
- **分析五层（只作证据分层标尺，快查勿机械列五标题；详规见 `span help method`）**：
  1. **原文事实**：对应 `[@C0001:0-8]` 或 `source_citation` 的实写内容。
  2. **角色叙述层**：口头猜测、撒谎或主观视角，不能直接当客观设定。
  3. **外围／副文本**：作者的话、请假条、感言、后记属创作证据；拼在章末的感言可能未标 `kind=peripheral`，须读内容判别。
  4. **品类内解读**：文类常规与节奏推论，涉及“读者反应”须给文本依据或标明推测。
  5. **外部资料与外部立场**：按“先文本取材 → 初判 → 按需联网 → 回原文核验”执行；外部资料单列网址与待核对状态（**不写进 `[@...]`**）；外部价值立场评价**仅在用户明确要求时**单列并注明立场。
- **矛盾处理**：并列两侧出处，区分叙事内原因与创作层原因，证据不足标“未定”；区分内部章序 `C0001`、故事时间与连载时间，多版本声明底本。
- **改稿与状态保护**：Markdown 稿保留 YAML 稿头（`snapshot_id`、`knowledge_horizon`、可选 `collection_id` 与约定字段）；移动 `--output` 文本文件时一并携带 `.state.json`；成稿后用 `resume`（大稿 `--citations-only`）或 `research index --draft ... --section 行号` 核验；异常按 `span help citations` / `troubleshooting` 如实汇报，不伪造坐标。
