"""Shared application service orchestration separated from CLI argument parsing."""

import json
import re
import tempfile
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .compare import compare_intervals, parse_interval_spec
from .display import format_output_receipt, format_text, output_receipt
from .documents import (
    check_draft,
    document_scope,
    draft_header,
    normalize_conventions,
    read_draft,
    resolve_conventions,
)
from .handoff import (
    collect_picks,
    continuation,
    continuation_group_picks,
    handoff_scope,
    inherited_text_options,
    load_result,
    parse_spans,
    prepare_state,
    read_example_picks,
    read_picks,
    reading_references,
    result_reading,
    save_text_result,
    scope_continuation,
    should_save_state,
    write_state_file,
)
from .library import import_txt, list_snapshots, open_scope, snapshot_chapter_count
from .model import (
    MAX_RESULT_BYTES,
    Scope,
    atomic_write,
    integer,
    parse_paragraph,
    parse_position,
    source_span,
)
from .reading import ReadSelection, annotate_reading, normalize_group_budget, read_selections, sample_openings, selection_origins
from .runs import create_run, ensure_scope_within, json_bytes, require_expansion
from .search import find_many, find_passages, resolve_find_examples
from .sources import (
    ScopedText,
    TOC_MAX_PAGE,
    chapter_diagnostics,
    read_text_source_details,
    resolve_text_boundary,
    structural_chapter_issues,
    text_chapter_issues,
)


TEXT_COMMANDS = ("overview", "find", "compare", "read")
TEXT_SOURCE_COMMANDS = TEXT_COMMANDS + ("resume",)


@dataclass(frozen=True)
class ExecutionOutcome:
    result: dict
    scope: Scope | None = None
    record_committed: bool = False


@dataclass(frozen=True)
class PreparedOutput:
    payload: str
    encoded: bytes
    state_id: str | None = None
    state_raw: bytes | None = None


def _resolve_library_as_of(library_root, snapshot_id, as_of):
    if isinstance(as_of, int):
        return as_of
    if as_of == "all":
        if not snapshot_id:
            raise ValueError("请用 --snapshot 选定版本，或用 --from-result 沿用上一步的来源")
        if library_root is None:
            raise ValueError("请用 --library 书库目录或 --text 书.txt/书.md 指定来源")
        return snapshot_chapter_count(library_root, snapshot_id)
    if isinstance(as_of, str) and re.fullmatch(r"C[0-9]+", as_of):
        return int(as_of[1:])
    raise ValueError("已有书库的 --as-of 请使用内部章序（如 12 或 C0012）或 all")


def _expand_chapter_selections(library, specs):
    if not specs:
        return []
    if not any(isinstance(item, tuple) for item in specs):
        return list(specs)
    scope = library.scope
    visible = {row["chapter"] for row in library.chapter_metadata()}
    expanded = []
    for item in specs:
        if isinstance(item, int):
            expanded.append(item)
            continue
        start, end = item
        if start < scope.first_chapter or end > scope.as_of:
            raise ValueError(
                f"章节区间 C{start:04d}–C{end:04d} 超出当前允许范围 "
                f"C{scope.first_chapter:04d}–C{scope.as_of:04d}"
            )
        in_range = [ch for ch in range(start, end + 1) if ch in visible]
        if not in_range:
            raise ValueError(f"章节区间 C{start:04d}–C{end:04d} 在当前范围内没有可读章节")
        expanded.extend(in_range)
    return expanded


def run_read(library, args, previous):
    budget = 4000 if args.budget is None else args.budget
    explicit_selection = args.sample is not None or any((args.spans, args.chapter, args.item, args.paragraph,
                                                         args.pick, args.example))
    if args.example and previous is None:
        raise ValueError("--example 需要 --from-result 指定带例段的 find 结果")
    if args.example and args.pick:
        raise ValueError("--example 选择例段，--pick 选择当前匹配页；请分开使用")
    if args.continue_group and (previous is None or explicit_selection):
        raise ValueError("--continue-group 需要 --from-result 上一页，请直接选择待续组")
    if args.sample is not None:
        if any((args.chapter, args.spans, args.item, args.paragraph, args.pick, args.example, args.resume,
                args.as_matches, args.before is not None, args.whole_paragraphs)):
            raise ValueError("--sample 用于开篇抽读；可用 --after 限制段数、--budget 限制正文篇幅")
        gb, gb_source = normalize_group_budget(budget, args.group_budget)
        return sample_openings(
            library, args.sample, budget=budget, after=args.after,
            group_budget=gb, group_budget_source=gb_source,
        )

    if previous is not None and not explicit_selection:
        if (args.before is not None or args.after is not None or args.whole_paragraphs
                or args.resume is not None or args.as_matches):
            raise ValueError("从结果续读时会沿用原选区与扩读设置；更改选区请开始新的读取")
        groups, options = continuation(previous, library.scope.snapshot_id)
        group_refs, example_refs = reading_references(previous, library.scope.snapshot_id)
        visible_chapters = {row["chapter"] for row in library.chapter_metadata()}
        groups, options, group_refs, example_refs = scope_continuation(
            groups,
            options,
            group_refs,
            example_refs,
            visible_chapters,
            library.scope.snapshot_id,
        )
        if args.continue_group:
            selected = continuation_group_picks(previous, args.continue_group, library.scope.snapshot_id)
            available = {group.group for group in groups}
            if not selected <= available:
                references = {row["group"]: row["reference"] for row in group_refs}
                numbers = "、".join(references.get(number, str(number)) for number in sorted(available))
                raise ValueError(f"当前页可续读的组：{numbers}")
            options["deferred_groups"] = [group for group in groups if group.group not in selected]
            groups = [group for group in groups if group.group in selected]
        if args.budget is not None:
            options["budget"] = args.budget
            if options["group_budget"] is not None and options.get("group_budget_source") == "auto":
                options["group_budget"], options["group_budget_source"] = normalize_group_budget(args.budget)
        if args.group_budget is not None:
            if options["group_budget"] is None:
                raise ValueError("共享预算（未分组）的续读结果不支持改为 --group-budget；请重新开始读取")
            options["group_budget"], options["group_budget_source"] = normalize_group_budget(
                options["budget"], args.group_budget,
            )
        page = read_selections(library, groups, **options)
        if group_refs:
            annotate_reading(page, group_refs, example_refs)
        return page

    chapters = _expand_chapter_selections(library, args.chapter)
    spans = tuple(source_span(value, library.scope.snapshot_id) for value in args.spans)
    paragraphs = tuple(parse_paragraph(value) for value in args.paragraph)
    matches = spans if args.as_matches else ()
    picked = read_picks(previous, args.pick, library.scope.snapshot_id) if args.pick else []
    examples = read_example_picks(previous, args.example, library.scope.snapshot_id) if args.example else []
    chosen = [*picked, *examples]
    if args.group_budget is None:
        groups = [ReadSelection(
            spans=spans + tuple(span for item in chosen for span in item.spans),
            paragraphs=paragraphs + tuple(paragraph for item in chosen for paragraph in item.paragraphs),
            item_ids=tuple(args.item),
            match_spans=matches + tuple(span for item in chosen for span in item.match_spans),
            resume=parse_position(args.resume) if args.resume is not None else None,
            chapter_numbers=tuple(chapters),
            selection_anchors=tuple(anchor for item in chosen for anchor in item.selection_anchors),
            context_anchors=tuple(anchor for item in chosen for anchor in item.context_anchors),
        )]
    else:
        if args.resume is not None:
            raise ValueError("分组读取请用 --from-result 上一页.json 续读")
        groups = [ReadSelection(spans=(span,), match_spans=(span,) if args.as_matches else ()) for span in spans]
        groups.extend(ReadSelection(paragraphs=(paragraph,)) for paragraph in paragraphs)
        groups.extend(ReadSelection(item_ids=(item,)) for item in args.item)
        groups.extend(ReadSelection(chapter_numbers=(chapter,)) for chapter in chapters)
        groups.extend(chosen)
        groups = [replace(group, group=index) for index, group in enumerate(groups, 1)]
    picked_from_search = picked and any(key in previous for key in ("matches", "candidates", "hit_chapters"))
    search_picks = (picked_from_search or examples) and not any((spans, paragraphs, chapters, args.item))
    gb, gb_source = (
        normalize_group_budget(budget, args.group_budget) if args.group_budget is not None else (None, None)
    )
    return read_selections(
        library, groups, before=0 if args.before is None else args.before,
        after=0 if args.after is None else args.after,
        whole_paragraphs=bool(args.whole_paragraphs or search_picks),
        budget=budget, group_budget=gb, group_budget_source=gb_source,
    )


def _resolve_import_parsing(args):
    def _pick(flag, sub_value, text_value, default=None):
        if sub_value is not None and text_value is not None:
            raise ValueError(f"请勿在 {args.command} 前后重复指定 {flag}")
        if sub_value is not None:
            return sub_value
        if text_value is not None:
            return text_value
        return default

    sub_single = bool(getattr(args, "single_chapter", False))
    text_single = bool(getattr(args, "text_single_chapter", False))
    if sub_single and text_single:
        raise ValueError(f"请勿在 {args.command} 前后重复指定 --single-chapter")

    return (
        _pick("--encoding", args.encoding, args.text_encoding, None),
        _pick("--chapter-pattern", args.chapter_pattern, args.text_pattern, None),
        _pick("--paragraphs", args.paragraphs, args.text_paragraphs, "lines"),
        _pick("--heading-level", getattr(args, "heading_level", None), getattr(args, "text_heading_level", None), None),
        sub_single or text_single,
    )


def validate_args(args, *, is_text):
    initializing = args.command == "research" and args.action == "init"
    expanding = args.command == "research" and args.action == "expand"
    indexing = args.command == "research" and args.action == "index"
    is_import = args.command in ("import", "import-txt")
    if args.text is not None and args.command not in TEXT_SOURCE_COMMANDS and not initializing:
        raise ValueError("--text 用于 overview、find、compare、read、resume 或 research init；持久书库操作用 --library")
    if (not is_import and args.text is None
            and (args.text_encoding or args.text_pattern is not None or args.text_paragraphs
                 or getattr(args, "text_heading_level", None) is not None
                 or getattr(args, "text_single_chapter", False))):
        raise ValueError(
            "参数 --encoding / --chapter-pattern / --paragraphs / --heading-level / --single-chapter 仅用于 --text 或 import-txt（或 import）"
        )
    if (args.library is None and args.text is None and not expanding and not indexing
            and not (is_import and args.preview)):
        raise ValueError("请指定 --text 书.txt/书.md/书.epub、--library 书库目录，或使用 --run 运行目录（放在子命令前后均可）")
    if is_text and args.command not in TEXT_COMMANDS:
        raise ValueError(f"{args.command} 固定输出 JSON，不支持 --format text；仅 overview、find、compare、read 支持 --format text")
    if args.command == "collect" and args.action in ("list", "remove"):
        missing = [flag for flag, value in (("--snapshot", args.snapshot), ("--as-of", args.as_of)) if value is None]
        if missing:
            raise ValueError(f"collect {args.action} 需要同时指定 {' 和 '.join(missing)}")
    if getattr(args, "distribution", False) and args.compact:
        raise ValueError("--distribution 需要保留分布，请去掉 --compact")
    if args.output is not None and ((is_import and not args.preview)
                                    or (args.command == "collect" and args.action != "list")
                                    or args.command == "research"):
        raise ValueError("--output 仅用于只读命令，不用于导入、收藏修改或运行管理")
    if args.command == "resume" and (args.draft is None or args.as_of is None):
        raise ValueError("resume 需要稿件路径和 --as-of；绑定运行时可沿运行默认值")
    if indexing and args.offset is not None and args.section is None:
        raise ValueError("--offset 需要与 --section 一起使用")


def run_operation(args, *, state_dir, run_scope=None, run_conventions=None, on_scope=None):
    if args.command in ("import", "import-txt"):
        encoding, chapter_pattern, paragraphs, heading_level, single_chapter = _resolve_import_parsing(args)
        result = import_txt(
            args.library,
            args.source,
            encoding=encoding,
            chapter_pattern=chapter_pattern,
            paragraphs=paragraphs,
            heading_level=heading_level,
            single_chapter=single_chapter,
            preview=args.preview,
        )
        return ExecutionOutcome(result)
    if args.command == "snapshots":
        result = list_snapshots(args.library, after_snapshot=args.after_snapshot, limit=args.limit)
        return ExecutionOutcome(result)

    document = None
    if args.command == "resume":
        if args.draft_horizon is not None and run_scope is None:
            raise ValueError("--draft-horizon 需要绑定报告所属的原运行")
        document = read_draft(args.draft, run_scope=run_scope, draft_horizon=args.draft_horizon)

    result_path = getattr(args, "from_result", None)
    previous = load_result(result_path, state_dir=state_dir) if result_path is not None else None
    snapshot, as_of = getattr(args, "snapshot", None), args.as_of
    if document is not None:
        snapshot = document["snapshot_id"]
    if as_of is None and previous is None:
        raise ValueError("请用 --as-of 指定允许读到的位置，或用 --from-result 沿用上一步的范围")

    first_chapter = args.from_chapter if args.from_chapter is not None else 1
    text_options = {}
    if args.text is not None:
        text_options = inherited_text_options(previous).copy()
        overrides = {
            "encoding": args.text_encoding,
            "chapter_pattern": args.text_pattern,
            "paragraphs": args.text_paragraphs,
            "heading_level": getattr(args, "text_heading_level", None),
            "single_chapter": True if getattr(args, "text_single_chapter", False) else None,
        }
        text_options.update((key, value) for key, value in overrides.items() if value is not None)
        expected_snapshot = snapshot
        if expected_snapshot is None and previous is not None:
            expected_snapshot = previous["scope"]["snapshot_id"]
        text_snapshot, chapters, candidates, source_details = read_text_source_details(
            args.text, **text_options, expected_snapshot=expected_snapshot,
        )
        snapshot = text_snapshot
        if as_of is not None:
            check_first = first_chapter
            check_peripheral = bool(args.include_peripheral)
            max_chapter = None
            if previous is not None:
                inherited = handoff_scope(previous["scope"], snapshot_id=snapshot, as_of=None,
                                          first_chapter=first_chapter, include_peripheral=args.include_peripheral)
                check_first = inherited.first_chapter
                check_peripheral = inherited.include_peripheral
                max_chapter = inherited.as_of
            if run_scope is not None:
                check_first = max(check_first, run_scope.first_chapter)
                check_peripheral = check_peripheral and run_scope.include_peripheral
                max_chapter = run_scope.as_of if max_chapter is None else min(max_chapter, run_scope.as_of)
            as_of = resolve_text_boundary(
                chapters, candidates, as_of,
                first_chapter=check_first, include_peripheral=check_peripheral,
                max_chapter=max_chapter,
            )
    elif as_of is not None:
        effective_snapshot = snapshot or (previous["scope"]["snapshot_id"] if previous else None)
        as_of = _resolve_library_as_of(args.library, effective_snapshot, as_of)

    if previous is None:
        if getattr(args, "pick", []):
            raise ValueError("--pick 需要与 --from-result 一起使用")
        if snapshot is None:
            raise ValueError("请用 --snapshot 选定版本，或用 --from-result 沿用上一步的来源")
        include_peripheral = bool(args.include_peripheral)
        scope = (document_scope(document, as_of, first_chapter, include_peripheral)
                 if document is not None else Scope(snapshot, as_of, first_chapter, include_peripheral))
    else:
        scope = handoff_scope(previous["scope"], snapshot_id=snapshot, as_of=as_of,
                              first_chapter=first_chapter, include_peripheral=args.include_peripheral)
    if run_scope is not None:
        ensure_scope_within(scope, run_scope)
    if on_scope is not None:
        on_scope(scope)

    writable = args.command == "collect" and args.action in ("add", "remove")
    metadata = {}
    show_issues = (args.command == "overview" and args.inspect is None) or getattr(args, "sample", None) is not None
    if args.text is None:
        storage = open_scope(args.library, scope, writable=writable)
    else:
        if getattr(args, "item", ()):
            raise ValueError("读取收藏需使用 --library 指定已有书库")
        integer(scope.as_of, "阅读进度", 0, len(chapters))
        if text_options:
            metadata["text_options"] = text_options
        if show_issues:
            issues = text_chapter_issues(chapters, candidates, scope,
                                         diagnostic_offset=getattr(args, "diagnostic_offset", 0))
            if issues:
                metadata["chapter_diagnostics"] = issues
        storage = nullcontext(ScopedText(chapters, scope, source_details))

    with storage as library:
        if args.text is None and show_issues:
            issues = structural_chapter_issues(library.chapter_metadata())
            filtered = [] if scope.include_peripheral else [
                row for row in library.chapter_metadata(include_filtered=True) if row["kind"] == "peripheral"
            ]
            issues = chapter_diagnostics(issues, filtered, offset=getattr(args, "diagnostic_offset", 0))
            if issues:
                metadata["chapter_diagnostics"] = issues
        if args.command == "overview":
            if args.inspect is not None:
                if (args.toc or args.title is not None or args.limit is not None or args.after_chapter
                        or args.diagnostic_offset):
                    raise ValueError("--inspect 单独核查章界，不与目录分页或筛选参数同用")
                result = {"inspection": library.inspect_chapter(args.inspect, args.line)}
            else:
                if args.line is not None:
                    raise ValueError("--line 需要与 --inspect C编号 一起使用")
                limit = args.limit if args.limit is not None else TOC_MAX_PAGE if args.toc else 20
                result = library.overview(after_chapter=args.after_chapter, limit=limit, toc=args.toc, title=args.title)
        elif args.command == "find":
            if args.hit_chapters and args.distribution:
                raise ValueError("--hit-chapters 与 --distribution 不能同用")
            examples = resolve_find_examples(query_groups=args.query_groups, examples=args.examples,
                                             has_text=args.text is not None, spread=args.spread,
                                             hit_chapters=args.hit_chapters)
            if args.query_groups:
                if (args.terms or args.all_terms or args.any_terms or args.exclude or args.near is not None
                        or args.offset or args.distribution_offset):
                    raise ValueError("多组查询把条件与分页写在各 --query JSON 中，不与单组条件或分页参数混用")
                result = find_many(library, args.query_groups, examples=examples, budget=args.budget,
                                   compact=args.compact, spread=args.spread, limit=args.limit,
                                   hit_chapters=args.hit_chapters)
            else:
                result = find_passages(library, all_terms=args.terms + args.all_terms,
                                       any_terms=args.any_terms, exclude=args.exclude, near=args.near,
                                       offset=args.offset, distribution_offset=args.distribution_offset,
                                       limit=args.limit, examples=examples, budget=args.budget,
                                       compact=args.compact, spread=args.spread, hit_chapters=args.hit_chapters)
        elif args.command == "compare":
            chapters_prefix = chapters[:scope.as_of] if args.text is not None else None
            left_range = parse_interval_spec(args.left, chapters_prefix, scope, label="左区间")
            right_range = parse_interval_spec(args.right, chapters_prefix, scope, label="右区间")
            result = compare_intervals(
                library, left_range, right_range,
                limit=args.limit, offset=args.offset, min_support=args.min_support,
                min_chapters=args.min_chapters, min_len=args.min_len, max_len=args.max_len,
                direction=args.direction, view=args.view, sort=args.sort, contains=args.contains,
                explain=args.explain, examples=args.examples, budget=args.budget, group_budget=args.group_budget,
            )
        elif args.command == "read":
            result = run_read(library, args, previous)
        elif args.command == "resume":
            result = check_draft(library, document, scope, include_markdown=not args.citations_only,
                                 include_inline=args.include_inline_citations)
            result["conventions"] = resolve_conventions(run_conventions, document.get("conventions"))
        elif args.action == "add":
            spans = list(parse_spans(args.spans, scope.snapshot_id))
            origins_by_span = {}
            if previous is not None and (args.pick or not spans):
                for row in collect_picks(previous, args.pick):
                    span, = parse_spans([row.get("read_span")], scope.snapshot_id)
                    spans.append(span)
                    if origins := selection_origins(row.get("selection_origins")):
                        origins_by_span.setdefault(span, []).extend(origins)
            result = {"items": library.add_items(args.collection, spans, args.note, origins_by_span=origins_by_span)}
        elif args.action == "list":
            result = library.list_items(args.collection, after_id=args.after_id, limit=args.limit)
        else:
            result = {"removed_ids": library.remove_items(args.item_ids)}
        return ExecutionOutcome({"scope": asdict(scope), **metadata, **result}, scope)


def init_research_run(
    task_directory,
    *,
    as_of,
    text=None,
    text_encoding=None,
    text_pattern=None,
    text_paragraphs=None,
    text_heading_level=None,
    text_single_chapter=False,
    library=None,
    snapshot=None,
    from_chapter=1,
    include_peripheral=False,
    conventions=None,
):
    normalized_conventions = normalize_conventions(conventions)
    if text is not None:
        resolved_snapshot, chapters, candidates, details = read_text_source_details(
            text, encoding=text_encoding, chapter_pattern=text_pattern, paragraphs=text_paragraphs or "lines",
            heading_level=text_heading_level, single_chapter=bool(text_single_chapter),
        )
        if snapshot is not None and snapshot != resolved_snapshot:
            raise ValueError("源文件内容或分章结果与指定快照不同")
        boundary = resolve_text_boundary(
            chapters, candidates, as_of,
            first_chapter=from_chapter, include_peripheral=include_peripheral,
        )
        parsing = details["parsing"]
        persisted_options = {
            key: parsing[key] for key in ("paragraphs", "single_chapter", "heading_level", "chapter_pattern")
            if key in parsing
        }
        if parsing.get("format") != "epub":
            persisted_options["encoding"] = details["encoding"]
        source = {"kind": "text", "path": str(text), "text_options": persisted_options}
    else:
        if library is None:
            raise ValueError("research init 需要 --text 书.txt/书.md/书.epub 或 --library 书库目录")
        if snapshot is None:
            raise ValueError("使用书库创建运行时须显式指定 --snapshot")
        resolved_snapshot = snapshot
        boundary = _resolve_library_as_of(library, resolved_snapshot, as_of)
        source = {"kind": "library", "path": str(library)}
    scope = Scope(resolved_snapshot, boundary, from_chapter, include_peripheral)
    if source["kind"] == "library":
        with open_scope(library, scope):
            pass
    run = create_run(
        task_directory,
        source=source,
        snapshot_id=resolved_snapshot,
        initial_scope=scope,
        conventions=normalized_conventions,
    )
    result = {
        "run_id": run.run_id,
        "run_directory": str(run.root),
        "snapshot_id": resolved_snapshot,
        "scope": asdict(scope),
        "conventions": normalized_conventions,
        "results_directory": str(run.root / "results"),
        "state_directory": str(run.root / "states"),
        "draft": str(run.root / "当前稿.md"),
        "draft_header": draft_header(resolved_snapshot, scope.as_of, conventions=normalized_conventions),
    }
    return ExecutionOutcome(result)


def expand_research(recorder, *, as_of=None, from_chapter=None, include_peripheral=False):
    if as_of is None and from_chapter is None and not include_peripheral:
        raise ValueError("research expand 至少需要一个扩展边界")
    run = recorder.run
    previous = recorder.current_scope
    first_chapter = previous.first_chapter if from_chapter is None else from_chapter
    include_peripheral = previous.include_peripheral or include_peripheral
    as_of = previous.as_of if as_of is None else as_of
    source = run.source
    if source["kind"] == "text":
        _, chapters, candidates, _ = read_text_source_details(
            source["path"], **source["text_options"], expected_snapshot=run.snapshot_id,
        )
        as_of = resolve_text_boundary(
            chapters, candidates, as_of,
            first_chapter=first_chapter, include_peripheral=include_peripheral,
        )
    else:
        as_of = _resolve_library_as_of(source["path"], run.snapshot_id, as_of)
    current = Scope(run.snapshot_id, as_of, first_chapter, include_peripheral)
    require_expansion(previous, current)
    if current == previous:
        raise ValueError("运行范围没有扩大，无需追加 scope_expand")
    if source["kind"] == "library":
        with open_scope(source["path"], current):
            pass
    recorder.expand(previous, current)
    result = {"run_id": run.run_id, "run_directory": str(run.root), "snapshot_id": run.snapshot_id,
              "previous_scope": asdict(previous), "scope": asdict(current)}
    return ExecutionOutcome(result, record_committed=True)


def prepare_output(result, *, is_text, output_path, distribution=False, command_context=None):
    state_id = state_raw = None
    if is_text:
        if should_save_state(result, output_path is not None):
            state_id, state_raw = prepare_state(result)
        reference = str(output_path) if output_path is not None else state_id
        payload = format_text(
            result, distribution=distribution, saved=output_path is not None,
            reference=reference, command_context=command_context,
        )
        if output_path is not None:
            payload = save_text_result(result, payload, state_id)
    else:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        raise ValueError("结果超过输出上限，请减小页大小或正文预算")
    return PreparedOutput(payload, encoded, state_id, state_raw)


def commit_state(prepared, state_dir):
    if prepared.state_raw is None:
        return None
    directory = Path(state_dir)
    path = directory / f"{prepared.state_id}.json"
    try:
        write_state_file(path, raw=prepared.state_raw)
    except OSError as exc:
        raise ValueError(f"无法写入状态目录 {directory}；请用 --state-dir 指定可写目录") from exc
    return prepared.state_id


def deliver_output(output_path, result, prepared, *, is_text, state_dir, command_context=None):
    try:
        atomic_write(output_path, prepared.encoded + b"\n", overwrite=False, temp_factory=tempfile.NamedTemporaryFile)
    except FileExistsError as exc:
        raise FileExistsError(exc.errno, exc.strerror, str(output_path)) from exc
    try:
        state_id = commit_state(prepared, state_dir)
        if prepared.state_raw is not None:
            companion = output_path.with_name(output_path.name + ".state.json")
            write_state_file(companion, prepared.state_raw)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    receipt = output_receipt(result, output_path, state_id=state_id, command_context=command_context)
    return format_output_receipt(receipt) if is_text else json.dumps(
        receipt, ensure_ascii=False, separators=(",", ":"))
