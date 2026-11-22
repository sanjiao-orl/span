"""Persistent run identity, scoped history and single-writer recording."""

import json
import os
import re
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .documents import normalize_conventions
from .library import open_scope
from .model import MAX_RESULT_BYTES, Scope, atomic_write, integer
from .search import resolve_find_examples
from .sources import ScopedText, read_text_source_details


RUN_VERSION = 1
RUN_ENV = "SPAN_RUN_DIR"
RUN_FILE = "run.json"
CALL_LOG = "calls.jsonl"
RUN_COMMANDS = {"overview", "find", "compare", "read", "resume"}
SNAPSHOT_ID = re.compile(r"snap_[0-9a-f]{64}")
RUN_ID = re.compile(r"run_[0-9a-f]{32}")
MANIFEST_KEYS = {"run_version", "run_id", "created_at", "tool_version", "source", "snapshot_id", "initial_scope"}
OPTIONAL_MANIFEST_KEYS = {"conventions"}
TEXT_OPTION_KEYS = {"encoding", "chapter_pattern", "paragraphs", "heading_level", "single_chapter"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolved(path):
    return Path(path).expanduser().resolve()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def parse_scope(value):
    if not isinstance(value, dict) or set(value) != {"snapshot_id", "as_of", "first_chapter", "include_peripheral"}:
        raise ValueError("运行范围缺少完整的快照和边界字段")
    snapshot = value["snapshot_id"]
    if not isinstance(snapshot, str) or not SNAPSHOT_ID.fullmatch(snapshot):
        raise ValueError("运行快照 ID 格式不正确")
    if type(value["include_peripheral"]) is not bool:
        raise ValueError("运行外围章设置无效")
    return Scope(snapshot, value["as_of"], value["first_chapter"], value["include_peripheral"])


def _valid_text_options(options, *, is_epub=False):
    if not isinstance(options, dict) or not set(options) <= TEXT_OPTION_KEYS:
        return False
    if options.get("paragraphs") not in ("lines", "blank-lines"):
        return False
    if "chapter_pattern" in options and not isinstance(options["chapter_pattern"], str):
        return False
    if "heading_level" in options and (type(options["heading_level"]) is not int or not 1 <= options["heading_level"] <= 6):
        return False
    if "single_chapter" in options and type(options["single_chapter"]) is not bool:
        return False
    active_modes = sum((
        "chapter_pattern" in options,
        "heading_level" in options,
        options.get("single_chapter") is True,
    ))
    if is_epub:
        if "encoding" in options or "heading_level" in options:
            return False
        return active_modes <= 1
    if not isinstance(options.get("encoding"), str) or not options["encoding"]:
        return False
    return active_modes == 1


def normalize_source(source):
    if not isinstance(source, dict) or not set(source) <= {"kind", "path", "text_options"}:
        raise ValueError("运行来源字段无效")
    kind = source.get("kind")
    path = source.get("path")
    if kind not in ("text", "library") or not isinstance(path, str) or not path:
        raise ValueError("运行来源须为直接文本文件或已有书库")
    options = source.get("text_options", {})
    if kind == "text":
        is_epub = Path(path).suffix.casefold() == ".epub"
        if not _valid_text_options(options, is_epub=is_epub):
            raise ValueError("运行缺少完整的文本解析设置")
    elif options:
        raise ValueError("书库运行不能携带文本解析设置")
    return {"kind": kind, "path": str(resolved(path)),
            **({"text_options": dict(options)} if kind == "text" else {})}


@dataclass(frozen=True)
class Run:
    root: Path
    manifest: dict

    @property
    def run_id(self):
        return self.manifest["run_id"]

    @property
    def snapshot_id(self):
        return self.manifest["snapshot_id"]

    @property
    def initial_scope(self):
        return parse_scope(self.manifest["initial_scope"])

    @property
    def source(self):
        return normalize_source(self.manifest["source"])

    @property
    def conventions(self):
        return normalize_conventions(self.manifest.get("conventions"))

    def records(self):
        return read_run_records(self)

    def current_scope(self, records=None):
        scope = self.initial_scope
        for record in self.records() if records is None else records:
            if record["event"] == "scope_expand":
                previous = parse_scope(record["previous_scope"])
                expanded = parse_scope(record["scope"])
                if previous != scope:
                    raise ValueError("运行范围事件的上一范围与历史不一致")
                require_expansion(previous, expanded)
                scope = expanded
        return scope


def load_run(directory):
    root = resolved(directory)
    path = root / RUN_FILE
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        runs_dir = root / "runs"
        if runs_dir.is_dir():
            candidates = sorted(
                (
                    candidate for candidate in runs_dir.iterdir()
                    if candidate.is_dir() and RUN_ID.fullmatch(candidate.name) and (candidate / RUN_FILE).is_file()
                ),
                key=lambda candidate: (candidate / RUN_FILE).stat().st_mtime,
            )
            if len(candidates) == 1:
                raise ValueError(
                    f"运行目录缺少 {RUN_FILE}：{root}；你传入的像是任务目录，"
                    f"实际应传给 --run 的运行目录是：{candidates[0]}"
                ) from exc
            if candidates:
                raise ValueError(
                    f"运行目录缺少 {RUN_FILE}：{root}；你传入的像是任务目录，"
                    f"请从 {runs_dir} 中选择具体运行（共 {len(candidates)} 个，最新如 {candidates[-1]}）"
                ) from exc
        raise ValueError(f"运行目录缺少 {RUN_FILE}：{root}（请传入 research init 返回的 run_directory）") from exc
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"运行清单损坏或不是 UTF-8 JSON：{path}") from exc
    if (
        not isinstance(manifest, dict)
        or not MANIFEST_KEYS <= set(manifest)
        or not set(manifest) <= (MANIFEST_KEYS | OPTIONAL_MANIFEST_KEYS)
    ):
        raise ValueError(f"运行清单字段不符合版本 {RUN_VERSION}：{path}")
    if "conventions" in manifest:
        normalize_conventions(manifest["conventions"])
    if manifest["run_version"] != RUN_VERSION or type(manifest["run_version"]) is not int:
        raise ValueError("运行清单格式版本不受支持")
    if not isinstance(manifest["run_id"], str) or not RUN_ID.fullmatch(manifest["run_id"]):
        raise ValueError("运行 ID 格式不正确")
    if manifest["run_id"] != root.name:
        raise ValueError("运行目录名与 run.json 中的运行 ID 不一致")
    if not all(isinstance(manifest[key], str) and manifest[key] for key in ("created_at", "tool_version")):
        raise ValueError("运行清单缺少创建时间或工具版本")
    normalize_source(manifest["source"])
    scope = parse_scope(manifest["initial_scope"])
    if not isinstance(manifest["snapshot_id"], str) or not SNAPSHOT_ID.fullmatch(manifest["snapshot_id"]):
        raise ValueError("运行固定快照 ID 格式不正确")
    if scope.snapshot_id != manifest["snapshot_id"]:
        raise ValueError("运行初始范围与固定快照不一致")
    return Run(root, manifest)


def create_run(task_directory, *, source, snapshot_id, initial_scope, conventions=None):
    source = normalize_source(source)
    scope = parse_scope(asdict(initial_scope))
    if scope.snapshot_id != snapshot_id or not SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("运行初始范围与固定快照不一致")
    normalized_conventions = normalize_conventions(conventions)
    task = resolved(task_directory)
    parents = task / "runs"
    parents.mkdir(parents=True, exist_ok=True)
    while True:
        run_id = "run_" + uuid.uuid4().hex
        root = parents / run_id
        try:
            root.mkdir()
            break
        except FileExistsError:
            pass
    (root / "results").mkdir()
    (root / "states").mkdir()
    (root / CALL_LOG).touch()
    manifest = {
        "run_version": RUN_VERSION,
        "run_id": run_id,
        "created_at": utc_now(),
        "tool_version": __version__,
        "source": source,
        "snapshot_id": snapshot_id,
        "initial_scope": asdict(scope),
    }
    if normalized_conventions:
        manifest["conventions"] = normalized_conventions
    atomic_write(root / RUN_FILE, json_bytes(manifest) + b"\n")
    return Run(root, manifest)


def find_cwd_run(cwd=None):
    current = resolved(cwd or Path.cwd())
    for candidate in (current, *current.parents):
        if (candidate / RUN_FILE).is_file():
            return candidate
    return None


def resolve_run(args, *, environ=None, cwd=None):
    if args.command == "research" and args.action == "init":
        if args.run is not None:
            raise ValueError("research init 新建独立运行，不接受 --run")
        return None
    research_run = args.command == "research" and args.action in ("expand", "index")
    if args.command not in RUN_COMMANDS and not research_run:
        if args.run is not None:
            if args.command == "collect":
                raise ValueError("collect 属于书库操作，不支持 --run；请改用 --library 书库目录 --snapshot 快照ID --as-of 末章")
            raise ValueError("该命令不支持 --run")
        return None
    if args.run is None and (getattr(args, "text", None) is not None or getattr(args, "library", None) is not None):
        return None
    environment = os.environ if environ is None else environ
    candidates = []
    if args.run is not None:
        candidates.append(("--run", resolved(args.run)))
    if environment.get(RUN_ENV):
        candidates.append((RUN_ENV, resolved(environment[RUN_ENV])))
    discovered = find_cwd_run(cwd)
    if discovered is not None:
        candidates.append(("cwd", discovered))
    identities = {}
    for label, path in candidates:
        identities.setdefault(os.path.normcase(str(path)), (label, path))
    if len(identities) > 1:
        raise ValueError("显式运行、环境变量和 cwd 指向不同运行，请只保留一个入口")
    return load_run(next(iter(identities.values()))[1]) if identities else None


@contextmanager
def run_write_lock(run):
    path = run.root / ".writer.lock"
    with path.open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(f"运行正由另一个进程写入，请串行调用同一运行：{run.root}") from exc
        try:
            yield
        finally:
            file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def require_expansion(previous, current):
    if current.snapshot_id != previous.snapshot_id:
        raise ValueError("范围扩展不能改变运行快照")
    if (current.as_of < previous.as_of or current.first_chapter > previous.first_chapter
            or previous.include_peripheral and not current.include_peripheral):
        raise ValueError("research expand 只能扩大范围，不能收窄任何边界")


def _format_scope_bounds(scope):
    extent = "空范围（末章 0）" if scope.as_of == 0 else f"C{scope.first_chapter:04d}–C{scope.as_of:04d}"
    return extent + ("（含外围章）" if scope.include_peripheral else "")


def ensure_scope_within(scope, current):
    if scope.snapshot_id != current.snapshot_id:
        raise ValueError("稿件、结果或来源快照与当前运行不同")
    if (scope.as_of > current.as_of or scope.first_chapter < current.first_chapter
            or scope.include_peripheral and not current.include_peripheral):
        raise ValueError(
            f"操作超出运行当前范围（当前 {_format_scope_bounds(current)}，请求 {_format_scope_bounds(scope)}）；"
            "请先执行 research expand，或在本次命令中收窄范围"
        )


def _non_empty_text(value):
    return isinstance(value, str) and bool(value)


def _validate_call_record(record, line_number):
    status = record.get("status")
    if status not in ("success", "failure"):
        raise ValueError(f"运行调用日志第 {line_number} 行调用状态无效")
    if not _non_empty_text(record.get("call_id")) or not _non_empty_text(record.get("command")):
        raise ValueError(f"运行调用日志第 {line_number} 行缺少调用标识或命令")
    failed_stage = record.get("failed_stage")
    origin = record.get("origin")
    error = record.get("error")
    if status == "success":
        if not _non_empty_text(origin) or failed_stage is not None or error is not None:
            raise ValueError(f"运行调用日志第 {line_number} 行成功记录字段无效")
    else:
        if not _non_empty_text(failed_stage) or not isinstance(error, str):
            raise ValueError(f"运行调用日志第 {line_number} 行失败记录字段无效")
        if origin is not None and not _non_empty_text(origin):
            raise ValueError(f"运行调用日志第 {line_number} 行调用来源无效")
        if origin is None and failed_stage != "bind":
            raise ValueError(f"运行调用日志第 {line_number} 行缺少调用来源")
    scope = record.get("scope")
    if scope is not None:
        try:
            parse_scope(scope)
        except ValueError as exc:
            raise ValueError(f"运行调用日志第 {line_number} 行范围字段无效") from exc
    elif status == "success":
        raise ValueError(f"运行调用日志第 {line_number} 行缺少成功范围")
    for field in ("result_path", "state_id"):
        value = record.get(field)
        if value is not None and not _non_empty_text(value):
            raise ValueError(f"运行调用日志第 {line_number} 行字段 {field} 无效")


def _validate_run_record(record, line_number, run_id):
    if (not isinstance(record, dict) or type(record.get("record_version")) is not int
            or record["record_version"] != RUN_VERSION):
        raise ValueError(f"运行调用日志第 {line_number} 行格式不受支持")
    if (record.get("run_id") != run_id or type(record.get("sequence")) is not int
            or record.get("sequence") != line_number):
        raise ValueError(f"运行调用日志第 {line_number} 行身份或序号不连续")
    if (not _non_empty_text(record.get("time")) or type(record.get("process_id")) is not int
            or not _non_empty_text(record.get("execution_id"))):
        raise ValueError(f"运行调用日志第 {line_number} 行缺少有效元信息")
    event = record.get("event")
    if event == "call":
        _validate_call_record(record, line_number)
    elif event == "scope_expand":
        for field in ("previous_scope", "scope"):
            try:
                parse_scope(record.get(field))
            except ValueError as exc:
                raise ValueError(f"运行调用日志第 {line_number} 行范围扩展字段无效") from exc
    else:
        raise ValueError(f"运行调用日志第 {line_number} 行事件类型无效")


def read_run_records(run):
    path = run.root / CALL_LOG
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"运行缺少调用日志：{path}") from exc
    records = []
    for line_number, line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise ValueError(f"运行调用日志第 {line_number} 行损坏：{path}") from exc
        _validate_run_record(record, line_number, run.run_id)
        records.append(record)
    return records


def call_origin(args):
    if args.command == "find":
        examples = resolve_find_examples(
            query_groups=args.query_groups,
            examples=args.examples,
            has_text=args.text is not None,
            spread=args.spread,
            hit_chapters=args.hit_chapters,
        )
        return "find_examples" if examples else "find"
    if args.command == "compare":
        return "compare_examples" if args.examples else "compare"
    if args.command == "read":
        if args.sample is not None:
            return "sample"
        explicit = any((args.spans, args.chapter, args.item, args.paragraph, args.pick, args.example))
        if args.from_result is not None and not explicit:
            return "continuation"
        if args.from_result is not None and (args.pick or args.example):
            return "result_expansion"
        return "read"
    return args.command


class RunRecorder:
    def __init__(self, run):
        self.run = run
        self.records = run.records()
        self.current_scope = run.current_scope(self.records)
        self.sequence = len(self.records) + 1
        self.execution_id = uuid.uuid4().hex
        self.call_id = "call_" + uuid.uuid4().hex
        self.result_path = None

    def save_result(self, result):
        raw = json_bytes(result)
        if len(raw) > MAX_RESULT_BYTES:
            raise ValueError("结果超过输出上限，请减小页大小或正文预算")
        relative = Path("results") / f"{self.sequence:06d}-{self.call_id}.json"
        atomic_write(self.run.root / relative, raw + b"\n")
        self.result_path = relative.as_posix()
        return self.result_path

    def append(self, record):
        complete = {
            "record_version": RUN_VERSION,
            "run_id": self.run.run_id,
            "sequence": self.sequence,
            "time": utc_now(),
            "process_id": os.getpid(),
            "execution_id": self.execution_id,
            **record,
        }
        raw = json_bytes(complete) + b"\n"
        with (self.run.root / CALL_LOG).open("ab") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        self.records.append(complete)
        self.sequence += 1
        return complete

    def call(self, args, *, status, scope=None, state_id=None, failed_stage=None, error=None):
        command = f"research {args.action}" if args.command == "research" else args.command
        origin = None if status == "failure" and failed_stage == "bind" else (
            command if args.command == "research" else call_origin(args))
        return self.append({
            "event": "call",
            "call_id": self.call_id,
            "command": command,
            "origin": origin,
            "status": status,
            "scope": asdict(scope) if scope is not None else None,
            "result_path": self.result_path,
            "state_id": state_id,
            "failed_stage": failed_stage,
            "error": str(error)[:500] if error is not None else None,
        })

    def expand(self, previous, current):
        record = self.append({
            "event": "scope_expand",
            "previous_scope": asdict(previous),
            "scope": asdict(current),
        })
        self.current_scope = current
        return record


def bind_run(args, run, current_scope):
    if args.command not in RUN_COMMANDS:
        raise ValueError("首版运行仅支持 overview、find、compare、read、resume 和 research 管理命令")
    if args.library is not None or args.text is not None:
        raise ValueError("绑定运行时不能再显式指定 --text 或 --library；请使用 --run")
    if args.state_dir is not None:
        raise ValueError("运行固定使用其 states 目录，不再另传 --state-dir")
    if (args.text_encoding or args.text_pattern is not None or args.text_paragraphs
            or getattr(args, "text_heading_level", None) is not None
            or getattr(args, "text_single_chapter", False)):
        raise ValueError("运行沿用初始化时的文本解析设置，不能在普通调用中改写")
    source = run.source
    if source["kind"] == "text":
        options = source["text_options"]
        args.text = Path(source["path"])
        args.text_encoding = options.get("encoding")
        args.text_pattern = options.get("chapter_pattern")
        args.text_paragraphs = options["paragraphs"]
        args.text_heading_level = options.get("heading_level")
        args.text_single_chapter = bool(options.get("single_chapter", False))
    else:
        args.library = Path(source["path"])
    args.state_dir = run.root / "states"
    if getattr(args, "snapshot", None) is None:
        if hasattr(args, "snapshot"):
            args.snapshot = run.snapshot_id
    elif args.snapshot != run.snapshot_id:
        raise ValueError("指定快照与当前运行固定快照不同")
    if args.as_of is None:
        args.as_of = current_scope.as_of
    if args.from_chapter is None:
        args.from_chapter = current_scope.first_chapter
    if current_scope.include_peripheral:
        args.include_peripheral = True
    if args.command == "resume" and args.draft is None:
        args.draft = run.root / "当前稿.md"
    return args


@contextmanager
def run_library(run, scope):
    source = run.source
    if source["kind"] == "library":
        with open_scope(source["path"], scope) as library:
            yield library
        return
    _, chapters, _, details = read_text_source_details(
        source["path"], **source["text_options"], expected_snapshot=run.snapshot_id,
    )
    with nullcontext(ScopedText(chapters, scope, details)) as library:
        yield library
