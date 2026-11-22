"""Read and paginate merged selections against a shared character budget."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .model import MAX_BODY_CHARS, MAX_SELECTIONS, Span, integer, parse_position, selections


SELECTION_ORIGIN_LABELS = {
    "first_match": "首个命中章取一处",
    "match_order": "按匹配页顺序",
    "even_hit_chapters": "按命中章等距",
    "hit_chapter_anchor": "命中章回读位置",
}


def selection_origins(values):
    """Keep only the closed vocabulary, without query details from a prior scope."""
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values
                              if isinstance(value, str) and value in SELECTION_ORIGIN_LABELS))


def context_labels(values):
    """Deduplicate non-empty context labels while preserving order."""
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


@dataclass
class Window:
    span: Span
    sources: list[Span]


@dataclass(frozen=True)
class ReadSelection:
    spans: tuple[Span, ...] = ()
    paragraphs: tuple[tuple[int, int, int], ...] = ()
    item_ids: tuple[int, ...] = ()
    match_spans: tuple[Span, ...] = ()
    resume: tuple[int, int] | None = None
    group: int = 1
    chapter_numbers: tuple[int, ...] = ()
    label: str | None = None
    selection_anchors: tuple[tuple[Span, str], ...] = ()
    context_anchors: tuple[tuple[Span, str], ...] = ()


def evenly_spaced(values, count):
    count = min(count, len(values))
    if count < 2:
        return values[:count]
    return [values[index * (len(values) - 1) // (count - 1)] for index in range(count)]


def normalize_group_budget(budget, group_budget=None):
    """Normalize user budget and optional group_budget into (group_budget, source)."""
    integer(budget, "正文预算", 1, MAX_BODY_CHARS)
    if group_budget is None:
        return budget, "auto"
    return integer(group_budget, "每组正文预算", 1, MAX_BODY_CHARS), "explicit"


def group_allowance(budget, group_budget, count):
    integer(group_budget, "每组正文预算", 1, MAX_BODY_CHARS)
    integer(budget, "分组读取的总正文预算", count, MAX_BODY_CHARS)
    return min(group_budget, budget // count)


def reclaim_budgets(demands, budget, group_budget):
    """Allocate a fair initial share per group, then reclaim unused surplus deterministically."""
    base = group_allowance(budget, group_budget, len(demands))
    caps = [min(limit, base) for limit in demands]
    remaining = budget - sum(caps)
    while remaining > 0:
        active = [index for index, limit in enumerate(demands) if caps[index] < min(limit, group_budget)]
        if not active:
            break
        share, extra = divmod(remaining, len(active))
        progressed = False
        for position, index in enumerate(active):
            add = share + (1 if position < extra else 0)
            if not add:
                continue
            target = min(demands[index], group_budget, caps[index] + add)
            if target > caps[index]:
                caps[index] = target
                progressed = True
        if not progressed:
            break
        remaining = budget - sum(caps)
    return caps


def span_value(value):
    if isinstance(value, Span):
        return value
    if not isinstance(value, str):
        raise ValueError("选区交付状态包含无效的字符范围")
    return Span.parse(value)


def covered_spans(target, reads):
    covered = []
    for span in sorted(set(reads)):
        if span.chapter != target.chapter or span.end <= target.start or span.start >= target.end:
            continue
        clipped = Span(target.chapter, max(target.start, span.start), min(target.end, span.end))
        if covered and covered[-1].end >= clipped.start:
            previous = covered[-1]
            covered[-1] = Span(target.chapter, previous.start, max(previous.end, clipped.end))
        else:
            covered.append(clipped)
    return covered


def delivery_row(group, target, requested, reads, chapter_title):
    covered = covered_spans(target, reads)
    delivered = sum(span.end - span.start for span in covered)
    position = target.start
    for span in covered:
        if span.start > position:
            break
        position = max(position, span.end)
    status = "complete" if position == target.end else "partial" if delivered else "not_started"
    return {
        "group": group,
        "chapter_title": chapter_title,
        "target_span": target.coordinate,
        "requested_spans": [span.coordinate for span in sorted(set(requested))],
        "read_spans": [span.coordinate for span in covered],
        "read_characters": delivered,
        "status": status,
        "next": None if status == "complete" else f"C{target.chapter:04d}:{position}",
    }


def normalize_delivery(rows):
    if not isinstance(rows, (list, tuple)) or len(rows) > MAX_SELECTIONS:
        raise ValueError("上一步结果包含无效的选区交付状态")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("上一步结果包含无效的选区交付状态")
        group = integer(row.get("group"), "组序号", 1, MAX_SELECTIONS)
        target = span_value(row.get("target_span"))
        requested_values, read_values = row.get("requested_spans", ()), row.get("read_spans", ())
        if not isinstance(requested_values, list) or not isinstance(read_values, list):
            raise ValueError("上一步结果包含无效的选区交付状态")
        requested = tuple(span_value(value) for value in requested_values)
        reads = tuple(span_value(value) for value in read_values)
        title = row.get("chapter_title")
        if not isinstance(title, str):
            title = target.coordinate
        normalized.append(delivery_row(group, target, requested, reads, title))
    return normalized


def merge_delivery(previous, current):
    rows = {}
    for row in (*normalize_delivery(previous), *normalize_delivery(current)):
        key = row["group"], row["target_span"]
        target = span_value(row["target_span"])
        requested = row["requested_spans"]
        reads = list(row["read_spans"])
        if key in rows:
            earlier = rows[key]
            requested = list(dict.fromkeys(earlier["requested_spans"] + requested))
            reads = earlier["read_spans"] + reads
        rows[key] = delivery_row(row["group"], target, tuple(map(span_value, requested)),
                                 tuple(map(span_value, reads)), row["chapter_title"])
    return [rows[key] for key in sorted(rows, key=lambda key: (key[0], span_value(key[1])))]


def sample_openings(library, count, *, budget, group_budget, after=None, group_budget_source="explicit"):
    integer(count, "抽读章数", 1, MAX_SELECTIONS)
    if after is not None:
        integer(after, "额外开篇段数")
    chapters = evenly_spaced(library.body_chapters(), count)
    if not chapters:
        raise ValueError("当前范围没有可抽读的正文章，可用 --chapter 指定可见的其他章节")
    chapter_ends = [library.chapter(number).paragraph_ends for number in chapters]
    desired = [min(group_budget, ends[-1] if after is None else ends[min(after, len(ends) - 1)])
               for ends in chapter_ends]
    allowances = reclaim_budgets(desired, budget, group_budget)
    groups = []
    for index, (number, ends, stop) in enumerate(zip(chapters, chapter_ends, allowances), 1):
        last_complete = bisect_right(ends, stop)
        end = ends[last_complete - 1] if last_complete else stop
        groups.append(ReadSelection(spans=(Span(number, 0, end),), group=index))
    return {"sample_chapters": chapters, **read_selections(
        library, groups, budget=budget, group_budget=group_budget, group_budget_source=group_budget_source,
    )}


def paragraph_span(chapter, first, last):
    ends = chapter.paragraph_ends
    if not ends:
        raise ValueError(f"C{chapter.number:04d} 没有正文段落，请另选章节")
    integer(first, "起始段", 1, len(ends))
    integer(last, "结束段", first, len(ends))
    return Span(chapter.number, ends[first - 2] if first > 1 else 0, ends[last - 1])


def expand(chapter, span, before, after, whole_paragraphs):
    if not (before or after or whole_paragraphs):
        return span
    ends = chapter.paragraph_ends
    if not ends:
        return span
    first = max(0, bisect_right(ends, span.start) - before)
    last = min(len(ends) - 1, bisect_left(ends, span.end) + after)
    return paragraph_span(chapter, first + 1, last + 1)


def merge(windows):
    merged = []
    for span, source in sorted(windows):
        previous = merged[-1] if merged else None
        if previous and previous.span.chapter == span.chapter and previous.span.end > span.start:
            previous.span = Span(span.chapter, previous.span.start, max(previous.span.end, span.end))
            previous.sources.append(source)
        else:
            merged.append(Window(span, [source]))
    return merged


def read_passages(library, spans=(), *, item_ids=(), paragraphs=(), chapter_numbers=(), match_spans=(),
                  before=0, after=0, whole_paragraphs=False, budget=4000, resume=None,
                  keep_selection=False, selection_anchors=(), context_anchors=()):
    integer(before, "向前扩读段数")
    integer(after, "向后扩读段数")
    integer(budget, "正文预算", 1, MAX_BODY_CHARS)
    spans, item_ids, paragraphs, chapter_numbers = map(tuple, (spans, item_ids, paragraphs, chapter_numbers))
    selections((*spans, *item_ids, *paragraphs, *chapter_numbers))
    selected_items = [library.item_selection(item_id) for item_id in dict.fromkeys(item_ids)]
    spans = (*spans, *(span for span, _ in selected_items))
    origins_by_span = {}
    for span, origin in selection_anchors:
        origins_by_span.setdefault(span, []).append(origin)
    for span, origins in selected_items:
        if origins:
            origins_by_span.setdefault(span, []).extend(origins)
    contexts_by_span = {}
    for span, label in context_anchors:
        if isinstance(label, str) and label:
            contexts_by_span.setdefault(span, []).append(label)
    chapters = {number: library.chapter(number) for number in
                {span.chapter for span in spans} | {number for number, _, _ in paragraphs} | set(chapter_numbers)}
    for number in chapter_numbers:
        if not chapters[number].body.strip():
            raise ValueError(f"C{number:04d} 没有正文，请另选章节")
        spans += (Span(number, 0, len(chapters[number].body)),)
    spans = (*spans, *(paragraph_span(chapters[number], first, last) for number, first, last in paragraphs))
    matches = set(match_spans)
    if not matches.issubset(spans):
        raise ValueError("匹配范围须来自本次选择的字符范围")
    windows = []
    expanded_with_origins = {}
    expanded_with_contexts = {}
    for span in sorted(set(spans)):
        chapter = chapters[span.chapter]
        chapter.check(span)
        target = expand(chapter, span, before, after, whole_paragraphs)
        windows.append((target, span))
        if span in origins_by_span:
            expanded_with_origins[span] = target
        if span in contexts_by_span:
            expanded_with_contexts[span] = target
    merged = merge(windows)

    if resume is not None:
        number, position = resume
        integer(number, "续读章序", 1)
        integer(position, "续读偏移")
        for index, window in enumerate(merged):
            span = window.span
            if span.chapter == number and span.start <= position < span.end:
                merged = merged[index:]
                break
        else:
            raise ValueError("续读位置不在本次选择范围内")

    tracked = list(merged)
    remaining, fragments, next_position = budget, [], None
    for index, window in enumerate(merged):
        target = window.span
        chapter = chapters[target.chapter]
        start = resume[1] if index == 0 and resume is not None else target.start
        end = min(target.end, start + remaining)
        if end < target.end:
            boundary = bisect_right(chapter.paragraph_ends, end) - 1
            if boundary >= 0 and chapter.paragraph_ends[boundary] > start:
                end = chapter.paragraph_ends[boundary]
            elif fragments:
                end = start
        if end > start:
            actual = Span(chapter.number, start, end)
            fragment = {
                "chapter_title": chapter.display_title,
                "source_body_line": chapter.source_body_line,
                "kind": chapter.kind,
                "requested_spans": [span.coordinate for span in sorted(window.sources)],
                "match_spans": [span.coordinate for span in sorted(matches.intersection(window.sources))],
                "target_span": target.coordinate,
                "read_span": actual.coordinate,
                "citation": actual.citation,
                "source_citation": actual.source_citation(library.scope.snapshot_id),
                "text": chapter.body[start:end],
            }
            origins = selection_origins([
                origin for source in window.sources if source in expanded_with_origins
                if expanded_with_origins[source].start < end and expanded_with_origins[source].end > start
                for origin in origins_by_span[source]
            ])
            if origins:
                fragment["selection_origins"] = origins
            contexts = context_labels([
                label for source in window.sources if source in expanded_with_contexts
                if expanded_with_contexts[source].start < end and expanded_with_contexts[source].end > start
                for label in contexts_by_span[source]
            ])
            if contexts:
                fragment["context_labels"] = contexts
            fragments.append(fragment)
            remaining -= end - start
        if end < target.end:
            next_position = f"C{target.chapter:04d}:{end}"
            break
    delivery = []
    for window in tracked:
        target = window.span
        reads = [Span.parse(fragment["read_span"]) for fragment in fragments
                 if fragment["target_span"] == target.coordinate]
        chapter = chapters[target.chapter]
        delivery.append(delivery_row(1, target, window.sources, reads, chapter.display_title))
    result = {"fragments": fragments, "read_characters": budget - remaining,
              "complete": next_position is None, "next": next_position, "delivery": delivery}
    if keep_selection and next_position is not None:
        pending_spans = {source for window in merged[index:] for source in window.sources}
        result["pending"] = {
            "spans": [span.coordinate for span in sorted(pending_spans)],
            "match_spans": [span.coordinate for span in sorted(matches & pending_spans)],
            "next": next_position,
        }
        anchors = [[span.coordinate, origin] for span in sorted(pending_spans)
                   for origin in selection_origins(origins_by_span.get(span))]
        if anchors:
            result["pending"]["selection_anchors"] = anchors
        pending_contexts = [[span.coordinate, label] for span in sorted(pending_spans)
                            for label in context_labels(contexts_by_span.get(span))]
        if pending_contexts:
            result["pending"]["context_anchors"] = pending_contexts
    return result


def group_passages(pages):
    """Keep each group's exact ranges and send overlapping text only once."""
    fragments = [(Span.parse(fragment["read_span"]), fragment)
                 for page in pages for fragment in page["fragments"]]
    passages = []
    current = None
    for span, fragment in sorted(fragments, key=lambda pair: pair[0]):
        text = fragment.pop("text")
        if current is not None and current.chapter == span.chapter and current.end > span.start:
            if span.end > current.end:
                passages[-1]["text"] += text[current.end - span.start:]
                current = Span(span.chapter, current.start, span.end)
                passages[-1]["span"] = current.coordinate
        else:
            current = span
            passages.append({"span": span.coordinate, "text": text})
        fragment["passage"] = len(passages)
    return passages


def _page_spans(page):
    return [Span.parse(fragment["read_span"]) for fragment in page["fragments"]]


def _merge_spans(spans):
    merged = []
    for span in sorted(spans):
        if merged and merged[-1].chapter == span.chapter and merged[-1].end >= span.start:
            if span.end > merged[-1].end:
                merged[-1] = Span(span.chapter, merged[-1].start, span.end)
        else:
            merged.append(span)
    return merged


def _union_chars(page_spans):
    return sum(span.end - span.start for span in _merge_spans(span for spans in page_spans for span in spans))


def _covered_run(merged_spans, chapter, position):
    for span in merged_spans:
        if span.chapter == chapter and span.start <= position < span.end:
            return span.end - position
        if span.chapter > chapter or (span.chapter == chapter and span.start > position):
            break
    return 0


def _grouped_pages(library, groups, *, before, after, whole_paragraphs, budget, group_budget):
    def read_group(group, group_cap):
        return read_passages(
            library, group.spans, paragraphs=group.paragraphs, item_ids=group.item_ids,
            chapter_numbers=group.chapter_numbers, match_spans=group.match_spans,
            resume=group.resume, budget=group_cap,
            before=before, after=after, whole_paragraphs=whole_paragraphs, keep_selection=True,
            selection_anchors=group.selection_anchors, context_anchors=group.context_anchors,
        )

    base = group_allowance(budget, group_budget, len(groups))
    budgets = [base] * len(groups)
    pages = [read_group(group, base) for group in groups]
    page_spans = [_page_spans(page) for page in pages]
    used = _union_chars(page_spans)

    def try_advance(index, extra_budget):
        nonlocal used
        cur_page = pages[index]
        cur_chars = cur_page["read_characters"]
        pending = cur_page.get("pending")
        if pending is None or cur_chars >= group_budget:
            return False
        other_merged = _merge_spans(span for pos, spans in enumerate(page_spans) if pos != index for span in spans)
        ch, pos = parse_position(pending["next"])
        free_ahead = _covered_run(other_merged, ch, pos)
        if extra_budget <= 0 and free_ahead <= 0:
            return False
        hi = min(group_budget, cur_chars + max(0, extra_budget) + free_ahead)
        if hi <= cur_chars:
            return False

        def evaluate_target(target):
            candidate = read_group(groups[index], target)
            if candidate["read_characters"] <= cur_chars:
                return None
            cand_spans = _page_spans(candidate)
            new_used = _union_chars([*page_spans[:index], cand_spans, *page_spans[index + 1:]])
            return target, candidate, cand_spans, new_used, new_used - used

        chosen = None
        top = evaluate_target(hi)
        if top is None:
            return False
        if top[4] <= extra_budget and top[3] <= budget:
            chosen = top
        else:
            lo, bound = cur_chars, hi - 1
            while lo < bound:
                mid = (lo + bound + 1) // 2
                probe = evaluate_target(mid)
                if probe is None:
                    lo = mid
                elif probe[4] <= extra_budget and probe[3] <= budget:
                    chosen = probe
                    lo = mid
                else:
                    bound = mid - 1
        if chosen is None:
            return False
        target, candidate, cand_spans, new_used, _ = chosen
        pages[index] = candidate
        page_spans[index] = cand_spans
        budgets[index] = max(budgets[index], target)
        used = new_used
        return True

    while True:
        active = [index for index, page in enumerate(pages)
                  if not page["complete"] and page["read_characters"] < group_budget]
        if not active:
            break
        remaining = budget - used
        progressed = False
        if remaining > 0:
            share, extra = divmod(remaining, len(active))
            for position, index in enumerate(active):
                add = share + (1 if position < extra else 0)
                if try_advance(index, add):
                    progressed = True
            if not progressed:
                for index in active:
                    if try_advance(index, budget - used):
                        progressed = True
        else:
            for index in active:
                if try_advance(index, 0):
                    progressed = True
        if not progressed:
            break
    return pages, budgets


def read_selections(library, groups, *, before=0, after=0, whole_paragraphs=False,
                    budget=4000, group_budget=None, group_budget_source=None,
                    prior_delivery=(), deferred_groups=()):
    integer(budget, "正文预算", 1, MAX_BODY_CHARS)
    if group_budget is None:
        if group_budget_source is not None:
            raise ValueError("共享预算阅读不应指定每组预算来源")
        effective_source = None
    else:
        effective_source = "explicit" if group_budget_source is None else group_budget_source
        if effective_source not in ("auto", "explicit"):
            raise ValueError("每组预算来源须为 auto 或 explicit")
    count = sum(len(group.spans) + len(group.paragraphs) + len(group.item_ids) + len(group.chapter_numbers)
                for group in groups)
    if count > MAX_SELECTIONS:
        raise ValueError(
            f"单次读取展开后合计最多 {MAX_SELECTIONS} 个选区（当前 {count} 个）；"
            "请拆成多次读取，或先读前一部分再用 --from-result 续读"
        )
    integer(count, "范围数", 1, MAX_SELECTIONS)
    if group_budget is not None:
        raw_pages, group_budgets = _grouped_pages(
            library, groups, before=before, after=after, whole_paragraphs=whole_paragraphs,
            budget=budget, group_budget=group_budget,
        )
    else:
        raw_pages = [
            read_passages(library, group.spans, paragraphs=group.paragraphs, item_ids=group.item_ids,
                          chapter_numbers=group.chapter_numbers, match_spans=group.match_spans,
                          resume=group.resume, budget=budget,
                          before=before, after=after, whole_paragraphs=whole_paragraphs, keep_selection=True,
                          selection_anchors=group.selection_anchors, context_anchors=group.context_anchors)
            for group in groups
        ]
        group_budgets = [budget] * len(groups)

    pages, pending, current_delivery = [], [], []
    for group, page, allowance in zip(groups, raw_pages, group_budgets):
        remainder = page.pop("pending", None)
        if remainder is not None:
            entry = {"group": group.group, **remainder}
            if group.label is not None:
                entry["label"] = group.label
            pending.append(entry)
        page_delivery = page.pop("delivery")
        for row in page_delivery:
            row["group"] = group.group
        current_delivery.extend(page_delivery)
        entry = {"group": group.group, "budget": allowance, **page}
        if group.label is not None:
            entry["label"] = group.label
        pages.append(entry)

    for group in deferred_groups:
        if group.resume is None:
            raise ValueError("保留待续组时缺少续读位置")
        entry = {
            "group": group.group,
            "spans": [span.coordinate for span in group.spans],
            "match_spans": [span.coordinate for span in group.match_spans],
            "next": f"C{group.resume[0]:04d}:{group.resume[1]}",
        }
        if group.label is not None:
            entry["label"] = group.label
        attach_selection_anchors(entry, group)
        attach_context_anchors(entry, group)
        pending.append(entry)
    pending.sort(key=lambda row: row["group"])
    delivery = merge_delivery(prior_delivery, current_delivery)
    if group_budget is None:
        result, = pages
        del result["group"], result["budget"]
    else:
        passages = group_passages(pages)
        result = {"groups": pages, "passages": passages,
                  "read_characters": sum(len(passage["text"]) for passage in passages), "complete": not pending}
    result["complete"] = not pending
    result["delivery"] = delivery
    result["continuation"] = None
    if pending:
        cont = {"groups": pending, "before": before, "after": after,
                "whole_paragraphs": whole_paragraphs, "budget": budget,
                "group_budget": group_budget, "delivery": delivery}
        if group_budget is not None:
            cont["group_budget_source"] = effective_source
        result["continuation"] = cont
    return result


def attach_selection_anchors(entry, group):
    spans = set(entry["spans"])
    anchors = [[span.coordinate, origin] for span, origin in group.selection_anchors
               if span.coordinate in spans]
    if anchors:
        entry["selection_anchors"] = anchors


def attach_context_anchors(entry, group):
    spans = set(entry["spans"])
    anchors = [[span.coordinate, label] for span, label in group.context_anchors
               if span.coordinate in spans]
    if anchors:
        entry["context_anchors"] = anchors


def annotate_reading(result, group_refs=(), example_refs=()):
    references = {row["group"]: row for row in group_refs}
    for rows in (result.get("delivery", []), result.get("groups", []),
                 result.get("continuation", {}).get("groups", [])
                 if result.get("continuation") is not None else []):
        for row in rows:
            reference = references.get(row["group"])
            if reference is not None:
                row["reference"] = reference["reference"]
                row["query"] = reference["query"]
    if group_refs:
        result["group_refs"] = [dict(row) for row in group_refs]
        result["example_refs"] = [dict(row) for row in example_refs]
    elif example_refs:
        result["example_refs"] = [dict(row) for row in example_refs]
    return result
