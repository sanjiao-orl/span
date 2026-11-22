"""Read the source, selections and pending positions from a previous CLI result."""

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .model import (MAX_BODY_CHARS, MAX_RESULT_BYTES, MAX_SELECTIONS, Scope, Span, atomic_write, integer,
                    parse_paragraph, parse_position, source_span)
from .reading import SELECTION_ORIGIN_LABELS, ReadSelection, context_labels, normalize_delivery, selection_origins


TOKEN_PREFIX = "span:"
TEXT_HEADER = "寻章结果 v2"
LEGACY_TEXT_HEADER = "寻章结果 v1"
TEXT_STATE = "寻章状态 "
LEGACY_TEXT_STATE = "寻章接续 "
STATE_PREFIX = "state_"
STATE_VERSION = 1
STATE_ID = re.compile(r"state_([0-9a-f]{24})")
STATE_KEYS = {"state_version", "scope", "text_options", "chapter_numbers", "empty_chapters",
              "matches", "hit_chapters", "candidates", "example_spans", "example_refs", "group_refs",
              "multi_query", "continuation", "fragments", "match_selection_origin", "example_selection_origin"}


@dataclass(frozen=True)
class StoredState:
    state_id: str
    path: Path
    raw: bytes


def result_reading(result):
    if isinstance(result.get("examples"), dict):
        return result["examples"]
    if isinstance(result.get("reading"), dict):
        return result["reading"]
    return result


def handoff_token(result):
    state = {key: result[key] for key in ("scope", "text_options") if key in result}
    reading = result_reading(result)
    state["continuation"] = reading["continuation"]
    if isinstance(result.get("examples"), dict):
        state["example_spans"] = reading.get("selected_spans", [])
        origins = selection_origins([reading.get("selection_origin")])
        if origins:
            state["example_selection_origin"] = origins[0]
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii")


def result_state(result):
    state = {key: result[key] for key in ("scope", "text_options") if key in result}
    state["state_version"] = STATE_VERSION
    if "chapters" in result:
        state["chapter_numbers"] = [row["chapter"] for row in result["chapters"]]
        empty = [row["chapter"] for row in result["chapters"] if row["first_paragraph"] is None]
        if empty:
            state["empty_chapters"] = empty
    elif "matches" in result:
        state["matches"] = [{"read_spans": row["read_spans"]} for row in result["matches"]]
        origins = selection_origins([result.get("match_selection_origin")])
        if origins:
            state["match_selection_origin"] = origins[0]
    elif "hit_chapters" in result:
        state["hit_chapters"] = [{"read_span": row["read_span"]} for row in result["hit_chapters"]]
    elif "candidates" in result:
        state["candidates"] = [{
            **({"term": row["term"]} if isinstance(row.get("term"), str) and row["term"] else {}),
            "left_spans": row["left"].get("read_spans", []),
            "right_spans": row["right"].get("read_spans", []),
            "read_spans": row["read_spans"],
        } for row in result["candidates"]]
    reading = result_reading(result)
    if isinstance(result.get("examples"), dict):
        state["example_spans"] = reading.get("selected_spans", [])
        origins = selection_origins([reading.get("selection_origin")])
        if origins:
            state["example_selection_origin"] = origins[0]
    if "queries" in result:
        state["multi_query"] = True
    for key in ("example_refs", "group_refs"):
        if key in reading:
            state[key] = reading[key]
    if "continuation" in reading:
        state["continuation"] = reading["continuation"]
    if "fragments" in reading or "groups" in reading:
        state["fragments"] = []
        for row in result_fragments(reading):
            fragment = {"read_span": row["read_span"]}
            if origins := selection_origins(row.get("selection_origins")):
                fragment["selection_origins"] = origins
            if contexts := context_labels(row.get("context_labels")):
                fragment["context_labels"] = contexts
            state["fragments"].append(fragment)
    return state


def state_bytes(state):
    if (not isinstance(state, dict) or type(state.get("state_version")) is not int
            or state["state_version"] != STATE_VERSION or state.keys() - STATE_KEYS):
        raise ValueError("状态文件格式不受支持；请保留原始状态文件，或从原查询重新生成")
    if not isinstance(state.get("scope"), dict):
        raise ValueError("状态文件缺少书库快照和阅读范围")
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                      allow_nan=False).encode("utf-8")


def state_identity(state):
    raw = state_bytes(state)
    return STATE_PREFIX + hashlib.sha256(raw).hexdigest()[:24], raw


def _read_bytes(path):
    with Path(path).open("rb") as file:
        raw = file.read(MAX_RESULT_BYTES + 3)
    if len(raw) > MAX_RESULT_BYTES + 2:
        raise ValueError("上一步结果超过可接收的大小")
    return raw


def validate_result_scope(scope):
    if not isinstance(scope, dict):
        raise ValueError("上一步结果缺少书库快照和阅读范围；请重新生成结果")
    snapshot = scope.get("snapshot_id")
    if not isinstance(snapshot, str) or not snapshot.strip():
        raise ValueError("上一步结果文件损坏：scope.snapshot_id 缺失或无效，请重新生成结果")
    if type(scope.get("as_of")) is not int or type(scope.get("first_chapter")) is not int:
        raise ValueError("上一步结果文件损坏：scope 阅读范围须为整数，请重新生成结果")
    if type(scope.get("include_peripheral")) is not bool:
        raise ValueError("上一步结果文件损坏：scope.include_peripheral 须为布尔值，请重新生成结果")
    try:
        return Scope(snapshot, scope["as_of"], scope["first_chapter"], scope["include_peripheral"])
    except ValueError as exc:
        raise ValueError(f"上一步结果文件损坏：{exc}，请重新生成结果") from exc


def _json_object(raw, message):
    try:
        result = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(message) from exc
    if not isinstance(result, dict):
        raise ValueError("上一步结果缺少书库快照和阅读范围")
    validate_result_scope(result.get("scope"))
    return result


def restore_state(state):
    if "chapter_numbers" not in state:
        return state
    numbers, empty = state["chapter_numbers"], state.get("empty_chapters", [])
    if not isinstance(numbers, list) or not isinstance(empty, list):
        raise ValueError("上一步文本目录的章序无效")
    empty = {integer(number, "无正文的章序", 1) for number in empty}
    state = dict(state)
    state["chapters"] = [{"chapter": number, "first_paragraph": None if number in empty else f"C{number:04d}:1"}
                         for number in (integer(value, "目录章序", 1) for value in numbers)]
    return state


def read_state_file(path, *, expected_id=None):
    path = Path(path)
    try:
        raw = _read_bytes(path)
    except FileNotFoundError as exc:
        raise ValueError(f"找不到接续状态：{path}；请一并复制 .state.json 伴生文件，"
                         "或使用生成结果时的状态目录") from exc
    state = _json_object(raw, f"状态文件损坏或不是 UTF-8 JSON：{path}；请恢复原始状态或重新生成结果")
    canonical = state_bytes(state)
    actual_id = STATE_PREFIX + hashlib.sha256(canonical).hexdigest()[:24]
    if expected_id is not None and actual_id != expected_id:
        raise ValueError(f"状态内容与 {expected_id} 不符：{path}；请恢复该结果原来的伴生状态")
    if expected_id is None and STATE_ID.fullmatch(path.stem):
        expected_id = path.stem
        if actual_id != expected_id:
            raise ValueError(f"状态文件名与内容哈希不符：{path}；请恢复原始状态文件")
    return restore_state(state)


def _verify_state_file(path, raw, message):
    existing = _json_object(_read_bytes(path), f"{message}：{path}")
    if state_bytes(existing) != raw:
        raise ValueError(f"状态目标已包含不同内容，未覆盖：{path}")


def write_state_file(path, raw):
    path = Path(path)
    if path.exists():
        _verify_state_file(path, raw, "已有状态文件无法解析")
        return
    try:
        atomic_write(path, raw, overwrite=False, temp_factory=tempfile.NamedTemporaryFile,
                     verify=lambda temporary: _verify_state_file(temporary, raw, "待发布的状态文件无法解析"))
    except FileExistsError:
        _verify_state_file(path, raw, "已有状态文件无法解析")


def prepare_state(result):
    return state_identity(result_state(result))


def should_save_state(result, has_output):
    reading = result_reading(result)
    return bool(
        has_output
        or reading.get("continuation") is not None
        or reading.get("selected_spans")
        or reading.get("example_refs")
        or reading.get("group_refs")
        or "candidates" in result
        or result.get("hit_chapters")
    )


def save_state(result, directory):
    state_id, raw = prepare_state(result)
    path = Path(directory) / f"{state_id}.json"
    try:
        write_state_file(path, raw)
    except OSError as exc:
        raise ValueError(f"无法写入状态目录 {Path(directory)}；请用 --state-dir 指定可写目录") from exc
    return StoredState(state_id, path, raw)


def save_text_result(result, body, state_id):
    if not STATE_ID.fullmatch(state_id):
        raise ValueError("保存文本需要有效的短状态柄")
    return f"{TEXT_HEADER}\n{TEXT_STATE}{state_id}\n{body}"


def inherited_text_options(previous):
    options = previous.get("text_options", {}) if previous is not None else {}
    keys = {"encoding", "chapter_pattern", "paragraphs", "heading_level", "single_chapter"}
    if not isinstance(options, dict) or options.keys() - keys:
        raise ValueError("上一步结果中的文本解析设置无效")
    if "encoding" in options and (not isinstance(options["encoding"], str) or not options["encoding"]):
        raise ValueError("上一步结果中的文本解析设置无效")
    if "chapter_pattern" in options and not isinstance(options["chapter_pattern"], str):
        raise ValueError("上一步结果中的文本解析设置无效")
    if "paragraphs" in options and options["paragraphs"] not in ("lines", "blank-lines"):
        raise ValueError("上一步结果中的文本解析设置无效")
    if "heading_level" in options and (
        type(options["heading_level"]) is not int or not 1 <= options["heading_level"] <= 6
    ):
        raise ValueError("上一步结果中的文本解析设置无效")
    if "single_chapter" in options and type(options["single_chapter"]) is not bool:
        raise ValueError("上一步结果中的文本解析设置无效")
    active_modes = sum((
        "chapter_pattern" in options,
        "heading_level" in options,
        options.get("single_chapter") is True,
    ))
    if active_modes > 1:
        raise ValueError("上一步结果中的文本解析设置无效")
    return options


def load_result(path, *, state_dir=None):
    if STATE_ID.fullmatch(path):
        if state_dir is None:
            raise ValueError("使用短状态柄时请指定书库、TXT 或 --state-dir")
        return read_state_file(Path(state_dir) / f"{path}.json", expected_id=path)
    if path.startswith(STATE_PREFIX) and not Path(path).exists():
        hint_dir = state_dir if state_dir is not None else "未指定"
        raise ValueError(
            f"找不到短状态柄 {path}（状态目录：{hint_dir}）；请确认完整 state_<24位十六进制>、"
            "保持 --library / --state-dir / --run 一致，或改传 --output 保存的文件"
        )
    if path.startswith(TOKEN_PREFIX):
        try:
            raw = base64.b64decode(path[len(TOKEN_PREFIX):], altchars=b"-_", validate=True)
        except ValueError as exc:
            raise ValueError("无效的续读值，请复制完整的 --from-result 参数") from exc
        if len(raw) > MAX_RESULT_BYTES + 2:
            raise ValueError("上一步结果超过可接收的大小")
        return _json_object(raw, "续读值无效，请复制完整的 --from-result 参数")
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_RESULT_BYTES + 3)
        source = None
    else:
        source = Path(path)
        try:
            raw = _read_bytes(source)
        except FileNotFoundError as exc:
            raise ValueError(f"找不到结果文件：{source}；请检查路径或传入有效的 state_... 状态柄") from exc
    try:
        payload = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("请传入 Span 用 --output 保存的结果文件、UTF-8 JSON、状态文件或续读值") from exc
    header = payload.partition("\n")[0].removesuffix("\r")
    if header == TEXT_HEADER:
        lines = payload.splitlines()
        if len(lines) < 2 or not lines[1].startswith(TEXT_STATE):
            raise ValueError("文本结果缺少短状态柄")
        state_id = lines[1][len(TEXT_STATE):]
        if not STATE_ID.fullmatch(state_id):
            raise ValueError("文本结果中的短状态柄无效")
        if source is None:
            raise ValueError("从标准输入无法定位文本的伴生状态；请传入保存的文本文件或完整状态文件")
        companion = source.with_name(source.name + ".state.json")
        return read_state_file(companion, expected_id=state_id)
    if header == LEGACY_TEXT_HEADER:
        footer = payload.rstrip("\r\n").rpartition("\n")[2]
        if not footer.startswith(LEGACY_TEXT_STATE):
            raise ValueError("旧文本结果缺少接续信息")
        result = _json_object(footer[len(LEGACY_TEXT_STATE):].encode("utf-8"), "旧文本结果的接续信息无效")
        return restore_state(result)
    result = _json_object(raw, "请传入 Span 用 --output 保存的结果文件、UTF-8 JSON、状态文件或完整续读值")
    if "state_version" in result:
        state_bytes(result)
        if source is not None and STATE_ID.fullmatch(source.stem):
            actual_id, _ = state_identity(result)
            if actual_id != source.stem:
                raise ValueError(f"状态文件名与内容哈希不符：{source}；请恢复原始状态文件")
        return restore_state(result)
    return result


def handoff_scope(previous, *, snapshot_id, as_of, first_chapter, include_peripheral):
    old = validate_result_scope(previous)
    if snapshot_id is not None and snapshot_id != old.snapshot_id:
        raise ValueError("指定的快照与上一步结果不同")
    effective_as_of = old.as_of if as_of is None else min(old.as_of, integer(as_of, "阅读进度"))
    effective_first = max(old.first_chapter, integer(first_chapter, "起始章", 1))
    effective_peripheral = (
        old.include_peripheral
        if include_peripheral is None
        else (old.include_peripheral and bool(include_peripheral))
    )
    if effective_as_of < effective_first and (effective_as_of > 0 or effective_first > 1):
        raise ValueError(f"收窄后的末章 C{effective_as_of:04d} 不能早于当前起始章 C{effective_first:04d}")
    return Scope(old.snapshot_id, effective_as_of, effective_first, effective_peripheral)


def choose(rows, picks):
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("上一步结果没有有效的可选条目")
    if not picks:
        return rows
    if not rows:
        raise ValueError("上一步结果没有可选条目")
    selected = []
    for index in dict.fromkeys(picks):
        integer(index, "条目序号", 1, len(rows))
        selected.append(rows[index - 1])
    return selected


def result_fragments(result):
    result = result_reading(result)
    if "groups" not in result:
        return choose(result.get("fragments"), ())
    return [fragment for group in choose(result["groups"], ())
            for fragment in choose(group.get("fragments"), ())]


def hit_chapter_picks(result, picks):
    if not picks:
        raise ValueError("请选择命中章当前页的条目序号（--pick），或直接传入回读坐标")
    return [{"read_span": row.get("read_span"), "selection_origins": ["hit_chapter_anchor"]}
            for row in choose(result["hit_chapters"], picks)]


def collect_picks(result, picks):
    if "hit_chapters" in result:
        return hit_chapter_picks(result, picks)
    return choose(result_fragments(result), picks)


def read_picks(result, picks, snapshot):
    if "queries" in result or result.get("multi_query"):
        hint = ("也可用 --example Q查询号E例段号选择已展开的例段"
                if result_reading(result).get("example_refs") else "")
        raise ValueError("多组查询的 --pick 有歧义，请直接传入对应查询组的回读坐标。" + hint)
    if "chapters" in result:
        selections = []
        for chapter in choose(result["chapters"], picks):
            paragraph = chapter.get("first_paragraph")
            if not isinstance(paragraph, str):
                raise ValueError(f"章目 C{chapter.get('chapter', '?')} 没有可读的起始段落，请另选章目")
            selections.append(ReadSelection(paragraphs=(parse_paragraph(paragraph),)))
        return selections
    if "matches" in result:
        selections = []
        origins = selection_origins([result.get("match_selection_origin")])
        for match in choose(result["matches"], picks):
            spans = parse_spans(match.get("read_spans", [match.get("span")]), snapshot)
            selections.append(picked_selection(spans, origins, matches=True))
        return selections
    if "hit_chapters" in result:
        return [picked_selection(parse_spans([row["read_span"]], snapshot), row["selection_origins"], matches=True)
                for row in hit_chapter_picks(result, picks)]
    if "candidates" in result:
        selections = []
        for cand in choose(result["candidates"], picks):
            term = cand.get("term") if isinstance(cand.get("term"), str) and cand["term"] else None
            left = cand.get("left_spans") or cand.get("left", {}).get("read_spans", [])
            right = cand.get("right_spans") or cand.get("right", {}).get("read_spans", [])
            if left and right:
                for side_name, raw_spans in (("左", left), ("右", right)):
                    spans = parse_spans(raw_spans, snapshot)
                    label = _candidate_context_label(term, side_name)
                    contexts = tuple((span, label) for span in spans) if label else ()
                    selections.append(ReadSelection(
                        spans=spans, match_spans=spans, label=label, context_anchors=contexts,
                    ))
            else:
                side_name = "左" if left else ("右" if right else None)
                raw_spans = left or right or cand.get("read_spans", [])
                spans = parse_spans(raw_spans, snapshot)
                label = _candidate_context_label(term, side_name)
                contexts = tuple((span, label) for span in spans) if label else ()
                selections.append(ReadSelection(
                    spans=spans, match_spans=spans, label=label, context_anchors=contexts,
                ))
        return selections
    if "items" in result:
        rows, key = choose(result["items"], picks), "span"
    elif "citations" in result:
        rows, key = choose(result["citations"], picks), "citation"
        if any(row.get("status") != "ok" for row in rows):
            raise ValueError("只可选择已核验为 ok 的引用")
    else:
        fragments = result["fragments"] if "fragments" in result and "groups" not in result else result_fragments(result)
        rows, key = choose(fragments, picks), "read_span"
    picked = []
    for row in rows:
        spans = parse_spans([row.get(key)], snapshot)
        contexts = context_labels(row.get("context_labels"))
        context_anchors = tuple((span, label) for span in spans for label in contexts)
        picked.append(picked_selection(spans, row.get("selection_origins"), context_anchors=context_anchors))
    return picked


def _candidate_context_label(term, side_name):
    if term and side_name in ("左", "右"):
        return f"「{term}」{side_name}"
    if term:
        return f"「{term}」"
    if side_name in ("左", "右"):
        return side_name
    return None


def picked_selection(spans, origins, *, matches=False, context_anchors=()):
    origins = selection_origins(origins)
    anchors = tuple((span, origin) for span in spans for origin in origins)
    return ReadSelection(
        spans=spans,
        match_spans=spans if matches else (),
        selection_anchors=anchors,
        context_anchors=tuple(context_anchors),
    )


def reading_references(result, snapshot):
    reading = result_reading(result)
    group_values = reading.get("group_refs")
    example_values = reading.get("example_refs")
    if group_values is None and example_values is None:
        return [], []
    message = "上一步多组结果的组或例段引用无效"
    if not isinstance(group_values, list) or not isinstance(example_values, list):
        raise ValueError(message)
    groups = {}
    group_rows = {}
    for row in group_values:
        if not isinstance(row, dict):
            raise ValueError(message)
        reference = row.get("reference")
        match = (re.fullmatch(r"Q([1-9][0-9]*)(?:E([1-9][0-9]*))?", reference)
                 if isinstance(reference, str) else None)
        if match is None:
            raise ValueError(message)
        query = integer(row.get("query"), "查询组序号", 1, MAX_SELECTIONS)
        group = integer(row.get("group"), "组序号", 1, MAX_SELECTIONS)
        if query != int(match[1]) or reference in groups or group in group_rows:
            raise ValueError(message)
        groups[reference] = group
        group_rows[group] = dict(row)
    examples = {}
    for row in example_values:
        if not isinstance(row, dict):
            raise ValueError(message)
        reference = row.get("reference")
        match = (re.fullmatch(r"Q([1-9][0-9]*)E([1-9][0-9]*)", reference)
                 if isinstance(reference, str) else None)
        if match is None:
            raise ValueError(message)
        query = integer(row.get("query"), "查询组序号", 1, MAX_SELECTIONS)
        example = integer(row.get("example"), "例段序号", 1, MAX_SELECTIONS)
        group = integer(row.get("group"), "组序号", 1, MAX_SELECTIONS)
        if (query != int(match[1]) or example != int(match[2]) or reference in examples
                or not isinstance(row.get("span"), str)
                or groups.get(reference, groups.get(f"Q{query}")) != group):
            raise ValueError(message)
        parse_spans([row["span"]], snapshot)
        example_row = {"reference": reference, "query": query, "example": example, "group": group, "span": row["span"]}
        if origins := selection_origins([row.get("selection_origin")]):
            example_row["selection_origin"] = origins[0]
        examples[reference] = example_row
    return list(group_rows.values()), list(examples.values())


def read_example_picks(result, picks, snapshot):
    references = result_reading(result).get("example_refs")
    if references is not None:
        _, references = reading_references(result, snapshot)
        selected = {row["reference"]: row for row in references}
        groups = []
        for reference in dict.fromkeys(picks):
            if reference not in selected:
                raise ValueError("多组查询的例段序号须为结果中实际列出的 Q查询号E例段号，例如 Q1E2")
            row = selected[reference]
            spans = parse_spans([row["span"]], snapshot)
            groups.append(picked_selection(spans, [row.get("selection_origin")], matches=True))
        return groups
    if result.get("multi_query") or "queries" in result:
        raise ValueError("上一步多组结果没有可选择的例段")
    examples = result.get("examples")
    values = examples.get("selected_spans") if isinstance(examples, dict) else result.get("example_spans")
    origin = examples.get("selection_origin") if isinstance(examples, dict) else result.get("example_selection_origin")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("上一步结果没有可选择的例段；请使用带例段的 find 结果")
    if not values:
        raise ValueError("上一步结果没有可选择的例段")
    groups = []
    for value in dict.fromkeys(picks):
        try:
            index = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("单组查询的例段序号为数字，多组查询使用 Q查询号E例段号") from exc
        integer(index, "例段序号", 1, len(values))
        spans = parse_spans([values[index - 1]], snapshot)
        groups.append(picked_selection(spans, [origin], matches=True))
    return groups


def continuation_group_picks(result, values, snapshot):
    reading = result_reading(result)
    if reading.get("group_refs") is not None or reading.get("example_refs") is not None:
        references, _ = reading_references(result, snapshot)
        groups = {row["reference"]: row["group"] for row in references}
        try:
            return {groups[value] for value in values}
        except KeyError as exc:
            raise ValueError("多组续读使用结果列出的组引用，例如 Q1 或 Q1E1") from exc
    selected = set()
    for value in values:
        try:
            value = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("单组阅读的 --continue-group 使用数字组号") from exc
        selected.add(integer(value, "组序号", 1, MAX_SELECTIONS))
    return selected


def parse_spans(values, snapshot):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("上一步结果包含无效的字符范围")
    return tuple(source_span(value, snapshot) for value in values)


def continuation_selection(row, snapshot):
    spans = parse_spans(row.get("spans"), snapshot)
    coordinates = {span.coordinate: span for span in spans}
    anchors = row.get("selection_anchors", [])
    if not isinstance(anchors, list) or len(anchors) > MAX_SELECTIONS * len(SELECTION_ORIGIN_LABELS):
        raise ValueError("上一步结果包含无效的选材范围")
    selected = []
    for pair in anchors:
        if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                or pair[0] not in coordinates or not selection_origins([pair[1]])):
            raise ValueError("上一步结果包含无效的选材范围")
        selected.append((coordinates[pair[0]], pair[1]))
    raw_contexts = row.get("context_anchors", [])
    if not isinstance(raw_contexts, list) or len(raw_contexts) > MAX_SELECTIONS * 16:
        raise ValueError("上一步结果包含无效的上下文范围")
    parsed_contexts = []
    for pair in raw_contexts:
        if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
                or pair[0] not in coordinates or not isinstance(pair[1], str) or not pair[1]):
            raise ValueError("上一步结果包含无效的上下文范围")
        parsed_contexts.append((coordinates[pair[0]], pair[1]))
    return ReadSelection(spans=spans, match_spans=parse_spans(row.get("match_spans"), snapshot),
                         resume=parse_position(row.get("next")),
                         group=integer(row.get("group"), "组序号", 1, MAX_SELECTIONS),
                         label=row.get("label") if isinstance(row.get("label"), str) else None,
                         selection_anchors=tuple(dict.fromkeys(selected)),
                         context_anchors=tuple(dict.fromkeys(parsed_contexts)))


def continuation(result, snapshot):
    original = result
    result = result_reading(result)
    pending = result.get("continuation")
    if not isinstance(pending, dict):
        if "candidates" in original:
            count = len(original["candidates"])
            if count:
                raise ValueError(
                    f"上一步 compare 结果没有待续读正文（共 {count} 项差异候选）；"
                    f"请加 --pick 1..{count}（例如 --pick 1 --group-budget 1200 --before 1 --after 1）"
                )
            raise ValueError("上一步 compare 结果没有差异候选可读取；请放宽筛选参数后重跑 compare")
        if "chapters" in original:
            count = len(original["chapters"])
            if count:
                raise ValueError(
                    f"上一步目录结果没有待续读正文（共 {count} 章）；请加 --pick 1..{count} 读章首，或用 --chapter C编号 读整章"
                )
            raise ValueError("上一步目录结果为空，没有可读取的章节")
        if "hit_chapters" in original:
            count = len(choose(original["hit_chapters"], ()))
            if count:
                raise ValueError(f"上一步命中章结果没有待续读选区；请用 --pick 1..{count} 读取本页回读位置")
            raise ValueError("上一步命中章结果的当前页没有可读取的条目")
        if "queries" in original or original.get("multi_query"):
            refs = [row.get("reference") for row in result.get("example_refs", []) if isinstance(row, dict) and row.get("reference")]
            if refs:
                raise ValueError(
                    f"上一步多组查询没有待续读选区；请用 --example {refs[0]} 等扩读，或直接传入匹配坐标"
                )
            raise ValueError("上一步多组查询没有待续读选区；请直接传入匹配坐标，或重跑 find 时加 --examples 1")
        if "matches" in original:
            ex_count = len(result.get("selected_spans", []) or original.get("example_spans", []))
            m_count = len(original.get("matches", []))
            hints = []
            if ex_count:
                hints.append(f"--example 1..{ex_count}")
            if m_count:
                hints.append(f"--pick 1..{m_count}")
            if hints:
                raise ValueError(f"上一步 find 结果没有待续读选区；可用 {' 或 '.join(hints)} 选择读取")
        raise ValueError("上一步结果没有待续读选区；要开始新读取，请用 --pick 或传入新的选区")
    rows = choose(pending.get("groups"), ())
    integer(len(rows), "待续读组数", 1, MAX_SELECTIONS)
    group_budget = pending.get("group_budget")
    if group_budget is not None:
        integer(group_budget, "每组正文预算", 1, MAX_BODY_CHARS)
    elif len(rows) != 1:
        raise ValueError("共享预算的续读结果只能有一组选区")
    group_budget_source = pending.get("group_budget_source")
    if group_budget_source not in (None, "auto", "explicit"):
        raise ValueError("上一步结果包含无效的每组预算来源")
    if group_budget is None and group_budget_source is not None:
        raise ValueError("共享预算的续读结果不应包含每组预算来源")
    if type(pending.get("whole_paragraphs")) is not bool:
        raise ValueError("上一步结果缺少有效的扩读设置")
    groups = [continuation_selection(row, snapshot) for row in rows]
    if len({group.group for group in groups}) != len(groups):
        raise ValueError("上一步结果包含重复的续读组序号")
    return groups, {"before": integer(pending.get("before"), "向前扩读段数"),
                    "after": integer(pending.get("after"), "向后扩读段数"),
                    "whole_paragraphs": pending["whole_paragraphs"],
                    "budget": integer(pending.get("budget"), "正文预算", 1, MAX_BODY_CHARS),
                    "group_budget": group_budget,
                    "group_budget_source": group_budget_source,
                    "prior_delivery": normalize_delivery(pending.get("delivery", []))}


def scope_continuation(groups, options, group_refs, example_refs, visible_chapters, snapshot):
    """Narrow continuation groups, prior delivery, and query refs to the current visible scope."""
    visible = set(visible_chapters)
    references = {row["group"]: row["reference"] for row in group_refs}
    prior_delivery = [
        row for row in options.get("prior_delivery", [])
        if Span.parse(row["target_span"]).chapter in visible
    ]
    scoped_groups = []
    for group in groups:
        spans = tuple(span for span in group.spans if span.chapter in visible)
        match_spans = tuple(span for span in group.match_spans if span.chapter in visible)
        if not spans:
            continue
        if group.resume is not None and group.resume[0] not in visible:
            name = references.get(group.group, f"组 {group.group}")
            raise ValueError(
                f"{name} 的待续位置 C{group.resume[0]:04d}:{group.resume[1]} "
                "已超出当前授权或章节范围；请用 --pick 或重新查询范围内选区后再读取"
            )
        anchors = tuple((span, origin) for span, origin in group.selection_anchors if span.chapter in visible)
        contexts = tuple((span, label) for span, label in group.context_anchors if span.chapter in visible)
        scoped_groups.append(replace(
            group, spans=spans, match_spans=match_spans,
            selection_anchors=anchors, context_anchors=contexts,
        ))
    if not scoped_groups:
        raise ValueError("上一步结果中的所有待续读选区都已超出当前授权或章节范围；请重新查询或选择范围内的章节")
    scoped_examples = [dict(row) for row in example_refs if source_span(row["span"], snapshot).chapter in visible]
    kept_groups = (
        {group.group for group in scoped_groups}
        | {row["group"] for row in prior_delivery}
        | {row["group"] for row in scoped_examples}
    )
    scoped_group_refs = [dict(row) for row in group_refs if row["group"] in kept_groups]
    return (
        scoped_groups,
        {**options, "prior_delivery": prior_delivery},
        scoped_group_refs,
        scoped_examples,
    )
