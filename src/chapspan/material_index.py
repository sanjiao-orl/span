"""Rebuild a physical delivery and draft-citation index for one run."""

from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .documents import (
    check_draft,
    document_scope,
    draft_headings,
    markdown_prose,
    prose_citations,
    read_draft,
    resolve_conventions,
)
from .handoff import load_result, result_fragments, result_reading
from .model import CONVENTION_LABELS, SnapshotMismatch, Span, integer, source_span
from .reading import SELECTION_ORIGIN_LABELS, selection_origins
from .runs import atomic_write, ensure_scope_within, parse_scope, run_library, utc_now


ORIGIN_LABELS = {
    "find_examples": "检索例段",
    "compare_examples": "区间对比例段",
    "compare": "区间对比",
    "read": "指定读取",
    "continuation": "续读",
    "result_expansion": "结果扩读",
    "sample": "章首抽读",
    "unknown": "来源不明",
    **SELECTION_ORIGIN_LABELS,
}
COVERAGE_LABELS = {"covered": "交付覆盖", "partial": "部分覆盖", "none": "无交付记录"}
STATUS_LABELS = {
    "ok": "坐标有效",
    "malformed": "格式错误",
    "snapshot_mismatch": "快照不符",
    "unavailable": "当前不可读",
    "invalid_range": "区间无效",
    "beyond_draft_horizon": "超稿件进度",
}
SECTION_PAGE = 24
SECTION_LABEL_CHARS = 160


def merge_spans(spans):
    merged = []
    for span in sorted(set(spans)):
        if merged and merged[-1].chapter == span.chapter and merged[-1].end >= span.start:
            previous = merged[-1]
            merged[-1] = Span(previous.chapter, previous.start, max(previous.end, span.end))
        else:
            merged.append(span)
    return merged


def span_coverage(span, delivered):
    overlap = sum(max(0, min(span.end, item.end) - max(span.start, item.start))
                  for item in delivered if item.chapter == span.chapter)
    total = span.end - span.start
    return "covered" if overlap == total else "partial" if overlap else "none"


def scope_contains(scope, span):
    return scope.first_chapter <= span.chapter <= scope.as_of


def collect_delivery(run, current_scope, records):
    by_chapter = {}
    titles = {}
    origins = {}
    ignored = Counter()
    registered = set()
    successful_results = 0
    for record in records:
        result_path = record.get("result_path")
        if result_path is None:
            continue
        registered.add(result_path)
        if record.get("event") != "call" or record.get("status") != "success":
            continue
        successful_results += 1
        relative = Path(result_path)
        if relative.is_absolute() or ".." in relative.parts:
            ignored["unreadable_results"] += 1
            continue
        try:
            result = load_result(str(run.root / relative))
            result_scope_value = result["scope"]
            if result_scope_value["snapshot_id"] != run.snapshot_id:
                ignored["other_snapshot_results"] += 1
                continue
            result_scope = parse_scope(result_scope_value)
            if (result_scope.as_of > current_scope.as_of
                    or result_scope.first_chapter < current_scope.first_chapter
                    or result_scope.include_peripheral and not current_scope.include_peripheral):
                ignored["out_of_scope_results"] += 1
                continue
            reading = result_reading(result)
            if not isinstance(reading, dict) or not ({"fragments", "groups"} & reading.keys()):
                continue
            fragments = result_fragments(result)
            local = {}
            local_selections = {}
            for fragment in fragments:
                span = source_span(fragment["read_span"], result_scope.snapshot_id)
                if not scope_contains(current_scope, span):
                    ignored["out_of_scope_fragments"] += 1
                    continue
                local.setdefault(span.chapter, []).append(span)
                local_selections.setdefault(span.chapter, set()).update(
                    selection_origins(fragment.get("selection_origins"))
                )
                titles.setdefault(span.chapter, fragment.get("chapter_title") or f"C{span.chapter:04d}")
            origin = record.get("origin") or "unknown"
            for chapter, spans in local.items():
                by_chapter.setdefault(chapter, []).extend(spans)
                origins.setdefault(chapter, Counter())[origin] += 1
                origins[chapter].update(local_selections[chapter])
        except (OSError, ValueError, KeyError, TypeError):
            ignored["unreadable_results"] += 1
    result_files = {path.relative_to(run.root).as_posix() for path in (run.root / "results").glob("*.json")}
    ignored["unregistered_results"] = len(result_files - registered)
    merged = {chapter: merge_spans(spans) for chapter, spans in by_chapter.items()}
    return merged, titles, origins, ignored, successful_results


def collect_citations(run, current_scope, delivered, titles, document, section, include_inline):
    scope = document_scope(document, current_scope.as_of, current_scope.first_chapter,
                           current_scope.include_peripheral)
    ensure_scope_within(scope, current_scope)
    first_line = document["markdown_start_line"]
    prose = markdown_prose(document["markdown"])
    headings = draft_headings(prose, first_line)
    skipped_inline = []
    occurrences = list(prose_citations(prose, headings, first_line, include_inline=include_inline,
                                       skipped_inline=skipped_inline))
    selected = None
    if section is not None:
        for index, (line, path) in enumerate(headings):
            if line == section:
                end = next((line for line, later in headings[index + 1:] if len(later) <= len(path)), None)
                selected = {"line": line, "heading_path": path}
                break
        if selected is None:
            raise ValueError(f"稿件第 {section} 行不是 ATX 标题；请使用磁盘稿件的 # 标题行号")
    with run_library(run, scope) as library:
        checked = check_draft(library, document, scope,
                              tokens=(row["citation"] for row in occurrences),
                              include_markdown=False, include_inline=include_inline)
        statuses = {row["citation"]: row["status"] for row in checked["citations"]}
        if selected is not None:
            occurrences = [row for row in occurrences if row["line"] >= section
                           and (end is None or row["line"] < end)]
            skipped_inline = [row for row in skipped_inline if row["line"] >= section
                              and (end is None or row["line"] < end)]
        citations = []
        for occurrence in occurrences:
            token = occurrence["citation"]
            status = statuses[token]
            span = None
            coverage = None
            try:
                span = source_span(token, scope.snapshot_id)
                coverage = span_coverage(span, delivered.get(span.chapter, []))
                if span.chapter not in titles:
                    try:
                        titles[span.chapter] = library.chapter(span.chapter).display_title
                    except ValueError:
                        pass
            except SnapshotMismatch:
                pass
            except ValueError:
                pass
            citations.append({**occurrence, "status": status, "span": span, "coverage": coverage})
        return citations, selected, len(skipped_inline)


def markdown_cell(value):
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", "<br>")


def spans_text(spans):
    return "、".join(f"[@{span.coordinate}]" for span in spans) if spans else "—"


def origins_text(counter):
    if not counter:
        return "—"
    calls, selections = [], []
    for origin, count in sorted(counter.items()):
        target = selections if origin in SELECTION_ORIGIN_LABELS else calls
        target.append(f"{ORIGIN_LABELS.get(origin, origin)} {count} 次")
    if selections:
        calls.append("选材：" + "、".join(selections))
    return " · ".join(calls)


def coverage_counts(citations):
    return Counter(row["coverage"] for row in citations if row["coverage"] is not None)


def coverage_text(counts):
    return " · ".join(f"{COVERAGE_LABELS[key]} {counts[key]}" for key in COVERAGE_LABELS if counts[key]) or "—"


def _convention_lines(conventions):
    effective = conventions["effective"]
    if not effective:
        return ["- 研究约定：未声明（仅记录口径，不影响范围与交付统计）。"]
    sources = conventions["sources"]
    conflicts = {row["field"]: row for row in conventions["conflicts"]}
    source_labels = {"draft": "稿头", "run": "运行"}
    rendered = []
    for field, value in effective.items():
        label = CONVENTION_LABELS.get(field, field)
        text = markdown_cell(value)
        if field in conflicts:
            run_text = markdown_cell(conflicts[field]["run"])
            rendered.append(f"- {label}（`{field}`，稿头优先，与运行不同）：{text}（运行原设：{run_text}）")
        else:
            rendered.append(f"- {label}（`{field}` · 来源：{source_labels[sources[field]]}）：{text}")
    return rendered


def render_index(run, current_scope, delivered, titles, origins, citations, draft_path,
                 draft_horizon, ignored, successful_results, include_inline, skipped_inline=0,
                 conventions=None):
    chapters = sorted(set(delivered) | {row["span"].chapter for row in citations if row["span"] is not None})
    counts = coverage_counts(citations)
    cited_by_chapter = {}
    for row in citations:
        if row["span"] is not None:
            cited_by_chapter.setdefault(row["span"].chapter, []).append(row)
    delivered_characters = sum(span.end - span.start for spans in delivered.values() for span in spans)
    scope_text = ("空范围（末章 0）" if current_scope.as_of == 0
                  else f"C{current_scope.first_chapter:04d}–C{current_scope.as_of:04d}")
    scope_text += " · 纳入外围章" if current_scope.include_peripheral else " · 默认过滤外围章"
    lines = [
        "# 材料索引",
        "",
        f"- 运行：`{run.run_id}`",
        f"- 快照：`{run.snapshot_id}`",
        f"- 运行当前范围：{scope_text}",
        f"- 生成时间：{utc_now()}",
        f"- 当前稿：{markdown_cell(draft_path) if draft_path is not None else '未创建'}"
        + (f"（形成进度 C{draft_horizon:04d}）" if draft_path is not None else ""),
        "- 口径：程序交付与稿内引用的物理对照；不表示宿主展示、实际阅读或论述成立。",
        "",
        "## 研究约定",
        "",
        *_convention_lines(conventions if conventions is not None else resolve_conventions(run.conventions, None)),
        "",
        "## 汇总",
        "",
        f"- 成功结果记录：{successful_results}；有交付或引用的章：{len(chapters)}。",
        f"- 本运行交付去重：{delivered_characters} 字符；稿内引用出现：{len(citations)} 次。",
        f"- 引用覆盖：{coverage_text(counts)}。",
        f"- 未展开的无交付／引用章：{max(0, current_scope.as_of - current_scope.first_chapter + 1 - len(chapters))}。",
    ]
    if draft_path is not None and skipped_inline:
        lines.append(
            f"- 默认跳过行内纯引用坐标：{skipped_inline} 处"
            "（仅统计单独包在反引号内的引用形态，不含普通代码与围栏代码块；纳入请加 `--include-inline-citations`）。"
        )
    ignored_text = " · ".join(f"{label} {ignored[key]}" for key, label in (
        ("unreadable_results", "不可读结果"), ("other_snapshot_results", "其他快照结果"),
        ("out_of_scope_results", "超范围结果"), ("out_of_scope_fragments", "超范围片段"),
        ("unregistered_results", "未登记结果")) if ignored[key])
    if ignored_text:
        lines.append(f"- 未计入：{ignored_text}；可按 calls.jsonl 的 result_path 定点核查。")
    lines.extend(["", "## 按原文章序", "",
                  "| 章 | 章题 | 本运行交付 | 稿内引用（出现次数） | 交付对照 | 来源 | 定点回读 |",
                  "|---|---|---|---|---|---|---|"])
    if not chapters:
        lines.append("| — | — | — | — | — | — | — |")
    for chapter in chapters:
        rows = cited_by_chapter.get(chapter, [])
        cited_spans = merge_spans([row["span"] for row in rows])
        citation_cell = spans_text(cited_spans)
        if rows:
            citation_cell += f"（{len(rows)} 次）"
        row_counts = coverage_counts(rows)
        title = titles.get(chapter)
        if chapter < current_scope.first_chapter or chapter > current_scope.as_of:
            read_action = f"不在当前运行范围（C{current_scope.first_chapter:04d}–C{current_scope.as_of:04d}）"
        elif chapter not in delivered and title is None:
            read_action = f"外围过滤或缺失（外围可试 --include-peripheral read --chapter C{chapter:04d}）"
        else:
            read_action = f"`read --chapter C{chapter:04d}`"
        values = [f"C{chapter:04d}", title or "—", spans_text(delivered.get(chapter, [])),
                  citation_cell, coverage_text(row_counts), origins_text(origins.get(chapter, Counter())),
                  read_action]
        lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
    inline_rule = "纳入单独包裹坐标的行内代码" if include_inline else "跳过行内代码"
    lines.extend(["", "## 按当前稿标题", "",
                  f"标题口径：ATX `#` 标题；文首单独分组；{inline_rule}；跳过顶层围栏代码块。", ""])
    if draft_path is None:
        lines.append("当前稿未创建，仅显示上方的程序交付。")
    elif not citations:
        if skipped_inline:
            lines.append(
                f"当前稿没有可识别的正文引用（另有 {skipped_inline} 处单独包在反引号内的纯引用坐标被默认跳过；"
                "加 `--include-inline-citations` 可纳入）。"
            )
        else:
            lines.append("当前稿没有可识别的引用。")
    else:
        sections = {}
        for row in citations:
            key = row["heading_line"], row["heading_path"]
            sections.setdefault(key, []).append(row)
        for (heading_line, path), rows in sections.items():
            title = "文首（首个 ATX 标题之前）" if heading_line is None else " / ".join(path)
            suffix = "" if heading_line is None else f"（标题行 {heading_line}）"
            lines.extend([f"### {title}{suffix}", "",
                          "| 稿件行 | 引用 | 坐标状态 | 交付对照 |",
                          "|---:|---|---|---|"])
            for row in rows:
                coverage = COVERAGE_LABELS.get(row["coverage"], "—")
                values = [row["line"], row["citation"], STATUS_LABELS.get(row["status"], row["status"]), coverage]
                lines.append("| " + " | ".join(markdown_cell(value) for value in values) + " |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def section_result(selected, citations, offset, *, skipped_inline=0):
    page = citations[offset:offset + SECTION_PAGE]
    rows = []
    for row in page:
        token = row["citation"]
        item = {"line": row["line"], "citation": token,
                "status": row["status"], "coverage": row["coverage"]}
        if len(token) > SECTION_LABEL_CHARS:
            item["citation"] = token[:SECTION_LABEL_CHARS - 1] + "…"
            item["citation_truncated"] = True
        rows.append(item)
    stop = offset + len(page)
    headings = [title[:SECTION_LABEL_CHARS - 1] + "…" if len(title) > SECTION_LABEL_CHARS else title
                for title in selected["heading_path"]]
    result = {"section": {**selected, "heading_path": headings}, "citation_count": len(citations),
              "skipped_inline_citations": skipped_inline, "citations": rows,
              "read_spans": list(dict.fromkeys(row["span"].coordinate for row in page if row["status"] == "ok")),
              "next_offset": stop if stop < len(citations) else None}
    if skipped_inline:
        result["note"] = (
            f"本节已跳过 {skipped_inline} 处单独包在反引号内的纯引用坐标（普通行内代码与围栏代码块不计入）；"
            "如需纳入索引请加 --include-inline-citations"
        )
    return result


def generate_material_index(run, current_scope, *, draft=None, draft_horizon=None, section=None, offset=0,
                            include_inline=False):
    path = run.root / "材料索引.md"
    draft_path = (run.root / "当前稿.md" if draft is None else draft).resolve()
    if section is not None:
        integer(section, "稿件标题行号", 1)
        integer(offset, "引用偏移")
    elif draft_path == path.resolve():
        raise ValueError("稿件不能与生成的材料索引为同一文件")
    try:
        document = read_draft(draft_path, run_scope=current_scope, draft_horizon=draft_horizon)
    except FileNotFoundError:
        if draft is not None or draft_horizon is not None or section is not None:
            raise ValueError(f"稿件不存在：{draft_path}") from None
        document = None
    records = run.records()
    delivered, titles, origins, ignored, successful_results = collect_delivery(run, current_scope, records)
    citations, selected, skipped_inline = (
        collect_citations(run, current_scope, delivered, titles, document, section, include_inline)
        if document is not None else ([], None, 0)
    )
    ignored_counts = {key: ignored[key] for key in sorted(ignored) if ignored[key]}
    conventions = resolve_conventions(run.conventions, document["conventions"] if document is not None else None)
    if selected is not None:
        return {"run_id": run.run_id, "scope": asdict(current_scope), "draft": str(draft_path),
                "knowledge_horizon": document["knowledge_horizon"], "conventions": conventions,
                "ignored": ignored_counts,
                **section_result(selected, citations, offset, skipped_inline=skipped_inline)}
    content = render_index(run, current_scope, delivered, titles, origins, citations,
                           draft_path if document is not None else None,
                           document["knowledge_horizon"] if document is not None else None,
                           ignored, successful_results, include_inline, skipped_inline=skipped_inline,
                           conventions=conventions)
    atomic_write(path, content.encode("utf-8"), overwrite=True)
    counts = coverage_counts(citations)
    note = "索引已原子重建；当前稿、研究笔记和原始结果未修改"
    if skipped_inline:
        note += f"；另默认跳过 {skipped_inline} 处单独包在反引号内的纯引用坐标（纳入请加 --include-inline-citations）"
    return {
        "run_id": run.run_id,
        "index": str(path),
        "scope": asdict(current_scope),
        "conventions": conventions,
        "chapters": len(set(delivered) | {row["span"].chapter for row in citations if row["span"] is not None}),
        "delivery_characters": sum(span.end - span.start for spans in delivered.values() for span in spans),
        "citations": len(citations),
        "skipped_inline_citations": skipped_inline,
        "coverage": {key: counts[key] for key in COVERAGE_LABELS if counts[key]},
        "current_draft": document is not None,
        "draft": str(draft_path) if document is not None else None,
        "ignored": ignored_counts,
        "note": note,
    }
