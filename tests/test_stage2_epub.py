import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from chapspan import epub as epub_mod
from chapspan.runs import RUN_FILE, load_run
from chapspan.sources import read_text_source_details


ROOT = Path(__file__).resolve().parents[1]


def _build_epub(path: Path, entries: dict[str, bytes | str]):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, data in entries.items():
            raw = data.encode("utf-8") if isinstance(data, str) else data
            zf.writestr(name, raw, compress_type=zipfile.ZIP_DEFLATED)


class Stage2EpubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def span_process(self, *args, cwd=None, environ=None):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONIOENCODING": "utf-8"}
        if environ:
            env.update(environ)
        return subprocess.run(
            [sys.executable, "-B", "-m", "chapspan.cli", *[str(a) for a in args]],
            cwd=cwd or self.root,
            env=env,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def span_json(self, *args, cwd=None, environ=None):
        proc = self.span_process(*args, cwd=cwd, environ=environ)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_epub2_opf_prefix_unescaped_amp_ruby_cjk_and_heading_dedup(self):
        book = self.root / "sonovel_style.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
                    '</container>'
                ),
                "OEBPS/content.opf": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<opf:package xmlns:opf="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">'
                    '<opf:metadata><dc:title>测试网文</dc:title></opf:metadata>'
                    '<opf:manifest>'
                    '<opf:item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    '<opf:item id="c1" href="001.html" media-type="application/xhtml+xml"/>'
                    '<opf:item id="c2" href="002.html" media-type="application/xhtml+xml"/>'
                    '<opf:item id="c3" href="003.html" media-type="application/xhtml+xml"/>'
                    '</opf:manifest>'
                    '<opf:spine toc="ncx">'
                    '<opf:itemref idref="c1"/>'
                    '<opf:itemref idref="c2"/>'
                    '<opf:itemref idref="c3"/>'
                    '</opf:spine>'
                    '</opf:package>'
                ),
                "OEBPS/toc.ncx": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                    '<navMap>'
                    '<navPoint id="p1" playOrder="1"><navLabel><text>第一章 雾港&灯塔</text></navLabel><content src="001.html"/></navPoint>'
                    '<navPoint id="p2" playOrder="2"><navLabel><text>第二章 异界注音</text></navLabel><content src="002.html"/></navPoint>'
                    '<navPoint id="p3" playOrder="3"><navLabel><text>上架感言</text></navLabel><content src="003.html"/></navPoint>'
                    '</navMap>'
                    '</ncx>'
                ),
                "OEBPS/001.html": (
                    '<html><head><title>第一章 雾港&灯塔</title>'
                    '<style>.x{color:red;}</style><script>var secret="SKIP";</script></head>'
                    '<body>'
                    '<h2>第一章  雾港&灯塔</h2>'
                    '<p>汉字换行\n继续衔接，English\nline join。</p>'
                    '<noscript>SKIP_NOSCRIPT</noscript>'
                    '<svg><text>SKIP_SVG</text></svg>'
                    '</body></html>'
                ),
                "OEBPS/002.html": (
                    '<html><body>'
                    '<h2>第二章 异界注音</h2>'
                    '<p>拿着<ruby>超电磁炮<rp>(</rp><rt>Railgun</rt><rp>)</rp></ruby>出发，灯塔亮了。</p>'
                    '</body></html>'
                ),
                "OEBPS/003.html": (
                    '<html><body>'
                    '<h2>上架感言</h2>'
                    '<p>求月票。</p>'
                    '</body></html>'
                ),
            },
        )

        prev = self.span_json("import", book, "--preview")
        self.assertEqual(prev["format"], "epub")
        self.assertEqual(prev["chapter_source"], "toc")
        self.assertEqual(prev["parsing"]["reader"], "epub-1")
        self.assertEqual(prev["heading_count"], 2)
        self.assertEqual(prev["skipped_spine_items"], [])

        ov = self.span_json("--text", book, "overview", "--as-of", "all", "--include-peripheral", "--format", "json")
        self.assertEqual([(c["chapter"], c["title"], c["kind"]) for c in ov["chapters"]], [
            (1, "第一章 雾港&灯塔", "body"),
            (2, "第二章 异界注音", "body"),
            (3, "上架感言", "peripheral"),
        ])

        read_c1 = self.span_json("--text", book, "read", "--chapter", "C0001", "--as-of", "all", "--format", "json")
        c1_text = read_c1["fragments"][0]["text"]
        self.assertFalse(c1_text.startswith("第一章"))
        self.assertEqual(c1_text, "汉字换行继续衔接，English line join。\n")
        for skipped in ("SKIP", "SKIP_NOSCRIPT", "SKIP_SVG"):
            self.assertNotIn(skipped, c1_text)

        read_c2 = self.span_json("--text", book, "read", "--chapter", "C0002", "--as-of", "all", "--format", "json")
        c2_text = read_c2["fragments"][0]["text"]
        self.assertEqual(c2_text, "拿着超电磁炮出发，灯塔亮了。\n")
        self.assertNotIn("Railgun", c2_text)

    def test_epub3_nav_linear_no_empty_cover_and_pre_toc_classification(self):
        book = self.root / "epub3_nav.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>'
                    '</container>'
                ),
                "OPS/package.opf": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>EPUB3测试</dc:title></metadata>'
                    '<manifest>'
                    '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="toc" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                    '<item id="front" href="front.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="nonlin" href="appendix.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="ch1" href="ch01.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest>'
                    '<spine>'
                    '<itemref idref="cover"/>'
                    '<itemref idref="toc"/>'
                    '<itemref idref="front"/>'
                    '<itemref idref="nonlin" linear="no"/>'
                    '<itemref idref="ch1"/>'
                    '</spine>'
                    '</package>'
                ),
                "OPS/cover.xhtml": '<html><body><div><img src="cover.jpg" alt="cover"/></div></body></html>',
                "OPS/nav.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                    '<body>'
                    '<nav epub:type="landmarks"><ol><li><a href="cover.xhtml">Cover</a></li></ol></nav>'
                    '<nav epub:type="toc"><ol>'
                    '<li><span>第一卷</span><ol><li><a href="ch01.xhtml">第一章 启航</a></li></ol></li>'
                    '</ol></nav>'
                    '</body></html>'
                ),
                "OPS/front.xhtml": '<html><body><h1>楔子</h1><p>开篇前情。</p></body></html>',
                "OPS/appendix.xhtml": '<html><body><h1>非线型附录</h1><p>不应自动进入正文。</p></body></html>',
                "OPS/ch01.xhtml": '<html><body><h1>第一章 启航</h1><p>海风吹过。<img src="map.png"/></p></body></html>',
            },
        )

        prev = self.span_json("import-txt", book, "--preview")
        self.assertEqual(prev["chapter_source"], "toc")
        self.assertEqual(prev["skipped_spine_items"], ["OPS/cover.xhtml", "OPS/nav.xhtml", "OPS/appendix.xhtml"])
        self.assertEqual(prev["image_count"], 2)

        ov = self.span_json("--text", book, "overview", "--as-of", "all", "--format", "json")
        self.assertEqual([(c["chapter"], c["title"], c["kind"]) for c in ov["chapters"]], [
            (1, "楔子", "unclassified"),
            (2, "第一章 启航", "body"),
        ])
        r1 = self.span_json("--text", book, "read", "--chapter", "C0001", "--as-of", "all", "--format", "json")
        self.assertEqual(r1["fragments"][0]["text"], "开篇前情。\n")
        r2 = self.span_json("--text", book, "read", "--chapter", "C0002", "--as-of", "all", "--format", "json")
        self.assertEqual(r2["fragments"][0]["text"], "海风吹过。")

    def test_single_file_multi_anchor_splitting(self):
        book = self.root / "multi_anchor.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>'
                    '</container>'
                ),
                "EPUB/package.opf": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>单文件多锚点</dc:title></metadata>'
                    '<manifest>'
                    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                    '<item id="all" href="s04.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest>'
                    '<spine><itemref idref="all"/></spine>'
                    '</package>'
                ),
                "EPUB/nav.xhtml": (
                    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                    '<body><nav epub:type="toc"><ol>'
                    '<li><a href="s04.xhtml#sec1">第一节 序曲</a></li>'
                    '<li><a href="s04.xhtml#sec2">第二节 遇险</a></li>'
                    '<li><a href="s04.xhtml#sec3">第三节 归来</a></li>'
                    '</ol></nav></body></html>'
                ),
                "EPUB/s04.xhtml": (
                    '<html><body>'
                    '<p>卷首说明。</p>'
                    '<section id="sec1"><h2>第一节 序曲</h2><p>第一段内容。</p></section>'
                    '<section id="sec2"><h2>第二节 遇险</h2><p>第二段内容。</p></section>'
                    '<section><h2 name="sec3">第三节 归来</h2><p>第三段内容。</p></section>'
                    '</body></html>'
                ),
            },
        )

        ov = self.span_json("--text", book, "overview", "--as-of", "all", "--format", "json")
        self.assertEqual([(c["chapter"], c["title"], c["kind"]) for c in ov["chapters"]], [
            (1, "章前文字", "unclassified"),
            (2, "第一节 序曲", "body"),
            (3, "第二节 遇险", "body"),
            (4, "第三节 归来", "body"),
        ])
        for ch, expected in ((1, "卷首说明。\n"), (2, "第一段内容。\n"), (3, "第二段内容。\n"), (4, "第三段内容。")):
            res = self.span_json("--text", book, "read", "--chapter", f"C{ch:04d}", "--as-of", "all", "--format", "json")
            self.assertEqual(res["fragments"][0]["text"], expected)

    def test_spine_fallback_when_no_toc_exists(self):
        book = self.root / "no_toc.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                    '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
                    '</container>'
                ),
                "content.opf": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>无目录</dc:title></metadata>'
                    '<manifest>'
                    '<item id="s1" href="a.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="s2" href="b.xhtml" media-type="application/xhtml+xml"/>'
                    '<item id="s3" href="c.xhtml" media-type="application/xhtml+xml"/>'
                    '</manifest>'
                    '<spine><itemref idref="s1"/><itemref idref="s2"/><itemref idref="s3"/></spine>'
                    '</package>'
                ),
                "a.xhtml": '<html><body><h3>序章</h3><p>楔子段落。</p></body></html>',
                "b.xhtml": '<html><body><h1>第一章 启程</h1><p>正文段落一。</p></body></html>',
                "c.xhtml": '<html><body><h2>后记</h2><p>完本感言段落。</p></body></html>',
            },
        )

        prev = self.span_json("import", book, "--preview")
        self.assertEqual(prev["chapter_source"], "spine")
        self.assertEqual(prev["parsing"]["chapter_source"], "spine")

        ov = self.span_json("--text", book, "overview", "--as-of", "all", "--include-peripheral", "--format", "json")
        self.assertEqual([(c["chapter"], c["title"], c["kind"]) for c in ov["chapters"]], [
            (1, "序章", "unclassified"),
            (2, "第一章 启程", "body"),
            (3, "后记", "peripheral"),
        ])

    def test_security_drm_font_obfuscation_zip_bounds_and_malformed_containers(self):
        drm_book = self.root / "drm.epub"
        _build_epub(
            drm_book,
            {
                "META-INF/container.xml": '<container version="1.0"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
                "META-INF/encryption.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                    'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                    '<enc:EncryptedData>'
                    '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
                    '<enc:CipherData><enc:CipherReference URI="OEBPS/ch01.xhtml"/></enc:CipherData>'
                    '</enc:EncryptedData>'
                    '</encryption>'
                ),
                "content.opf": '<package><manifest><item id="c1" href="OEBPS/ch01.xhtml"/></manifest><spine><itemref idref="c1"/></spine></package>',
                "OEBPS/ch01.xhtml": "encrypted",
            },
        )
        drm_res = self.span_process("import", drm_book, "--preview")
        self.assertNotEqual(drm_res.returncode, 0)
        self.assertIn("DRM", drm_res.stderr)

        font_obf_book = self.root / "font_obf.epub"
        _build_epub(
            font_obf_book,
            {
                "META-INF/container.xml": '<container version="1.0"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
                "META-INF/encryption.xml": (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                    'xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                    '<enc:EncryptedData>'
                    '<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
                    '<enc:CipherData><enc:CipherReference URI="fonts/SourceHan.woff2"/></enc:CipherData>'
                    '</enc:EncryptedData>'
                    '</encryption>'
                ),
                "content.opf": (
                    '<package><manifest><item id="c1" href="ch01.xhtml" media-type="application/xhtml+xml"/></manifest>'
                    '<spine><itemref idref="c1"/></spine></package>'
                ),
                "fonts/SourceHan.woff2": b"\x00\x01\x02",
                "ch01.xhtml": '<html><body><h1>第一章 字体混淆允许</h1><p>正常正文。</p></body></html>',
            },
        )
        font_ov = self.span_json("--text", font_obf_book, "overview", "--as-of", "all", "--format", "json")
        self.assertEqual(font_ov["chapters"][0]["title"], "第一章 字体混淆允许")

        missing_container = self.root / "no_container.epub"
        _build_epub(missing_container, {"OEBPS/ch01.xhtml": "<p>hi</p>"})
        mc_res = self.span_process("import", missing_container, "--preview")
        self.assertNotEqual(mc_res.returncode, 0)
        self.assertIn("container.xml", mc_res.stderr)

        missing_opf = self.root / "no_opf.epub"
        _build_epub(
            missing_opf,
            {"META-INF/container.xml": '<container><rootfiles><rootfile full-path="missing.opf"/></rootfiles></container>'},
        )
        mo_res = self.span_process("import", missing_opf, "--preview")
        self.assertNotEqual(mo_res.returncode, 0)
        self.assertIn("OPF", mo_res.stderr)

        # Zip bomb / single-entry bound check
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
            zf.writestr("c.opf", '<package><manifest><item id="a" href="a.html"/></manifest><spine><itemref idref="a"/></spine></package>')
            zf.writestr("a.html", "<html><body><h1>第一章</h1><p>" + ("甲" * 2048) + "</p></body></html>")
        with patch.object(epub_mod, "MAX_EPUB_ENTRY_BYTES", 512):
            with self.assertRaisesRegex(ValueError, "单文件安全上限"):
                epub_mod.parse_epub_bytes(
                    buf.getvalue(),
                    source_name="bomb.epub",
                    peripheral_re=__import__("re").compile("$^"),
                    unclassified_re=__import__("re").compile("$^"),
                    chapter_sections_fn=lambda l, p: ([], []),
                )

    def test_epub_options_rejection_and_overrides(self):
        book = self.root / "options.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
                "content.opf": (
                    '<package><manifest>'
                    '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    '<item id="c1" href="c1.html" media-type="application/xhtml+xml"/>'
                    '</manifest><spine toc="ncx"><itemref idref="c1"/></spine></package>'
                ),
                "toc.ncx": '<ncx><navMap><navPoint id="n1"><navLabel><text>第一部</text></navLabel><content src="c1.html"/></navPoint></navMap></ncx>',
                "c1.html": '<html><body><p>第一章 破晓</p><p>甲段。</p><p>第二章 黎明</p><p>乙段。</p></body></html>',
            },
        )

        # Reject --encoding and --heading-level on EPUB (both --text and import / import-txt)
        for cmd in (
            ("--text", book, "--encoding", "utf-8", "overview", "--as-of", "all"),
            ("--text", book, "--heading-level", "2", "overview", "--as-of", "all"),
            ("import", book, "--encoding", "gb18030", "--preview"),
            ("import-txt", book, "--heading-level", "1", "--preview"),
        ):
            bad = self.span_process(*cmd)
            self.assertNotEqual(bad.returncode, 0, cmd)

        # --chapter-pattern overrides TOC splitting on derived text
        pat_ov = self.span_json(
            "--text", book, "--chapter-pattern", r"第[一二]章 .*",
            "overview", "--as-of", "all", "--format", "json",
        )
        self.assertEqual([c["title"] for c in pat_ov["chapters"]], ["第一章 破晓", "第二章 黎明"])

        # --single-chapter overrides TOC splitting on derived text
        sc_ov = self.span_json("--text", book, "--single-chapter", "overview", "--as-of", "all", "--format", "json")
        self.assertEqual(len(sc_ov["chapters"]), 1)
        self.assertEqual(sc_ov["chapters"][0]["title"], "第一部" if False else "options")

    def test_epub_cross_entry_parity_inspect_library_run_and_handoff(self):
        book = self.root / "parity.epub"
        _build_epub(
            book,
            {
                "META-INF/container.xml": '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
                "OEBPS/content.opf": (
                    '<opf:package xmlns:opf="http://www.idpf.org/2007/opf" version="2.0">'
                    '<opf:metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>跨入口一致</dc:title></opf:metadata>'
                    '<opf:manifest>'
                    '<opf:item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    '<opf:item id="c1" href="01.xhtml" media-type="application/xhtml+xml"/>'
                    '<opf:item id="c2" href="02.xhtml" media-type="application/xhtml+xml"/>'
                    '<opf:item id="c3" href="03.xhtml" media-type="application/xhtml+xml"/>'
                    '</opf:manifest>'
                    '<opf:spine toc="ncx">'
                    '<opf:itemref idref="c1"/><opf:itemref idref="c2"/><opf:itemref idref="c3"/>'
                    '</opf:spine>'
                    '</opf:package>'
                ),
                "OEBPS/toc.ncx": (
                    '<ncx><navMap>'
                    '<navPoint id="n1"><navLabel><text>第一章 港口</text></navLabel><content src="01.xhtml"/></navPoint>'
                    '<navPoint id="n2"><navLabel><text>第二章 钟楼</text></navLabel><content src="02.xhtml"/></navPoint>'
                    '<navPoint id="n3"><navLabel><text>第三章 密信</text></navLabel><content src="03.xhtml"/></navPoint>'
                    '</navMap></ncx>'
                ),
                "OEBPS/01.xhtml": '<html><body><h2>第一章 港口</h2><p>港口雾气浓重，青铜钥匙在船舱里。</p><p>第一章结尾秘密。</p></body></html>',
                "OEBPS/02.xhtml": '<html><body><h2>第二章 钟楼</h2><p>钟楼敲响三声。</p><p>青铜钥匙再次出现。</p></body></html>',
                "OEBPS/03.xhtml": '<html><body><h2>第三章 密信</h2><p>密信揭示真相。</p></body></html>',
            },
        )

        lib = self.root / "epub_lib"
        imported = self.span_json("--library", lib, "import", book)
        snap = imported["snapshot_id"]
        self.assertTrue(imported["source"].endswith(".epub"))
        self.assertTrue((lib / imported["source"]).is_file())
        self.assertEqual(
            hashlib.sha256((lib / imported["source"]).read_bytes()).hexdigest(),
            imported["source_sha256"],
        )

        # Deterministic snapshot_id via Python API too
        api_snap, _, _, details = read_text_source_details(book)
        self.assertEqual(api_snap, snap)
        self.assertEqual(details["parsing"]["reader"], "epub-1")

        init_res = self.span_json("--text", book, "research", "init", self.root / "epub_task", "--as-of", "第二章")
        run_dir = Path(init_res["run_directory"])
        self.assertEqual(init_res["snapshot_id"], snap)
        self.assertEqual(init_res["scope"]["as_of"], 2)
        manifest = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["text_options"], {"paragraphs": "lines"})
        self.assertEqual(load_run(run_dir).snapshot_id, snap)

        expanded = self.span_json("--run", run_dir, "research", "expand", "--as-of", "第三章")
        self.assertEqual(expanded["scope"]["as_of"], 3)

        ov_text = self.span_json("--text", book, "overview", "--as-of", "all", "--format", "json")
        ov_lib = self.span_json("--library", lib, "overview", "--snapshot", snap, "--as-of", "all")
        ov_run = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(ov_text["scope"], ov_lib["scope"])
        self.assertEqual(ov_text["scope"], ov_run["scope"])
        self.assertEqual(ov_text["chapters"], ov_lib["chapters"])
        self.assertEqual(ov_text["chapters"], ov_run["chapters"])

        find_text = self.span_json("--text", book, "find", "青铜钥匙", "--as-of", "all", "--examples", "0", "--format", "json")
        find_lib = self.span_json("--library", lib, "find", "青铜钥匙", "--snapshot", snap, "--as-of", "all", "--examples", "0")
        find_run = self.span_json("--run", run_dir, "find", "青铜钥匙", "--examples", "0", "--format", "json")
        self.assertEqual(find_text["matches"], find_lib["matches"])
        self.assertEqual(find_text["matches"], find_run["matches"])

        # --inspect works identically across --text, --library, --run and stays strictly bounded to chapter C0002
        insp_text = self.span_json("--text", book, "overview", "--as-of", "2", "--inspect", "C0002", "--format", "json")
        insp_lib = self.span_json("--library", lib, "overview", "--snapshot", snap, "--as-of", "2", "--inspect", "C0002")
        insp_run = self.span_json("--run", run_dir, "overview", "--as-of", "2", "--inspect", "C0002", "--format", "json")
        self.assertEqual(insp_text["inspection"], insp_lib["inspection"])
        self.assertEqual(insp_text["inspection"], insp_run["inspection"])
        self.assertEqual(insp_text["inspection"]["source_href"], "OEBPS/02.xhtml")
        excerpt_joined = "\n".join(row["text"] for row in insp_text["inspection"]["source_lines"])
        self.assertNotIn("第一章结尾秘密", excerpt_joined)
        self.assertNotIn("密信揭示真相", excerpt_joined)

        # --from-result handoff works on --text book.epub
        out_find = self.root / "epub_find.txt"
        saved_find = self.span_process("--text", book, "--output", out_find, "find", "青铜钥匙", "--as-of", "all")
        self.assertEqual(saved_find.returncode, 0, saved_find.stderr)
        followup = self.span_json("--text", book, "read", "--from-result", out_find, "--example", "1", "--format", "json")
        self.assertIn("青铜钥匙", followup["fragments"][0]["text"])

        # resume + research index on EPUB run
        coord = find_run["matches"][0]["span"]
        draft = run_dir / "当前稿.md"
        draft.write_text(f"{init_res['draft_header']}# 笔记\n发现钥匙 [@{coord}]。\n", encoding="utf-8")
        resumed = self.span_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(resumed["citations"][0]["status"], "ok")
        indexed = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(indexed["citations"], 1)


if __name__ == "__main__":
    unittest.main()
