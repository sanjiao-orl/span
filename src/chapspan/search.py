"""Exact literal and paragraph-window queries over the visible snapshot."""

from bisect import bisect_left
from dataclasses import dataclass
from itertools import islice

from .model import MAX_BODY_CHARS, MAX_PAGE, MAX_SELECTIONS, Span, integer
from .reading import ReadSelection, annotate_reading, evenly_spaced, normalize_group_budget, read_selections


MAX_TERMS = 16
MAX_TERM_CHARS = 128
MAX_POSITIONS = 24
MAX_ALIAS_READ_SPANS = 3
QUERY_KEYS = {"all", "any", "exclude", "near", "offset", "distribution_offset", "limit"}


def resolve_find_examples(*, query_groups, examples, has_text, spread, hit_chapters=False):
    if examples is not None:
        return examples
    if query_groups or hit_chapters:
        return 0
    return 3 if has_text or spread else 0


@dataclass(frozen=True)
class QuerySpec:
    all_terms: tuple
    any_terms: tuple
    exclude: tuple
    near: int | None
    offset: int
    distribution_offset: int
    limit: int
    terms: tuple
    positive: tuple
    indexed_terms: tuple
    mode: str


def normalize_query(*, all_terms=(), any_terms=(), exclude=(), near=None, offset=0,
                    distribution_offset=0, limit=20):
    integer(offset, "匹配分页起点")
    integer(distribution_offset, "分布分页起点")
    integer(limit, "页大小", 1, MAX_PAGE)
    if near is not None:
        integer(near, "邻近段数", 1)
    all_terms, any_terms, exclude = tuple(all_terms), tuple(any_terms), tuple(exclude)
    positive = (*all_terms, *any_terms)
    if not positive:
        raise ValueError("至少需要一个正向查询词")
    if any(not isinstance(t, str) or not t or len(t) > MAX_TERM_CHARS for t in (*positive, *exclude)):
        raise ValueError(f"查询词长度须为 1 到 {MAX_TERM_CHARS} 个字符")
    terms = tuple(dict.fromkeys((*positive, *exclude)))
    integer(len(terms), "不同查询词数量", 1, MAX_TERMS)
    positive = tuple(dict.fromkeys(positive))
    all_terms, any_terms, exclude = (tuple(dict.fromkeys(group)) for group in (all_terms, any_terms, exclude))
    indexed_terms = tuple((index, term) for index, term in enumerate(terms) if term in positive)
    mode = "paragraphs" if near is not None else "literal" if len(positive) == 1 and not exclude else "chapter"
    return QuerySpec(all_terms, any_terms, exclude, near, offset, distribution_offset, limit,
                     terms, positive, indexed_terms, mode)


def literal_starts(text, term, start, end):
    while (position := text.find(term, start, end)) != -1:
        yield position
        start = position + len(term)


def minimal_windows(chapter, all_terms, any_terms, exclude, near):
    """Take the earliest possible end per left edge, then keep the latest left for that end."""
    ends = chapter.paragraph_ends
    text = chapter.body
    positions = {}

    def first_end(term, start, limit):
        # Paragraph starts only advance. Keep a later occurrence across windows
        # instead of rescanning the same sparse gap for every paragraph.
        position = positions.get(term)
        if position is None or 0 <= position < start:
            position = positions[term] = text.find(term, start)
        end = position + len(term)
        return limit + 1 if position == -1 or end > limit else end

    pending = None
    for index in range(len(ends)):
        start = ends[index - 1] if index else 0
        limit = ends[min(index + near, len(ends)) - 1]
        required_end = max((first_end(t, start, limit) for t in all_terms), default=start)
        one_end = min((first_end(t, start, limit) for t in any_terms), default=start)
        end = max(required_end, one_end)
        if end > limit:
            continue
        end = ends[bisect_left(ends, end)]
        if any(first_end(term, start, end) <= end for term in exclude):
            continue
        if pending is not None and end != pending[1]:
            yield pending
        pending = (start, end)
    if pending is not None:
        yield pending


def match_anchors(chapter, start, end, indexed_terms):
    anchors = []
    for _, term in indexed_terms:
        position = chapter.body.find(term, start, end)
        if position != -1:
            anchors.append(Span(chapter.number, position, position + len(term)).coordinate)
    return list(dict.fromkeys(anchors))


def match_details(chapter, start, end, indexed_terms, *, compact=False, chapter_match=False):
    span = Span(chapter.number, start, end).coordinate
    anchors = match_anchors(chapter, start, end, indexed_terms) if chapter_match else [span]
    result = {"chapter": chapter.number, "span": span, "read_spans": anchors}
    if compact:
        return result

    positions, total = [], 0
    for index, term in indexed_terms:
        total += chapter.body.count(term, start, end)
        remaining = MAX_POSITIONS - len(positions)
        for position in islice(literal_starts(chapter.body, term, start, end), remaining):
            positions.append({"term_index": index, "start": position, "end": position + len(term)})
    return {**result, "positions": positions, "positions_omitted": total - len(positions)}


def alias_delta_for(query):
    if (query.all_terms or query.exclude or query.near is not None or len(query.any_terms) != 2):
        return None
    first, second = query.any_terms
    return {
        "first_only": {"term": first, "chapter_count": 0, "read_spans": []},
        "both": {"chapter_count": 0},
        "second_only": {"term": second, "chapter_count": 0, "read_spans": []},
    }


def count_alias_chapter(delta, chapter, counts):
    first = delta["first_only"]
    second = delta["second_only"]
    if counts[first["term"]] and counts[second["term"]]:
        delta["both"]["chapter_count"] += 1
        return
    side = first if counts[first["term"]] else second
    side["chapter_count"] += 1
    if len(side["read_spans"]) < MAX_ALIAS_READ_SPANS:
        term = side["term"]
        start = chapter.body.find(term)
        side["read_spans"].append(Span(chapter.number, start, start + len(term)).coordinate)


def scan_queries(library, queries, *, compact=False, spread=False, examples=0, hit_chapters=False):
    records = [{"spec": query, "term_counts": dict.fromkeys(query.terms, 0), "matches": [],
                "distribution": [], "chapter_examples": [], "hit_chapters": [],
                "alias_delta": alias_delta_for(query),
                "match_count": 0, "chapter_count": 0}
               for query in queries]
    with_paragraphs = any(query.near is not None for query in queries)
    for chapter in library.chapters(with_paragraphs=with_paragraphs):
        for record in records:
            query = record["spec"]
            counts = {term: chapter.body.count(term) for term in query.terms}
            for term, count in counts.items():
                record["term_counts"][term] += count
            local_count = 0
            chapter_index = len(record["hit_chapters"]) if hit_chapters else record["chapter_count"]
            on_chapter_page = (not compact and
                               query.distribution_offset <= chapter_index < query.distribution_offset + query.limit)
            collect_anchor = (spread and examples) or (hit_chapters and on_chapter_page)
            anchor = None
            has_positive = (all(counts[t] for t in query.all_terms)
                            and (not query.any_terms or any(counts[t] for t in query.any_terms)))
            if has_positive and query.mode == "literal":
                term = query.positive[0]
                local_count = counts[term]
                if collect_anchor:
                    start = chapter.body.find(term)
                    anchor = Span(chapter.number, start, start + len(term)).coordinate
                if not hit_chapters:
                    first = max(0, query.offset - record["match_count"])
                    stop = min(local_count, query.offset + query.limit - record["match_count"])
                    if first < stop:
                        for start in islice(literal_starts(chapter.body, term, 0, len(chapter.body)), first, stop):
                            record["matches"].append(match_details(
                                chapter, start, start + len(term), query.indexed_terms, compact=compact))
            elif has_positive:
                windows = (minimal_windows(chapter, query.all_terms, query.any_terms, query.exclude, query.near)
                           if query.near is not None else
                           () if any(counts[t] for t in query.exclude) else ((0, len(chapter.body)),))
                for start, end in windows:
                    if collect_anchor and local_count == 0:
                        anchor = (match_anchors(chapter, start, end, query.indexed_terms)[0]
                                  if query.near is None else Span(chapter.number, start, end).coordinate)
                    if (not hit_chapters
                            and query.offset <= record["match_count"] + local_count < query.offset + query.limit):
                        record["matches"].append(match_details(
                            chapter, start, end, query.indexed_terms, compact=compact,
                            chapter_match=query.near is None))
                    local_count += 1
            if anchor is not None and spread and examples:
                record["chapter_examples"].append(anchor)
            if on_chapter_page and (local_count or not hit_chapters):
                row = {"chapter": chapter.number, "title": chapter.display_title,
                       "kind": chapter.kind, "match_count": local_count}
                if hit_chapters:
                    row["read_span"] = anchor
                record["distribution"].append(row)
            record["chapter_count"] += 1
            record["match_count"] += local_count
            if local_count:
                record["hit_chapters"].append(chapter.number)
                if record["alias_delta"] is not None:
                    count_alias_chapter(record["alias_delta"], chapter, counts)
    return records


def chapter_ranges(chapters):
    ranges = []
    for chapter in chapters:
        if ranges and chapter == ranges[-1]["last_chapter"] + 1:
            ranges[-1]["last_chapter"] = chapter
        else:
            ranges.append({"first_chapter": chapter, "last_chapter": chapter})
    return ranges


def query_result(record, *, compact=False, hit_chapters=False):
    query = record["spec"]
    chapters = record["hit_chapters"]
    ranges = chapter_ranges(chapters)
    shown_ranges = ranges if len(ranges) <= 6 else ranges[:3] + ranges[-3:]
    result = {
        "mode": query.mode,
        "near_paragraphs": query.near,
        "counting": "区分大小写，从左到右非重叠，逐章计数",
        "conditions": {"all": query.all_terms, "any": query.any_terms, "exclude": query.exclude},
        "term_counts": [{"term": term, "count": record["term_counts"][term]} for term in query.terms],
        "match_count": record["match_count"],
        "hit_chapter_count": len(chapters),
        "first_hit_chapter": chapters[0] if chapters else None,
        "last_hit_chapter": chapters[-1] if chapters else None,
        "hit_chapter_range_count": len(ranges),
        "hit_chapter_ranges": shown_ranges,
        "hit_chapter_ranges_omitted": len(ranges) - len(shown_ranges),
    }
    if not hit_chapters:
        next_offset = query.offset + query.limit
        result["matches"] = record["matches"]
        if record["matches"]:
            result["match_selection_origin"] = "match_order"
        result["next_offset"] = next_offset if next_offset < record["match_count"] else None
    if record["alias_delta"] is not None:
        result["alias_delta"] = record["alias_delta"]
    if not compact:
        chapter_count = len(chapters) if hit_chapters else record["chapter_count"]
        result["hit_chapters" if hit_chapters else "distribution"] = record["distribution"]
        next_offset = query.distribution_offset + query.limit
        result["next_distribution_offset"] = next_offset if next_offset < chapter_count else None
    return result


def select_examples(record, examples, spread):
    query = record["spec"]
    origin = "match_order"
    if spread:
        selected = evenly_spaced(record["chapter_examples"], examples)
        basis = "一处" if query.mode == "chapter" else "首处命中"
        if not record["chapter_examples"]:
            selection = "当前查询没有可展开的例段。"
        elif len(selected) == 1:
            origin = "first_match"
            selection = f"只取首个命中章的{basis}；单个例段不构成跨阶段对照。"
        else:
            origin = "even_hit_chapters"
            selection = f"从 {len(record['chapter_examples'])} 个命中章等距取例，每章取{basis}，各例分配独立篇幅。"
    else:
        selected = [match["read_spans"][0] for match in record["matches"][:examples]]
        selection = "按当前匹配页的顺序取例。"
    if query.mode == "chapter":
        selection += "章级例段从条件顺序中首个命中的词所在段落取材。"
    return selected, selection, origin if selected else None


def empty_examples():
    return {"fragments": [], "read_characters": 0, "complete": True, "next": None, "continuation": None}


def validate_chapter_view(queries, *, compact, spread, examples):
    if compact:
        raise ValueError("--hit-chapters 与 --compact 不能同用")
    if examples or spread:
        raise ValueError("--hit-chapters 只列回读位置，请去掉 --spread 和大于 0 的 --examples")
    if any(query.offset for query in queries):
        raise ValueError("--hit-chapters 使用 --distribution-offset 或各组的 distribution_offset 翻页")


def find_passages(library, *, all_terms=(), any_terms=(), exclude=(), near=None,
                  offset=0, distribution_offset=0, limit=20, examples=0, budget=4000, compact=False,
                  spread=False, hit_chapters=False):
    integer(examples, "展开例段数", 0, 10)
    integer(budget, "正文预算", 1, MAX_BODY_CHARS)
    if compact and distribution_offset:
        raise ValueError("查看分布分页时请去掉 --compact")
    query = normalize_query(all_terms=all_terms, any_terms=any_terms, exclude=exclude, near=near,
                            offset=offset, distribution_offset=distribution_offset, limit=limit)
    if hit_chapters:
        validate_chapter_view([query], compact=compact, spread=spread, examples=examples)
    record = scan_queries(library, [query], compact=compact, spread=spread, examples=examples,
                          hit_chapters=hit_chapters)[0]
    result = query_result(record, compact=compact, hit_chapters=hit_chapters)
    if examples:
        selected, selection, origin = select_examples(record, examples, spread)
        if spread:
            groups = [ReadSelection(spans=(span,), match_spans=(span,), group=index,
                                    selection_anchors=((span, origin),))
                      for index, span in enumerate(map(Span.parse, selected), 1)]
        else:
            spans = tuple(map(Span.parse, selected))
            groups = [ReadSelection(spans=spans, match_spans=spans,
                                    selection_anchors=tuple((span, origin) for span in spans))]
        gb, gb_source = normalize_group_budget(budget) if spread else (None, None)
        reading = read_selections(
            library, groups, whole_paragraphs=True, budget=budget, group_budget=gb,
            group_budget_source=gb_source,
        ) if selected else empty_examples()
        result["examples"] = {"selection": selection, "selected_spans": selected,
                              "fragment_count": (sum(len(group["fragments"]) for group in reading["groups"])
                                                 if "groups" in reading else len(reading["fragments"])), **reading}
        if origin:
            result["examples"]["selection_origin"] = origin
    return result


def find_many(library, query_groups, *, examples=0, budget=4000, compact=False, spread=False, limit=20,
              hit_chapters=False):
    integer(len(query_groups), "独立查询组数", 1, MAX_SELECTIONS)
    integer(examples, "每组展开例段数", 0, 10)
    integer(budget, "共享正文预算", 1, MAX_BODY_CHARS)
    integer(limit, "每组默认页大小", 1, MAX_PAGE)
    specs = []
    for value in query_groups:
        if not isinstance(value, dict) or value.keys() - QUERY_KEYS:
            raise ValueError("每组查询须为仅含 all、any、exclude、near、offset、distribution_offset、limit 的 JSON 对象")
        for key in ("all", "any", "exclude"):
            if key in value and (not isinstance(value[key], list)
                                 or not all(isinstance(term, str) for term in value[key])):
                raise ValueError(f"独立查询组的 {key} 须为 JSON 字符串数组")
        if compact and value.get("distribution_offset", 0):
            raise ValueError("查看分布分页时请去掉 --compact")
        specs.append(normalize_query(all_terms=value.get("all", ()), any_terms=value.get("any", ()),
                                     exclude=value.get("exclude", ()), near=value.get("near"),
                                     offset=value.get("offset", 0),
                                     distribution_offset=value.get("distribution_offset", 0),
                                     limit=value.get("limit", limit)))
    if hit_chapters:
        validate_chapter_view(specs, compact=compact, spread=spread, examples=examples)
    records = scan_queries(library, specs, compact=compact, spread=spread, examples=examples,
                           hit_chapters=hit_chapters)
    results = []
    for index, record in enumerate(records, 1):
        result = {"query": index, **query_result(record, compact=compact, hit_chapters=hit_chapters)}
        if examples:
            selected, selection, origin = select_examples(record, examples, spread)
            result["examples"] = {"selection": selection, "selected_spans": selected, "spread": spread}
            if origin:
                result["examples"]["selection_origin"] = origin
        results.append(result)
    if not examples:
        return {"queries": results}

    groups, group_refs, example_refs = [], [], []
    query_group_ids = {}
    for query, record, result in zip(range(1, len(records) + 1), records, results):
        selected = result["examples"]["selected_spans"]
        origin = result["examples"].get("selection_origin")
        ids = []
        if selected and spread:
            for example, span in enumerate(selected, 1):
                group = len(groups) + 1
                parsed = Span.parse(span)
                groups.append(ReadSelection(spans=(parsed,), match_spans=(parsed,), group=group,
                                            selection_anchors=((parsed, origin),)))
                reference = f"Q{query}E{example}"
                group_refs.append({"group": group, "reference": reference, "query": query})
                example_refs.append({"reference": reference, "query": query, "example": example,
                                     "span": span, "group": group, "selection_origin": origin})
                ids.append(group)
        elif selected:
            group = len(groups) + 1
            spans = tuple(map(Span.parse, selected))
            groups.append(ReadSelection(spans=spans, match_spans=spans, group=group,
                                        selection_anchors=tuple((span, origin) for span in spans)))
            group_refs.append({"group": group, "reference": f"Q{query}", "query": query})
            for example, span in enumerate(selected, 1):
                example_refs.append({"reference": f"Q{query}E{example}", "query": query, "example": example,
                                     "span": span, "group": group, "selection_origin": origin})
            ids.append(group)
        query_group_ids[query] = ids
    if groups:
        gb, gb_source = normalize_group_budget(budget)
        reading = read_selections(
            library, groups, whole_paragraphs=True, budget=budget, group_budget=gb, group_budget_source=gb_source,
        )
        annotate_reading(reading, group_refs, example_refs)
    else:
        reading = {"groups": [], "passages": [], "read_characters": 0, "complete": True,
                   "delivery": [], "continuation": None}
    for query, result in enumerate(results, 1):
        if not result.get("examples"):
            continue
        ids = query_group_ids[query]
        rows = [row for row in reading["delivery"] if row["group"] in ids]
        result["examples"].update({
            "groups": ids,
            "delivery": rows,
            "read_characters": sum(row["read_characters"] for row in rows),
            "complete": all(row["status"] == "complete" for row in rows),
            "fragment_count": sum(len(group["fragments"]) for group in reading["groups"] if group["group"] in ids),
        })
    return {"queries": results, "reading": reading}
