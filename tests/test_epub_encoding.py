"""UTF-16 BOM decoding and replay of existing EPUB snapshot identities."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from chapspan import epub, sources
from chapspan.library import import_txt, open_scope
from chapspan.model import Scope
from chapspan.runs import create_run, run_library


class EpubEncodingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def build_epub(self, name, *, target=None, endian="le", ncx=False):
        toc = ('<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>' if ncx else
               '<item id="toc" href="nav.xhtml" properties="nav"/>')
        entries = {
            "META-INF/container.xml": '<container><rootfile full-path="package.opf"/></container>',
            "package.opf": '<package><manifest>' + toc +
                '<item id="one" href="one.xhtml"/><item id="two" href="two.xhtml"/>'
                '</manifest><spine toc="toc"><itemref idref="one"/><itemref idref="two"/>'
                '</spine></package>',
            "nav.xhtml": '<html><body><nav><a href="one.xhtml">第一章</a>'
                '<a href="two.xhtml">第二章</a></nav></body></html>',
            "toc.ncx": '<ncx><navMap><navPoint><navLabel><text>第一章</text></navLabel>'
                '<content src="one.xhtml"/></navPoint><navPoint><navLabel><text>第二章</text>'
                '</navLabel><content src="two.xhtml"/></navPoint></navMap></ncx>',
            "one.xhtml": '<html><body><h1>第一章</h1><p>正文甲。</p></body></html>',
            "two.xhtml": '<html><body><h1>第二章</h1><p>青铜钥匙 &amp; 正文乙。</p></body></html>',
        }
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for href, text in entries.items():
                if href == target:
                    bom = b"\xff\xfe" if endian == "le" else b"\xfe\xff"
                    raw = bom + text.encode("utf-16-" + endian)
                else:
                    raw = text.encode("utf-8")
                archive.writestr(href, raw)
        return path

    def cli(self, *args):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                       "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, "-B", "-m", "chapspan.cli", *map(str, args)],
                                cwd=self.root, env=environment, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_utf16_bom_in_package_navigation_and_content_for_both_byte_orders(self):
        for endian in ("le", "be"):
            for target in ("META-INF/container.xml", "package.opf", "nav.xhtml", "toc.ncx", "two.xhtml"):
                with self.subTest(endian=endian, target=target):
                    book = self.build_epub("encoding.epub", target=target, endian=endian,
                                           ncx=target == "toc.ncx")
                    _, chapters, _, details = sources.read_text_source_details(book)
                    self.assertEqual([chapter.title for chapter in chapters], ["第一章", "第二章"])
                    self.assertEqual(chapters[1].body, "青铜钥匙 & 正文乙。")
                    self.assertEqual(details["parsing"]["xml_decoding"], "utf16-bom-1")
                    self.assertEqual(details["parsing"]["reader"], "epub-1")

    def test_utf8_identity_and_parsing_stay_unchanged_and_use_one_derivation(self):
        book = self.build_epub("healthy.epub")
        old = sources.parse_source(book, legacy_xml_decoding=True)
        chapters = sources.split_chapters(old["lines"], old["sections"], "lines")
        expected = sources.snapshot_identity(old["raw"], chapters)[1]
        with patch.object(sources, "parse_epub_bytes", wraps=epub.parse_epub_bytes) as parse:
            actual = sources.read_text_source_details(book, expected_snapshot=expected)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(actual[0], expected)
        self.assertEqual(actual[3]["parsing"], old["parsing"])
        self.assertNotIn("xml_decoding", actual[3]["parsing"])

    def test_new_utf16_snapshot_agrees_across_text_library_run_and_inspection(self):
        book = self.build_epub("new.epub", target="two.xhtml", endian="be")
        library = self.root / "new_library"
        imported = import_txt(library, book)
        snapshot = imported["snapshot_id"]
        run = create_run(self.root / "new_task",
                         source={"kind": "text", "path": str(book), "text_options": {"paragraphs": "lines"}},
                         snapshot_id=snapshot, initial_scope=Scope(snapshot, 2))
        direct = self.cli("--text", book, "read", "--chapter", "2", "--as-of", "all", "--format", "json")
        archived = self.cli("--library", library, "read", "--chapter", "2", "--snapshot", snapshot,
                            "--as-of", "all", "--format", "json")
        bound = self.cli("--run", run.root, "read", "--chapter", "2", "--format", "json")
        self.assertEqual(direct["fragments"], archived["fragments"])
        self.assertEqual(direct["fragments"], bound["fragments"])
        self.assertIn("青铜钥匙", direct["fragments"][0]["text"])
        with open_scope(library, Scope(snapshot, 2)) as scoped:
            inspection = scoped.inspect_chapter(2)
        self.assertEqual(inspection["parsing"]["xml_decoding"], "utf16-bom-1")
        self.assertIn("青铜钥匙", str(inspection["source_lines"]))
        saved = self.root / "new_find.json"
        self.cli("--run", run.root, "find", "青铜钥匙", "--examples", "0", "--format", "json", "--output", saved)
        followed = self.cli("--text", book, "read", "--from-result", saved, "--pick", "1", "--format", "json")
        self.assertEqual(followed["scope"]["snapshot_id"], snapshot)

    def test_existing_utf16_snapshot_replays_old_decoding_only_for_exact_identity(self):
        book = self.build_epub("legacy.epub", target="two.xhtml")
        library = self.root / "old_library"

        def old_parser(raw, **kwargs):
            return epub.parse_epub_bytes(raw, **{**kwargs, "legacy_xml_decoding": True})

        with patch.object(sources, "parse_epub_bytes", side_effect=old_parser):
            old_import = import_txt(library, book)
            old_source = sources.read_text_source_details(book)
        snapshot = old_import["snapshot_id"]
        self.assertNotEqual(sources.read_text_source_details(book)[0], snapshot)
        replayed = sources.read_text_source_details(book, expected_snapshot=snapshot)
        self.assertEqual(replayed[0], snapshot)
        self.assertEqual(replayed[1], old_source[1])
        self.assertNotIn("xml_decoding", replayed[3]["parsing"])
        with open_scope(library, Scope(snapshot, 2)) as scoped:
            inspection = scoped.inspect_chapter(2)
        self.assertNotIn("xml_decoding", inspection["parsing"])
        self.assertEqual([row["text"] for row in inspection["source_lines"]], old_source[3]["lines"][2:])
        run = create_run(self.root / "old_task",
                         source={"kind": "text", "path": str(book), "text_options": {"paragraphs": "lines"}},
                         snapshot_id=snapshot, initial_scope=Scope(snapshot, 2))
        with run_library(run, run.initial_scope) as scoped:
            self.assertEqual(scoped.chapter(2).body, old_source[1][1].body)
        saved = self.root / "old_find.json"
        self.cli("--run", run.root, "find", "正文甲", "--examples", "0", "--format", "json", "--output", saved)
        followed = self.cli("--text", book, "read", "--from-result", saved, "--pick", "1", "--format", "json")
        self.assertEqual(followed["scope"]["snapshot_id"], snapshot)
        self.assertIn("正文甲", followed["fragments"][0]["text"])
        with self.assertRaisesRegex(ValueError, "快照不同"):
            sources.read_text_source_details(book, expected_snapshot="snap_" + "0" * 64)

    def test_invalid_utf16_bom_text_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "UTF-16 BOM"):
            epub.extract_xhtml_content(b"\xff\xfe<")

    def test_fixed_old_identity_replays_when_bom_decode_fails_or_removes_all_text(self):
        for name, content in (
            ("truncated", b"\xff\xfe<"),
            ("image_only", "<html><body><img src='cover.png'/></body></html>".encode("utf-16")),
        ):
            with self.subTest(name=name):
                book = self.build_epub(name + ".epub")
                with zipfile.ZipFile(book) as archive:
                    entries = {href: archive.read(href) for href in archive.namelist()}
                entries["package.opf"] = entries["package.opf"].replace(b'<itemref idref="one"/>', b"")
                entries["two.xhtml"] = content
                with zipfile.ZipFile(book, "w") as archive:
                    for href, raw in entries.items():
                        archive.writestr(href, raw)
                old = sources.parse_source(book, legacy_xml_decoding=True)
                old_chapters = sources.split_chapters(old["lines"], old["sections"], "lines")
                sources.ensure_body_sections(old)
                expected = sources.snapshot_identity(old["raw"], old_chapters)[1]
                with self.assertRaises(ValueError):
                    sources.read_text_source_details(book)
                replayed = sources.read_text_source_details(book, expected_snapshot=expected)
                self.assertEqual(replayed[0], expected)
                self.assertEqual(replayed[1], old_chapters)
                self.assertNotIn("xml_decoding", replayed[3]["parsing"])
                with self.assertRaisesRegex(ValueError, "快照不同"):
                    sources.read_text_source_details(book, expected_snapshot="snap_" + "0" * 64)
                run = create_run(self.root / (name + "_task"),
                                 source={"kind": "text", "path": str(book),
                                         "text_options": {"paragraphs": "lines"}},
                                 snapshot_id=expected, initial_scope=Scope(expected, 1))
                with run_library(run, run.initial_scope) as scoped:
                    self.assertEqual(scoped.chapter(1).body, old_chapters[0].body)


if __name__ == "__main__":
    unittest.main()
