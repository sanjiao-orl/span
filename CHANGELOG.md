# Changelog

## [0.1.0] - 2026-11-22

首个公开发布版本。

### 核心能力

- **范围与快照隔离**：提供 `--text`（直读文件）、`--library`（SQLite 持久书库）与 `--run`（固定研究运行与 `.writer.lock` 串行写锁）三种来源模式，按 `--as-of` / `--from-chapter` 与默认外围章过滤严格限制可见边界；同一 `snapshot_id` 下统一使用一维物理坐标 `C0001..Cxxxx` 与字符半开区间。
- **原生多格式读取**：纯标准库支持 TXT（可配 `--encoding` / `--chapter-pattern` / `--paragraphs`）、Markdown（ATX 分章、跳过 YAML front matter 与围栏代码块、显式 `--heading-level` 与 `--single-chapter`）以及无 DRM 加密的 EPUB 2/3（`nav.xhtml` / `toc.ncx` / Spine 回退、`<ruby>` 剥注音、CJK 换行合并、资源与正文解压上限保护）。
- **探测、对比与带预算阅读**：支持 `import --preview` 入库前结构体检、`overview --toc/--inspect`、`find`（字面词、布尔组合、`--near` 段落窗口、多组 `--query`、命中章分页、`--hit-chapters`、`--spread` 等距取例与别称增量对比）、`compare`（G2 对照差异筛选、`--view base` 与 `--explain` 复核）、`read`（整章/区间/分组并读与 `--from-result` 精确续读）。
- **收藏、材料索引与稿件核验**：支持书库跨任务收藏 `collect add/list/remove`、基于实际交付区间生成 `材料索引.md` 的 `research index`，以及校验 Markdown 笔记引用的 `resume`。
- **随包指引与跨平台构建**：内置 7 个 `span help <topic>` 离线帮助专题与发行版 `AGENTS.md`，提供干净 staging 构建与一致性校验脚本 `scripts/build_release.py`。

### 边界说明

- 保持对原书文件只读，不内置 DRM 移除、PDF / DOCX / MOBI / AZW3 解析或自动正文清洗。
- 面向跨平台 Python 3.10+ 运行环境发布源码包、`.whl` 与跨平台发行 ZIP。
