from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from chapspan import sources
from chapspan.library import DB_NAME, SCHEMA, archive_source, import_txt, open_scope
from chapspan.model import Scope
from chapspan.runs import create_run, run_library


class InputRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build_epub(self, name, manifest, spine, entries):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            def write(href, content):
                info = zipfile.ZipInfo(href, date_time=(2020, 1, 1, 0, 0, 0))
                info.create_system = 3
                archive.writestr(info, content)

            write(
                "META-INF/container.xml",
                '<container><rootfile full-path="package.opf"/></container>',
            )
            write(
                "package.opf",
                f"<package><manifest>{manifest}</manifest><spine>{spine}</spine></package>",
            )
            for href, content in entries.items():
                write(href, content)
        return path

    def test_missing_linear_spine_content_fails_before_archiving(self):
        valid_manifest = '<item id="ok" href="ok.xhtml"/>'
        valid_spine = '<itemref idref="ok"/>'
        entries = {"ok.xhtml": "<h1>第二章</h1><p>不能把缺失的第一章静默丢掉。</p>"}
        for name, manifest, expected in (
            ("missing_manifest", "", "Manifest"),
            ("missing_href", '<item id="lost"/>', "路径"),
            ("missing_file", '<item id="lost" href="lost.xhtml"/>', "lost.xhtml"),
        ):
            with self.subTest(name=name):
                book = self.build_epub(
                    name + ".epub", manifest + valid_manifest,
                    '<itemref idref="lost"/>' + valid_spine, entries,
                )
                library = self.root / name
                for preview in (True, False):
                    with self.assertRaisesRegex(ValueError, expected):
                        import_txt(library, book, preview=preview)
                self.assertFalse((library / DB_NAME).exists())
                self.assertFalse((library / "sources").exists())

    def test_missing_non_linear_spine_content_still_skips(self):
        book = self.build_epub(
            "optional.epub",
            '<item id="lost" href="optional.xhtml"/><item id="ok" href="ok.xhtml"/>',
            '<itemref idref="absent" linear="no"/>'
            '<itemref idref="lost" linear="no"/><itemref idref="ok"/>',
            {"ok.xhtml": "<h1>第一章</h1><p>正文。</p>"},
        )
        parsed = sources.parse_source(book)
        self.assertEqual(parsed["sections"], [(0, "第一章", "body")])
        self.assertEqual(parsed["skipped_spine_items"], ["optional.xhtml"])

    def test_non_linear_reference_does_not_hide_a_missing_linear_reference(self):
        book = self.build_epub(
            "mixed_refs.epub",
            '<item id="lost" href="lost.xhtml"/><item id="ok" href="ok.xhtml"/>',
            '<itemref idref="lost" linear="no"/><itemref idref="lost"/>'
            '<itemref idref="ok"/>',
            {"ok.xhtml": "<h1>第二章</h1><p>正文。</p>"},
        )
        with self.assertRaisesRegex(ValueError, "lost.xhtml"):
            sources.parse_source(book)

    def test_saved_incomplete_epub_replays_only_the_exact_old_snapshot(self):
        book = self.build_epub(
            "legacy.epub",
            '<item id="lost" href="lost.xhtml"/><item id="one" href="one.xhtml"/>'
            '<item id="two" href="two.xhtml"/>',
            '<itemref idref="lost"/><itemref idref="one"/><itemref idref="two"/>',
            {"one.xhtml": "<h1>第一章</h1><p>正文甲。</p>",
             "two.xhtml": "<h1>第二章</h1><p>正文乙。</p>"},
        )
        parsed = sources.parse_source(book, allow_incomplete_epub=True)
        chapters = sources.split_chapters(parsed["lines"], parsed["sections"], "lines")
        source_hash, snapshot = sources.snapshot_identity(parsed["raw"], chapters)
        # Fixed ZIP metadata makes this identity reproducible; independently verified with the
        # original epub-1 reader from commit 58d5a89 before strict spine validation was added.
        self.assertEqual(source_hash, "06bae9b7038f5d2d92ab91e44169b0e2f5d9d0e49f8e877291399a0978ce6aa7")
        self.assertEqual(snapshot, "snap_c67a380f254bab3d66ce2567f27cbe1959395b891e6bb9a6bca6639393ac1a4b")
        library = self.root / "old_library"
        archive_source(library, source_hash, parsed["raw"], suffix=".epub")
        with closing(sqlite3.connect(library / DB_NAME)) as connection:
            connection.executescript(SCHEMA)
            with connection:
                connection.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                                   (snapshot, source_hash, book.name, "utf-8", json.dumps(parsed["parsing"])))
                connection.executemany(
                    "INSERT INTO chapters VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ((snapshot, c.number, c.title, c.kind, c.source_body_line, c.body,
                      json.dumps(c.paragraph_ends)) for c in chapters),
                )
        with open_scope(library, Scope(snapshot, 1)) as scoped:
            inspection = scoped.inspect_chapter(1)
            self.assertEqual(inspection["source_href"], "one.xhtml")
            self.assertNotIn("正文乙", str(inspection))
        replayed = sources.read_text_source_details(book, expected_snapshot=snapshot)
        self.assertEqual(replayed[0], snapshot)
        with self.assertRaisesRegex(ValueError, "快照不同"):
            sources.read_text_source_details(book, expected_snapshot="snap_" + "0" * 64)
        with self.assertRaisesRegex(ValueError, "Spine"):
            sources.read_text_source_details(book)

        run = create_run(
            self.root / "old_task",
            source={"kind": "text", "path": str(book), "text_options": {"paragraphs": "lines"}},
            snapshot_id=snapshot, initial_scope=Scope(snapshot, 1),
        )
        with run_library(run, run.initial_scope) as scoped:
            self.assertEqual(scoped.chapter(1).body, chapters[0].body)
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                       "PYTHONIOENCODING": "utf-8"}

        def cli(*args):
            result = subprocess.run(
                [sys.executable, "-B", "-m", "chapspan.cli", *map(str, args)],
                cwd=self.root, env=environment, capture_output=True, encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        overview = cli("--run", run.root, "overview", "--format", "json")
        self.assertEqual(overview["scope"]["snapshot_id"], snapshot)
        expanded = cli("--run", run.root, "research", "expand", "--as-of", "2")
        self.assertEqual(expanded["scope"]["as_of"], 2)
        saved = self.root / "old_find.json"
        cli("--run", run.root, "find", "正文", "--examples", "0", "--format", "json", "--output", saved)
        followed = cli("--text", book, "read", "--from-result", saved, "--pick", "1", "--format", "json")
        self.assertEqual(followed["scope"]["snapshot_id"], snapshot)
        self.assertIn("正文甲", followed["fragments"][0]["text"])

        fresh_task = self.root / "new_task"
        refused = subprocess.run(
            [sys.executable, "-B", "-m", "chapspan.cli", "--text", str(book),
             "research", "init", str(fresh_task), "--as-of", "all"],
            cwd=self.root, env=environment, capture_output=True, encoding="utf-8",
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("Spine", refused.stderr)
        self.assertFalse((fresh_task / "runs").exists())

        # A stored ID alone is insufficient: the legacy derivation must reproduce that ID.
        wrong_snapshot = "snap_" + "0" * 64
        with closing(sqlite3.connect(library / DB_NAME)) as connection:
            with connection:
                connection.execute("UPDATE snapshots SET id = ?", (wrong_snapshot,))
                connection.execute("UPDATE chapters SET snapshot_id = ?", (wrong_snapshot,))
        with open_scope(library, Scope(wrong_snapshot, 1)) as scoped:
            with self.assertRaisesRegex(ValueError, "分章结果与指定快照不同"):
                scoped.inspect_chapter(1)
        with zipfile.ZipFile(book, "a") as archive:
            archive.writestr("changed.txt", "原件改变，即使剩余章节相同也不能复用快照")
        with self.assertRaisesRegex(ValueError, "快照不同"):
            with run_library(run, run.initial_scope):
                self.fail("changed source was accepted")

    def test_import_builds_chapters_once_and_keeps_preview_statistics(self):
        for suffix, content in (
            ("txt", "前言\n第一章 甲\n甲正文。\n第二章 乙\n乙正文更长。\n"),
            ("md", "前言\n## 第一章 甲\n甲正文。\n## 第二章 乙\n乙正文更长。\n"),
        ):
            with self.subTest(suffix=suffix):
                source = self.root / f"book.{suffix}"
                source.write_text(content, encoding="utf-8")
                preview = import_txt(self.root / suffix, source, preview=True)
                with patch.object(sources, "_build_chapters", wraps=sources._build_chapters) as build:
                    imported = import_txt(self.root / suffix, source)
                self.assertEqual(build.call_count, 1)
                self.assertEqual(imported["chapter_length_stats"], preview["chapter_length_stats"])
                self.assertEqual(imported["snapshot_id"], sources.read_text_source(source)[0])
                with self.assertRaisesRegex(ValueError, "快照不同"):
                    sources.read_text_source_details(source, expected_snapshot="snap_" + "0" * 64)

    def test_line_samples_preserve_priority_without_copying_the_book(self):
        lines = [" " + "正文内容" * 250 + " " for _ in range(20_000)]
        lines[8:12] = ["", " 独立短行 ", "", "普通短行"]
        lines[14] = "另一短行"
        tracemalloc.start()
        try:
            samples = sources._line_samples(lines)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual([row["source_line"] for row in samples], [1, 2, 3, 10, 12, 15])
        self.assertEqual(samples[3]["text"], "独立短行")
        self.assertTrue(samples[0]["text"].endswith("…"))
        self.assertEqual(len(samples[0]["text"]), sources.PREVIEW_LINE_CHARS)
        self.assertLess(peak, 1_000_000)


if __name__ == "__main__":
    unittest.main()
