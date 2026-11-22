"""Immutable SQLite snapshots and scope-checked library access."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .epub import IncompleteSpineError, parse_epub_bytes
from .model import MAX_PAGE, Chapter, Span, atomic_write, integer, selections
from .reading import selection_origins
from .sources import (
    DEFAULT_HEADING,
    PERIPHERAL,
    PREVIEW_EXAMPLES,
    PRINTED_HEADING,
    TIGHT_CHAPTER_PATTERN,
    TOC_MAX_PAGE,
    UNCLASSIFIED,
    ScopedText,
    build_chapter_inspection,
    chapter_diagnostics,
    chapter_entry,
    chapter_kind_summary,
    chapter_sections,
    ensure_body_sections,
    normalized_lines,
    overview_page,
    paragraph_ends,
    parse_source,
    parse_text,
    preview_chapters,
    read_text_source,
    read_text_source_details,
    snapshot_identity,
    split_chapters,
    structural_chapter_issues,
    text_boundary,
    text_chapter_issues,
)


DB_NAME = "workbench.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    source_name TEXT NOT NULL,
    encoding TEXT NOT NULL,
    parsing TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('body', 'unclassified', 'peripheral')),
    source_body_line INTEGER NOT NULL,
    body TEXT NOT NULL,
    paragraph_ends TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, number)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    note TEXT,
    note_horizon INTEGER,
    selection_origins TEXT,
    UNIQUE (collection, snapshot_id, chapter, start, end),
    FOREIGN KEY (snapshot_id, chapter) REFERENCES chapters(snapshot_id, number)
);
CREATE INDEX IF NOT EXISTS items_by_collection ON items(snapshot_id, collection, id);
"""


def archive_source(root, source_hash, raw, *, suffix=".txt"):
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    target = sources / f"{source_hash}{suffix}"
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != source_hash:
            raise ValueError("已存原件与内容哈希不符")
        return target
    atomic_write(target, raw, overwrite=True)
    return target


def connect(root, mode):
    path = Path(root).resolve() / DB_NAME
    connection = sqlite3.connect(f"{path.as_uri()}?mode={mode}", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def import_txt(
    root,
    source,
    *,
    encoding=None,
    chapter_pattern=None,
    paragraphs="lines",
    heading_level=None,
    single_chapter=False,
    preview=False,
):
    """Parse before writing and publish all rows for a snapshot together."""
    source = Path(source)
    parsed = parse_source(
        source,
        encoding=encoding,
        chapter_pattern=chapter_pattern,
        paragraphs=paragraphs,
        heading_level=heading_level,
        single_chapter=single_chapter,
        allow_ambiguous_markdown=preview,
    )
    chapters = None
    if not preview:
        ensure_body_sections(parsed)
        chapters = split_chapters(parsed["lines"], parsed["sections"], paragraphs)
    diagnostics = preview_chapters(
        parsed["sections"],
        parsed["candidates"],
        lines=parsed["lines"],
        paragraphs=paragraphs,
        source_format=parsed["source_format"],
        markdown_heading_levels=parsed["markdown_heading_levels"],
        markdown_heading_examples=parsed["markdown_heading_examples"],
        ambiguous_markdown=parsed["ambiguous_markdown"],
        chapter_source=parsed.get("chapter_source"),
        skipped_spine_items=parsed.get("skipped_spine_items"),
        image_count=parsed.get("image_count"),
        chapters=chapters,
    )
    if preview:
        return {"preview": True, "encoding": parsed["encoding"], "parsing": parsed["parsing"], **diagnostics}
    source_hash, snapshot_id = snapshot_identity(parsed["raw"], chapters)
    root = Path(root)
    if parsed["source_format"] == "epub":
        archive_suffix = ".epub"
    elif parsed["source_format"] == "markdown":
        archive_suffix = ".md"
    else:
        archive_suffix = ".txt"
    archived = archive_source(root, source_hash, parsed["raw"], suffix=archive_suffix)

    with closing(connect(root, "rwc")) as connection:
        connection.executescript(SCHEMA)
        with connection:
            inserted = connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                (snapshot_id, source_hash, source.name, parsed["encoding"], json.dumps(parsed["parsing"])),
            ).rowcount == 1
            if inserted:
                connection.executemany(
                    "INSERT INTO chapters VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ((snapshot_id, c.number, c.title, c.kind, c.source_body_line,
                      c.body, json.dumps(c.paragraph_ends)) for c in chapters),
                )
    return {
        "snapshot_id": snapshot_id,
        "created": inserted,
        "chapters": len(chapters),
        "unclassified_chapters": sum(c.kind == "unclassified" for c in chapters),
        "peripheral_chapters": sum(c.kind == "peripheral" for c in chapters),
        "empty_chapters": sum(not c.body.strip() for c in chapters),
        "source_sha256": source_hash,
        "source": archived.relative_to(root).as_posix(),
        "encoding": parsed["encoding"],
        "parsing": parsed["parsing"],
        **diagnostics,
    }


def list_snapshots(root, *, after_snapshot="", limit=20):
    """List imported versions without opening chapter data or selecting a version."""
    integer(limit, "页大小", 1, MAX_PAGE)
    if not (Path(root) / DB_NAME).is_file():
        raise ValueError("书库目录中没有 workbench.db，请核对 --library 或先用 import-txt 导入")
    with closing(connect(root, "ro")) as connection:
        rows = connection.execute(
            "SELECT id, source_name, source_sha256, encoding, parsing FROM snapshots "
            "WHERE id > ? ORDER BY id LIMIT ?",
            (after_snapshot, limit + 1),
        ).fetchall()
    return {
        "snapshots": [
            {"snapshot_id": row["id"], "source_name": row["source_name"],
             "source_sha256": row["source_sha256"], "encoding": row["encoding"],
             "parsing": json.loads(row["parsing"])}
            for row in rows[:limit]
        ],
        "next_after_snapshot": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def snapshot_chapter_count(root, snapshot_id):
    """Return the highest internal chapter number in a library snapshot."""
    root_path = Path(root)
    if not (root_path / DB_NAME).is_file():
        raise ValueError("书库目录中没有 workbench.db，请核对 --library 或先用 import-txt 导入")
    with closing(connect(root_path, "ro")) as connection:
        if connection.execute("SELECT 1 FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone() is None:
            raise ValueError("未找到指定快照")
        row = connection.execute(
            "SELECT COALESCE(MAX(number), 0) AS total FROM chapters WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return row["total"]


@contextmanager
def open_scope(root, scope, *, writable=False):
    root_path = Path(root)
    if not (root_path / DB_NAME).is_file():
        raise ValueError("书库目录中没有 workbench.db，请核对 --library 或先用 import-txt 导入")
    with closing(connect(root_path, "rw" if writable else "ro")) as connection:
        if connection.execute("SELECT 1 FROM snapshots WHERE id = ?", (scope.snapshot_id,)).fetchone() is None:
            raise ValueError("未找到指定快照")
        yield ScopedLibrary(connection, scope, root_path.resolve())


class ScopedLibrary:
    """Every passage and collection lookup is constrained before it leaves SQLite."""

    _visible = "c.snapshot_id = ? AND c.number BETWEEN ? AND ? AND (c.kind <> 'peripheral' OR ?)"
    _join_items = "JOIN chapters c ON c.snapshot_id = i.snapshot_id AND c.number = i.chapter"

    def __init__(self, connection, scope, root):
        self._connection = connection
        self._root = root
        self.scope = scope
        self._range = (scope.snapshot_id, scope.first_chapter, scope.as_of, scope.include_peripheral)

    @staticmethod
    def _chapter(row):
        ends = row["paragraph_ends"]
        return Chapter(row["number"], row["title"], row["kind"], row["source_body_line"],
                       row["body"], tuple(json.loads(ends)) if ends is not None else ())

    def chapters(self, *, with_paragraphs=True):
        ends = "c.paragraph_ends" if with_paragraphs else "NULL AS paragraph_ends"
        for row in self._connection.execute(
            f"SELECT c.number, c.title, c.kind, c.source_body_line, c.body, {ends} "
            f"FROM chapters c WHERE {self._visible} ORDER BY c.number", self._range,
        ):
            yield self._chapter(row)

    def body_chapters(self):
        return [row["number"] for row in self._connection.execute(
            f"SELECT c.number FROM chapters c WHERE {self._visible} AND c.kind = 'body' "
            "AND c.paragraph_ends != '[]' ORDER BY c.number",
            self._range,
        )]

    def overview(self, *, after_chapter=0, limit=20, toc=False, title=None):
        integer(after_chapter, "目录起点")
        integer(limit, "页大小", 1, TOC_MAX_PAGE)
        kind_counts = chapter_kind_summary({
            row["kind"]: row["n"] for row in self._connection.execute(
                f"SELECT c.kind, COUNT(*) AS n FROM chapters c WHERE {self._visible} GROUP BY c.kind",
                self._range,
            )
        })
        paragraph_column = "(c.paragraph_ends != '[]') AS has_paragraph" if toc else "c.paragraph_ends, LENGTH(c.body) AS characters"
        rows = self._connection.execute(
            f"SELECT c.number, c.title, c.kind, c.source_body_line, {paragraph_column} FROM chapters c "
            f"WHERE {self._visible} AND c.number > ? AND (? IS NULL OR instr(c.title, ?) > 0) "
            "ORDER BY c.number LIMIT ?",
            (*self._range, after_chapter, title, title, limit + 1),
        )
        entries = (chapter_entry(row["number"], row["title"], row["kind"],
                                 row["has_paragraph"] if toc else len(json.loads(row["paragraph_ends"])),
                                 row["source_body_line"], toc,
                                 characters=None if toc else row["characters"]) for row in rows)
        return overview_page(entries, limit, toc, kind_counts)

    def chapter(self, number):
        integer(number, "章序", 1)
        row = self._connection.execute(
            f"SELECT c.* FROM chapters c WHERE {self._visible} AND c.number = ?", (*self._range, number)
        ).fetchone()
        if row is None:
            if (not self.scope.include_peripheral
                    and self.scope.first_chapter <= number <= self.scope.as_of):
                filtered = self._connection.execute(
                    "SELECT kind FROM chapters WHERE snapshot_id = ? AND number = ?",
                    (self.scope.snapshot_id, number),
                ).fetchone()
                if filtered is not None and filtered["kind"] == "peripheral":
                    raise ValueError(
                        f"C{number:04d} 为当前范围内默认过滤的外围章，当前不可读取；如需读取或核查，请加 --include-peripheral"
                    )
            raise ValueError(f"C{number:04d} 在当前范围内不可读取")
        return self._chapter(row)

    def chapter_metadata(self, *, include_filtered=False):
        visible = "c.snapshot_id = ? AND c.number BETWEEN ? AND ?" if include_filtered else self._visible
        parameters = self._range[:3] if include_filtered else self._range
        rows = self._connection.execute(
            f"SELECT c.number, c.title, c.kind, c.source_body_line FROM chapters c WHERE {visible} "
            "ORDER BY c.number", parameters,
        )
        return [{"chapter": row["number"], "title": row["title"], "kind": row["kind"],
                 "source_body_line": row["source_body_line"],
                 "source_line": max(1, row["source_body_line"] - 1)} for row in rows]

    def inspect_chapter(self, number, target_line=None):
        chapter = self.chapter(number)
        row = self._connection.execute(
            "SELECT source_sha256, source_name, encoding, parsing FROM snapshots WHERE id = ?",
            (self.scope.snapshot_id,),
        ).fetchone()
        parsing = json.loads(row["parsing"])
        fmt = parsing.get("format")
        source_suffix = Path(row["source_name"]).suffix.casefold()
        is_epub = fmt == "epub" or source_suffix == ".epub"
        is_markdown = fmt == "markdown" or source_suffix in (".md", ".markdown")
        if is_epub:
            suffixes = (".epub", ".txt", ".md")
        elif is_markdown:
            suffixes = (".md", ".txt", ".epub")
        else:
            suffixes = (".txt", ".md", ".epub")
        source = next(
            (candidate for ext in suffixes if (candidate := self._root / "sources" / f"{row['source_sha256']}{ext}").is_file()),
            None,
        )
        if source is None:
            raise ValueError("书库中找不到快照对应的原始文件；请恢复原件或重新导入")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["source_sha256"]:
            raise ValueError("书库中的原始文件与来源哈希不符；请恢复原件或重新导入")
        if is_epub:
            epub_options = dict(
                source_name=row["source_name"],
                chapter_pattern=parsing.get("chapter_pattern"),
                paragraphs=parsing.get("paragraphs", "lines"),
                single_chapter=bool(parsing.get("single_chapter", False)),
                legacy_xml_decoding=not parsing.get("xml_decoding"),
                peripheral_re=PERIPHERAL,
                unclassified_re=UNCLASSIFIED,
                chapter_sections_fn=chapter_sections,
            )
            try:
                epub_parsed = parse_epub_bytes(raw, **epub_options)
            except IncompleteSpineError:
                epub_parsed = parse_epub_bytes(raw, **epub_options, allow_incomplete_spine=True)
                replayed = split_chapters(epub_parsed["lines"], epub_parsed["sections"],
                                          parsing.get("paragraphs", "lines"))
                if snapshot_identity(raw, replayed)[1] != self.scope.snapshot_id:
                    raise ValueError("归档原件的分章结果与指定快照不同；请重新导入核对来源")
            details = {
                "lines": epub_parsed["lines"],
                "encoding": row["encoding"],
                "parsing": parsing,
                "spine_map": epub_parsed["spine_map"],
            }
        else:
            encoding, lines = normalized_lines(raw, row["encoding"])
            details = {"lines": lines, "encoding": encoding, "parsing": parsing}
        return build_chapter_inspection(chapter, self.chapter_metadata(), details, target_line)

    @staticmethod
    def _check_collection(collection):
        if not isinstance(collection, str) or not collection.strip() or len(collection) > 120:
            raise ValueError("收藏集名称须为 1 到 120 字符的非空文本")

    def _item(self, row):
        span = Span(row["chapter"], row["start"], row["end"])
        visible_note = row["note_horizon"] is not None and row["note_horizon"] <= self.scope.as_of
        item = {"id": row["id"], "collection": row["collection"], "span": span.coordinate,
                "citation": span.citation, "source_citation": span.source_citation(self.scope.snapshot_id),
                "note": row["note"] if visible_note else None,
                "note_horizon": row["note_horizon"] if visible_note else None}
        if origins := self._item_origins(row):
            item["selection_origins"] = origins
        return item

    @staticmethod
    def _item_origins(row):
        if "selection_origins" in row.keys() and row["selection_origins"]:
            return selection_origins(row["selection_origins"].split(","))
        return []

    def _item_row(self, item_id):
        integer(item_id, "收藏 ID", 1)
        row = self._connection.execute(
            f"SELECT i.* FROM items i {self._join_items} WHERE {self._visible} AND i.id = ?",
            (*self._range, item_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"收藏 {item_id} 在当前范围内不可用")
        return row

    def item_span(self, item_id):
        return self.item_selection(item_id)[0]

    def item_selection(self, item_id):
        row = self._item_row(item_id)
        return Span(row["chapter"], row["start"], row["end"]), self._item_origins(row)

    def add_items(self, collection, spans, note=None, *, origins_by_span=None):
        self._check_collection(collection)
        spans = selections(spans)
        if note is not None and (not isinstance(note, str) or not note.strip() or len(note) > 500):
            raise ValueError("批注须为 1 到 500 字符的非空文本")
        spans = tuple(dict.fromkeys(spans))
        chapters = {number: self.chapter(number) for number in {span.chapter for span in spans}}
        for span in spans:
            chapters[span.chapter].check(span)

        results = []
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            columns = self._connection.execute("PRAGMA table_info(items)")
            if not any(row["name"] == "selection_origins" for row in columns):
                self._connection.execute("ALTER TABLE items ADD COLUMN selection_origins TEXT")
            for span in spans:
                key = (collection, self.scope.snapshot_id, span.chapter, span.start, span.end)
                origins = selection_origins(origins_by_span.get(span)) if origins_by_span else []
                created = self._connection.execute(
                    "INSERT INTO items (collection, snapshot_id, chapter, start, end, note, note_horizon, selection_origins) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (collection, snapshot_id, chapter, start, end) DO NOTHING",
                    (*key, note, self.scope.as_of if note is not None else None, ",".join(origins) or None),
                ).rowcount == 1
                row = self._connection.execute(
                    "SELECT * FROM items WHERE collection = ? AND snapshot_id = ? "
                    "AND chapter = ? AND start = ? AND end = ?", key,
                ).fetchone()
                results.append({**self._item(row), "created": created})
        return results

    def list_items(self, collection, *, after_id=0, limit=20):
        self._check_collection(collection)
        integer(after_id, "收藏起点")
        integer(limit, "页大小", 1, MAX_PAGE)
        rows = self._connection.execute(
            f"SELECT i.* FROM items i {self._join_items} WHERE {self._visible} "
            "AND i.collection = ? AND i.id > ? ORDER BY i.id LIMIT ?",
            (*self._range, collection, after_id, limit + 1),
        ).fetchall()
        return {"items": [self._item(row) for row in rows[:limit]],
                "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None}

    def remove_items(self, item_ids):
        item_ids = tuple(dict.fromkeys(selections(item_ids)))
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            for item_id in item_ids:
                self._item_row(item_id)
            self._connection.executemany("DELETE FROM items WHERE id = ?", ((i,) for i in item_ids))
        return list(item_ids)
