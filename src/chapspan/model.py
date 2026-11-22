"""Coordinates and limits shared by the application services."""

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


MAX_PAGE = 100
MAX_CHAPTER_RANGE_SPAN = 100
MAX_SELECTIONS = 32
MAX_BODY_CHARS = 24_000
MAX_RESULT_BYTES = 512_000
MAX_SQL_INTEGER = 2**63 - 1
MAX_CONVENTION_CHARS = 500
CONVENTION_FIELDS = (
    "purpose",
    "audience",
    "evaluation_framework",
    "external_policy",
    "version_note",
)
CONVENTION_LABELS = {
    "purpose": "用途",
    "audience": "受众",
    "evaluation_framework": "评价框架",
    "external_policy": "外部资料政策",
    "version_note": "版本说明",
}


def integer(value, label, minimum=0, maximum=MAX_SQL_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}须为 {minimum} 到 {maximum} 之间的整数")
    return value


def display_title(title):
    return title if len(title) <= 120 else title[:119] + "…"


@dataclass(frozen=True, order=True)
class Span:
    chapter: int
    start: int
    end: int

    def __post_init__(self):
        integer(self.chapter, "章序", 1)
        integer(self.start, "起点", 0, MAX_SQL_INTEGER - 1)
        integer(self.end, "终点", self.start + 1)

    @classmethod
    def parse(cls, value):
        if value.startswith("[@") and value.endswith("]"):
            value = value[2:-1]
        match = re.fullmatch(r"C([0-9]+):([0-9]+)-([0-9]+)", value)
        if not match:
            raise ValueError("字符范围格式为 C0001:0-10 或 [@C0001:0-10]")
        return cls(*map(int, match.groups()))

    @property
    def coordinate(self):
        return f"C{self.chapter:04d}:{self.start}-{self.end}"

    @property
    def citation(self):
        return f"[@{self.coordinate}]"

    def source_citation(self, snapshot_id):
        return f"[@{snapshot_id}/{self.coordinate}]"


class SnapshotMismatch(ValueError):
    def __init__(self, span):
        super().__init__("引用的快照与本次选定版本不同")
        self.span = span


def source_span(value, snapshot_id):
    if value.startswith("[@") and value.endswith("]"):
        value = value[2:-1]
    if "/" in value:
        source, value = value.split("/", 1)
        if not re.fullmatch(r"snap_[0-9a-f]{64}", source):
            raise ValueError("引用中的快照 ID 格式不正确")
        span = Span.parse(value)
        if source != snapshot_id:
            raise SnapshotMismatch(span)
        return span
    return Span.parse(value)


@dataclass(frozen=True)
class Scope:
    snapshot_id: str
    as_of: int
    first_chapter: int = 1
    include_peripheral: bool = False

    def __post_init__(self):
        integer(self.as_of, "阅读进度")
        integer(self.first_chapter, "起始章", 1, max(1, self.as_of))


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    kind: str
    source_body_line: int
    body: str
    paragraph_ends: tuple[int, ...]

    def check(self, span):
        if span.chapter != self.number or span.end > len(self.body):
            raise ValueError(f"字符范围超出 C{self.number:04d} 正文（共 {len(self.body)} 字符）")

    @property
    def display_title(self):
        return display_title(self.title)


def selections(values):
    values = tuple(values)
    integer(len(values), "范围数", 1, MAX_SELECTIONS)
    return values


def parse_paragraph(value):
    match = re.fullmatch(r"C([0-9]+):([0-9]+)(?:-([0-9]+))?", value)
    if not match:
        raise ValueError("段落范围格式为 C0001:1 或 C0001:1-3（含首尾段）")
    chapter, first, last = match.groups()
    return int(chapter), int(first), int(last or first)


def parse_position(value):
    match = re.fullmatch(r"C([0-9]+):([0-9]+)", value) if isinstance(value, str) else None
    if not match:
        raise ValueError("续读位置须为 next 中的坐标字符串，例如 C0001:10")
    return tuple(map(int, match.groups()))


def atomic_write(path, raw, *, overwrite=False, temp_factory=tempfile.NamedTemporaryFile, verify=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file = temp_factory(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False)
    temporary = Path(file.name)
    try:
        with file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        if verify is not None:
            verify(temporary)
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
