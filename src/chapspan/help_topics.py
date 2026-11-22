"""Built-in help topics shipped with the installed span package."""

from .model import MAX_CHAPTER_RANGE_SPAN, MAX_SELECTIONS
from .sources import TIGHT_CHAPTER_PATTERN

HELP_TOPICS = {
    "quickstart": (
        "按任务选择入口、三种来源模式与单本 4 步上手流程",
        f"""寻章 Span · 快速上手（quickstart）

1. 按任务选择命令
  了解陌生书       span overview --toc / span read --sample 3
  核实已知词/事件  span find '词1' '词2' --near 2 / span find --any '甲' --any '乙'
  查词命中哪些章   span find '城门' --hit-chapters --limit 10
  无预设对比变化   span compare --left 1-100 --right 101-200
  并读或连续读     span read --chapter C0002 --chapter C0006 --group-budget 2000 / read --chapter C0020-C0023
                   （单个 --chapter 区间宽度 <= {MAX_CHAPTER_RANGE_SPAN} 章，单次读取展开后合计 <= {MAX_SELECTIONS} 个选区）
  多组独立检索     span find --compact --examples 0 --limit 1 --query '{{"all":["灯"]}}' --query '{{"all":["门"]}}'
  书库收藏与回读   span collect add/list/remove / span read --item ID
  研究运行与索引   span research init|expand|index
  成稿核验与恢复   span resume 稿件.md [--citations-only]

2. 三种来源模式（来源与输出参数放在子命令前后均可）
  --text 书.txt/文稿.md/书.epub 直接读取 TXT、Markdown 或无 DRM EPUB；overview/find/compare/read 默认输出可读文本
  --library 书库目录            已有 SQLite 书库；默认输出单行 UTF-8 JSON，需配合 --snapshot
  --run 运行目录                绑定 research init 返回的 run_directory，固定快照与范围
                                （在 run_directory 内部执行或设 SPAN_RUN_DIR 时可省略 --run）
  注：--encoding / --chapter-pattern / --paragraphs / --heading-level / --single-chapter
      用于 --text 或 import / import-txt（放子命令前后均可）。
      数 MB 以上的长篇 EPUB 建议先 span --library 书库 import 书.epub 入库再配合 --run 使用。

3. 单本 TXT 最小四步闭环
  span --text 书.txt overview --as-of 第十二章 --toc
  span --text 书.txt compare --left 1-6 --right 7-12 --as-of 12 --output cmp.txt
  span --text 书.txt read --from-result cmp.txt --pick 1 --group-budget 1200 --before 1 --after 1
  span --text 书.txt resume 研究稿.md --as-of 第十二章
  提示：检索、对比或截断阅读后可直接复制页尾打印的「回读」「扩读」「续读」命令（已自动绑定快照与参数）。

更多专题：span help method | agent | json | run | citations | troubleshooting""",
    ),
    "method": (
        "选材策略、分析分层、何时先问、阶段顺序、矛盾处理与外部资料标注（C1/C3）",
        """寻章 Span · 阅读方法与外部资料约定（method）

1. 按任务尺度选材（快查不走重流程）
  - 一次性核实词面或段落：直接 --text 运行 overview/find/read，无需初始化 research。
  - 在分散命中的章中选材料：用 find --hit-chapters 看各章匹配情况和回读坐标。跨章取例可用 find --spread --examples 3。
  - 因果、状态改变或反转判断：用 read --before/--after 或 --whole-paragraphs 补足前因与后果。
  - 评估节奏、文风或留存变化：用 read --chapter C0020-C0023 --budget 24000 连读连续章节。
  - 比较人物前后或两段候选：用 read --group-budget 并读可比段落，不用单边截断代替对比。
  - 核对实际交付量：留意行内“未完成”及页尾“未开始”“本轮未续”；--output 落盘与引用坐标不代表已读。

2. 何时先问
  - 先继承用户已给出的要求、当前运行与稿头；快查及其他明确任务直接开始，不强制填写研究问卷。
  - 仅当缺失信息会实质改变“阅读授权范围（读到哪一章）、评价框架（用途/受众/立场）或投入尺度”时先问。
  - 复杂长篇任务可先交一小段校准方向，把确认结果写入稿头，换会话接续时不重复询问。

3. 分析五层（区分证据身份；快查不必机械套五个标题，不把推测或立场写成原文事实）
  (1) 原文事实     可回指正文坐标（[@C0001:0-8] 或 source_citation）的叙述、动作与描写。
  (2) 角色叙述     角色口头猜测、承诺、撒谎或不可靠视角；不能直接等同客观设定。
  (3) 外围／副文本 作者的话、请假条、上架/完本感言、后记属于创作状态证据；拼在正文章末尾的
                   感言可能不带 kind=peripheral 标签，须读内容判别身份，不把解析标签当充分判断。
  (4) 品类内解读   结合该品类读者与作者期待、节奏与套路变奏的分析；涉及“读者会怎样”须给依据或标推测。
  (5) 外部资料与外部立场 外部事实资料（版本、连载时间、作者说明）按第 4 节按需查证并带网址单列；
                   外部价值立场评价仅在用户明确要求时单列并注明立场，不自行判定“必要”而带入。

4. 阶段顺序
  文本取材 -> 文本内初步理解 -> 用户补充与质疑 -> 按需联网对照 -> 回原文复核。
  - 前期不搜索作品后续内容与网络评价；版本、连载时间等来源元信息可查并标注来源。
  - 用户要求联网比较时按其授权范围执行并记录资料进入时机，把外部资料当作待验证假设。

5. 矛盾处理与版本边界
  - 前后文不一致时并列两侧出处，不强行调和。
  - 区分叙事内原因（说谎、不可靠叙述、伏笔揭晓）与创作层原因（遗忘、改设定、事后解释），证据不足标“未定”。
  - 区分文件内部章序（C0001 起算）、故事时间与创作/连载时间；多版本作品明确声明所用版本，警惕混入其他版本记忆。

6. 外部资料 Markdown 标注例子（与 source_citation 区分）
  在研究稿中记录外部资料时，使用独立外链注释或引用块，不放进 [@...] 原文坐标中：

    > [外部资料·待核对] 作者访谈（连载中期补充设定）
    > - 来源网址：https://example.invalid/interview
    > - 用途与进入时机：初步文本取材后，对照“城门时间线”创作层矛盾
    > - 状态：待回原文第 12 章核对（不计作原文交付或 source_citation）

  说明：外部网址只供人工阅读与追溯，不参与 Span 原文引用校验，也不计入材料索引交付量。""",
    ),
    "agent": (
        "发行版使用者工作区 AGENTS.md 原文（可重定向保存为工作区 AGENTS.md）",
        """# 寻章 Span — 使用者工作区 Agent 作战手册

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
- **改稿与状态保护**：Markdown 稿保留 YAML 稿头（`snapshot_id`、`knowledge_horizon`、可选 `collection_id` 与约定字段）；移动 `--output` 文本文件时一并携带 `.state.json`；成稿后用 `resume`（大稿 `--citations-only`）或 `research index --draft ... --section 行号` 核验；异常按 `span help citations` / `troubleshooting` 如实汇报，不伪造坐标。""",
    ),
    "json": (
        "输出格式切换规则、核心 JSON 字段清单与单位边界",
        """寻章 Span · 输出格式与 JSON 字段（json）

1. 输出格式默认规则
  默认可读文本：--text 下的 overview、find、compare、read（不以 .json 结尾时）
  默认单行 JSON：--library 全部命令；所有模式下 import / import-txt、snapshots、collect、
                 resume、research init|expand|index；--output 指向 *.json 文件
  绑定 --run 时：overview/find/compare/read 继承建 run 时来源（--text 建 run 默认文本，
                 --library 建 run 默认 JSON）；可用 --format text|json 显式覆盖
  例外：resume、import / import-txt、snapshots、collect、research 固定输出 JSON。

2. 各命令核心 JSON 字段
  公共头部       scope: {snapshot_id, as_of, first_chapter, include_peripheral}
  import/import-txt
                 snapshot_id, created, chapters, unclassified_chapters,
                 peripheral_chapters, empty_chapters, format, heading_count,
                 tight_candidate_count, chapter_length_stats（有章时）,
                 total_lines / line_samples（0 命中且无紧连候选时）,
                 markdown_heading_levels（Markdown 文件）,
                 chapter_source / skipped_spine_items / image_count（EPUB 文件）
  snapshots      snapshots[{snapshot_id, source_name, encoding, parsing}], next_after_snapshot
  overview       chapters[{chapter, title, kind, first_paragraph, source_body_line, paragraph_count, characters}],
                 chapter_kind_counts, next_after_chapter, chapter_diagnostics（--inspect 时含 source_lines 与可选 source_href）
  find           mode(literal|chapter|paragraphs), term_counts[{term,count}], match_count,
                 hit_chapter_count, hit_chapter_ranges, matches[{chapter,span,read_spans}],
                 next_offset, distribution, next_distribution_offset,
                 examples{selected_spans, selection_origin, fragments, delivery, continuation}
                 --hit-chapters 用 hit_chapters[{chapter,title,kind,match_count,read_span}] 分页
                 纯两词 OR 含 alias_delta{first_only,both,second_only}，各组有 chapter_count，
                 独有组有 term 和 read_spans
  find --query   queries[]（逐组独立字段与 example_refs 如 Q1E1）, reading{...}
  compare        left_interval, right_interval, candidate_count, next_offset,
                 pool_truncated/total_candidates,
                 candidates[{rank, term, trend, g2_score, left, right, read_spans}], explanations[], reading
  read           fragments[{chapter_title, target_span, read_span, citation, source_citation, text}],
                 可追溯的片段含 selection_origins
                 read_characters, complete, delivery[], continuation, passages[](分组并读正文在此)
  resume         scope, collection_id, conventions{effective, sources, conflicts},
                 citation_count, skipped_inline_citations, citations[{citation, status, note}],
                 markdown（--citations-only 仍保留 conventions）
  research init  run_id, run_directory, snapshot_id, scope, conventions, draft, draft_header
  research index run_id, index, scope, conventions, chapters, delivery_characters, citations,
                 skipped_inline_citations, coverage（--section 返回 conventions、citation_count 与 skipped_inline_citations）

3. 单位边界与 PowerShell 技巧
  - 词面次数（term_counts）、命中章数（hit_chapter_count）、对照 G2 分数与交付字符数
    （read_characters）各自代表不同度量单位，互不替代。
  - next_distribution_offset 在默认分布中按可见章计，在 --hit-chapters 中按命中章计。
  - selection_origin 说明取例方式。first_match 表示首个命中章取一处，match_order 表示按匹配页顺序，
    even_hit_chapters 表示按命中章等距。回读片段和收藏项可通过 selection_origins 保留来源。
  - resume 的 citation_count 是去重后的已检查引用数；research index 的 citations 是稿内
    引用出现总次数；skipped_inline_citations 仅统计默认跳过的纯引用形态行内代码处数
    （不含普通行内代码与围栏代码块）。
  - complete=true 仅代表本次所选目标（如例段或单段）交付完，不代表读完原章。
  - PowerShell 读取 JSON：$res = span --library 库 find '城门' --snapshot $id --as-of 12 | ConvertFrom-Json
  - PowerShell 传 --query：--query '{"all":["灯"],"near":2}'""",
    ),
    "run": (
        "独立研究运行、任务目录与 run_directory 区分、--run 与材料索引口径",
        """寻章 Span · 独立研究运行（run）

1. 初始化与目录结构
  span --text 书.txt research init ./study-task --as-of 第十二章 --purpose '剧情核实' --evaluation-framework '叙事与因果'
  # 返回的 JSON 中 run_directory 形如 ./study-task/runs/run_xxxxxxxxxxxxxxxx
  可选约定参数：--purpose、--audience、--evaluation-framework、--external-policy、--version-note
                （均 <= 500 字，仅作记录与恢复回显，不改变范围与交付统计）。
  注意：传给 --run 或 SPAN_RUN_DIR 的必须是 run_directory（含 run.json），
        而不是外层“任务目录”。若在 run_directory 内部执行，cwd 会自动识别运行。

2. 支持与不支持 --run 的边界
  支持 --run：overview、find、compare、read、resume、research expand、research index
  固定约束 ：同一运行绑定固定来源、快照、解析参数与 states 目录；普通调用不得再传
             --text / --library / --state-dir / 文件解析参数。
  临时收窄 ：运行内 overview/find/compare/read/resume 可用 --from-chapter / --as-of 收窄当次范围。
  显式扩界 ：span --run $run research expand --as-of 20 [--from-chapter 1] [--include-peripheral]
  不支持   ：collect 属于书库持久收藏，不支持 --run；请用 --library <书库> --snapshot <ID> --as-of <末章>。
             import / import-txt / snapshots 也不支持 --run。

3. 改稿与材料索引（research index）
  - 可将 init 返回的 draft_header 写入 <run_directory>/当前稿.md 开头，运行内 resume 默认读取它。
  - 全量重建索引：span --run $run research index   # 写入 <run_directory>/材料索引.md
  - 只看一节引文：span --run $run research index --draft 报告.md --section 36
    注意：--section 传的是 ATX # 标题所在的 1-based 整数行号，不是标题文字。
  - 交付统计口径：仅按成功调用实际返回的 read_span 去重计算字符数；纯统计 compare 计 0 字，
    带 --examples 的 compare 仅计实际返回的例段。""",
    ),
    "citations": (
        "引用坐标格式、YAML 稿头与 resume 核验状态",
        """寻章 Span · 引用坐标与稿件恢复（citations）

1. 两种坐标形式
  短坐标      [@C0002:0-22]                      适合同一运行/同一快照正文内引用
  完整来源坐标 [@snap_<64位hex>/C0002:0-22]       跨文件或持久稿件推荐，包含快照身份
  注：正文章序号以目录实际 C0001 起算（序言/楔子/章前文字也占内部章序）。

2. Markdown YAML 稿头
  ---
  snapshot_id: snap_完整的64位十六进制快照ID
  knowledge_horizon: 12
  collection_id: 可选收藏集名
  purpose: 可选研究用途
  audience: 可选目标受众
  evaluation_framework: 可选评价框架
  external_policy: 可选外部资料政策
  version_note: 可选版本说明
  ---

  说明：研究约定字段全部可选，显式 null（YAML 留空）等同未声明，其余非字符串值一律拒绝；
        --run 下 resume / research index 逐字段以稿头显式值优先、缺省沿用运行值，
        在 conventions 中标明来源并在两者不同时同时列出 conflicts（research init 则返回平面声明字典）。

3. resume 校验与状态含义
  ok                   坐标合法且在当前范围内可读
  malformed            引用格式损坏
  snapshot_mismatch    引用声明的快照与当前校验快照不同
  unavailable          章节不在当前可见范围（如低于 --from-chapter、被外围过滤或章节不存在）
  beyond_draft_horizon 章节在当前 --as-of 内但超出稿件知识地平线（knowledge_horizon / --draft-horizon）
  invalid_range        章号合法但字符起止超出实际正文长度

4. 大稿与无稿头场景
  - 稿件已在本地时加 --citations-only 避免重复返回整篇 Markdown，防止超出输出上限。
  - 同一研究运行内的无 YAML 稿头报告，可用 --run $run resume 稿件.md --draft-horizon 12 确认进度。
  - 默认跳过行内代码和围栏代码块；若存在单独包在反引号里的纯引用坐标，会统计在
    skipped_inline_citations 并附恢复提示（普通行内代码与围栏块不计入），加
    --include-inline-citations 即可纳入校验。""",
    ),
    "troubleshooting": (
        "常见报错定位、TXT/Markdown 体检与分章修复、外部前置工具指南与状态规则",
        f"""寻章 Span · 常见问题排查与前置文本准备（troubleshooting）

1. 编码乱码、漏分章、Markdown 多层级、EPUB 与单章文稿
  - 先无副作用体检：span import 书.txt --preview  或  span import 文稿.md --preview  或  span import 书.epub --preview（兼容旧名 import-txt）
    * 0 命中且无紧连候选时，preview 返回 total_lines 与最多 6 条截断短行样例（line_samples）；
    * 有识别章节时，preview 返回 chapter_length_stats（中位数、最大值与最长前 3 章），用于发现外部转换导致的粘连漏章；
    * Markdown 文件在 preview 中报告 format="markdown" 与各 ATX 标题层级数量（markdown_heading_levels）；
    * EPUB 文件在 preview 中报告 format="epub"、chapter_source（toc/spine/pattern/single_chapter）、skipped_spine_items 与 image_count；数 MB 以上长篇 EPUB 建议先 import 入库，避免每次 --text 重复解包。
    * EPUB 清单、目录和正文支持带 BOM 的 UTF-16。若提示 Spine 正文条目缺失，请重新导出完整 EPUB，再用 import --preview 核对章数。
  - 针对疑点章/行看原始行：span --text 书.txt overview --as-of all --inspect C0002 --line 7
  - 按需传解析参数（放子命令前后均可）：
      --encoding gb18030    # 仅用于 TXT / Markdown，不用于 EPUB
      --paragraphs blank-lines
      --chapter-pattern '{TIGHT_CHAPTER_PATTERN}'
      --heading-level 2     # 多层级 Markdown（如 # 卷 + ## 章）显式选定分章层级（1–6，不静默猜测）
      --single-chapter      # 无章界单篇同人/设定/大纲显式按单章 C0001 导入/读取（与分章规则互斥）
  - 注意：更改解析参数或外部清洗都会改变快照 ID，新旧快照的坐标不可混用。

2. 前置文本准备与外部工具指南（先体检 → 会话外定版 → 记录血缘）
  工作流硬顺序：
    (1) 先用 import --preview（兼容别名 import-txt）体检编码、章数与章长分布；
    (2) 若需转码、繁转简、去广告或格式转换，在**独立于阅读分析的终端/会话**中另存新的 UTF-8 文件并保留原件（避免未读完作品剧透污染阅读 Agent）；
    (3) 对定版后的新文件重新 preview 复核，再执行 research init / 写稿，并把前置工具与参数记入 --version-note。

  常用工具与实测避坑：
  - opencc（推荐：编码转换 + 繁转简一步到位）：
      python -m opencc -i 原书.txt -o 书_utf8_simp.txt -c tw2s --in-enc gb18030 --out-enc utf-8
      说明：网文繁转简须用 -c tw2s 而非 -c t2s（t2s 会大量残留“著”而非“着”）；古典文献与异文考据则只转码不转简。
  - charset-normalizer（纯编码探测与转 UTF-8）：
      normalizer -m 书.txt     # 探测编码
      normalizer -n 书.txt     # 转为 UTF-8（注意：输出文件名会带原编码后缀如 书.gb18030.txt，实际内容已是 UTF-8）
  - rockbenben/novel-processor（非命令行用户清洗盗版杂乱 TXT）：
      浏览器本地离线运行不上传；注意已知三项副作用：① 标点规范正则可能误改正文中的“章、”“回：”等标点；
      ② 回目独占一行时可能被智能合并断行粘到首句；③ 广告过滤可能误删作者官方群通知行。清洗后请再用 --preview 核查 chapter_length_stats。
  - 明确不推荐中文小说直接使用 ftfy 默认 CLI：
      ftfy 默认硬开全角转半角与弯引号拉直且无命令行关闭选项，会批量破坏中文全角标点与段首全角空格。
  - 其他格式转换边界：
      * .epub：原生支持无 DRM 加密的 EPUB 2/3（直接 --text 书.epub 或 span --library 书库 import 书.epub）；若提示含有 DRM 加密，需先在合法授权下移除加密或转换后再接入。不建议用 pandoc 直接把中文 EPUB 转为 plain TXT（易产生章题与上章末句粘连、句中插半角空格打断字面检索）。
      * .mobi / .azw3：可用 Calibre（ebook-convert 书.mobi 书.epub 或 书.txt）转换后再体检。
      * .docx：可用 pandoc 书.docx -t gfm --wrap=none -o 书.md 转为 Markdown 后接入 Span。
      * .pdf：页眉页脚、分栏与硬断行容易破坏章界和检索，不推荐直接转换用于精读。

3. 外围文字与 [未分类]
  - 序章/楔子/番外/章前文字（含 Markdown 开头 YAML front matter）默认保留并标为 [未分类]。
  - 作者的话/上架感言/请假条/完本感言/后记默认作为外围章过滤；若确实需要读取，加 --include-peripheral。

4. 找不到 state_... 短状态柄或无法 --from-result 续读
  - 直接文件默认把状态写在系统用户状态目录，书库写在 <书库>/states，--run 写在 <运行目录>/states。
  - 跨目录或换环境续读时，传同一 --state-dir，或直接把当初 --output 保存的 .txt/.json 传给 --from-result。
  - 移动文本输出文件（如 out.txt）时，须一并携带伴生的 out.txt.state.json。
  - compare / overview / 无待续区的 find 不能裸传给 read --from-result：须搭配 --pick 1..N 或 --example N。

5. --output 不覆盖规则
  - Span 对 --output 启用独占新建写保护，目标已存在即拒写并返回失败；请每次更换新文件名。

6. 传 --run 时提示缺少 run.json
  - 检查是否把外层的“任务目录”误传成“运行目录”，实际运行目录在 <任务目录>/runs/run_... 下。""",
    ),
}


def render_help_topic(topic=None):
    """Return formatted topical help text, or the topic index when topic is None."""
    if topic is None:
        lines = [
            "寻章 Span 内置帮助专题（在安装后独立环境中无需仓库文档即可查阅）：",
            "",
            *[f"  {name:<16} {summary}" for name, (summary, _) in HELP_TOPICS.items()],
            "",
            "用法：span help <专题名>   例如：span help json",
            "全部命令与参数概览：span --help 或 span <命令> --help",
        ]
        return "\n".join(lines)
    key = topic.strip().casefold()
    if key not in HELP_TOPICS:
        available = "、".join(HELP_TOPICS)
        raise ValueError(f"未知帮助专题：{topic}；可选专题：{available}")
    return HELP_TOPICS[key][1]
