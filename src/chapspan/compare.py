"""Scoped interval comparison without preset search terms."""

import heapq
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict

from .model import MAX_BODY_CHARS, MAX_PAGE, Span, integer
from .reading import ReadSelection, normalize_group_budget, read_selections
from .sources import text_boundary

RANGE_NUMERIC = re.compile(r"^\s*(C?[0-9]+)\s*(?:-|–|—|\.\.|:|：|,)\s*(C?[0-9]+)\s*$")
HAN_CLAUSE = re.compile(r"[\u4e00-\u9fff]+")
DIRECTION_VALUES = ("all", "right", "left")
VIEW_VALUES = ("contrast", "base")
SORT_VALUES = ("total", "g2", "left", "right", "delta")
CONTRAST_POOL_LIMIT = 500
MIN_CONTRAST_G2 = 1.0
MAX_EXPLAIN_TERMS = 16
_SEEN_MASK = (1 << 20) - 1


def _resolve_endpoint_number(token, chapters_prefix, scope, *, label):
    token = token.strip()
    if not token:
        raise ValueError(f"{label}不能为空")
    if re.fullmatch(r"C[0-9]+", token):
        return integer(int(token[1:]), f"{label}章序", 1)
    if re.fullmatch(r"[0-9]+", token):
        return integer(int(token), f"{label}章序", 1)
    if chapters_prefix is None:
        raise ValueError(f"已有书库的{label}请使用内部章序或 C编号")
    num, _ = text_boundary(
        chapters_prefix, token, max_chapter=scope.as_of,
        first_chapter=scope.first_chapter, include_peripheral=scope.include_peripheral,
    )
    return num


def _resolve_endpoint(token, chapters_prefix, scope, *, label):
    num = _resolve_endpoint_number(token, chapters_prefix, scope, label=label)
    if num < scope.first_chapter or num > scope.as_of:
        raise ValueError(f"{label} C{num:04d} 超出当前允许范围 C{scope.first_chapter:04d}–C{scope.as_of:04d}")
    return num


def parse_interval_spec(spec, chapters_prefix, scope, *, label="比较区间"):
    """Parse a scoped chapter range like '1-100', 'C0001-C0100', '1..100', or '第一章:第五十章'."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError(f"请指定有效的{label}")
    raw = spec.strip()
    matched = RANGE_NUMERIC.fullmatch(raw)
    if matched:
        start = _resolve_endpoint(matched.group(1), chapters_prefix, scope, label=label)
        end = _resolve_endpoint(matched.group(2), chapters_prefix, scope, label=label)
    else:
        try:
            num = _resolve_endpoint_number(raw, chapters_prefix, scope, label=label)
        except ValueError as exact_error:
            for sep in ("..", "–", "—", ":", "：", ",", "-"):
                if raw.count(sep) == 1:
                    left_part, right_part = raw.split(sep)
                    start = _resolve_endpoint(left_part, chapters_prefix, scope, label=label)
                    end = _resolve_endpoint(right_part, chapters_prefix, scope, label=label)
                    break
            else:
                raise exact_error
        else:
            if num < scope.first_chapter or num > scope.as_of:
                raise ValueError(f"{label} C{num:04d} 超出当前允许范围 C{scope.first_chapter:04d}–C{scope.as_of:04d}")
            start = end = num
    if start > end:
        raise ValueError(f"{label}起止章序颠倒：C{start:04d} > C{end:04d}")
    return start, end


def _g2_contrast(c1, c2, n1, n2):
    """Dunning log-likelihood ratio G^2 normalized for asymmetric interval lengths."""
    total_c = c1 + c2
    total_n = n1 + n2
    if total_c <= 0 or total_n <= 0 or n1 <= 0 or n2 <= 0:
        return 0.0
    p = total_c / total_n
    e1 = n1 * p
    e2 = n2 * p
    g2 = 0.0
    for obs, exp in ((c1, e1), (c2, e2), (n1 - c1, n1 - e1), (n2 - c2, n2 - e2)):
        if obs > 0 and exp > 0:
            g2 += obs * math.log(obs / exp)
    return 2.0 * g2


def _matches_direction(lc, rc, n1, n2, direction, *, allow_equal=False):
    r1 = lc / n1
    r2 = rc / n2
    if direction == "all":
        return True if allow_equal else r1 != r2
    return r2 > r1 if direction == "right" else r1 > r2


def _compute_trend(raw_lc, raw_rc, rate_left, rate_right):
    if raw_lc == 0:
        return "right_only"
    if raw_rc == 0:
        return "left_only"
    if rate_right > rate_left:
        return "up"
    if rate_right < rate_left:
        return "down"
    return "flat"


def _extract_clauses(chapter, min_len):
    return [(match.start(), match.group(0))
            for match in HAN_CLAUSE.finditer(chapter.body)
            if len(match.group(0)) >= min_len]


def _count_min_len(chapter_clauses, min_len, min_support, *, use_seen_filter):
    counts = Counter()
    seen = bytearray(_SEEN_MASK + 1) if (use_seen_filter and min_support >= 2) else None
    for _, clauses in chapter_clauses:
        for _, text in clauses:
            limit = len(text) - min_len + 1
            for i in range(limit):
                gram = text[i:i + min_len]
                if seen is not None:
                    slot = hash(gram) & _SEEN_MASK
                    if not seen[slot]:
                        seen[slot] = 1
                        continue
                counts[gram] += 1
    return counts


def _recount_exact_grams(chapter_clauses, min_len, candidates):
    counts = Counter()
    for _, clauses in chapter_clauses:
        for _, text in clauses:
            limit = len(text) - min_len + 1
            for i in range(limit):
                gram = text[i:i + min_len]
                if gram in candidates:
                    counts[gram] += 1
    return counts


def _count_extended_level(chapter_clauses, length, valid_substrings):
    counts = Counter()
    prev_len = length - 1
    for _, clauses in chapter_clauses:
        for _, text in clauses:
            limit = len(text) - length + 1
            for i in range(limit):
                if text[i:i + prev_len] in valid_substrings and text[i + 1:i + length] in valid_substrings:
                    counts[text[i:i + length]] += 1
    return counts


def _prune_subsumed_with_reasons(term_counts, min_support):
    """Suppress shorter substrings absorbed by a superstring with >= 85% of their count."""
    by_len = defaultdict(dict)
    for term, pair in term_counts.items():
        by_len[len(term)][term] = pair
    lengths = sorted(by_len)
    if len(lengths) < 2:
        return {term: counts for term, counts in term_counts.items() if max(counts) >= min_support}, {}

    suppressed = {}
    for length in lengths:
        longer_map = by_len.get(length + 1)
        if not longer_map:
            continue
        shorter_map = by_len[length]
        for super_term, (slc, src) in longer_map.items():
            stotal = slc + src
            for term in (super_term[:-1], super_term[1:]):
                if term in suppressed:
                    continue
                pair = shorter_map.get(term)
                if pair is None:
                    continue
                lc, rc = pair
                total = lc + rc
                if total <= 0 or stotal < 0.85 * total:
                    continue
                if (lc < min_support or slc >= 0.75 * lc) and (rc < min_support or src >= 0.75 * rc):
                    suppressed[term] = super_term
    promoted = {term: counts for term, counts in term_counts.items()
                if term not in suppressed and max(counts) >= min_support}
    return promoted, suppressed


def _prune_subsumed_fragments(term_counts, min_support):
    """Suppress shorter substrings absorbed by a superstring with >= 85% of their count."""
    promoted, _ = _prune_subsumed_with_reasons(term_counts, min_support)
    return promoted


def _overlaps(covered_sorted, start, end):
    """Check whether [start, end) overlaps any interval in sorted disjoint list."""
    if not covered_sorted:
        return False
    idx = bisect_right(covered_sorted, (start, end))
    if idx > 0 and covered_sorted[idx - 1][1] > start:
        return True
    if idx < len(covered_sorted) and covered_sorted[idx][0] < end:
        return True
    return False


def _add_covered(covered_sorted, spans):
    if not spans:
        return covered_sorted
    merged = []
    for start, end in sorted((*covered_sorted, *spans)):
        if merged and merged[-1][1] >= start:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _pick_representative_spans(chapter_hits, max_picks=2):
    if not chapter_hits:
        return []
    chapters = sorted(chapter_hits)
    if len(chapters) <= max_picks:
        chosen_chapters = chapters
    else:
        chosen_chapters = [chapters[0], chapters[-1]] if max_picks == 2 else [chapters[0]]
    spans = []
    for ch in chosen_chapters[:max_picks]:
        start, end = chapter_hits[ch][0]
        spans.append(Span(ch, start, end).coordinate)
    return spans


def _localize_terms(terms, left_clauses, right_clauses, min_len):
    left_occ = defaultdict(lambda: defaultdict(list))
    right_occ = defaultdict(lambda: defaultdict(list))
    chapter_terms = defaultdict(set)
    if not terms:
        return left_occ, right_occ, chapter_terms
    terms_by_len = defaultdict(set)
    for term in terms:
        terms_by_len[len(term)].add(term)
    lengths_desc = sorted(terms_by_len.items(), reverse=True)
    prefixes = {term[:min_len] for term in terms}
    for chap_clauses, occ_map in ((left_clauses, left_occ), (right_clauses, right_occ)):
        for chap_num, clauses in chap_clauses:
            for base_offset, text in clauses:
                n = len(text)
                for i in range(n - min_len + 1):
                    if text[i:i + min_len] not in prefixes:
                        continue
                    for length, words in lengths_desc:
                        if i + length <= n:
                            gram = text[i:i + length]
                            if gram in words:
                                occ_map[gram][chap_num].append((base_offset + i, base_offset + i + length))
                                chapter_terms[chap_num].add(gram)
    return left_occ, right_occ, chapter_terms


def _count_hit_chapters(terms, chap_clauses, min_len):
    if not terms:
        return Counter()
    terms_by_len = defaultdict(set)
    for term in terms:
        terms_by_len[len(term)].add(term)
    lengths_desc = sorted(terms_by_len.items(), reverse=True)
    prefixes = {term[:min_len] for term in terms}
    chaps = Counter()
    for _, clauses in chap_clauses:
        seen_in_chapter = set()
        for _, text in clauses:
            n = len(text)
            for i in range(n - min_len + 1):
                if text[i:i + min_len] not in prefixes:
                    continue
                for length, words in lengths_desc:
                    if i + length <= n:
                        gram = text[i:i + length]
                        if gram in words:
                            seen_in_chapter.add(gram)
        if seen_in_chapter:
            chaps.update(seen_in_chapter)
    return chaps


def _build_candidate_entry(rank, term, raw_l_occ, raw_r_occ, left_chars, right_chars,
                           left_chap_total, right_chap_total, *, score, selection=None,
                           uncovered_left=None, uncovered_right=None, total_count=None):
    raw_lc = sum(len(spans) for spans in raw_l_occ.values())
    raw_rc = sum(len(spans) for spans in raw_r_occ.values())
    raw_l_chaps = len(raw_l_occ)
    raw_r_chaps = len(raw_r_occ)
    rate_left = raw_lc * 10_000 / left_chars
    rate_right = raw_rc * 10_000 / right_chars
    trend = _compute_trend(raw_lc, raw_rc, rate_left, rate_right)
    l_spans = _pick_representative_spans(raw_l_occ, max_picks=2)
    r_spans = _pick_representative_spans(raw_r_occ, max_picks=2)
    combined_read = []
    if l_spans:
        combined_read.append(l_spans[0])
    if r_spans:
        combined_read.append(r_spans[0])
    left_entry = {
        "count": raw_lc,
        "rate_per_10k": round(rate_left, 4),
        "hit_chapters": raw_l_chaps,
        "chapter_ratio": round(raw_l_chaps / left_chap_total, 4),
        "read_spans": l_spans,
    }
    right_entry = {
        "count": raw_rc,
        "rate_per_10k": round(rate_right, 4),
        "hit_chapters": raw_r_chaps,
        "chapter_ratio": round(raw_r_chaps / right_chap_total, 4),
        "read_spans": r_spans,
    }
    if uncovered_left is not None:
        left_entry["uncovered_count"] = uncovered_left[0]
        left_entry["uncovered_chapters"] = uncovered_left[1]
    if uncovered_right is not None:
        right_entry["uncovered_count"] = uncovered_right[0]
        right_entry["uncovered_chapters"] = uncovered_right[1]
    entry = {
        "rank": rank,
        "term": term,
        "trend": trend,
        "g2_score": round(score, 4),
        "rate_delta_per_10k": round(rate_right - rate_left, 4),
        "log2_rate_ratio": round(math.log2((rate_right + 0.05) / (rate_left + 0.05)), 4),
    }
    if total_count is not None:
        entry["total_count"] = total_count
    if selection is not None:
        entry["selection"] = selection
    entry["left"] = left_entry
    entry["right"] = right_entry
    entry["read_spans"] = combined_read
    return entry


def _base_sort_key(sort, left_chars, right_chars):
    def key_fn(item):
        term, lc, rc, score = item
        tie = (-max(lc, rc), -len(term), term)
        if sort == "total":
            return (-(lc + rc), *tie)
        if sort == "g2":
            return (-score, *tie)
        if sort == "left":
            return (-lc, *tie)
        if sort == "right":
            return (-rc, *tie)
        delta = abs(rc * 10_000 / right_chars - lc * 10_000 / left_chars)
        return (-delta, *tie)

    return key_fn


def _select_base_candidates(all_counts, left_clauses, right_clauses, left_chars, right_chars,
                            left_chap_count, right_chap_count, *, min_support, min_chapters,
                            min_len, direction, sort, contains, offset, limit):
    filtered = {}
    for term, (lc, rc) in all_counts.items():
        if contains is not None and contains not in term:
            continue
        if not _matches_direction(lc, rc, left_chars, right_chars, direction, allow_equal=True):
            continue
        filtered[term] = (lc, rc)
    if min_chapters > 1 and filtered:
        l_chaps = _count_hit_chapters(filtered, left_clauses, min_len)
        r_chaps = _count_hit_chapters(filtered, right_clauses, min_len)
        filtered = {
            term: (lc, rc) for term, (lc, rc) in filtered.items()
            if (lc >= min_support and l_chaps.get(term, 0) >= min_chapters)
            or (rc >= min_support and r_chaps.get(term, 0) >= min_chapters)
        }
    items = [(term, lc, rc, _g2_contrast(lc, rc, left_chars, right_chars))
             for term, (lc, rc) in filtered.items()]
    items.sort(key=_base_sort_key(sort, left_chars, right_chars))
    total_candidates = len(items)
    page_items = items[offset:offset + limit]
    next_offset = offset + limit if offset + limit < total_candidates else None
    left_occ, right_occ, _ = _localize_terms([row[0] for row in page_items], left_clauses, right_clauses, min_len)
    candidates = [
        _build_candidate_entry(
            offset + idx, term, left_occ.get(term, {}), right_occ.get(term, {}),
            left_chars, right_chars, left_chap_count, right_chap_count,
            score=score, total_count=lc + rc,
        )
        for idx, (term, lc, rc, score) in enumerate(page_items, 1)
    ]
    return candidates, total_candidates, next_offset


def _combined_rep_spans(raw_l_occ, raw_r_occ):
    l_spans = _pick_representative_spans(raw_l_occ, max_picks=2)
    r_spans = _pick_representative_spans(raw_r_occ, max_picks=2)
    combined = []
    if l_spans:
        combined.append(l_spans[0])
    if r_spans:
        combined.append(r_spans[0])
    return combined


def _format_g2_below_threshold(score):
    threshold = f"{MIN_CONTRAST_G2:g}"
    for digits in (4, 6, 8):
        text = f"{score:.{digits}f}"
        if float(text) < MIN_CONTRAST_G2:
            return f"G2 {text} < {threshold}"
    return f"G2 略低于门槛 {threshold}"


def _uncovered_snapshot(left_occ, right_occ, term, covered, left_chars, right_chars):
    lc = rc = l_chaps = r_chaps = 0
    for ch, spans in left_occ.get(term, {}).items():
        cov = covered.get(ch, ())
        kept = sum(1 for sp in spans if not _overlaps(cov, sp[0], sp[1])) if cov else len(spans)
        if kept:
            l_chaps += 1
            lc += kept
    for ch, spans in right_occ.get(term, {}).items():
        cov = covered.get(ch, ())
        kept = sum(1 for sp in spans if not _overlaps(cov, sp[0], sp[1])) if cov else len(spans)
        if kept:
            r_chaps += 1
            rc += kept
    exact_g2 = _g2_contrast(lc, rc, left_chars, right_chars)
    return {
        "left_count": lc,
        "right_count": rc,
        "left_chapters": l_chaps,
        "right_chapters": r_chaps,
        "g2_score": round(exact_g2, 4),
    }, exact_g2


def _explain_contrast_terms(explain_terms, *, min_support, min_chapters, min_len, max_len, direction,
                            offset, limit, left_chars, right_chars, left_clauses, right_clauses,
                            pool_terms, pool_left_occ, pool_right_occ, trace):
    valid_explain = [t for t in explain_terms if HAN_CLAUSE.fullmatch(t) and min_len <= len(t) <= max_len]
    rep_terms = {
        term: trace[term]["representative_term"]
        for term in valid_explain
        if trace[term]["reason"] == "subsumed_by_superstring"
    }
    masker_terms = {
        masker
        for entry in trace.values()
        for masker in entry.get("masked_by", ())
    }
    extra_terms = (set(valid_explain) | set(rep_terms.values()) | masker_terms) - pool_terms
    ex_left_occ, ex_right_occ, _ = _localize_terms(extra_terms, left_clauses, right_clauses, min_len)

    def get_occ(term):
        if term in pool_terms:
            return pool_left_occ.get(term, {}), pool_right_occ.get(term, {})
        return ex_left_occ.get(term, {}), ex_right_occ.get(term, {})

    explanations = []
    for term in explain_terms:
        if not HAN_CLAUSE.fullmatch(term):
            explanations.append({
                "term": term,
                "stage": "extract",
                "reason": "not_extracted",
                "note": f"当前仅抽取连续汉字串（{min_len}–{max_len} 字），该词含非汉字字符；已知词请用 find",
            })
            continue
        if len(term) < min_len or len(term) > max_len:
            explanations.append({
                "term": term,
                "stage": "extract",
                "reason": "not_extracted",
                "note": f"该词长度 {len(term)} 不在当前提取词长 {min_len}–{max_len} 范围内；可用 find 或调整 --min-len/--max-len",
            })
            continue

        raw_l_occ, raw_r_occ = get_occ(term)
        raw_lc = sum(len(s) for s in raw_l_occ.values())
        raw_rc = sum(len(s) for s in raw_r_occ.values())
        raw_l_chaps = len(raw_l_occ)
        raw_r_chaps = len(raw_r_occ)
        raw_g2 = round(_g2_contrast(raw_lc, raw_rc, left_chars, right_chars), 4)
        raw_block = {
            "left_count": raw_lc,
            "right_count": raw_rc,
            "left_chapters": raw_l_chaps,
            "right_chapters": raw_r_chaps,
            "g2_score": raw_g2,
            "left_read_spans": _pick_representative_spans(raw_l_occ, max_picks=2),
            "right_read_spans": _pick_representative_spans(raw_r_occ, max_picks=2),
        }

        outcome = trace[term]
        reason = outcome["reason"]
        entry = {
            "term": term,
            "stage": outcome["stage"],
            "reason": reason,
            "raw": raw_block,
        }
        if reason == "below_min_support":
            entry["note"] = (
                "当前比较区间原文零出现"
                if raw_lc == 0 and raw_rc == 0
                else f"左右出现次数（左 {raw_lc} / 右 {raw_rc}）均低于 --min-support {min_support}"
            )
        elif reason == "subsumed_by_superstring":
            rep = outcome["representative_term"]
            rep_l_occ, rep_r_occ = get_occ(rep)
            rep_lc = sum(len(s) for s in rep_l_occ.values())
            rep_rc = sum(len(s) for s in rep_r_occ.values())
            entry["representative_term"] = rep
            entry["representative_raw"] = {"left_count": rep_lc, "right_count": rep_rc}
            entry["representative_read_spans"] = _combined_rep_spans(rep_l_occ, rep_r_occ)
            entry["note"] = f"被高覆盖长串「{rep}」（左 {rep_lc} / 右 {rep_rc}）包含抑制"
        elif reason == "pool_truncated":
            pool_rank = outcome["scored_pool_rank"]
            entry["scored_pool_rank"] = pool_rank
            entry["pool_limit"] = CONTRAST_POOL_LIMIT
            entry["note"] = f"初筛 G2 排名第 {pool_rank}，超过当前候选池上限 {CONTRAST_POOL_LIMIT}"
        elif reason == "below_min_chapters":
            entry["note"] = f"强侧命中章数（左 {raw_l_chaps} / 右 {raw_r_chaps}）均低于 --min-chapters {min_chapters}"
        elif reason in ("masked_below_threshold", "masked_below_min_g2"):
            maskers = outcome["masked_by"]
            masker_spans = []
            for masker in maskers:
                ml, mr = get_occ(masker)
                masker_spans.extend(_combined_rep_spans(ml, mr))
            entry["uncovered"] = outcome["uncovered"]
            entry["masked_by"] = maskers
            entry["masked_by_read_spans"] = list(dict.fromkeys(masker_spans))
            joined = "、".join(f"「{m}」" for m in maskers)
            entry["note"] = (
                f"被已选高分词 {joined} 重叠掩蔽后，剩余支持不足以通过门槛"
                if reason == "masked_below_threshold"
                else f"被已选高分词 {joined} 重叠掩蔽后，剩余 {_format_g2_below_threshold(outcome['uncovered_g2'])}"
            )
        elif reason == "below_min_g2":
            entry["note"] = f"原文两侧分布差异不显著（{_format_g2_below_threshold(outcome['g2_score'])}）"
        elif reason == "direction_filtered":
            entry["trend"] = outcome["trend"]
            entry["note"] = f"该词方向（{outcome['trend']}）不符合 --direction {direction}"
        elif reason == "before_current_offset":
            entry["candidate_rank"] = outcome["candidate_rank"]
            entry["note"] = f"已入选第 {outcome['candidate_rank']} 名，位于当前 --offset {offset} 之前"
        elif reason == "selected":
            entry["candidate_rank"] = outcome["candidate_rank"]
            entry["note"] = f"已入选当前页第 {outcome['candidate_rank']} 名"
        elif reason == "beyond_page_limit":
            if "candidate_rank" in outcome:
                entry["candidate_rank"] = outcome["candidate_rank"]
                entry["note"] = f"排在第 {outcome['candidate_rank']} 名，超过本页显示范围（offset {offset} + limit {limit}）"
            else:
                entry["uncovered"] = outcome["uncovered"]
                entry["note"] = f"在当前返回范围（offset {offset} + limit {limit}）内尚未轮到，后续仍受重叠屏蔽影响"
        explanations.append(entry)
    return explanations


def _select_contrast_candidates(all_counts, left_clauses, right_clauses, left_chars, right_chars,
                                left_chap_count, right_chap_count, *, min_support, min_chapters,
                                min_len, max_len, direction, offset, limit, explain_terms=()):
    explain_valid_terms = {
        term for term in explain_terms
        if HAN_CLAUSE.fullmatch(term) and min_len <= len(term) <= max_len
    }
    promoted, suppressed = _prune_subsumed_with_reasons(all_counts, min_support)
    trace = {}
    for term in explain_valid_terms:
        if term in suppressed:
            rep = suppressed[term]
            while rep in suppressed:
                rep = suppressed[rep]
            trace[term] = {"stage": "subsumption", "reason": "subsumed_by_superstring", "representative_term": rep}
        elif term not in promoted:
            trace[term] = {"stage": "support", "reason": "below_min_support"}

    scored_pool = []
    for term, (lc, rc) in promoted.items():
        score = _g2_contrast(lc, rc, left_chars, right_chars)
        if score < MIN_CONTRAST_G2:
            if term in explain_valid_terms:
                trace[term] = {"stage": "contrast_score", "reason": "below_min_g2", "g2_score": score}
            continue
        scored_pool.append((score, max(lc, rc), len(term), term))
    scored_pool.sort(key=lambda row: (-row[0], -row[1], -row[2], row[3]))
    scored_pool_count = len(scored_pool)
    pool_truncated = scored_pool_count > CONTRAST_POOL_LIMIT
    pool_terms = {row[3] for row in scored_pool[:CONTRAST_POOL_LIMIT]}
    if explain_valid_terms and pool_truncated:
        for rank, row in enumerate(scored_pool[CONTRAST_POOL_LIMIT:], CONTRAST_POOL_LIMIT + 1):
            if row[3] in explain_valid_terms:
                trace[row[3]] = {"stage": "pool_cap", "reason": "pool_truncated", "scored_pool_rank": rank}
    # Retain promoted substrings of high-contrast compounds so masking can expose hidden root shifts.
    for term in list(pool_terms):
        n = len(term)
        for start in range(n):
            for end in range(start + min_len, n + 1):
                sub = term[start:end]
                if sub in promoted:
                    pool_terms.add(sub)

    explain_pool_terms = explain_valid_terms & pool_terms
    overlapped_by = defaultdict(list)

    all_candidates = []
    left_occ = right_occ = {}
    if pool_terms:
        left_occ, right_occ, chapter_terms = _localize_terms(pool_terms, left_clauses, right_clauses, min_len)

        def evaluate(term, covered, *, enforce_filters=True):
            l_filtered = {}
            r_filtered = {}
            lc = rc = 0
            for ch, spans in left_occ.get(term, {}).items():
                cov = covered.get(ch, ())
                kept = [sp for sp in spans if not _overlaps(cov, sp[0], sp[1])] if cov else spans
                if kept:
                    l_filtered[ch] = kept
                    lc += len(kept)
            for ch, spans in right_occ.get(term, {}).items():
                cov = covered.get(ch, ())
                kept = [sp for sp in spans if not _overlaps(cov, sp[0], sp[1])] if cov else spans
                if kept:
                    r_filtered[ch] = kept
                    rc += len(kept)
            l_chaps, r_chaps = len(l_filtered), len(r_filtered)
            ok_left = lc >= min_support and l_chaps >= min_chapters
            ok_right = rc >= min_support and r_chaps >= min_chapters
            if not (ok_left or ok_right):
                return None
            score = _g2_contrast(lc, rc, left_chars, right_chars)
            if enforce_filters and score < MIN_CONTRAST_G2:
                return None
            return score, lc, rc, l_filtered, r_filtered

        covered_spans = {}
        heap = []
        latest_epoch = {}
        pending_terms = set()
        for term in pool_terms:
            ev = evaluate(term, covered_spans, enforce_filters=False)
            if ev is not None:
                score, lc, rc, _, _ = ev
                latest_epoch[term] = 0
                pending_terms.add(term)
                heapq.heappush(heap, (-score, -max(lc, rc), -len(term), term, 0))
            elif term in explain_pool_terms:
                trace[term] = {"stage": "chapters", "reason": "below_min_chapters"}

        epoch = 0
        target_count = offset + limit + 1
        while heap and len(all_candidates) < target_count:
            neg_score, _, _, term, checked_epoch = heapq.heappop(heap)
            if term not in pending_terms or checked_epoch != latest_epoch.get(term):
                continue
            ev = evaluate(term, covered_spans, enforce_filters=True)
            if ev is None:
                pending_terms.discard(term)
                if term in explain_pool_terms:
                    snap, exact_g2 = _uncovered_snapshot(
                        left_occ, right_occ, term, covered_spans, left_chars, right_chars,
                    )
                    maskers = list(overlapped_by.get(term, ()))
                    if maskers:
                        ok_l = snap["left_count"] >= min_support and snap["left_chapters"] >= min_chapters
                        ok_r = snap["right_count"] >= min_support and snap["right_chapters"] >= min_chapters
                        reason = "masked_below_min_g2" if (ok_l or ok_r) else "masked_below_threshold"
                        trace[term] = {
                            "stage": "greedy_mask", "reason": reason,
                            "uncovered": snap, "uncovered_g2": exact_g2, "masked_by": maskers,
                        }
                    else:
                        lc_raw, rc_raw = promoted[term]
                        trace[term] = {
                            "stage": "contrast_score",
                            "reason": "below_min_g2",
                            "g2_score": _g2_contrast(lc_raw, rc_raw, left_chars, right_chars),
                        }
                continue
            new_score, lc, rc, l_filtered, r_filtered = ev

            # Accept candidate, mask its spans, and refresh all overlapping pending terms immediately.
            pending_terms.discard(term)
            epoch += 1
            newly_masked = defaultdict(list)
            for ch, spans in l_filtered.items():
                covered_spans[ch] = _add_covered(covered_spans.get(ch, ()), spans)
                newly_masked[ch].extend(spans)
            for ch, spans in r_filtered.items():
                covered_spans[ch] = _add_covered(covered_spans.get(ch, ()), spans)
                newly_masked[ch].extend(spans)

            affected_terms = set()
            for ch, raw_spans in newly_masked.items():
                merged_new = _add_covered((), raw_spans)
                for other in pending_terms & chapter_terms[ch]:
                    if other in affected_terms:
                        continue
                    for side_occ in (left_occ.get(other), right_occ.get(other)):
                        spans = side_occ.get(ch) if side_occ else None
                        if spans and any(_overlaps(merged_new, sp[0], sp[1]) for sp in spans):
                            affected_terms.add(other)
                            break
            for other in affected_terms & explain_pool_terms:
                if term not in overlapped_by[other]:
                    overlapped_by[other].append(term)
            for other in affected_terms:
                other_ev = evaluate(other, covered_spans, enforce_filters=False)
                if other_ev is None:
                    pending_terms.discard(other)
                    if other in explain_pool_terms:
                        trace[other] = {
                            "stage": "greedy_mask",
                            "reason": "masked_below_threshold",
                            "uncovered": _uncovered_snapshot(
                                left_occ, right_occ, other, covered_spans, left_chars, right_chars
                            )[0],
                            "masked_by": list(overlapped_by.get(other, ())),
                        }
                else:
                    other_score, other_lc, other_rc, _, _ = other_ev
                    latest_epoch[other] = epoch
                    heapq.heappush(heap, (-other_score, -max(other_lc, other_rc), -len(other), other, epoch))

            raw_l_occ = left_occ.get(term, {})
            raw_r_occ = right_occ.get(term, {})
            raw_lc = sum(len(spans) for spans in raw_l_occ.values())
            raw_rc = sum(len(spans) for spans in raw_r_occ.values())
            if not _matches_direction(raw_lc, raw_rc, left_chars, right_chars, direction):
                if term in explain_pool_terms:
                    trend = _compute_trend(raw_lc, raw_rc, raw_lc * 10_000 / left_chars, raw_rc * 10_000 / right_chars)
                    trace[term] = {"stage": "direction", "reason": "direction_filtered", "trend": trend}
                continue

            rank = len(all_candidates) + 1
            if term in explain_pool_terms:
                if rank <= offset:
                    trace[term] = {"stage": "paging", "reason": "before_current_offset", "candidate_rank": rank}
                elif rank <= offset + limit:
                    trace[term] = {"stage": "selected", "reason": "selected", "candidate_rank": rank}
                else:
                    trace[term] = {"stage": "paging", "reason": "beyond_page_limit", "candidate_rank": rank}

            all_candidates.append(_build_candidate_entry(
                rank, term, raw_l_occ, raw_r_occ,
                left_chars, right_chars, left_chap_count, right_chap_count,
                score=new_score,
                selection={
                    "g2_score": round(new_score, 4),
                    "left_uncovered_count": lc,
                    "right_uncovered_count": rc,
                    "left_uncovered_chapters": len(l_filtered),
                    "right_uncovered_chapters": len(r_filtered),
                },
                uncovered_left=(lc, len(l_filtered)),
                uncovered_right=(rc, len(r_filtered)),
            ))

        for term in pending_terms & explain_pool_terms:
            trace[term] = {
                "stage": "paging",
                "reason": "beyond_page_limit",
                "uncovered": _uncovered_snapshot(
                    left_occ, right_occ, term, covered_spans, left_chars, right_chars
                )[0],
            }

    candidates = all_candidates[offset:offset + limit]
    next_offset = offset + limit if len(all_candidates) > offset + limit else None
    explanations = (
        _explain_contrast_terms(
            explain_terms,
            min_support=min_support, min_chapters=min_chapters, min_len=min_len, max_len=max_len,
            direction=direction, offset=offset, limit=limit,
            left_chars=left_chars, right_chars=right_chars,
            left_clauses=left_clauses, right_clauses=right_clauses,
            pool_terms=pool_terms, pool_left_occ=left_occ, pool_right_occ=right_occ, trace=trace,
        )
        if explain_terms else None
    )
    return candidates, scored_pool_count, pool_truncated, next_offset, explanations


def compare_intervals(library, left_range, right_range, *, limit=20, offset=0, min_support=3,
                      min_chapters=1, min_len=2, max_len=8, direction="all", view="contrast",
                      sort=None, contains=None, explain=None, examples=0, budget=4000, group_budget=None):
    """Discover distinctive or raw-frequency phrases across two chapter intervals within Scope."""
    scope = library.scope
    left_start, left_end = left_range
    right_start, right_end = right_range
    integer(left_start, "左区间起始章", scope.first_chapter, scope.as_of)
    integer(left_end, "左区间结束章", left_start, scope.as_of)
    integer(right_start, "右区间起始章", scope.first_chapter, scope.as_of)
    integer(right_end, "右区间结束章", right_start, scope.as_of)
    if (left_start, left_end) == (right_start, right_end):
        raise ValueError("左右两个比较区间完全相同，请指定不同的区间范围")

    integer(limit, "返回条数", 1, MAX_PAGE)
    integer(offset, "分页起点")
    integer(min_support, "最小出现次数", 1)
    integer(min_chapters, "最少命中章数", 1)
    integer(min_len, "最短词长", 1, 12)
    integer(max_len, "最长词长", min_len, 16)
    if direction not in DIRECTION_VALUES:
        raise ValueError(f"对比方向须为 {' / '.join(DIRECTION_VALUES)}")
    if view not in VIEW_VALUES:
        raise ValueError(f"查看模式须为 {' / '.join(VIEW_VALUES)}")
    effective_sort = ("total" if view == "base" else "g2") if sort is None else sort
    if effective_sort not in SORT_VALUES:
        raise ValueError(f"排序方式须为 {' / '.join(SORT_VALUES)}")
    if view == "contrast":
        if contains is not None:
            raise ValueError("--contains 仅用于 --view base 基础候选浏览")
        if sort is not None and sort != "g2":
            raise ValueError("--sort 仅用于 --view base 基础候选浏览")
    elif contains is not None and (not isinstance(contains, str) or not contains or len(contains) > 128):
        raise ValueError("--contains 筛选词须为 1 到 128 个字符的非空文本")
    explain_terms = ()
    if explain is not None:
        explain_terms = tuple(dict.fromkeys(explain))
        if explain_terms:
            if view != "contrast":
                raise ValueError("--explain 仅用于默认 contrast 筛选复核；查看基础统计请使用 --view base --contains")
            integer(len(explain_terms), "复核词数", 1, MAX_EXPLAIN_TERMS)
            for term in explain_terms:
                if not isinstance(term, str) or not term or len(term) > 128:
                    raise ValueError("--explain 复核词须为 1 到 128 个字符的非空文本")
    integer(examples, "展开例段数", 0, 10)
    integer(budget, "正文预算", 1, MAX_BODY_CHARS)
    if group_budget is not None:
        integer(group_budget, "每组正文预算", 1, MAX_BODY_CHARS)

    left_chapters, right_chapters = [], []
    left_clauses, right_clauses = [], []
    left_chars = right_chars = 0
    left_han = right_han = 0

    for chapter in library.chapters(with_paragraphs=False):
        in_left = left_start <= chapter.number <= left_end
        in_right = right_start <= chapter.number <= right_end
        if not in_left and not in_right:
            continue
        clauses = _extract_clauses(chapter, min_len) if chapter.body else []
        han_count = sum(len(text) for _, text in clauses)
        if in_left:
            left_chapters.append(chapter.number)
            left_chars += len(chapter.body)
            left_han += han_count
            left_clauses.append((chapter.number, clauses))
        if in_right:
            right_chapters.append(chapter.number)
            right_chars += len(chapter.body)
            right_han += han_count
            right_clauses.append((chapter.number, clauses))

    if not left_chapters or not left_chars:
        raise ValueError(f"左区间 C{left_start:04d}–C{left_end:04d} 在当前范围内没有可读正文")
    if not right_chapters or not right_chars:
        raise ValueError(f"右区间 C{right_start:04d}–C{right_end:04d} 在当前范围内没有可读正文")

    # Stage 1 & 2: Apriori level-wise mining per interval (zero preset terms).
    use_filter = (left_han + right_han) > 50_000 and min_support >= 2
    left_level = _count_min_len(left_clauses, min_len, min_support, use_seen_filter=use_filter)
    right_level = _count_min_len(right_clauses, min_len, min_support, use_seen_filter=use_filter)

    if use_filter:
        candidate_grams = (
            {g for g, c in left_level.items() if c >= min_support - 1}
            | {g for g, c in right_level.items() if c >= min_support - 1}
        )
        exact_left = _recount_exact_grams(left_clauses, min_len, candidate_grams) if candidate_grams else Counter()
        exact_right = _recount_exact_grams(right_clauses, min_len, candidate_grams) if candidate_grams else Counter()
    else:
        exact_left, exact_right = left_level, right_level

    combined_seeds = (
        {g for g, c in exact_left.items() if c >= min_support}
        | {g for g, c in exact_right.items() if c >= min_support}
    )
    all_counts = {gram: (exact_left.get(gram, 0), exact_right.get(gram, 0)) for gram in combined_seeds}

    seeds = combined_seeds
    for length in range(min_len + 1, max_len + 1):
        if not seeds:
            break
        l_next = _count_extended_level(left_clauses, length, seeds)
        r_next = _count_extended_level(right_clauses, length, seeds)
        seeds = (
            {g for g, c in l_next.items() if c >= min_support}
            | {g for g, c in r_next.items() if c >= min_support}
        )
        for gram in seeds:
            all_counts[gram] = (l_next.get(gram, 0), r_next.get(gram, 0))

    parameters = {
        "limit": limit,
        "offset": offset,
        "min_support": min_support,
        "min_chapters": min_chapters,
        "min_len": min_len,
        "max_len": max_len,
        "direction": direction,
        "view": view,
        "sort": effective_sort,
        "examples": examples,
    }

    if view == "base":
        candidates, total_candidates, next_offset = _select_base_candidates(
            all_counts, left_clauses, right_clauses, left_chars, right_chars,
            len(left_chapters), len(right_chapters),
            min_support=min_support, min_chapters=min_chapters, min_len=min_len,
            direction=direction, sort=effective_sort, contains=contains, offset=offset, limit=limit,
        )
        if contains is not None:
            parameters["contains"] = contains
        parameters["counting_basis"] = "raw_interval_stats"
        extra_fields = {
            "total_candidates": total_candidates,
            "next_offset": next_offset,
        }
        mode = "base_candidates"
    else:
        candidates, scored_pool_count, pool_truncated, next_offset, explanations = _select_contrast_candidates(
            all_counts, left_clauses, right_clauses, left_chars, right_chars,
            len(left_chapters), len(right_chapters),
            min_support=min_support, min_chapters=min_chapters, min_len=min_len, max_len=max_len,
            direction=direction, offset=offset, limit=limit, explain_terms=explain_terms,
        )
        parameters["pool_limit"] = CONTRAST_POOL_LIMIT
        if explain_terms:
            parameters["explain"] = list(explain_terms)
        parameters["counting_basis"] = "raw_interval_stats_with_uncovered_selection"
        extra_fields = {
            "scored_pool_count": scored_pool_count,
            "pool_truncated": pool_truncated,
            "next_offset": next_offset,
        }
        if explanations is not None:
            extra_fields["explanations"] = explanations
        mode = "interval_compare"

    left_filtered_peripheral = right_filtered_peripheral = 0
    if not library.scope.include_peripheral:
        for row in library.chapter_metadata(include_filtered=True):
            if row["kind"] != "peripheral":
                continue
            num = row["chapter"]
            if left_start <= num <= left_end:
                left_filtered_peripheral += 1
            if right_start <= num <= right_end:
                right_filtered_peripheral += 1

    result = {
        "mode": mode,
        "left_interval": {
            "first_chapter": left_start,
            "last_chapter": left_end,
            "chapter_count": len(left_chapters),
            "characters": left_chars,
            "han_characters": left_han,
            "filtered_peripheral_count": left_filtered_peripheral,
        },
        "right_interval": {
            "first_chapter": right_start,
            "last_chapter": right_end,
            "chapter_count": len(right_chapters),
            "characters": right_chars,
            "han_characters": right_han,
            "filtered_peripheral_count": right_filtered_peripheral,
        },
        "parameters": parameters,
        **extra_fields,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }

    if examples and candidates:
        groups = []
        for cand in candidates[:examples]:
            for side_key in ("left", "right"):
                side_spans = cand[side_key]["read_spans"]
                if not side_spans:
                    continue
                span = Span.parse(side_spans[0])
                side_label = "左" if side_key == "left" else "右"
                label = f"「{cand['term']}」{side_label}"
                groups.append(ReadSelection(
                    spans=(span,), match_spans=(span,), group=len(groups) + 1,
                    label=label, context_anchors=((span, label),),
                ))
        if groups:
            gb, gb_source = normalize_group_budget(budget, group_budget)
            result["reading"] = read_selections(
                library, groups, whole_paragraphs=True, budget=budget,
                group_budget=gb, group_budget_source=gb_source,
            )

    return result
