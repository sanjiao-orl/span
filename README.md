# 寻章 Span

**人与 AI 助手共用的本地长篇小说检索、区间对比与原文回溯 CLI**（命令：`span` · Python 包：`chapspan`）

直接把数百万字的小说塞进大模型上下文，容易跳读、记混前后剧情、剧透未读章节或编造引文。**寻章 Span** 在本地用确定性索引划定阅读进度上限（`--as-of`），先做不占正文预算的目录定位、多词统计和无预设区间对比，再按预算调取带物理坐标（`[@C0001:4-6]`）的原段落与上下文，让每一次判断都可追溯、可复核、可跨会话接续。

## 真实使用演示

以英文公版长篇《白鲸》（EPUB，全书 141 章）为例，直接检索并跟进页尾给出的回读命令：

```text
$ span --text moby-dick.epub find Rachel --as-of 141
"Rachel" 5 次 / 3 章（C0132 · C0134 · C0140） · 范围 C0001–C0141
例段 3：C0132(3)（跨章 --spread）

[例1] Chapter 128. The Pequod Meets The Rachel. [@C0132:0-368]
Next day, a large ship, the Rachel, was descried, bearing directly down upon the Pequod, all her spars thickly clustering with men...

[例2] Chapter 128. The Pequod Meets The Rachel. [@C0132:4364-5888]
Now, as it shortly turned out, what made this incident of the Rachel’s the more melancholy...

[例3] Chapter 128. The Pequod Meets The Rachel. [@C0132:7587-7797]
But by her still halting course and winding, woeful way, you plainly saw that this ship that so wept with spray, still remained without comfort. She was Rachel, weeping for her children, because they were not.

---
扩读（1–3）：span --text moby-dick.epub read --from-result state_6d7282ecf8ede8509391421d --example 1 --before 1 --after 1
其余：--hit-chapters 看各章 | --examples 0 看坐标
```

每次检索、对比或截断阅读都会在页尾生成**当前终端可直接复制执行**的下一步命令（自动附带快照校验、状态目录和当前启动入口）。

## 核心特性

- **严格防剧透范围控制**：通过 `--as-of`（支持 `all`、`C0012`、原书章号或章题）与 `--from-chapter` 锁定可见章节上限，目录、统计、采样和引文校验均不越界。
- **确定性坐标与防改写核验**：内部统一按一维章序 `C0001..Cxxxx` 与字符半开区间定位，文本被外部修改导致 `snapshot_id` 变化时明确拒绝继续按旧坐标回读。
- **不占正文预算的全局探索**：支持单组布尔词与邻近段检索（`find --near`）、多组批查（`find --query`）、别名增量比较（`--any`）与基于对数似然比的区间差异发现（`compare`）。
- **原生多格式只读解析**：零外部电子书依赖支持 TXT、Markdown 与无 DRM 的 EPUB 2/3，保留原书只读，默认过滤感言/后记/请假条等外围章（可用 `--include-peripheral` 纳入）。
- **为人与 AI 助手共同设计**：直接阅读默认给出紧凑可读文本，`--library` 与管理命令提供结构化 JSON，内置 `span help agent` 可直接作为 AI 编码助手的工作区规范。

## 安装

需要 **Python 3.10 及以上版本**（纯 Python + `PyYAML`，发布包为跨平台 `py3-none-any` wheel）。

### macOS / Linux（bash / zsh）

从 [GitHub Releases](https://github.com/sanjiao-orl/span/releases) 下载 `.whl` 或发行 ZIP，或在克隆源码仓库后安装：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./chapspan-0.1.0-py3-none-any.whl   # 或在源码目录执行: python -m pip install .
span --version
```

### Windows（PowerShell）

下载发行包 ZIP 解压后，在解压目录中通过独立虚拟环境安装（无需激活环境即可直接通过 `& $span` 使用）：

```powershell
python -m venv .venv
$wheel = (Resolve-Path '.\chapspan-*.whl').Path
.\.venv\Scripts\python.exe -m pip install $wheel
$span = (Resolve-Path '.\.venv\Scripts\span.exe').Path
& $span --version
```

## 常用工作流

### 1. 快查细节、目录导航与跳读（零配置）

不需要先建书库或初始化目录，直接对文件使用 `--text`：

```bash
span --text book.epub overview --as-of 120 --toc
span --text book.txt find "城门" "灯" --near 2 --as-of 第十二章 --examples 2
span --text book.txt find "城门" --hit-chapters --as-of 50
span --text book.txt read --chapter C0020-C0023 --budget 12000 --as-of 25
```

### 2. 无预设对比前后章节变化

对剧情转向、文风变化或人物出场消长，用 `compare` 抽出显著差异词后再并读原文：

```bash
span --text book.txt compare --left 1-100 --right 101-200 --as-of 200 --examples 2 --budget 1200
```

### 3. 交给 AI 助手操作

从发行包复制随附的 `AGENTS.md` 到你的阅读/研究工作区，或直接生成并喂给 AI 助手：

```bash
span help agent > AGENTS.md
```

之后只需用自然语言告诉 AI 三件事：**文件在哪、要查或分析什么、当前读到第几章**。

### 4. 长篇研究、持久化运行与稿件引文核验

跨会话长篇分析建议用 `research init` 固定来源与范围边界，再通过 `resume` 和 `research index` 审计 Markdown 笔记中的 `[@C0001:4-6]` 引用：

```bash
span --text book.epub research init ./study-task --as-of 300 --purpose "人物线梳理"
span --run ./study-task/runs/run_xxxx find "主角" --spread --examples 3
span --run ./study-task/runs/run_xxxx research expand --as-of 600
span --run ./study-task/runs/run_xxxx resume
span --run ./study-task/runs/run_xxxx research index
```

## 支持格式与边界

- **原生支持**：TXT（`.txt` / `.text` / 无后缀）、Markdown（`.md` / `.markdown`，支持 `--heading-level 1..6` 与 `--single-chapter`）以及无 DRM 加密的 EPUB 2/3（`.epub`）。
- **入库与预览体检**：遇到分章异常、非 UTF-8 编码或超大 EPUB 时，可先运行 `span import <文件> --preview` 查看编码、章数、紧连候选与结构状态；多 MB 以上长篇建议通过 `span --library <书库目录> import <文件>` 导入持久书库再分析。
- **能力边界**：始终对原文件只读；不破解 DRM 加密，不内置 PDF / DOCX / MOBI / AZW3 解析，也不在内部静默做繁简转换、纠错或去广告清洗（外部前置工具建议与防剧透隔离做法见内置 `troubleshooting` 专题）。

## 内置离线帮助

安装后无需联网即可查看所有命令详情、JSON 字段、外部预处理指南与引用审计规则：

- `span --help` / `span <子命令> --help` — 全量参数与示例
- `span help quickstart` — 三种来源模式与最小闭环
- `span help method` — 选材策略、证据五层与外部资料标注方法
- `span help agent` — 与发行版 `AGENTS.md` 一致的 AI 操作手册
- `span help json` — 各命令 JSON 字段清单与单位口径
- `span help run` — `research` 运行目录与 `材料索引.md` 说明
- `span help citations` — YAML 稿头与 `resume` 引用校验六种状态
- `span help troubleshooting` — 编码排查、分章覆盖与外部预处理指南

## 开源许可

本项目采用 [MIT License](LICENSE) 开源。
