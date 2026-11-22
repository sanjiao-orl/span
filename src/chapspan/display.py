"""Render scoped results for direct reading without an external JSON wrapper."""

import json
import os
import re
import shlex
from collections import Counter
from dataclasses import dataclass

from .handoff import result_reading
from .model import Span
from .reading import SELECTION_ORIGIN_LABELS, context_labels, selection_origins


LABELS = {"complete": "完整", "partial": "部分", "not_started": "未开始"}
PERIPHERAL_LABELS = {
    "author_note": "作者的话",
    "launch_note": "上架感言",
    "leave_notice": "请假条",
    "ending_note": "完本感言",
    "afterword": "后记",
    "other": "其他外围",
}
CHAPTER_KIND_LABELS = {"body": "正篇", "unclassified": "未分类", "peripheral": "外围"}
PS_SINGLE_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")
BARE_SHELL_TOKEN = re.compile(
    r"[^\s'\"\\`$()\[\]{}&|;<>?*#~!^,%@\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u2013\u2014\u2015]+"
)


def shell_quote(value):
    """Quote an argument so it executes verbatim in PowerShell and POSIX shells."""
    text = str(value)
    if not text:
        return "''"
    if BARE_SHELL_TOKEN.fullmatch(text):
        return text
    if os.name == "nt":
        escaped = text
        for quote in PS_SINGLE_QUOTES:
            escaped = escaped.replace(quote, quote + quote)
        return f"'{escaped}'"
    return shlex.quote(text)


def _format_command(parts):
    quoted = [shell_quote(part) for part in parts]
    if os.name == "nt" and quoted and quoted[0].startswith("'"):
        return "& " + " ".join(quoted)
    return " ".join(quoted)


@dataclass(frozen=True)
class CommandContext:
    """Capture user invocation context so printed follow-up commands execute verbatim."""

    prog_tokens: tuple[str, ...] = ("span",)
    source_args: tuple[str, ...] = ()
    state_dir_args: tuple[str, ...] = ()
    format_args: tuple[str, ...] = ()
    parsing_args: tuple[str, ...] = ()
    scope_args: tuple[str, ...] = ()

    def from_result_cmd(self, reference, *extra_args, saved_file=False):
        del saved_file
        parts = [
            *self.prog_tokens,
            *self.source_args,
            *self.state_dir_args,
            *self.format_args,
            "read",
            "--from-result",
            str(reference),
            *extra_args,
        ]
        return _format_command(parts)

    def read_span_cmd(self, span_coord, *extra_args):
        parts = [
            *self.prog_tokens,
            *self.source_args,
            *self.state_dir_args,
            *self.parsing_args,
            *self.format_args,
            "read",
            str(span_coord),
            *extra_args,
            *self.scope_args,
        ]
        return _format_command(parts)


def build_command_context(raw_invocation, actual_scope, *, run_scope=None, in_run=False, text_options=None):
    """Build a CommandContext from pre-bind CLI options and resolved scope."""
    if raw_invocation is None or actual_scope is None:
        return None
    if in_run:
        raw_run = raw_invocation.get("run")
        source_args = ("--run", str(raw_run)) if raw_run is not None else ()
        state_dir_args = ()
        parsing_args = ()
        scope_parts = []
        if run_scope is not None:
            if actual_scope.as_of != run_scope.as_of:
                scope_parts.extend(("--as-of", str(actual_scope.as_of)))
            if actual_scope.first_chapter != run_scope.first_chapter:
                scope_parts.extend(("--from-chapter", str(actual_scope.first_chapter)))
        scope_args = tuple(scope_parts)
    elif raw_invocation.get("text") is not None:
        source_args = ("--text", str(raw_invocation["text"]))
        raw_state = raw_invocation.get("state_dir")
        state_dir_args = ("--state-dir", str(raw_state)) if raw_state is not None else ()
        opts = text_options if isinstance(text_options, dict) else {}
        parsing_parts = []
        encoding = opts.get("encoding") or raw_invocation.get("text_encoding")
        if encoding:
            parsing_parts.extend(("--encoding", str(encoding)))
        pattern = opts.get("chapter_pattern")
        if pattern is None:
            pattern = raw_invocation.get("text_pattern")
        if pattern is not None:
            parsing_parts.extend(("--chapter-pattern", str(pattern)))
        paragraphs = opts.get("paragraphs") or raw_invocation.get("text_paragraphs")
        if paragraphs and paragraphs != "lines":
            parsing_parts.extend(("--paragraphs", str(paragraphs)))
        heading_level = opts.get("heading_level")
        if heading_level is None:
            heading_level = raw_invocation.get("text_heading_level")
        if heading_level is not None:
            parsing_parts.extend(("--heading-level", str(heading_level)))
        if opts.get("single_chapter") or raw_invocation.get("text_single_chapter"):
            parsing_parts.append("--single-chapter")
        parsing_args = tuple(parsing_parts)
        scope_parts = ["--snapshot", actual_scope.snapshot_id, "--as-of", str(actual_scope.as_of)]
        if actual_scope.first_chapter > 1:
            scope_parts.extend(("--from-chapter", str(actual_scope.first_chapter)))
        if actual_scope.include_peripheral:
            scope_parts.append("--include-peripheral")
        scope_args = tuple(scope_parts)
    elif raw_invocation.get("library") is not None:
        source_args = ("--library", str(raw_invocation["library"]))
        raw_state = raw_invocation.get("state_dir")
        state_dir_args = ("--state-dir", str(raw_state)) if raw_state is not None else ()
        parsing_args = ()
        scope_parts = ["--snapshot", actual_scope.snapshot_id, "--as-of", str(actual_scope.as_of)]
        if actual_scope.first_chapter > 1:
            scope_parts.extend(("--from-chapter", str(actual_scope.first_chapter)))
        if actual_scope.include_peripheral:
            scope_parts.append("--include-peripheral")
        scope_args = tuple(scope_parts)
    else:
        return None
    raw_format = raw_invocation.get("format")
    format_args = ("--format", raw_format) if raw_format in ("text", "json") else ()
    prog_tokens = tuple(raw_invocation.get("prog_prefix") or ("span",))
    return CommandContext(
        prog_tokens=prog_tokens,
        source_args=source_args,
        state_dir_args=state_dir_args,
        format_args=format_args,
        parsing_args=parsing_args,
        scope_args=scope_args,
    )


def _from_result_cmd(command_context, reference, *extra_args, saved_file=False):
    if command_context is not None:
        return command_context.from_result_cmd(reference, *extra_args, saved_file=saved_file)
    ref_text = str(reference) if reference == "<状态柄或结果文件>" else shell_quote(reference)
    parts = ["read", "--from-result", ref_text] + [shell_quote(arg) for arg in extra_args]
    return " ".join(parts)


def _read_span_cmd(command_context, span_coord, *extra_args):
    if command_context is not None:
        return command_context.read_span_cmd(span_coord, *extra_args)
    parts = ["read", shell_quote(span_coord)] + [shell_quote(arg) for arg in extra_args]
    return " ".join(parts)


def _format_context_labels(labels):
    """Collapse same-term left/right tags while preserving attribution order."""
    ordered = context_labels(labels)
    if not ordered:
        return ""
    term_sides = {}
    bare_sides = []
    order_keys = []
    for label in ordered:
        if label in ("左", "右"):
            if ("bare", None) not in order_keys:
                order_keys.append(("bare", None))
            if label not in bare_sides:
                bare_sides.append(label)
        elif label.startswith("「") and len(label) >= 4 and label[-1] in ("左", "右") and "」" in label:
            term = label[:-1]
            side = label[-1]
            if ("term", term) not in order_keys:
                order_keys.append(("term", term))
            sides = term_sides.setdefault(term, [])
            if side not in sides:
                sides.append(side)
        else:
            key = ("other", label)
            if key not in order_keys:
                order_keys.append(key)
    parts = []
    for kind, key in order_keys:
        if kind == "term":
            sides = "、".join(side for side in ("左", "右") if side in term_sides[key])
            parts.append(f"{key}{sides}")
        elif kind == "bare":
            parts.append("、".join(side for side in ("左", "右") if side in bare_sides))
        else:
            parts.append(key)
    return " · ".join(parts)


def example_delivery(reading):
    rows = reading.get("delivery", [])
    delivered = []
    if "example_refs" in reading:
        for entry in reading["example_refs"]:
            span = entry["span"]
            row = next((row for row in rows if row["group"] == entry["group"]
                        and span in row["requested_spans"]), None)
            delivered.append((entry["reference"], span, row))
        return delivered
    for index, span in enumerate(reading.get("selected_spans", []), 1):
        row = next((row for row in rows if span in row["requested_spans"]), None)
        delivered.append((str(index), span, row))
    return delivered


def delivery_text(reading, *, examples=False):
    rows = reading.get("delivery", [])
    selected = example_delivery(reading) if examples else []
    if not rows and not selected:
        return [f"本页正文 {reading['read_characters']} 字符"]
    statuses = [row["status"] if row is not None else "not_started" for _, _, row in selected] if examples else [
        row["status"] for row in rows
    ]
    counts = Counter(statuses)
    summary = " · ".join(f"{LABELS[status]} {counts[status]}" for status in LABELS if counts[status])
    label, unit = ("例段交付", "例") if examples else ("选区交付", "项")
    line = f"{label}：共 {len(statuses)} {unit}（{summary}）· 本页正文 {reading['read_characters']} 字符"
    if not reading["complete"] or sum(row["read_characters"] for row in rows) != reading["read_characters"]:
        line += f" · 累计交付（按选区） {sum(row['read_characters'] for row in rows)} 字符"
    return [line]


def not_started_text(reading, *, examples=False):
    if examples:
        waiting = [f"{label if label.startswith('Q') else '例' + label}（{span}）"
                   for label, span, row in example_delivery(reading)
                   if row is None or row["status"] == "not_started"]
    else:
        grouped = "groups" in reading
        active = {group["group"] for group in reading.get("groups", [])}
        waiting = [f"{row.get('reference', '组' + str(row['group']))}（{row['target_span']}）"
                   if grouped else row["target_span"]
                   for row in reading.get("delivery", [])
                   if row["status"] == "not_started" and (not grouped or row["group"] in active)]
    return ["未开始：" + " · ".join(waiting)] if waiting else []


def _group_label(group, *, examples=False):
    prefix = group.get("reference", ("例" if examples else "组") + str(group["group"]))
    return f"{prefix} · {group['label']}" if group.get("label") else prefix


def _delivery_progress(delivery):
    target = Span.parse(delivery["target_span"])
    total = target.end - target.start
    return delivery["read_characters"], total


def _delivery_group_label(reading, group_number, *, examples=False):
    for row in reading.get("groups", ()):
        if row["group"] == group_number:
            return _group_label(row, examples=examples)
    cont_groups = (reading.get("continuation") or {}).get("groups", ())
    for row in cont_groups:
        if row["group"] == group_number:
            ref = next((r["reference"] for r in reading.get("group_refs", ()) if r["group"] == group_number), None)
            prefix = ref or ("例" if examples else "组") + str(group_number)
            return f"{prefix} · {row['label']}" if row.get("label") else prefix
    for row in reading.get("delivery", ()):
        if row["group"] == group_number:
            return row.get("reference", ("例" if examples else "组") + str(group_number))
    return ("例" if examples else "组") + str(group_number)


def reading_footer_lines(reading, *, saved, examples=False, reference=None, command_context=None):
    """Build concise actionable footer lines for truncated/pending reading."""
    if reading.get("complete", True):
        return []
    if reference is None:
        reference = "本结果文件" if saved else "<状态柄或结果文件>"
    example_rows = example_delivery(reading) if examples else []
    footer = []

    deferred_items = []
    deferred_refs = []
    if "groups" in reading:
        active = {group["group"] for group in reading["groups"]}
        seen_deferred = set()
        for row in reading.get("delivery", []):
            if row["group"] in active or row["status"] == "complete" or row["group"] in seen_deferred:
                continue
            seen_deferred.add(row["group"])
            g_rows = [r for r in reading.get("delivery", []) if r["group"] == row["group"]]
            g_read = sum(r["read_characters"] for r in g_rows)
            g_target = sum(Span.parse(r["target_span"]).end - Span.parse(r["target_span"]).start for r in g_rows)
            g_next = next((r["next"] for r in g_rows if r.get("next")), None)
            g_label = _delivery_group_label(reading, row["group"], examples=examples)
            detail = f"{g_read}/{g_target} 字" + (f"，待续 {g_next}" if g_next else "")
            deferred_items.append(f"{g_label}（{detail}）")
            deferred_refs.append((row["group"], g_label, row.get("reference", str(row["group"]))))
    if deferred_items:
        footer.append("本轮未续：" + " · ".join(deferred_items))

    not_started_items = []
    not_started_group_refs = []
    if examples:
        for label, span_coord, row in example_rows:
            if row is None or row["status"] == "not_started":
                disp_label = label if label.startswith("Q") else f"例{label}"
                not_started_items.append(f"{disp_label}（{span_coord}）")
    elif "groups" in reading:
        active = {group["group"] for group in reading["groups"]}
        seen_ns = set()
        for row in reading.get("delivery", []):
            if row["status"] == "not_started" and row["group"] in active and row["group"] not in seen_ns:
                seen_ns.add(row["group"])
                g_label = _delivery_group_label(reading, row["group"], examples=examples)
                not_started_items.append(f"{g_label}（{row['target_span']}）")
                not_started_group_refs.append((row["group"], g_label, row.get("reference", str(row["group"]))))
    else:
        for row in reading.get("delivery", []):
            if row["status"] == "not_started":
                not_started_items.append(row["target_span"])
    if not_started_items:
        footer.append("未开始：" + " · ".join(not_started_items))

    pending_groups = [row["group"] for row in (reading.get("continuation") or {}).get("groups", [])]
    base_cmd = _from_result_cmd(command_context, reference, saved_file=saved)
    if "groups" in reading and len(pending_groups) > 1:
        footer.append(f"续全部：{base_cmd}")
        if not_started_group_refs:
            _, pick_label, pick_ref = not_started_group_refs[0]
            action_label = f"续 {pick_label}（未开始）"
        elif deferred_refs:
            _, pick_label, pick_ref = deferred_refs[0]
            action_label = f"续 {pick_label}（未续）"
        else:
            first_g = pending_groups[0]
            pick_label = _delivery_group_label(reading, first_g, examples=examples)
            ref_map = {row["group"]: row["reference"] for row in reading.get("group_refs", [])}
            pick_ref = ref_map.get(first_g, str(first_g))
            action_label = f"续 {pick_label}"
        single_cmd = _from_result_cmd(command_context, reference, "--continue-group", str(pick_ref), saved_file=saved)
        footer.append(f"{action_label}：{single_cmd}")
    else:
        footer.append(f"续读：{base_cmd}")

    if examples or reading.get("example_refs"):
        ex_ref = (reading["example_refs"][0]["reference"] if reading.get("example_refs")
                  else (example_rows[0][0] if example_rows else "1"))
        expand_cmd = _from_result_cmd(
            command_context, reference, "--example", str(ex_ref), "--before", "1", "--after", "1",
            saved_file=saved,
        )
        if str(ex_ref).startswith("Q"):
            ex_heading = f"扩读 {ex_ref}"
        else:
            ex_count = len(example_rows)
            ex_heading = f"扩读（1–{ex_count}）" if ex_count > 1 else "扩读"
        footer.append(f"{ex_heading}：{expand_cmd}")

    return footer


def reading_text(result, reading, *, saved, examples=False, reference=None, command_context=None,
                 include_footer=True):
    if reference is None:
        reference = "本结果文件" if saved else "<状态柄或结果文件>"
    lines = []
    grouped_rows = {(row["group"], row["target_span"]): row for row in reading.get("delivery", [])}
    example_rows = example_delivery(reading) if examples else []

    def origins_text(row):
        origins = selection_origins(row.get("selection_origins")) if not examples else []
        return " · 选材 " + "、".join(SELECTION_ORIGIN_LABELS[origin] for origin in origins) if origins else ""

    def context_text(row, *, skip=()):
        if examples:
            return ""
        labels = [label for label in context_labels(row.get("context_labels")) if label not in skip]
        formatted = _format_context_labels(labels)
        return f" · {formatted}" if formatted else ""

    def fragment_label(row, group=None):
        if not examples:
            return ""
        numbers = [label for label, span, delivery in example_rows
                   if span in row["requested_spans"]
                   and (group is None or delivery is not None and delivery["group"] == group)]
        if numbers and numbers[0].startswith("Q"):
            return "、".join(numbers)
        return "例" + "、".join(numbers or [str(row["passage"])])

    pickable = saved and reading is result
    pick = 0
    if "groups" in reading:
        shown = set()
        uses = Counter(row["passage"] for group in reading["groups"] for row in group["fragments"])
        for group in reading["groups"]:
            rows = [row for row in reading.get("delivery", []) if row["group"] == group["group"]]
            if not group["fragments"]:
                continue
            if examples:
                for row in group["fragments"]:
                    pick += 1
                    number = row["passage"]
                    passage = reading["passages"][number - 1]
                    choice = f"[{pick}] " if pickable else ""
                    if group.get("reference", "").startswith("Q") and "E" not in group["reference"]:
                        ref_label = fragment_label(row, group["group"])
                    else:
                        ref_label = group.get("reference") or f"例{group['group']}"
                    delivery = grouped_rows.get((group["group"], row["target_span"]))
                    if delivery is not None and delivery["status"] != "complete":
                        delivered, total = _delivery_progress(delivery)
                        tag = f"[{ref_label} · 未完成 {delivered}/{total} 字]"
                    else:
                        tag = f"[{ref_label}]"
                    if number in shown:
                        lines.extend(("", f"{choice}{tag} {row['chapter_title']} {row['citation']}（见已显示的正文 {number}）"))
                        continue
                    prefix = f"正文 {number} · " if uses[number] > 1 else ""
                    merge_note = f" · 合并原文 [@{passage['span']}]" if passage["span"] != row["read_span"] else ""
                    lines.extend((
                        "",
                        f"{prefix}{choice}{tag} {row['chapter_title']} {row['citation']}{merge_note}",
                        passage["text"].rstrip("\n"),
                    ))
                    shown.add(number)
                continue

            read = sum(row["read_characters"] for row in rows)
            target = sum(Span.parse(row["target_span"]).end - Span.parse(row["target_span"]).start for row in rows)
            complete = all(row["status"] == "complete" for row in rows)
            label = _group_label(group, examples=False)
            group_header = f"[{label}]" if complete else f"[{label} · 未完成 {read}/{target} 字]"
            lines.extend(("", group_header))
            skip_labels = {group["label"]} if group.get("label") else set()
            for row in group["fragments"]:
                pick += 1
                number = row["passage"]
                passage = reading["passages"][number - 1]
                choice = f"[{pick}] " if pickable else ""
                ctx = context_text(row, skip=skip_labels)
                orig = origins_text(row)
                heading = f"{choice}{row['chapter_title']} {row['citation']}{ctx}{orig}"
                if number in shown:
                    lines.append(f"{heading}（见已显示的正文 {number}）")
                    continue
                if passage["span"] != row["read_span"]:
                    heading += f" · 合并原文 [@{passage['span']}]"
                prefix = f"正文 {number} · " if uses[number] > 1 else ""
                lines.extend((prefix + heading, passage["text"].rstrip("\n")))
                shown.add(number)
    else:
        for pick, row in enumerate(reading["fragments"], 1):
            delivery = grouped_rows.get((row.get("group", 1), row["target_span"]))
            incomplete = delivery is not None and delivery["status"] != "complete"
            if examples:
                label = fragment_label(row)
                if incomplete:
                    delivered, total = _delivery_progress(delivery)
                    tag = f"[{label} · 未完成 {delivered}/{total} 字] "
                else:
                    tag = f"[{label}] "
                choice = tag
            else:
                choice = f"[{pick}] " if pickable else ""
            ctx = context_text(row)
            orig = origins_text(row)
            suffix = ""
            if not examples and incomplete:
                delivered, total = _delivery_progress(delivery)
                suffix = f"（未完成 {delivered}/{total} 字）"
            lines.extend((
                "",
                f"{choice}{row['chapter_title']} {row['citation']}{ctx}{orig}{suffix}",
                row["text"].rstrip("\n"),
            ))
    if include_footer:
        footer = reading_footer_lines(
            reading, saved=saved, examples=examples, reference=reference, command_context=command_context,
        )
        if footer:
            lines.extend(("", "---", *footer))
    return lines


def _first_readable_chapter_pick(chapters):
    for index, row in enumerate(chapters, 1):
        if row.get("first_paragraph") is not None:
            return index
    return None


def _first_match_span(result):
    if "queries" in result:
        for query in result["queries"]:
            if query.get("matches"):
                return query["matches"][0]["read_spans"][0]
            if query.get("hit_chapters"):
                return query["hit_chapters"][0]["read_span"]
        return None
    if result.get("matches"):
        return result["matches"][0]["read_spans"][0]
    if result.get("hit_chapters"):
        return result["hit_chapters"][0]["read_span"]
    return None


def output_receipt(result, path, *, state_id=None, command_context=None):
    receipt = {"output": str(path), "note": "结果未在终端展开"}
    if state_id is not None:
        receipt["state"] = state_id
    if "scope" in result:
        receipt["snapshot_id"] = result["scope"]["snapshot_id"]
        receipt["as_of"] = result["scope"]["as_of"]
    for key, label in (("chapters", "chapters"), ("snapshots", "snapshots"), ("matches", "matches"),
                       ("candidates", "candidates"), ("items", "items"), ("citations", "citations")):
        if key in result:
            receipt[label] = len(result[key])
    if "skipped_inline_citations" in result:
        receipt["skipped_inline_citations"] = result["skipped_inline_citations"]
        if result["skipped_inline_citations"] and "note" in result:
            receipt["citation_note"] = result["note"]
    if "chapters" in result:
        if result.get("next_after_chapter") is not None:
            receipt["next_after_chapter"] = result["next_after_chapter"]
        if issues := result.get("chapter_diagnostics"):
            if issue_count := issues.get("issue_count", 0):
                receipt["diagnostic_issues"] = issue_count
            if filtered_count := issues.get("filtered_peripheral_count", 0):
                receipt["filtered_peripheral"] = filtered_count
    if "candidates" in result:
        if result.get("total_candidates") is not None:
            receipt["total_candidates"] = result["total_candidates"]
        if result.get("pool_truncated"):
            receipt["pool_truncated"] = True
        if result.get("next_offset") is not None:
            receipt["next_offset"] = result["next_offset"]
        if "explanations" in result:
            receipt["explanations"] = len(result["explanations"])
    if "heading_count" in result:
        receipt["heading_count"] = result["heading_count"]
        receipt["tight_candidate_count"] = result["tight_candidate_count"]
    if "queries" in result:
        receipt["query_groups"] = len(result["queries"])
        receipt["query_matches"] = [{
            "query": row["query"], "mode": row["mode"], "match_count": row["match_count"],
            "hit_chapter_count": row["hit_chapter_count"],
            "first_hit_chapter": row["first_hit_chapter"], "last_hit_chapter": row["last_hit_chapter"],
        } for row in result["queries"]]
    elif "match_count" in result:
        receipt["match_count"] = result["match_count"]
        receipt["mode"] = result["mode"]
        receipt["hit_chapter_count"] = result["hit_chapter_count"]
        if "hit_chapters" in result:
            receipt["page_hit_chapters"] = len(result["hit_chapters"])
            receipt["next_distribution_offset"] = result["next_distribution_offset"]
    reading = result_reading(result)
    if "read_characters" in reading:
        receipt["read_characters"] = reading["read_characters"]
        receipt["complete"] = reading["complete"]
    rows = reading.get("delivery", [])
    if isinstance(result.get("examples"), dict) or "example_refs" in reading:
        selected = example_delivery(reading)
        receipt["examples"] = len(selected)
        if selected:
            receipt["delivery"] = dict(Counter(row["status"] if row is not None else "not_started"
                                               for _, _, row in selected))
    elif rows:
        receipt["delivery"] = dict(Counter(row["status"] for row in rows))
    if reading.get("continuation") is not None:
        receipt["next_action"] = _from_result_cmd(command_context, str(path), saved_file=True)
    elif result.get("examples", {}).get("selected_spans") or result.get("reading", {}).get("example_refs"):
        example = reading["example_refs"][0]["reference"] if "queries" in result else "1"
        receipt["next_action"] = _from_result_cmd(
            command_context, str(path), "--example", str(example), "--before", "1", "--after", "1",
            saved_file=True,
        )
    elif result.get("candidates"):
        receipt["next_action"] = _from_result_cmd(
            command_context, str(path), "--pick", "1", "--group-budget", "1200", "--before", "1", "--after", "1",
            saved_file=True,
        )
    elif result.get("chapters"):
        if (first_pick := _first_readable_chapter_pick(result["chapters"])) is not None:
            receipt["next_action"] = _from_result_cmd(
                command_context, str(path), "--pick", str(first_pick), saved_file=True,
            )
    elif result.get("hit_chapters") or (
        result.get("matches") and not result.get("queries")
    ):
        receipt["next_action"] = _from_result_cmd(command_context, str(path), "--pick", "1", saved_file=True)
    elif "queries" in result and (first_span := _first_match_span(result)):
        receipt["next_action"] = _read_span_cmd(command_context, first_span, "--whole-paragraphs")
    return receipt


def hit_extent(result):
    first, last = result["first_hit_chapter"], result["last_hit_chapter"]
    if first is None:
        return None
    return f"C{first:04d}" if first == last else f"C{first:04d}–C{last:04d}"


def hit_ranges_text(result):
    ranges = result["hit_chapter_ranges"]
    values = [f"C{row['first_chapter']:04d}" if row["first_chapter"] == row["last_chapter"]
              else f"C{row['first_chapter']:04d}–C{row['last_chapter']:04d}" for row in ranges]
    if result["hit_chapter_ranges_omitted"]:
        values.insert(len(values) // 2, "…")
    return " · ".join(values)


def format_output_receipt(receipt):
    lines = [f"已保存：{receipt['output']}"]
    if "state" in receipt:
        lines.append(f"状态：{receipt['state']}")
    for key, label in (("chapters", "章目"), ("snapshots", "快照"), ("matches", "本页匹配"),
                       ("candidates", "本页候选"), ("total_candidates", "候选总数"), ("explanations", "筛选复核"),
                       ("items", "收藏"), ("citations", "引用"), ("skipped_inline_citations", "跳过行内引用"),
                       ("query_groups", "独立查询"), ("page_hit_chapters", "本页命中章"), ("examples", "已选例段"),
                       ("read_characters", "本页正文字符")):
        if key in receipt:
            lines.append(f"{label}：{receipt[key]}")
    if "citation_note" in receipt:
        lines.append(receipt["citation_note"])
    if receipt.get("pool_truncated"):
        lines.append("池截断：已达对比候选池上限（原表 --view base）")
    if receipt.get("diagnostic_issues"):
        lines.append(f"章界诊断：{receipt['diagnostic_issues']} 处（核查 overview --inspect C编号）")
    if receipt.get("filtered_peripheral"):
        lines.append(f"过滤外围：{receipt['filtered_peripheral']} 章（纳入 --include-peripheral）")
    if receipt.get("next_after_chapter") is not None:
        lines.append(f"后续目录：--after-chapter {receipt['next_after_chapter']}")
    if receipt.get("next_offset") is not None:
        lines.append(f"后续候选：--offset {receipt['next_offset']}")
    if "heading_count" in receipt:
        lines.append(f"识别标题：{receipt['heading_count']}；紧连候选：{receipt['tight_candidate_count']}")
    if receipt.get("next_distribution_offset") is not None:
        lines.append(f"下一页命中章：--distribution-offset {receipt['next_distribution_offset']}")
    for row in receipt.get("query_matches", [receipt] if "match_count" in receipt else []):
        count, chapters = row["match_count"], row["hit_chapter_count"]
        line = f"Q{row['query']}：" if "query" in row else ""
        if row["mode"] == "chapter":
            line += f"{chapters} 章符合条件"
        elif row["mode"] == "paragraphs":
            line += f"{count} 个段落窗口，覆盖 {chapters} 章"
        else:
            line += f"出现 {count} 次，分布在 {chapters} 章"
        if "first_hit_chapter" in row and (extent := hit_extent(row)):
            line += f" · 首末 {extent}"
        lines.append(line)
    if "delivery" in receipt:
        summary = " · ".join(f"{LABELS[key]} {value}" for key, value in receipt["delivery"].items())
        suffix = "（仍有待续）" if receipt.get("complete") is False else ""
        lines.append(f"选区交付：{summary}{suffix}")
    elif "complete" in receipt:
        lines.append("本批已交付完" if receipt["complete"] else "本批仍有待续内容")
    if "next_action" in receipt:
        lines.append(f"下一步：{receipt['next_action']}")
    return "\n".join(lines)


def _inline_hit_ranges(result):
    return (
        0 < result.get("hit_chapter_range_count", 0) <= 3
        and not result.get("hit_chapter_ranges_omitted")
    )


def query_summary(result):
    count, chapters = result["match_count"], result["hit_chapter_count"]
    mode = result["mode"]
    if mode == "literal":
        term = json.dumps(result["term_counts"][0]["term"], ensure_ascii=False)
        if not count:
            return [f"当前范围内未找到 {term}"]
        if _inline_hit_ranges(result):
            return [f"{term} {count} 次 / {chapters} 章（{hit_ranges_text(result)}）"]
        return [f"{term} {count} 次 / {chapters} 章（首末 {hit_extent(result)}）"]

    near = result["near_paragraphs"]
    area = "同章" if near is None else ("同一段" if near == 1 else f"同章至多 {near} 个连续段落")
    terms = result["conditions"]
    conditions = []
    for key, label in (("all", "含"), ("any", "至少含一项"), ("exclude", "排除")):
        if terms[key]:
            quoted = "、".join(json.dumps(t, ensure_ascii=False) for t in terms[key])
            conditions.append(f"{label} {quoted}")
    if not count:
        summary = f"{area}内没有符合条件的匹配"
    elif mode == "chapter":
        summary = f"{chapters} 章符合条件"
    else:
        summary = f"{count} 个段落窗口 / {chapters} 章"
    filters = "，".join(conditions)
    counts = "、".join(f"{json.dumps(row['term'], ensure_ascii=False)} {row['count']} 次"
                      for row in result["term_counts"])
    if count:
        range_part = hit_ranges_text(result) if _inline_hit_ranges(result) else f"首末 {hit_extent(result)}"
        summary += f"（{area}内{filters} · {range_part}）"
    else:
        summary += f"（{filters}）"
    return [summary, f"各词出现：{counts}"]


def chapter_distribution_lines(rows, mode, *, number_hits=False):
    lines = []
    index = 0
    while index < len(rows):
        row = rows[index]
        first = last = row["chapter"]
        count = row["match_count"]
        index += 1
        if count:
            amount = {"literal": f"{count} 次", "chapter": "符合条件", "paragraphs": f"{count} 个窗口"}[mode]
            anchor = f" · {row['read_span']}" if "read_span" in row else ""
            number = f"[{index}] " if number_hits else ""
            lines.append(f"{number}C{first:04d} {row['title']} · {amount}{anchor}")
            continue
        while index < len(rows) and rows[index]["match_count"] == 0 and rows[index]["chapter"] == last + 1:
            last = rows[index]["chapter"]
            index += 1
        extent = f"C{first:04d} {row['title']}" if first == last else f"C{first:04d}–C{last:04d}"
        lines.append(f"{extent} · 无匹配")
    return lines


def _wrapped_coordinates(items, *, label="本页坐标：", sep="  ", per_line=5):
    lines = []
    indent = " " * len(label)
    for start in range(0, len(items), per_line):
        chunk = sep.join(items[start:start + per_line])
        lines.append(f"{label}{chunk}" if start == 0 else f"{indent}{chunk}")
    return lines


def query_text(result, *, distribution=False, qualified=False, has_examples=False):
    lines = query_summary(result)
    if qualified:
        lines[0] = f"[Q{result['query']}] {lines[0]}"
    hits_view = "hit_chapters" in result
    if result["match_count"] and (delta := result.get("alias_delta")):
        a, b = delta["first_only"], delta["second_only"]
        first, second = (json.dumps(side["term"], ensure_ascii=False) for side in (a, b))
        lines.append(f"在 {first} 之外，加查 {second} 多找到 {b['chapter_count']} 章"
                     f"（两词同章 {delta['both']['chapter_count']} 章，只含 {first} {a['chapter_count']} 章）。")
        if not (distribution or hits_view):
            for term, side in ((first, a), (second, b)):
                if side["read_spans"]:
                    lines.append(f"只含 {term} 坐标：{' · '.join(side['read_spans'])}")
    if hits_view or distribution:
        rows = result["hit_chapters"] if hits_view else result["distribution"]
        title = "命中章" if hits_view else "可见章分布"
        if rows:
            lines.append(f"本页{title}（{len(rows)} 章）")
            lines.extend(chapter_distribution_lines(rows, result["mode"], number_hits=hits_view and not qualified))
            if not hits_view and rows[-1]["chapter"] - rows[0]["chapter"] + 1 > len(rows):
                lines.append("本页仅统计列出的可见章位，跳过的章号未计入零值。")
            if not hits_view and result["match_count"] and not any(row["match_count"] for row in rows):
                lines.append("本页都是零；可用 --hit-chapters 直接列出命中章。")
        elif result["match_count"] or not hits_view:
            lines.append(f"本页没有可列出的{title}。")
        offset = result["next_distribution_offset"]
        if offset is not None:
            arg = f"将该组 distribution_offset 设为 {offset}" if qualified else f"--distribution-offset {offset}"
            lines.append(f"下一页{title}：{arg}")
        return lines
    if result["hit_chapter_range_count"] > 1 and not _inline_hit_ranges(result):
        omitted = result.get("hit_chapter_ranges_omitted", 0)
        omitted_note = f"（省 {omitted} 段，完整 --hit-chapters）" if omitted else ""
        lines.append(f"区段（{result['hit_chapter_range_count']} 段）：{hit_ranges_text(result)}{omitted_note}")
    if not has_examples and result.get("matches"):
        if qualified:
            coords = [",".join(row["read_spans"]) for row in result["matches"]]
            lines.extend(_wrapped_coordinates(coords, label="本页坐标：", sep=" · "))
        else:
            coords = [f"{index}={','.join(row['read_spans'])}" for index, row in enumerate(result["matches"], 1)]
            lines.extend(_wrapped_coordinates(coords, label="本页坐标：", sep="  "))
    if not has_examples and result.get("next_offset") is not None:
        offset = result["next_offset"]
        arg = f"将该组 offset 设为 {offset}" if qualified else f"--offset {offset}"
        lines.append(f"下一页匹配：{arg}")
    return lines


def _example_chapter_counts(spans):
    counts = Counter()
    order = []
    for span in spans:
        chapter_code = span.partition(":")[0]
        if chapter_code not in counts:
            order.append(chapter_code)
        counts[chapter_code] += 1
    return [(code, counts[code]) for code in order]


def _example_scope_line(selected_spans, *, spread, hit_chapter_count, mode, prefix=""):
    items = _example_chapter_counts(selected_spans)
    count = len(selected_spans)
    if not count:
        return None
    if any(n > 1 for _, n in items):
        chapters_str = " · ".join(f"{code}({n})" for code, n in items)
    else:
        chapters_str = " · ".join(code for code, _ in items)
    spread_tag = "（跨章）" if spread and count > 1 else ""
    notes = []
    if not spread and count > 1 and len(items) < min(count, hit_chapter_count):
        notes.append("跨章 --spread")
    if mode == "chapter":
        notes.append("章级取首词段")
    note_str = f"（{'；'.join(notes)}）" if notes else ""
    return f"{prefix}例段 {count}{spread_tag}：{chapters_str}{note_str}"


def _overview_diagnostic_lines(issues, *, for_sample=False):
    lines = []
    kind_labels = {"tight_heading": "紧连标题", "duplicate_number": "重复章号", "number_gap": "编号跳号",
                   "duplicate_title": "重复章题", "trailing_peripheral": "尾部外围"}
    examples = issues["examples"][:2]
    issue_count = issues.get("issue_count", len(issues["examples"]))
    kind_counts = issues.get("kind_counts") or Counter(row["kind"] for row in issues["examples"])
    counts = " · ".join(f"{kind_labels.get(kind, '章界疑点')} {count}" for kind, count in kind_counts.items())
    if issue_count:
        next_offset = issues.get("next_diagnostic_offset")
        offset = issues.get("diagnostic_offset", 0)
        lines.append(f"章界诊断：共 {issue_count} 处（{counts} · 展示 {len(examples)}/{issue_count}）")
        lines.extend(
            f"  C{row['chapter']:04d} 第{row['source_line']}行 "
            f"{kind_labels.get(row['kind'], '章界疑点')}「{row['title']}」"
            for row in examples
        )
        cmd_prefix = "overview " if for_sample else ""
        hints = ["核查 overview --inspect C编号 [--line 来源行]"]
        if issue_count > len(examples):
            if offset + len(examples) < issue_count:
                hints.append(f"续看 {cmd_prefix}--diagnostic-offset {offset + len(examples)}")
            if next_offset is not None:
                hints.append(f"JSON 下一页 {cmd_prefix}--diagnostic-offset {next_offset}")
            hints.append(f"完整明细 {cmd_prefix}--format json")
        lines.append(" | ".join(hints))
    filtered = issues.get("filtered_peripheral_examples", [])
    filtered_count = issues.get("filtered_peripheral_count", len(filtered))
    if filtered_count:
        cat_counts = issues.get("filtered_peripheral_category_counts") or {}
        summary = " · ".join(f"{PERIPHERAL_LABELS.get(cat, '外围')} {count}" for cat, count in cat_counts.items())
        header = f"过滤外围：{filtered_count} 章" + (f"（{summary}）" if summary else "")
        shown = "、".join(
            f"C{row['chapter']:04d} 第{row['source_line']}行 "
            f"{PERIPHERAL_LABELS.get(row.get('category'), '外围')}「{row['title']}」"
            for row in filtered
        )
        more = "；含外围完整目录 overview --include-peripheral --toc" if filtered_count > len(filtered) else ""
        lines.append(f"{header}（展示 {len(filtered)}/{filtered_count}）：{shown}（纳入加 --include-peripheral{more}）")
    return lines


def format_text(result, *, distribution=False, saved=False, reference=None, command_context=None):
    scope = result["scope"]
    extent = f"C{scope['first_chapter']:04d}–C{scope['as_of']:04d}" if scope["as_of"] else "首章前"
    periph_short = "（含外围）" if scope["include_peripheral"] else ""
    is_chapters = "chapters" in result
    is_inspect = "inspection" in result
    is_single_find = not is_chapters and not is_inspect and "queries" not in result and (
        "matches" in result or "hit_chapters" in result
    )
    if "candidates" in result or is_single_find:
        lines = []
    elif is_inspect:
        lines = [f"快照 {scope['snapshot_id']} · 范围 {extent}"]
        if scope["include_peripheral"]:
            lines[0] += "（含已标记的外围文字）"
    elif is_chapters:
        kind_counts = result.get("chapter_kind_counts")
        if kind_counts and (kind_counts.get("unclassified") or kind_counts.get("peripheral")):
            parts = " · ".join(f"{CHAPTER_KIND_LABELS.get(k, k)} {v}" for k, v in kind_counts.items())
            kind_note = f" · 范围可见 {parts}"
        else:
            kind_note = ""
        lines = [f"目录（本页 {len(result['chapters'])} 章{kind_note}） · 范围 {extent}{periph_short}"]
    elif "queries" in result:
        lines = [f"独立查询 {len(result['queries'])} 组 · 范围 {extent}{periph_short}"]
    elif "sample_chapters" in result:
        lines = [
            f"章首抽读 {len(result['sample_chapters'])} 章（仅章首片段，整章用 read --chapter C编号） · 范围 {extent}{periph_short}"
        ]
    else:
        lines = [f"范围 {extent}{periph_short}"]
    if "inspection" in result:
        inspection = result["inspection"]
        href_note = f" · 内部文件 {inspection['source_href']}" if inspection.get("source_href") else ""
        lines.append(f"核查 C{inspection['chapter']:04d}「{inspection['title']}」· 类型 {inspection['kind']} · "
                     f"正文起始行 {inspection['source_body_line']}{href_note}")
        lines.append(f"解析：{inspection['encoding']} · "
                     + json.dumps(inspection["parsing"], ensure_ascii=False, separators=(",", ":")))
        for row in inspection["adjacent_chapters"]:
            lines.append(f"相邻 C{row['chapter']:04d} · 第{row['source_line']}行 · {row['title']} · {row['kind']}")
        for row in inspection["source_lines"]:
            marker = "→" if row["target"] else " "
            suffix = f"（共 {row['characters']} 字符）" if row["characters"] > 240 else ""
            lines.append(f"{marker} 行 {row['line']} | {row['text']}{suffix}")
        lines.append("修正解析：直接读取可调整 --chapter-pattern/--heading-level/--single-chapter/--paragraphs；书库用 import / import-txt 发布新快照。")
    if "queries" in result:
        reading = result.get("reading", {})
        has_any_examples = bool(reading.get("example_refs"))
        for query in result["queries"]:
            q_has_examples = bool(
                has_any_examples
                and query.get("match_count")
                and isinstance(query.get("examples"), dict)
                and query["examples"].get("selected_spans")
            )
            lines.extend(query_text(query, distribution=distribution, qualified=True, has_examples=q_has_examples))
            if q_has_examples:
                selected = query["examples"]
                scope_line = _example_scope_line(
                    selected["selected_spans"],
                    spread=selected["spread"],
                    hit_chapter_count=query["hit_chapter_count"],
                    mode=query["mode"],
                    prefix=f"Q{query['query']} ",
                )
                if scope_line:
                    lines.append(scope_line)
        if has_any_examples:
            if reference is None:
                reference = "本结果文件" if saved else "<状态柄或结果文件>"
            lines.extend(reading_text(
                result, reading, saved=saved, examples=True, reference=reference,
                command_context=command_context, include_footer=False,
            ))
            footer = reading_footer_lines(
                reading, saved=saved, examples=True, reference=reference, command_context=command_context,
            )
            if reading["complete"]:
                ex_refs = reading["example_refs"]
                first_ref = ex_refs[0]["reference"]
                if len(ex_refs) > 1:
                    preview = "/".join(r["reference"] for r in ex_refs[:4]) + (" 等" if len(ex_refs) > 4 else "")
                    note = f"（{preview}）"
                else:
                    note = ""
                expand_cmd = _from_result_cmd(
                    command_context, reference, "--example", first_ref, "--before", "1", "--after", "1",
                    saved_file=saved,
                )
                footer.append(f"扩读 {first_ref}{note}：{expand_cmd}")
            more_matches = any(
                q.get("match_count", 0) > len((q.get("examples") or {}).get("selected_spans", ()))
                or q.get("next_offset") is not None
                for q in result["queries"]
            )
            if more_matches:
                next_parts = []
                for q in result["queries"]:
                    if q.get("next_offset") is not None:
                        q_spread = bool((q.get("examples") or {}).get("spread"))
                        suffix = "（配 --examples 0）" if q_spread else ""
                        next_parts.append(f"Q{q['query']} 设 offset={q['next_offset']}{suffix}")
                rest = "其余：--hit-chapters 看各章 | --examples 0 看坐标"
                if next_parts:
                    rest += " | 下一页：" + " · ".join(next_parts)
                footer.append(rest)
            if footer:
                lines.extend(("", "---", *footer))
        elif not distribution and not any("hit_chapters" in q for q in result["queries"]):
            first_span = _first_match_span(result)
            if first_span is not None:
                read_cmd = _read_span_cmd(command_context, first_span, "--whole-paragraphs")
                lines.extend(("", "---", f"回读首处：{read_cmd}"))
    elif "chapters" in result:
        if result["chapters"]:
            for index, row in enumerate(result["chapters"], 1):
                choice = f"[{index}] " if saved else ""
                kind_tag = {"unclassified": " [未分类]", "peripheral": " [外围]"}.get(row.get("kind"), "")
                if row["first_paragraph"] is None:
                    details = "（无可读段落）"
                elif "characters" in row:
                    details = f"（{row['characters']} 字 · {row['paragraph_count']} 段）"
                else:
                    details = ""
                lines.append(f"{choice}C{row['chapter']:04d} {row['title']}{kind_tag}{details}")
        else:
            lines.append("当前条件无匹配章目。")
        footer = []
        if result["next_after_chapter"] is not None:
            footer.append(f"后续目录：--after-chapter {result['next_after_chapter']}")
        if saved and result["chapters"]:
            if (first_pick := _first_readable_chapter_pick(result["chapters"])) is not None:
                ref = reference or "本结果文件"
                n = len(result["chapters"])
                readable_count = sum(1 for row in result["chapters"] if row.get("first_paragraph") is not None)
                if readable_count == n and n > 1:
                    label = f"回读（1–{n}）"
                elif readable_count > 1:
                    label = f"回读（可选非空章 1–{n}）"
                else:
                    label = "回读"
                pick_cmd = _from_result_cmd(command_context, ref, "--pick", str(first_pick), saved_file=True)
                footer.append(f"{label}：{pick_cmd}")
        if issues := result.get("chapter_diagnostics"):
            footer.extend(_overview_diagnostic_lines(issues))
        if footer:
            lines.extend(("", "---", *footer))
    elif "matches" in result or "hit_chapters" in result:
        reading = result.get("examples") if isinstance(result.get("examples"), dict) else {}
        has_examples = bool(result["match_count"] and reading.get("selected_spans"))
        q_lines = query_text(result, distribution=distribution, has_examples=has_examples)
        q_lines[0] += f" · 范围 {extent}{periph_short}"
        lines.extend(q_lines)
        if result.get("hit_chapters"):
            if reference is None and saved:
                reference = "本结果文件"
            if reference is not None:
                pick_cmd = _from_result_cmd(command_context, reference, "--pick", "1", saved_file=saved)
                n = len(result["hit_chapters"])
                label = f"回读（1–{n}）" if n > 1 else "回读"
                lines.extend(("", "---", f"{label}：{pick_cmd}"))
        elif has_examples:
            if reference is None:
                reference = "本结果文件" if saved else "<状态柄或结果文件>"
            spread = "groups" in reading
            scope_line = _example_scope_line(
                reading["selected_spans"],
                spread=spread,
                hit_chapter_count=result["hit_chapter_count"],
                mode=result["mode"],
            )
            if scope_line:
                lines.append(scope_line)
            lines.extend(reading_text(
                result, reading, saved=saved, examples=True, reference=reference,
                command_context=command_context, include_footer=False,
            ))
            footer = reading_footer_lines(
                reading, saved=saved, examples=True, reference=reference, command_context=command_context,
            )
            if reading["complete"]:
                ex_count = len(reading["selected_spans"])
                label = f"扩读（1–{ex_count}）" if ex_count > 1 else "扩读"
                expand_cmd = _from_result_cmd(
                    command_context, reference, "--example", "1", "--before", "1", "--after", "1",
                    saved_file=saved,
                )
                footer.append(f"{label}：{expand_cmd}")
            if result["match_count"] > len(reading["selected_spans"]) or result.get("next_offset") is not None:
                rest = "其余：--hit-chapters 看各章 | --examples 0 看坐标"
                if result.get("next_offset") is not None:
                    if spread:
                        rest += f" | 下一页坐标 --examples 0 --offset {result['next_offset']}"
                    else:
                        rest += f" | 下一页 --offset {result['next_offset']}"
                footer.append(rest)
            if footer:
                lines.extend(("", "---", *footer))
        elif result["match_count"] and not distribution and result.get("matches"):
            if saved:
                if reference is None:
                    reference = "本结果文件"
                pick_cmd = _from_result_cmd(command_context, reference, "--pick", "1", saved_file=True)
                n = len(result["matches"])
                label = f"回读（1–{n}）" if n > 1 else "回读"
                lines.extend(("", "---", f"{label}：{pick_cmd}"))
            else:
                first_span = result["matches"][0]["read_spans"][0]
                read_cmd = _read_span_cmd(command_context, first_span, "--whole-paragraphs")
                lines.extend(("", "---", f"回读首处：{read_cmd}"))
    elif "candidates" in result:
        left_info = result["left_interval"]
        right_info = result["right_interval"]
        params = result["parameters"]
        view = params.get("view", "base" if result.get("mode") == "base_candidates" else "contrast")
        offset = params.get("offset", 0)
        sort_labels = {"total": "总次数", "g2": "G2", "left": "左次数", "right": "右次数", "delta": "出现率差"}
        l_periph = (
            f" · 过滤外围 {left_info['filtered_peripheral_count']} 章"
            if left_info.get("filtered_peripheral_count") else ""
        )
        r_periph = (
            f" · 过滤外围 {right_info['filtered_peripheral_count']} 章"
            if right_info.get("filtered_peripheral_count") else ""
        )
        periph_scope = "（含外围）" if scope["include_peripheral"] else ""
        lines.append(
            f"对比：左 C{left_info['first_chapter']:04d}–C{left_info['last_chapter']:04d}"
            f"（{left_info['chapter_count']} 章 · {left_info['characters']} 字{l_periph}）"
            f" vs 右 C{right_info['first_chapter']:04d}–C{right_info['last_chapter']:04d}"
            f"（{right_info['chapter_count']} 章 · {right_info['characters']} 字{r_periph}）{periph_scope}"
        )
        overlap_start = max(left_info["first_chapter"], right_info["first_chapter"])
        overlap_end = min(left_info["last_chapter"], right_info["last_chapter"])
        if overlap_start <= overlap_end:
            lines.append(f"注意：左右区间在 C{overlap_start:04d}–C{overlap_end:04d} 有章节重叠，各自按完整区间统计。")
        if view == "base":
            sort_name = sort_labels.get(params.get("sort", "total"), params.get("sort", "total"))
            contains_note = f" · 含「{params['contains']}」" if params.get("contains") else ""
            total = result.get("total_candidates", result["candidate_count"])
            span_note = (f"本页第 {offset + 1}–{offset + len(result['candidates'])} 项"
                         if result["candidates"] else "本页 0 项")
            lines.append(f"基础候选（排序 {sort_name}{contains_note}）：共 {total} 项，{span_note}")
        elif result.get("pool_truncated"):
            lines.append(
                f"池截断：{result.get('scored_pool_count', 0)}→前{params.get('pool_limit', 500)}，"
                f"本页 {len(result['candidates'])} 项（原表 --view base）"
            )
        elif params.get("direction", "all") != "all":
            lines.append(f"方向 {params['direction']} · 返回 {result['candidate_count']} 项")
        trend_labels = {"right_only": "右现", "left_only": "左现", "up": "右升", "down": "左降", "flat": "持平"}
        if not result["candidates"]:
            l_han = left_info.get("han_characters")
            r_han = right_info.get("han_characters")
            direction = params.get("direction", "all")
            if offset > 0:
                lines.append(f"当前分页起点 --offset {offset} 之后无候选，可减小 --offset。")
            elif l_han == 0 and r_han == 0:
                lines.append(f"左右区间均没有达到最小词长（{params['min_len']} 字）的连续汉字串；已知词请用 find。")
            elif direction == "left" and l_han == 0:
                lines.append(f"左区间没有达到最小词长（{params['min_len']} 字）的连续汉字串；已知词请用 find。")
            elif direction == "right" and r_han == 0:
                lines.append(f"右区间没有达到最小词长（{params['min_len']} 字）的连续汉字串；已知词请用 find。")
            else:
                lines.append("当前区间未发现满足条件的差异词条，可调整筛选参数或区间。")
        else:
            if reference is None:
                reference = "本结果文件" if saved else "<状态柄或结果文件>"
            for pick_index, row in enumerate(result["candidates"], 1):
                l_part = row["left"]
                r_part = row["right"]
                trend = trend_labels.get(row["trend"], row["trend"])
                l_un = l_part.get("uncovered_count")
                r_un = r_part.get("uncovered_count")
                residual = (
                    f" · 去重剩余 左 {l_un}/右 {r_un}"
                    if l_un is not None and r_un is not None and (l_un != l_part["count"] or r_un != r_part["count"])
                    else ""
                )
                rank_label = f"{pick_index}" if row["rank"] == pick_index else f"{pick_index} · 第{row['rank']}名"
                lines.append(
                    f"[{rank_label}] 「{row['term']}」 {trend} · G2 {row['g2_score']:.2f}{residual} · "
                    f"左 {l_part['count']} 次（{l_part['rate_per_10k']:.2f}/万字 · {l_part['hit_chapters']}/{left_info['chapter_count']} 章）"
                    f" vs 右 {r_part['count']} 次（{r_part['rate_per_10k']:.2f}/万字 · {r_part['hit_chapters']}/{right_info['chapter_count']} 章）"
                )
                l_coords = "、".join(l_part["read_spans"]) or "无"
                r_coords = "、".join(r_part["read_spans"]) or "无"
                lines.append(f"    左 {l_coords} | 右 {r_coords}")
        if explanations := result.get("explanations"):
            lines.append(f"筛选复核（{len(explanations)} 项）：")
            for exp in explanations:
                raw = exp.get("raw")
                if raw:
                    g2_part = (
                        ""
                        if exp.get("reason") == "below_min_g2" and raw["g2_score"] >= 1.0
                        else f"（G2 {raw['g2_score']:.2f}）"
                    )
                    raw_summary = (
                        f" · 原文 左 {raw['left_count']} 次/{raw['left_chapters']} 章"
                        f" vs 右 {raw['right_count']} 次/{raw['right_chapters']} 章{g2_part}"
                    )
                else:
                    raw_summary = ""
                lines.append(f"- 「{exp['term']}」 [{exp['stage']}/{exp['reason']}]{raw_summary}：{exp['note']}")
                if rep_spans := exp.get("representative_read_spans"):
                    lines.append(f"  代表词「{exp['representative_term']}」坐标：{'、'.join(rep_spans)}")
                if masker_spans := exp.get("masked_by_read_spans"):
                    maskers = "、".join(f"「{m}」" for m in exp.get("masked_by", ()))
                    lines.append(f"  掩蔽词 {maskers} 坐标：{'、'.join(masker_spans)}")
        has_reading = bool(result["candidates"] and "reading" in result)
        if has_reading:
            lines.extend(reading_text(
                result, result["reading"], saved=saved, examples=False, reference=reference,
                command_context=command_context, include_footer=False,
            ))
        if result["candidates"]:
            footer = []
            if has_reading:
                footer.extend(reading_footer_lines(
                    result["reading"], saved=saved, examples=False, reference=reference,
                    command_context=command_context,
                ))
            if result.get("next_offset") is not None:
                footer.append(f"下一页：--offset {result['next_offset']}")
            cand_count = len(result["candidates"])
            label = f"回读（1–{cand_count}）" if cand_count > 1 else "回读"
            pick_cmd = _from_result_cmd(
                command_context, reference, "--pick", "1", "--group-budget", "1200", "--before", "1", "--after", "1",
                saved_file=saved,
            )
            footer.append(f"{label}：{pick_cmd}")
            lines.extend(("", "---", *footer))
    elif "inspection" not in result:
        lines.extend(reading_text(
            result, result, saved=saved, reference=reference,
            command_context=command_context, include_footer=False,
        ))
        footer = reading_footer_lines(
            result, saved=saved, reference=reference, command_context=command_context,
        )
        if "sample_chapters" in result and (issues := result.get("chapter_diagnostics")):
            footer.extend(_overview_diagnostic_lines(issues, for_sample=True))
        if footer:
            lines.extend(("", "---", *footer))
    return "\n".join(lines)
