"""Read a Markdown draft and check its source coordinates."""

import re
from bisect import bisect_right
from pathlib import Path

import yaml

from .model import (
    CONVENTION_FIELDS,
    CONVENTION_LABELS,
    MAX_CONVENTION_CHARS,
    Scope,
    SnapshotMismatch,
    integer,
    source_span,
)


FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n|\Z)", re.MULTILINE | re.DOTALL)
FRONTMATTER_START = re.compile(r"\A---[ \t]*(?:\n|\Z)")
MAX_DRAFT_CHARS = 512_000
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
INLINE = re.compile(
    r"(?<![`\\])(?P<ticks>`+)(?!`)(?P<code>.*?)(?<!`)(?P=ticks)(?!`)|(?<!\\)(?P<citation>\[@)",
    re.DOTALL,
)
INLINE_CITATION = re.compile(r"\[@(?:snap_[0-9a-f]{64}/)?C[0-9]+:[0-9]+-[0-9]+\]")
INLINE_CITATIONS = re.compile(rf"\s*{INLINE_CITATION.pattern}(?:\s+{INLINE_CITATION.pattern})*\s*")
UNCLOSED_CITATION_PREFIX = re.compile(r"(?:C[0-9]|snap_)")
MAX_UNCLOSED_CITATION_CHARS = 80
ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*)|[ \t]*)$")
BLOCK_SEPARATOR = re.compile(r"\n[ \t]*\n")


def markdown_prose(markdown):
    prose = []
    fence = None
    for line in markdown.split("\n"):
        marker = FENCE.fullmatch(line)
        if fence is not None:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = None
            prose.append("")
        elif marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
            prose.append("")
        else:
            prose.append(line)
    return "\n".join(prose)


def draft_headings(prose, first_line):
    headings = []
    stack = []
    for line_number, line in enumerate(prose.split("\n"), first_line):
        match = ATX.fullmatch(line)
        if match is None:
            continue
        level = len(match[1])
        title = re.sub(r"[ \t]+#+[ \t]*$", "", match[2] or "").strip() or "（空标题）"
        stack = [row for row in stack if row[0] < level]
        stack.append((level, title))
        headings.append((line_number, tuple(title for _, title in stack)))
    return headings


def inline_citations(block, include_inline, *, skipped=None):
    position = closing = newline = 0
    while (match := INLINE.search(block, position)) is not None:
        position = match.end()
        if match["citation"] is not None:
            # Reuse the next delimiters when several openers occur on an unfinished line.
            if closing < position:
                closing = block.find("]", position)
                if closing == -1:
                    closing = len(block)
            if newline < position:
                newline = block.find("\n", position)
                if newline == -1:
                    newline = len(block)
            if closing < newline:
                position = closing + 1
                yield match.start(), block[match.start():position]
            elif UNCLOSED_CITATION_PREFIX.match(block, position, newline):
                stop = min(newline, match.start() + MAX_UNCLOSED_CITATION_CHARS)
                position = newline
                yield match.start(), block[match.start():stop].rstrip("\r")
        elif (include_inline or skipped is not None) and "[@" in match["code"] and INLINE_CITATIONS.fullmatch(match["code"]):
            for token in INLINE_CITATION.finditer(match["code"]):
                if include_inline:
                    yield match.start("code") + token.start(), token[0]
                else:
                    skipped.append((match.start("code") + token.start(), token[0]))


def prose_citations(prose, headings, first_line, *, include_inline=False, skipped_inline=None):
    heading_lines = [row[0] for row in headings]
    block_line = first_line
    for block in re.split(r"(\n[ \t]*\n)", prose):
        if not BLOCK_SEPARATOR.fullmatch(block):
            position, line = 0, block_line
            skipped = [] if (skipped_inline is not None and not include_inline) else None
            for start, token in inline_citations(block, include_inline, skipped=skipped):
                line += block.count("\n", position, start)
                position = start
                index = bisect_right(heading_lines, line) - 1
                heading_line, path = headings[index] if index >= 0 else (None, ())
                yield {"citation": token, "line": line, "heading_line": heading_line, "heading_path": path}
            if skipped:
                for start, token in skipped:
                    skipped_inline.append({"citation": token, "line": block_line + block.count("\n", 0, start)})
        block_line += block.count("\n")


def citation_occurrences(markdown, *, first_line=1, include_inline=False, skipped_inline=None):
    prose = markdown_prose(markdown)
    yield from prose_citations(prose, draft_headings(prose, first_line), first_line,
                               include_inline=include_inline, skipped_inline=skipped_inline)


def citation_tokens(markdown, *, include_inline=False, skipped_inline=None):
    for occurrence in citation_occurrences(markdown, include_inline=include_inline, skipped_inline=skipped_inline):
        yield occurrence["citation"]


def normalize_conventions(raw=None, *, allow_unknown=False):
    """Validate and normalize optional research convention text fields."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("研究约定须为键值对对象")
    if not allow_unknown:
        unknown = [key for key in raw if key not in CONVENTION_FIELDS]
        if unknown:
            raise ValueError(f"包含未知的研究约定字段：{', '.join(map(str, unknown))}")
    normalized = {}
    for field in CONVENTION_FIELDS:
        if field not in raw or raw[field] is None:
            continue
        value = raw[field]
        label = CONVENTION_LABELS[field]
        if type(value) is not str:
            raise ValueError(f"研究约定 {field}（{label}）须为文本（如含特殊格式请加引号）")
        cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not cleaned:
            raise ValueError(f"研究约定 {field}（{label}）不能为空白文本")
        if len(cleaned) > MAX_CONVENTION_CHARS:
            raise ValueError(f"研究约定 {field}（{label}）超过 {MAX_CONVENTION_CHARS} 字符上限")
        normalized[field] = cleaned
    return normalized


def resolve_conventions(run_conventions=None, draft_conventions=None):
    """Merge run-level and draft-level conventions with draft precedence and conflict reporting."""
    run_map = normalize_conventions(run_conventions)
    draft_map = normalize_conventions(draft_conventions)
    effective = {}
    sources = {}
    conflicts = []
    for field in CONVENTION_FIELDS:
        if field in draft_map:
            effective[field] = draft_map[field]
            sources[field] = "draft"
            if field in run_map and run_map[field] != draft_map[field]:
                conflicts.append({"field": field, "draft": draft_map[field], "run": run_map[field]})
        elif field in run_map:
            effective[field] = run_map[field]
            sources[field] = "run"
    return {"effective": effective, "sources": sources, "conflicts": conflicts}


def draft_header(snapshot_id, knowledge_horizon, *, collection_id=None, conventions=None):
    header = {"snapshot_id": snapshot_id, "knowledge_horizon": knowledge_horizon}
    if collection_id is not None:
        header["collection_id"] = collection_id
    header.update(normalize_conventions(conventions))
    dumped = yaml.safe_dump(
        header,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    return f"---\n{dumped.rstrip(chr(10))}\n---\n"


def read_draft(draft, *, run_scope=None, draft_horizon=None):
    with Path(draft).open(encoding="utf-8-sig") as file:
        text = file.read(MAX_DRAFT_CHARS + 1)
    if len(text) > MAX_DRAFT_CHARS:
        raise ValueError(f"Markdown 稿件超过 {MAX_DRAFT_CHARS} 字符上限，请拆分稿件")
    header = FRONTMATTER.match(text) if FRONTMATTER_START.match(text) else None
    if header is None:
        if FRONTMATTER_START.match(text):
            raise ValueError("稿件开头的 YAML 文件头缺少结束分隔线 ---")
        if run_scope is None or draft_horizon is None:
            raise ValueError("稿件开头须有 YAML 文件头；无稿头的 Markdown 可绑定原运行并显式指定 --draft-horizon")
        return {"snapshot_id": run_scope.snapshot_id,
                "knowledge_horizon": integer(draft_horizon, "稿件形成进度 --draft-horizon"),
                "collection_id": None, "conventions": {},
                "markdown": text, "markdown_start_line": 1}
    if draft_horizon is not None:
        raise ValueError("稿件已有 YAML 文件头，请使用其中的形成进度，去掉 --draft-horizon")
    try:
        metadata = yaml.safe_load(header[1])
    except yaml.YAMLError as exc:
        raise ValueError("稿件 YAML 文件头格式错误") from exc
    if not isinstance(metadata, dict):
        raise ValueError("稿件文件头须声明 snapshot_id 和 knowledge_horizon")
    snapshot = metadata.get("snapshot_id")
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ValueError("稿件文件头须声明有效的 snapshot_id")
    horizon = integer(metadata.get("knowledge_horizon"), "稿件形成进度 knowledge_horizon")
    collection = metadata.get("collection_id")
    if collection is not None and (not isinstance(collection, str) or not collection.strip() or len(collection) > 120):
        raise ValueError("collection_id 须为有效的收藏集名称")
    conventions = normalize_conventions(metadata, allow_unknown=True)
    return {"snapshot_id": snapshot, "knowledge_horizon": horizon, "collection_id": collection,
            "conventions": conventions,
            "markdown": text[header.end():], "markdown_start_line": text[:header.end()].count("\n") + 1}


def document_scope(document, as_of, first_chapter=1, include_peripheral=False):
    scope = Scope(document["snapshot_id"], as_of, first_chapter, include_peripheral)
    if document["knowledge_horizon"] > scope.as_of:
        raise ValueError("稿件形成进度超过当前阅读上限，无法恢复")
    return scope


def _ahead_of_scope_error(document, token, scope, *, include_inline=False):
    first_line = document.get("markdown_start_line", 1)
    line = next(
        (row["line"] for row in citation_occurrences(document["markdown"], first_line=first_line,
                                                     include_inline=include_inline)
         if row["citation"] == token),
        None,
    )
    loc = f"第 {line} 行 {token}" if line is not None else token
    return ValueError(f"稿中有引用超过当前阅读上限（{loc}，上限 C{scope.as_of:04d}），无法恢复")


def check_draft(library, document, scope, *, tokens=None, include_markdown=True, include_inline=False):
    horizon = document["knowledge_horizon"]
    checks = []
    chapters = {}
    skipped = []
    first_lines = {}
    if tokens is None:
        occurrences = list(citation_occurrences(
            document["markdown"],
            first_line=document.get("markdown_start_line", 1),
            include_inline=include_inline,
            skipped_inline=skipped,
        ))
        tokens = []
        for row in occurrences:
            token = row["citation"]
            tokens.append(token)
            first_lines.setdefault(token, row["line"])
    for token in dict.fromkeys(tokens):
        try:
            span = source_span(token, scope.snapshot_id)
        except SnapshotMismatch as exc:
            if exc.span.chapter > scope.as_of:
                raise _ahead_of_scope_error(document, token, scope, include_inline=include_inline) from exc
            checks.append({"citation": token, "status": "snapshot_mismatch"})
            continue
        except ValueError:
            entry = {"citation": token, "status": "malformed"}
            if token.startswith("[@") and not token.endswith("]"):
                line = first_lines.get(token)
                entry["note"] = (
                    f"第 {line} 行引用缺少右方括号 ]" if line is not None else "引用缺少右方括号 ]"
                )
            checks.append(entry)
            continue
        if span.chapter > scope.as_of:
            raise _ahead_of_scope_error(document, token, scope, include_inline=include_inline)
        if span.chapter not in chapters:
            try:
                chapters[span.chapter] = library.chapter(span.chapter)
            except ValueError:
                chapters[span.chapter] = None
        chapter = chapters[span.chapter]
        note = None
        if chapter is None:
            status = "unavailable"
            note = ("章节低于当前起始范围 --from-chapter" if span.chapter < scope.first_chapter
                    else "章节不存在或属于默认过滤的外围文字（可加 --include-peripheral 核查）")
        elif span.end > len(chapter.body):
            status = "invalid_range"
        elif span.chapter > horizon:
            status = "beyond_draft_horizon"
        else:
            status = "ok"
        entry = {"citation": token, "status": status}
        if note is not None:
            entry["note"] = note
        checks.append(entry)
    skipped_count = len(skipped)
    result = {
        "knowledge_horizon": horizon,
        "collection_id": document["collection_id"],
        "citation_count": len(checks),
        "skipped_inline_citations": skipped_count,
        "citations": checks,
    }
    if skipped_count:
        result["note"] = (
            f"已检查 {len(checks)} 条去重引用；默认跳过 {skipped_count} 处单独包在反引号内的纯引用坐标"
            "（普通行内代码与围栏代码块不计入），如需纳入校验请加 --include-inline-citations"
        )
    if include_markdown:
        result["markdown"] = document["markdown"]
    return result
