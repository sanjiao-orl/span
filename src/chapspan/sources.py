"""TXT, Markdown, and EPUB parsing, boundary resolution, structural diagnostics and scoped in-memory reading."""

import codecs
import hashlib
import json
import re
from bisect import bisect_left
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .epub import Utf16DecodingError, parse_epub_bytes
from .model import MAX_PAGE, Chapter, Scope, display_title, integer


HEADING_NUMBER = r"[0-9０-９〇○零一二三四五六七八九十百千万萬两兩壹贰貳叁參拾佰仟]+"
NUMBERED_HEADING = rf"第{HEADING_NUMBER}[章节節回]"
PRINTED_HEADING = re.compile(
    rf"第(?P<number>{HEADING_NUMBER})[章节節回]|(?i:chapter)\s+(?P<arabic>[0-9]+)"
)
CHINESE_DIGITS = {char: value for value, chars in enumerate(
    ("〇○零", "一壹", "二两兩贰貳", "三叁參", "四", "五", "六", "七", "八", "九")
) for char in chars}
CHINESE_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
TITLE_SEPARATORS = r"\s:：.．、，·—-"
SHORT_TITLE = r"(?=.{1,50}\Z)"
DEFAULT_HEADING = rf"{SHORT_TITLE}(?:{NUMBERED_HEADING}|(?i:chapter)\s+[0-9]+)(?:[{TITLE_SEPARATORS}].*)?"
TIGHT_CHAPTER_PATTERN = (
    rf"{SHORT_TITLE}(?:{NUMBERED_HEADING}|(?i:chapter)\s+[0-9]+).*"
)
TIGHT_HEADING = re.compile(rf"{SHORT_TITLE}{NUMBERED_HEADING}[^{TITLE_SEPARATORS}].*")
DEFAULT_HEADING_RE = re.compile(DEFAULT_HEADING)
PREVIEW_EXAMPLES = 6
PREVIEW_LINE_CHARS = 120
TOC_MAX_PAGE = 2000
TOC_MAX_BYTES = 400_000
TXT_SUFFIXES = frozenset(("", ".txt", ".text"))
MARKDOWN_SUFFIXES = frozenset((".md", ".markdown"))
EPUB_SUFFIXES = frozenset((".epub",))
YAML_FRONTMATTER_FENCE = re.compile(r"---[ \t]*")
MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
MARKDOWN_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
PERIPHERAL_CATEGORIES = (
    ("author_note", re.compile(r"(?:作者有话说|作者的话)(?:[\s:：].*)?")),
    ("launch_note", re.compile(r"上架感言(?:[\s:：].*)?")),
    ("leave_notice", re.compile(r"请假条(?:[\s:：].*)?")),
    ("ending_note", re.compile(r"完本感言(?:[\s:：].*)?")),
    ("afterword", re.compile(r"后记(?:[\s:：].*)?")),
)
PERIPHERAL = re.compile(r"(?:作者有话说|作者的话|上架感言|请假条|完本感言|后记)(?:[\s:：].*)?")
UNCLASSIFIED = re.compile(r"(?:楔子|序章|番外)(?:[\s:：].*)?")


def detect_source_format(source):
    suffix = Path(source).suffix.casefold()
    if suffix in TXT_SUFFIXES:
        return "txt"
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in EPUB_SUFFIXES:
        return "epub"
    if suffix in (".mobi", ".azw3"):
        raise ValueError(
            f"不支持直接读取 {suffix} 电子书格式；请先用 Calibre（ebook-convert 书{suffix} 书.epub 或 书.txt）"
            "转换为 EPUB 或 UTF-8 TXT 后再用 import --preview 体检"
        )
    if suffix in (".docx", ".doc"):
        raise ValueError(
            f"不支持直接读取 {suffix} 文档格式；请先用 pandoc（pandoc 书{suffix} -t gfm --wrap=none -o 书.md）"
            "转换为 Markdown 后再接入 Span"
        )
    if suffix == ".pdf":
        raise ValueError(
            "不支持直接读取 .pdf 文件（PDF 版面、页眉页脚与分栏断行容易破坏章界和字面检索，不推荐直接用于精读）；"
            "如确需处理，请先经外部工具提取并人工核对为 UTF-8 TXT 或 Markdown"
        )
    raise ValueError(
        f"不支持的源文件后缀 {suffix}；当前支持 TXT（.txt / .text / 无后缀）、Markdown（.md / .markdown）和 EPUB（.epub），"
        "其他格式请先用外部工具（如 Calibre 或 pandoc）转换"
    )


def validate_text_options(
    *,
    chapter_pattern=None,
    heading_level=None,
    single_chapter=False,
    encoding=None,
    source_format=None,
):
    if type(single_chapter) is not bool:
        raise ValueError("--single-chapter 须为布尔值")
    if heading_level is not None:
        integer(heading_level, "Markdown 标题层级 --heading-level", 1, 6)
    if single_chapter and chapter_pattern is not None:
        raise ValueError("--single-chapter 不能与 --chapter-pattern 同时使用")
    if single_chapter and heading_level is not None:
        raise ValueError("--single-chapter 不能与 --heading-level 同时使用")
    if heading_level is not None and chapter_pattern is not None:
        raise ValueError("--heading-level 不能与 --chapter-pattern 同时使用")
    if source_format == "txt" and heading_level is not None:
        raise ValueError("--heading-level 仅用于 Markdown 文件（.md / .markdown）")
    if source_format == "epub":
        if heading_level is not None:
            raise ValueError("--heading-level 仅用于 Markdown 文件（.md / .markdown）；EPUB 使用容器目录与结构分章")
        if encoding is not None:
            raise ValueError("--encoding 不适用于 EPUB 文件；EPUB 使用容器内声明的编码与结构")


def _leading_frontmatter_end(lines):
    if not lines or not YAML_FRONTMATTER_FENCE.fullmatch(lines[0]):
        return 0
    for index in range(1, len(lines)):
        if YAML_FRONTMATTER_FENCE.fullmatch(lines[index]):
            return index + 1
    return 0


def markdown_atx_headings(lines):
    start = _leading_frontmatter_end(lines)
    headings = []
    fence = None
    for index in range(start, len(lines)):
        line = lines[index]
        marker = MARKDOWN_FENCE.fullmatch(line)
        if fence is not None:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = None
            continue
        if marker and (marker[1][0] == "~" or "`" not in marker[2]):
            fence = marker[1]
            continue
        match = MARKDOWN_ATX_HEADING.fullmatch(line)
        if match is None:
            continue
        level = len(match.group(1))
        title = re.sub(r"(?:^|[ \t]+)#+$", "", match.group(2)).strip()
        if title:
            headings.append((index, level, title))
    return headings


def _classify_markdown_sections(headings, target_level):
    sections = []
    for index, level, title in headings:
        if level != target_level:
            continue
        if PERIPHERAL.fullmatch(title):
            sections.append((index, title, "peripheral"))
        elif UNCLASSIFIED.fullmatch(title):
            sections.append((index, title, "unclassified"))
        else:
            sections.append((index, title, "body"))
    return sections


def _format_level_counts(level_counts):
    return "、".join(f"{lvl} 级（{'#' * int(lvl)}）{cnt} 个" for lvl, cnt in level_counts.items())


def _single_chapter_title(source, lines, source_format, md_headings=None):
    if source_format == "markdown" and md_headings:
        return md_headings[0][2]
    if source_format == "txt":
        for line in lines:
            label = line.strip()
            if label:
                if DEFAULT_HEADING_RE.fullmatch(label):
                    return label
                break
    return Path(source).stem or "单章正文"


def paragraph_ends(body, mode):
    starts = []
    offset, previous_blank = 0, True
    for line in body.split("\n"):
        blank = not line.strip()
        if not blank and (previous_blank or mode == "lines"):
            starts.append(offset)
        offset += len(line) + 1
        previous_blank = blank
    return (*starts[1:], len(body)) if starts else ()


def chapter_sections(lines, heading_pattern):
    heading = re.compile(heading_pattern)
    sections, candidates = [], []
    for index, line in enumerate(lines):
        label = line.strip()
        if not label:
            continue
        if heading.fullmatch(label):
            sections.append((index, line, "body"))
        elif PERIPHERAL.fullmatch(label):
            sections.append((index, line, "peripheral"))
        elif UNCLASSIFIED.fullmatch(label):
            sections.append((index, line, "unclassified"))
        elif TIGHT_HEADING.fullmatch(label):
            candidates.append((index, label))
    return sections, candidates


def _build_chapters(lines, sections, paragraphs):
    resolved_sections = list(sections)
    if resolved_sections[0][0] > 0 and any(line.strip() for line in lines[:resolved_sections[0][0]]):
        resolved_sections.insert(0, (-1, "章前文字", "unclassified"))

    chapters = []
    boundaries = [index for index, _, _ in resolved_sections[1:]] + [len(lines)]
    for number, ((index, title, kind), stop) in enumerate(zip(resolved_sections, boundaries), 1):
        body = "\n".join(lines[index + 1:stop])
        if stop < len(lines) and stop > index + 1:
            body += "\n"
        chapters.append(Chapter(number, title, kind, index + 2, body, paragraph_ends(body, paragraphs)))
    return chapters


def split_chapters(lines, sections, paragraphs):
    if not any(kind == "body" for _, _, kind in sections):
        raise ValueError(
            "无法识别分章标题，请先用 import-txt --preview 查看样例，再指定 --chapter-pattern"
            "（无章界单篇文稿可显式使用 --single-chapter）"
        )
    chapters = _build_chapters(lines, sections, paragraphs)
    if not any(chapter.kind == "body" and chapter.body.strip() for chapter in chapters):
        raise ValueError("识别到了章标题，但没有带正文的普通章节")
    return chapters


def _truncate_sample(text, max_chars=PREVIEW_LINE_CHARS):
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def _line_samples(lines, *, limit=PREVIEW_EXAMPLES, max_chars=PREVIEW_LINE_CHARS):
    # Keep only enough candidates for each priority, never a stripped copy of every source line.
    first, isolated, short, rest = [], [], [], []
    non_empty = 0
    for index, line in enumerate(lines):
        label = line.strip()
        if not label:
            continue
        non_empty += 1
        if non_empty <= 3:
            bucket = first
        elif len(label) <= 50 and (
            index == 0 or not lines[index - 1].strip()
            or index + 1 == len(lines) or not lines[index + 1].strip()
        ):
            bucket = isolated
        elif len(label) <= 50:
            bucket = short
        else:
            bucket = rest
        if len(bucket) < limit:
            bucket.append((index, _truncate_sample(label, max_chars)))
    selected = sorted((first + isolated + short + rest)[:limit])
    return [{"source_line": index + 1, "text": label} for index, label in selected]


def _chapter_length_stats(lines, sections, paragraphs, *, chapters=None):
    if chapters is None:
        chapters = _build_chapters(lines, sections, paragraphs)
    body_chapters = [chapter for chapter in chapters if chapter.kind == "body"]
    if not body_chapters:
        return None
    lengths = sorted(len(chapter.body) for chapter in body_chapters)
    mid = len(lengths) // 2
    median_chars = lengths[mid] if len(lengths) % 2 else (lengths[mid - 1] + lengths[mid]) // 2
    longest = sorted(body_chapters, key=lambda chapter: (-len(chapter.body), chapter.number))[:3]
    return {
        "chapter_count": len(body_chapters),
        "median_characters": median_chars,
        "max_characters": lengths[-1],
        "longest_chapters": [
            {
                "chapter": chapter.number,
                "source_line": max(1, chapter.source_body_line - 1),
                "title": display_title(chapter.title),
                "characters": len(chapter.body),
            }
            for chapter in longest
        ],
    }


def preview_chapters(
    sections,
    candidates,
    *,
    lines=None,
    paragraphs="lines",
    source_format=None,
    markdown_heading_levels=None,
    markdown_heading_examples=None,
    ambiguous_markdown=False,
    chapter_source=None,
    skipped_spine_items=None,
    image_count=None,
    chapters=None,
):
    headings = {index: display_title(title.strip()) for index, title, kind in sections if kind == "body"}
    positions = list(headings)

    def reference(index):
        return {"source_line": max(1, index + 1), "title": headings[index]}

    examples = []
    for index, label in candidates[:PREVIEW_EXAMPLES]:
        following = bisect_left(positions, index)
        examples.append({
            "source_line": index + 1,
            "title": label,
            "previous_heading": reference(positions[following - 1]) if following else None,
            "next_heading": reference(positions[following]) if following < len(positions) else None,
        })
    sample = positions[:3] + positions[max(3, len(positions) - 3):]
    result = {
        "heading_count": len(headings),
        "heading_examples": [reference(i) for i in sample],
        "tight_candidate_count": len(candidates),
        "tight_candidate_examples": examples,
    }
    if source_format is not None:
        result["format"] = source_format
    if chapter_source is not None:
        result["chapter_source"] = chapter_source
    if skipped_spine_items is not None:
        result["skipped_spine_items"] = list(skipped_spine_items)
    if image_count is not None:
        result["image_count"] = image_count
    if markdown_heading_levels is not None:
        result["markdown_heading_levels"] = markdown_heading_levels
    if ambiguous_markdown and markdown_heading_levels:
        if markdown_heading_examples is not None:
            result["markdown_heading_examples"] = markdown_heading_examples
        result["hint"] = (
            f"检测到多个 Markdown 标题层级（{_format_level_counts(markdown_heading_levels)}），"
            "不自动猜测；请使用 --heading-level N 指定分章层级"
        )
    if not headings and candidates:
        result["suggested_chapter_pattern"] = TIGHT_CHAPTER_PATTERN
    if not headings and not candidates and lines is not None:
        result["total_lines"] = len(lines)
        result["line_samples"] = _line_samples(lines)
    if headings and lines is not None:
        stats = _chapter_length_stats(lines, sections, paragraphs, chapters=chapters)
        if stats is not None:
            result["chapter_length_stats"] = stats
    return result


def _suggest_txt_encoding(raw, failed_encoding):
    candidates = ("utf-8-sig", "gb18030", "big5")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        candidates = ("utf-16",) + candidates
    for candidate in candidates:
        if candidate == failed_encoding:
            continue
        try:
            raw.decode(candidate)
            return candidate
        except UnicodeDecodeError:
            continue
    return None


def normalized_lines(raw, encoding):
    try:
        encoding = codecs.lookup(encoding).name
        decoded = raw.decode(encoding)
    except LookupError as exc:
        raise ValueError(f"无法以 {encoding} 解码源文件，请核对 --encoding（中文旧文本常见 gb18030 / gbk / big5 / utf-16）") from exc
    except UnicodeDecodeError as exc:
        suggested = _suggest_txt_encoding(raw, encoding)
        if suggested is not None:
            raise ValueError(
                f"无法以 {encoding} 解码源文件；检测到文件可按 {suggested} 解码，"
                f"请加上 --encoding {suggested}（未知分章格式建议先运行 import-txt --preview --encoding {suggested}）"
            ) from exc
        raise ValueError(f"无法以 {encoding} 解码源文件，请核对 --encoding（中文旧文本常见 gb18030 / gbk / big5 / utf-16）") from exc
    return encoding, decoded.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def parse_source(
    source,
    encoding=None,
    chapter_pattern=None,
    paragraphs="lines",
    *,
    heading_level=None,
    single_chapter=False,
    allow_ambiguous_markdown=False,
    allow_incomplete_epub=False,
    legacy_xml_decoding=False,
):
    source = Path(source)
    source_format = detect_source_format(source)
    validate_text_options(
        chapter_pattern=chapter_pattern,
        heading_level=heading_level,
        single_chapter=single_chapter,
        encoding=encoding,
        source_format=source_format,
    )
    if paragraphs not in ("lines", "blank-lines"):
        raise ValueError("分段口径须为 lines 或 blank-lines")
    raw = source.read_bytes()

    if source_format == "epub":
        epub_data = parse_epub_bytes(
            raw,
            source_name=source.name,
            chapter_pattern=chapter_pattern,
            paragraphs=paragraphs,
            single_chapter=single_chapter,
            allow_incomplete_spine=allow_incomplete_epub,
            legacy_xml_decoding=legacy_xml_decoding,
            peripheral_re=PERIPHERAL,
            unclassified_re=UNCLASSIFIED,
            chapter_sections_fn=chapter_sections,
        )
        return {
            "raw": raw,
            "encoding": "utf-8",
            "source_format": source_format,
            "parsing": epub_data["parsing"],
            "lines": epub_data["lines"],
            "sections": epub_data["sections"],
            "candidates": epub_data["candidates"],
            "markdown_heading_levels": None,
            "markdown_heading_examples": None,
            "ambiguous_markdown": False,
            "chapter_source": epub_data["chapter_source"],
            "skipped_spine_items": epub_data["skipped_spine_items"],
            "image_count": epub_data["image_count"],
            "spine_map": epub_data["spine_map"],
        }

    effective_encoding = "utf-8-sig" if encoding is None else encoding
    encoding, lines = normalized_lines(raw, effective_encoding)

    markdown_heading_levels = None
    markdown_heading_examples = None
    ambiguous_markdown = False

    if source_format == "markdown":
        md_headings = markdown_atx_headings(lines)
        counts = Counter(level for _, level, _ in md_headings)
        markdown_heading_levels = {str(level): counts[level] for level in sorted(counts)}
        markdown_heading_examples = {}
        for index, level, title in md_headings:
            bucket = markdown_heading_examples.setdefault(str(level), [])
            if len(bucket) < 3:
                bucket.append({"source_line": index + 1, "title": display_title(title)})

        if single_chapter:
            title = _single_chapter_title(source, lines, source_format, md_headings)
            parsing = {"format": "markdown", "single_chapter": True, "paragraphs": paragraphs, "newlines": "LF"}
            sections, candidates = [(-1, title, "body")], []
        elif chapter_pattern is not None:
            parsing = {"format": "markdown", "chapter_pattern": chapter_pattern, "paragraphs": paragraphs, "newlines": "LF"}
            try:
                sections, candidates = chapter_sections(lines, chapter_pattern)
            except re.error as exc:
                raise ValueError(f"无效的分章正则：{exc}") from exc
        elif heading_level is not None:
            parsing = {"format": "markdown", "heading_level": heading_level, "paragraphs": paragraphs, "newlines": "LF"}
            sections = _classify_markdown_sections(md_headings, heading_level)
            candidates = []
        elif len(markdown_heading_levels) == 1:
            resolved_level = int(next(iter(markdown_heading_levels)))
            parsing = {"format": "markdown", "heading_level": resolved_level, "paragraphs": paragraphs, "newlines": "LF"}
            sections = _classify_markdown_sections(md_headings, resolved_level)
            candidates = []
        elif len(markdown_heading_levels) >= 2:
            if not allow_ambiguous_markdown:
                raise ValueError(
                    f"Markdown 文件包含多个 ATX 标题层级（{_format_level_counts(markdown_heading_levels)}），"
                    "不自动猜测分章层级；请先运行 import-txt --preview 查看各层级，或显式指定 --heading-level N"
                )
            ambiguous_markdown = True
            parsing = {"format": "markdown", "heading_level": None, "paragraphs": paragraphs, "newlines": "LF"}
            sections, candidates = [], []
        else:
            parsing = {"format": "markdown", "heading_level": None, "paragraphs": paragraphs, "newlines": "LF"}
            sections, candidates = [], []
    else:
        if single_chapter:
            title = _single_chapter_title(source, lines, source_format)
            parsing = {"format": "txt", "single_chapter": True, "paragraphs": paragraphs, "newlines": "LF"}
            sections, candidates = [(-1, title, "body")], []
        else:
            effective_pattern = DEFAULT_HEADING if chapter_pattern is None else chapter_pattern
            parsing = {"format": "txt", "chapter_pattern": effective_pattern, "paragraphs": paragraphs, "newlines": "LF"}
            try:
                sections, candidates = chapter_sections(lines, effective_pattern)
            except re.error as exc:
                raise ValueError(f"无效的分章正则：{exc}") from exc

    return {
        "raw": raw,
        "encoding": encoding,
        "source_format": source_format,
        "parsing": parsing,
        "lines": lines,
        "sections": sections,
        "candidates": candidates,
        "markdown_heading_levels": markdown_heading_levels,
        "markdown_heading_examples": markdown_heading_examples,
        "ambiguous_markdown": ambiguous_markdown,
    }


def parse_text(source, encoding, chapter_pattern, paragraphs, *, heading_level=None, single_chapter=False):
    parsed = parse_source(
        source,
        encoding=encoding,
        chapter_pattern=chapter_pattern,
        paragraphs=paragraphs,
        heading_level=heading_level,
        single_chapter=single_chapter,
    )
    return (
        parsed["raw"],
        parsed["encoding"],
        parsed["parsing"],
        parsed["lines"],
        parsed["sections"],
        parsed["candidates"],
    )


def snapshot_identity(raw, chapters):
    source_hash = hashlib.sha256(raw).hexdigest()
    identity = hashlib.sha256(f"chapspan-snapshot-v1\n{source_hash}\n".encode())
    for chapter in chapters:
        record = json.dumps(asdict(chapter), ensure_ascii=False, separators=(",", ":"))
        identity.update(record.encode("utf-8") + b"\n")
    return source_hash, f"snap_{identity.hexdigest()}"


def chapter_entry(number, title, kind, paragraph_count, source_body_line, toc, *, characters=None):
    entry = {
        "chapter": number, "title": display_title(title), "kind": kind,
        "first_paragraph": f"C{number:04d}:1" if paragraph_count else None,
    }
    if not toc:
        entry.update(source_body_line=source_body_line, paragraph_count=paragraph_count)
        if characters is not None:
            entry["characters"] = characters
    return entry


def chapter_kind_summary(kinds):
    counts = Counter(kinds)
    return {kind: counts[kind] for kind in ("body", "unclassified", "peripheral") if counts[kind]}


def overview_page(entries, limit, toc, kind_counts=None):
    chapters, byte_count = [], 0
    for entry in entries:
        byte_count += len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if len(chapters) == limit or byte_count > TOC_MAX_BYTES:
            page = {"chapters": chapters, "next_after_chapter": chapters[-1]["chapter"]}
            if kind_counts is not None:
                page["chapter_kind_counts"] = kind_counts
            return page
        chapters.append(entry)
    page = {"chapters": chapters, "next_after_chapter": None}
    if kind_counts is not None:
        page["chapter_kind_counts"] = kind_counts
    return page


def ensure_body_sections(parsed):
    sections = parsed["sections"]
    if any(kind == "body" for _, _, kind in sections):
        return
    candidates = parsed["candidates"]
    if candidates:
        raise ValueError(
            f"无法识别分章标题，发现 {len(candidates)} 个紧连标题候选。"
            "请先用 import-txt --preview 查看来源行，"
            f"再用 --chapter-pattern 指定整行标题规则（参考正则：'{TIGHT_CHAPTER_PATTERN}'）"
        )
    if parsed["source_format"] == "markdown":
        levels = parsed["markdown_heading_levels"] or {}
        requested = parsed["parsing"].get("heading_level")
        if requested is not None and levels:
            raise ValueError(
                f"Markdown 文件中没有 {requested} 级（{'#' * requested}）正文章标题"
                f"（当前各层级标题数：{_format_level_counts(levels)}）；请用 import-txt --preview 核对并调整 --heading-level"
            )
        raise ValueError(
            "Markdown 文件中未识别到可用的 ATX 正文章标题；请先用 import-txt --preview 查看样例，"
            "再指定 --heading-level / --chapter-pattern，或对无章界单篇文稿显式使用 --single-chapter"
        )
    if parsed["source_format"] == "epub":
        raise ValueError(
            "EPUB 文件中未识别到可用的正文章节；请先用 import --preview 查看目录与 Spine 状态，"
            "再指定 --chapter-pattern，或对无章界单篇作品显式使用 --single-chapter"
        )
    raise ValueError(
        "无法识别分章标题，请先用 import-txt --preview 查看样例，再指定 --chapter-pattern"
        "（无章界单篇文稿可显式使用 --single-chapter）"
    )


def read_text_source_details(
    source,
    encoding=None,
    chapter_pattern=None,
    paragraphs="lines",
    *,
    heading_level=None,
    single_chapter=False,
    expected_snapshot=None,
):
    # Old epub-1 snapshots skipped unresolved spine references. An existing identity permits
    # replay of those rules, but must match exactly before any derived content is returned.
    options = dict(
        encoding=encoding, chapter_pattern=chapter_pattern, paragraphs=paragraphs,
        heading_level=heading_level, single_chapter=single_chapter,
        allow_incomplete_epub=expected_snapshot is not None,
    )
    parsed = None
    try:
        parsed = parse_source(source, **options)
        ensure_body_sections(parsed)
    except ValueError as exc:
        if expected_snapshot is None or not (
            isinstance(exc, Utf16DecodingError)
            or (parsed is not None and parsed["parsing"].get("xml_decoding"))
        ):
            raise
        # A fixed old identity can replay lossy decoding, but still must pass the
        # full snapshot check below; fresh imports retain the new failure.
        parsed = parse_source(source, **options, legacy_xml_decoding=True)
        ensure_body_sections(parsed)
    chapters = split_chapters(parsed["lines"], parsed["sections"], paragraphs)
    _, snapshot_id = snapshot_identity(parsed["raw"], chapters)
    if (expected_snapshot is not None and snapshot_id != expected_snapshot
            and parsed["parsing"].get("xml_decoding")):
        parsed = parse_source(source, **options, legacy_xml_decoding=True)
        ensure_body_sections(parsed)
        chapters = split_chapters(parsed["lines"], parsed["sections"], paragraphs)
        _, snapshot_id = snapshot_identity(parsed["raw"], chapters)
    if expected_snapshot is not None and snapshot_id != expected_snapshot:
        raise ValueError("源文件内容或分章结果与指定快照不同")
    source_details = {"lines": parsed["lines"], "encoding": parsed["encoding"], "parsing": parsed["parsing"]}
    if parsed.get("spine_map") is not None:
        source_details["spine_map"] = parsed["spine_map"]
    return snapshot_id, chapters, parsed["candidates"], source_details


def read_text_source(
    source,
    encoding=None,
    chapter_pattern=None,
    paragraphs="lines",
    *,
    heading_level=None,
    single_chapter=False,
):
    snapshot_id, chapters, candidates, _ = read_text_source_details(
        source,
        encoding=encoding,
        chapter_pattern=chapter_pattern,
        paragraphs=paragraphs,
        heading_level=heading_level,
        single_chapter=single_chapter,
    )
    return snapshot_id, chapters, candidates


def printed_number(match):
    if match is None:
        return None
    digits = match["number"] or match["arabic"]
    total = section = value = 0
    for char in digits:
        if char in CHINESE_UNITS:
            section += (value or 1) * CHINESE_UNITS[char]
            value = 0
        elif char in "万萬":
            total += (section + value or 1) * 10000
            section = value = 0
        else:
            digit = CHINESE_DIGITS[char] if char in CHINESE_DIGITS else int(char)
            value = value * 10 + digit
    return total + section + value


def text_boundary(chapters, boundary, *, first_chapter=1, max_chapter=None, include_peripheral=False):
    if isinstance(boundary, int):
        return integer(boundary, "阅读进度", 0, len(chapters)), None
    if boundary == "all":
        return len(chapters), None
    if re.fullmatch(r"C[0-9]+", boundary):
        return integer(int(boundary[1:]), "阅读进度", 0, len(chapters)), None

    numbers = [printed_number(PRINTED_HEADING.match(c.title.lstrip())) if c.kind == "body" else None
               for c in chapters]
    requested = printed_number(PRINTED_HEADING.fullmatch(boundary))
    if requested is None:
        matches = [c.number for c in chapters if c.title.strip() == boundary]
    else:
        matches = [index for index, number in enumerate(numbers, 1) if number == requested]
    if not matches:
        raise ValueError("找不到指定原书章节，请核对章号或章题；也可使用内部章序，或用 all 读取全书")
    if len(matches) > 1:
        visible = [
            number for number in matches
            if first_chapter <= number and (max_chapter is None or number <= max_chapter)
            and (include_peripheral or chapters[number - 1].kind != "peripheral")
        ]
        if visible:
            sample = "、".join(f"C{number:04d}" for number in visible[:5])
            suffix = "等" if len(visible) > 5 else ""
            scope_note = f"（当前范围内共 {len(visible)} 处：{sample}{suffix}"
            if len(visible) < len(matches):
                scope_note += "，另有重号超出当前范围"
            scope_note += "）"
        else:
            scope_note = "（均超出当前范围）"
        raise ValueError(
            f"原书章号或章题有重号{scope_note}。请用完整且唯一的章题，或在确认文件顺序后指定内部章序"
        )
    end = matches[0]
    return end, numbers[end - 1]


def diagnostic_entry(chapter):
    return {"chapter": chapter.number, "title": chapter.title, "kind": chapter.kind,
            "source_body_line": chapter.source_body_line,
            "source_line": max(1, chapter.source_body_line - 1)}


def structural_chapter_issues(entries):
    entries = sorted(entries, key=lambda row: row["chapter"])
    issues = []

    def related(rows):
        return [{"chapter": row["chapter"], "source_line": row["source_line"], "title": display_title(row["title"])}
                for row in rows]

    def add(kind, rows, **details):
        row = rows[-1]
        issues.append({"kind": kind, "chapter": row["chapter"], "source_line": row["source_line"],
                       "title": display_title(row["title"]), "related_chapters": related(rows), **details})

    numbered = [(row, printed_number(PRINTED_HEADING.match(row["title"].lstrip())))
                for row in entries if row["kind"] == "body"]
    numbered = [(row, number) for row, number in numbered if number is not None]
    by_number = {}
    for row, number in numbered:
        by_number.setdefault(number, []).append(row)
    for number, rows in by_number.items():
        if len(rows) > 1:
            add("duplicate_number", rows, printed_number=number)
    for (previous, first), (current, second) in zip(numbered, numbered[1:]):
        if second > first + 1:
            add("number_gap", [previous, current], previous_printed=first, printed_number=second)
    by_title = {}
    for row in entries:
        by_title.setdefault(row["title"].strip(), []).append(row)
    for title, rows in by_title.items():
        if title and len(rows) > 1:
            add("duplicate_title", rows)
    body_numbers = [row["chapter"] for row in entries if row["kind"] == "body"]
    if body_numbers:
        trailing = [row for row in entries if row["kind"] == "peripheral" and row["chapter"] > max(body_numbers)]
        for row in trailing:
            add("trailing_peripheral", [row])
    return issues


def _lookup_spine_href(spine_map, line_number):
    for entry in spine_map:
        if entry["start_line"] <= line_number <= entry["end_line"]:
            return entry["href"]
    return spine_map[-1]["href"] if spine_map else None


def build_chapter_inspection(chapter, adjacent, source, target_line=None):
    lines = source["lines"]
    # Strip only the synthetic separator newline after the final body line so EOF chapters without a trailing newline keep their last line.
    body_lines = (len(chapter.body[:-1].split("\n")) if chapter.body.endswith("\n")
                  else len(chapter.body.split("\n")) if chapter.body else 0)
    start = max(1, chapter.source_body_line - 1)
    end = chapter.source_body_line + body_lines - 1
    if not lines or end > len(lines):
        raise ValueError("快照的来源行映射超出原始文件；请重新导入核对来源")
    if target_line is None:
        target_line = start
    integer(target_line, "核查来源行", start, max(start, end))
    previous = next((row for row in adjacent if row["chapter"] == chapter.number - 1), None)
    following = next((row for row in adjacent if row["chapter"] == chapter.number + 1), None)
    first = max(start - 1, target_line - 3)
    last = min(len(lines), target_line + 3, end)
    excerpt = []
    for index in range(first, last):
        text = lines[index]
        excerpt.append({"line": index + 1, "text": text[:240] + "…" if len(text) > 240 else text,
                        "characters": len(text), "target": index + 1 == target_line})
    inspection = {
        "chapter": chapter.number, "title": chapter.display_title, "kind": chapter.kind,
        "source_body_line": chapter.source_body_line, "source_start_line": start, "source_end_line": end,
        "target_line": target_line, "encoding": source["encoding"], "parsing": source["parsing"],
        "adjacent_chapters": [{"chapter": row["chapter"], "source_line": row["source_line"],
                               "title": display_title(row["title"]), "kind": row["kind"]}
                              for row in (previous, following) if row is not None],
        "source_lines": excerpt,
    }
    if source.get("spine_map") is not None:
        href = _lookup_spine_href(source["spine_map"], target_line)
        if href is not None:
            inspection["source_href"] = href
    return inspection


def peripheral_category(title):
    label = title.strip()
    for category, pattern in PERIPHERAL_CATEGORIES:
        if pattern.fullmatch(label):
            return category
    return "other"


def sample_peripheral_by_category(entries, limit=PREVIEW_EXAMPLES):
    ordered = sorted(entries, key=lambda row: (row["chapter"], row["source_line"]))
    buckets = {}
    categorized = []
    for row in ordered:
        category = peripheral_category(row["title"])
        item = {**row, "category": category}
        categorized.append(item)
        buckets.setdefault(category, []).append(item)
    counts = dict(sorted(Counter(item["category"] for item in categorized).items()))
    selected = []
    depth = 0
    while len(selected) < limit:
        added = False
        for bucket in buckets.values():
            if depth < len(bucket):
                selected.append(bucket[depth])
                added = True
                if len(selected) == limit:
                    break
        if not added:
            break
        depth += 1
    selected.sort(key=lambda row: (row["chapter"], row["source_line"]))
    return counts, selected


def chapter_diagnostics(issues, filtered_peripheral=(), *, offset=0):
    integer(offset, "诊断分页起点")
    issues = sorted(issues, key=lambda row: (row["kind"] != "tight_heading", row["source_line"], row["kind"]))
    filtered_peripheral = list(filtered_peripheral)
    if not issues and not filtered_peripheral:
        return None
    examples = issues[offset:offset + PREVIEW_EXAMPLES]
    peripheral_counts, peripheral_examples = sample_peripheral_by_category(filtered_peripheral)
    return {
        "issue_count": len(issues),
        "unresolved_tight_headings": sum(row["kind"] == "tight_heading" for row in issues),
        "kind_counts": dict(sorted(Counter(row["kind"] for row in issues).items())),
        "diagnostic_offset": offset,
        "shown_count": len(examples),
        "next_diagnostic_offset": offset + len(examples) if offset + len(examples) < len(issues) else None,
        "examples": examples,
        "filtered_peripheral_count": len(filtered_peripheral),
        "filtered_peripheral_category_counts": peripheral_counts,
        "filtered_peripheral_examples": peripheral_examples,
    }


def text_chapter_issues(chapters, candidates, scope, *, printed_limit=None, diagnostic_offset=0):
    """Inspect only the selected physical prefix, before returning any of its content."""
    physical = chapters[scope.first_chapter - 1:scope.as_of]
    visible = [chapter for chapter in physical
               if scope.include_peripheral or chapter.kind != "peripheral"]
    filtered = [] if scope.include_peripheral else [diagnostic_entry(chapter) for chapter in physical
                                                   if chapter.kind == "peripheral"]
    issues = structural_chapter_issues(map(diagnostic_entry, visible))
    for chapter in visible:
        title = chapter.title.strip()
        heading = PRINTED_HEADING.match(title)
        chapter_number = printed_number(heading)
        if printed_limit is not None and chapter_number is not None and chapter_number > printed_limit:
            raise ValueError(f"第 {max(1, chapter.source_body_line - 1)} 行的章号超过指定阅读进度。"
                             "请核对错插或重新编号，确认文件顺序后可用 --as-of C章序")
        start = bisect_left(candidates, (chapter.source_body_line - 1, ""))
        stop = (bisect_left(candidates, (chapters[chapter.number].source_body_line - 2, ""), lo=start)
                if chapter.number < len(chapters) else len(candidates))
        for index, label in candidates[start:stop]:
            candidate = PRINTED_HEADING.match(label)
            candidate_number = printed_number(candidate)
            if (candidate_number == chapter_number and heading and candidate
                    and label[candidate.end():] == re.sub(rf"^[{TITLE_SEPARATORS}]+", "", title[heading.end():])):
                continue
            if printed_limit is not None and candidate_number is not None and candidate_number > printed_limit:
                raise ValueError(f"第 {index + 1} 行的疑似章题超过指定阅读进度，可能混入后续正文。"
                                 "请用 import-txt --preview 核对，并在需要时指定 --chapter-pattern")
            issues.append({"kind": "tight_heading", "source_line": index + 1,
                           "chapter": chapter.number, "title": label})
    return chapter_diagnostics(issues, filtered, offset=diagnostic_offset)


def resolve_text_boundary(chapters, candidates, boundary, *, first_chapter=1, include_peripheral=False,
                          max_chapter=None):
    """Resolve a TXT --as-of boundary and enforce printed-number safety checks."""
    as_of, printed_limit = text_boundary(
        chapters, boundary, first_chapter=first_chapter, max_chapter=max_chapter,
        include_peripheral=include_peripheral,
    )
    check_end = as_of if max_chapter is None else min(as_of, max_chapter)
    if printed_limit is not None and check_end >= first_chapter:
        scope = Scope("boundary", check_end, first_chapter, bool(include_peripheral))
        text_chapter_issues(chapters, candidates, scope, printed_limit=printed_limit)
    return as_of


class ScopedText:
    """Use the same normalized chapter coordinates directly from a TXT."""

    def __init__(self, chapters, scope, source=None):
        self.scope = scope
        self._source = source
        self._all_chapters = chapters[scope.first_chapter - 1:scope.as_of]
        self._chapters = {chapter.number: chapter for chapter in self._all_chapters
                          if scope.include_peripheral or chapter.kind != "peripheral"}

    def chapters(self, *, with_paragraphs=True):
        return iter(self._chapters.values())

    def chapter(self, number):
        integer(number, "章序", 1)
        if number not in self._chapters:
            if (not self.scope.include_peripheral
                    and self.scope.first_chapter <= number <= self.scope.as_of):
                in_bounds = next((c for c in self._all_chapters if c.number == number), None)
                if in_bounds is not None and in_bounds.kind == "peripheral":
                    raise ValueError(
                        f"C{number:04d} 为当前范围内默认过滤的外围章，当前不可读取；如需读取或核查，请加 --include-peripheral"
                    )
            raise ValueError(f"C{number:04d} 在当前范围内不可读取")
        return self._chapters[number]

    def body_chapters(self):
        return [c.number for c in self.chapters() if c.kind == "body" and c.paragraph_ends]

    def overview(self, *, after_chapter=0, limit=20, toc=False, title=None):
        integer(after_chapter, "目录起点")
        integer(limit, "页大小", 1, TOC_MAX_PAGE)
        entries = (chapter_entry(c.number, c.title, c.kind, len(c.paragraph_ends), c.source_body_line, toc,
                                 characters=len(c.body))
                   for c in self.chapters()
                   if c.number > after_chapter and (title is None or title in c.title))
        kind_counts = chapter_kind_summary(c.kind for c in self.chapters())
        return overview_page(entries, limit, toc, kind_counts)

    def chapter_metadata(self, *, include_filtered=False):
        chapters = self._all_chapters if include_filtered else self.chapters(with_paragraphs=False)
        return [diagnostic_entry(chapter) for chapter in chapters]

    def inspect_chapter(self, number, target_line=None):
        chapter = self.chapter(number)
        if self._source is None:
            raise ValueError("当前 TXT 来源未提供就地核查所需的来源行")
        return build_chapter_inspection(chapter, self.chapter_metadata(), self._source, target_line)
