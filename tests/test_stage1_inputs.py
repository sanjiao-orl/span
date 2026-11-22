import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from chapspan.help_topics import HELP_TOPICS
from chapspan.library import DB_NAME, SCHEMA, archive_source, import_txt, list_snapshots, open_scope
from chapspan.model import Scope
from chapspan.runs import RUN_FILE, RUN_VERSION, load_run
from chapspan.search import QUERY_KEYS
from chapspan.sources import (
    DEFAULT_HEADING,
    PRINTED_HEADING,
    TIGHT_CHAPTER_PATTERN,
    printed_number,
    read_text_source_details,
)


ROOT = Path(__file__).resolve().parents[1]


class Stage1InputTests(unittest.TestCase):
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

    def test_traditional_wan_heading_and_txt_snapshot_invariance(self):
        trad = self.root / "繁體萬字.txt"
        trad.write_text(
            "第一萬零一章 繁體開篇\n第一段內容。\n"
            "第一萬零二章 續篇\n第二段內容。\n"
            "第壹萬零三回 測試\n第三段內容。\n",
            encoding="utf-8",
        )
        preview = self.span_json("import-txt", trad, "--preview")
        self.assertEqual(preview["heading_count"], 3)
        self.assertEqual(preview["format"], "txt")
        self.assertEqual(printed_number(PRINTED_HEADING.match("第一萬零一章 繁體開篇")), 10001)

        ov = self.span_json("--text", trad, "overview", "--as-of", "第一萬零一章", "--format", "json")
        self.assertEqual(ov["scope"]["as_of"], 1)
        self.assertEqual([c["title"] for c in ov["chapters"]], ["第一萬零一章 繁體開篇"])

        # Tight traditional 萬 candidate is detected and matched by suggested_chapter_pattern.
        tight_trad = self.root / "繁體緊連.txt"
        tight_trad.write_text("第一萬零一章緊連\n正文一。\n第一萬零二章續篇\n正文二。\n", encoding="utf-8")
        tp = self.span_json("import-txt", tight_trad, "--preview")
        self.assertEqual(tp["heading_count"], 0)
        self.assertEqual(tp["tight_candidate_count"], 2)
        self.assertEqual(tp["suggested_chapter_pattern"], TIGHT_CHAPTER_PATTERN)

        # Simplified TXT snapshot_id stays invariant (same raw + chapters hash).
        simp = self.root / "简体不变.txt"
        simp.write_bytes(
            ("\ufeff第一章 开门\r\n甲😀乙。\r\n\r\n哈哈哈。\r\n灯亮了。\r\n"
             "坏灯熄了。\r\n第二章 后续\r\n秘密灯亮了。\r\n"
             "作者有话说\r\n外围材料\r\n").encode("utf-8")
        )
        snap_direct, _, _, _ = read_text_source_details(simp)
        snap_import = import_txt(self.root / "lib_inv", simp)["snapshot_id"]
        self.assertEqual(snap_direct, snap_import)
        self.assertTrue(snap_direct.startswith("snap_"))

    def test_preview_zero_hit_line_samples_and_chapter_length_stats(self):
        # 1. Zero-hit TXT with non-standard headings and a >120-char line
        long_line = "很长的正文" * 40
        nonstandard = self.root / "roman.txt"
        nonstandard.write_text(
            f"书名测试\n\nI.\n{long_line}\n\nII.\n第二段短内容。\n\nIII.\n第三段。\n",
            encoding="utf-8",
        )
        preview = self.span_json("import-txt", nonstandard, "--preview")
        self.assertEqual(preview["heading_count"], 0)
        self.assertEqual(preview["tight_candidate_count"], 0)
        self.assertEqual(preview["total_lines"], 11)
        self.assertIn("line_samples", preview)
        self.assertNotIn("chapter_length_stats", preview)
        samples = preview["line_samples"]
        self.assertLessEqual(len(samples), 6)
        self.assertGreaterEqual(len(samples), 4)
        for item in samples:
            self.assertIsInstance(item["source_line"], int)
            self.assertLessEqual(len(item["text"]), 120)
        sample_texts = [item["text"] for item in samples]
        self.assertIn("I.", sample_texts)
        self.assertIn("II.", sample_texts)
        truncated = next(item["text"] for item in samples if item["source_line"] == 4)
        self.assertEqual(len(truncated), 120)
        self.assertTrue(truncated.endswith("…"))

        # 2. Tight-only TXT does NOT include line_samples
        tight = self.root / "tight.txt"
        tight.write_text("第一章引子\n甲。\n第二章继续\n乙。\n", encoding="utf-8")
        tight_preview = self.span_json("import-txt", tight, "--preview")
        self.assertEqual(tight_preview["tight_candidate_count"], 2)
        self.assertNotIn("line_samples", tight_preview)
        self.assertIn("suggested_chapter_pattern", tight_preview)

        # 3. Detected chapters include chapter_length_stats exposing merged/missed chapters
        merged = self.root / "merged.txt"
        merged.write_text(
            "引子前文\n"
            "第一章 短章一\n" + ("甲" * 100) + "\n"
            "第二章 粘连超长章\n" + ("乙" * 2000) + "\n"
            "第三章 短章三\n" + ("丙" * 120) + "\n"
            "第四章 偏长章\n" + ("丁" * 600) + "\n"
            "第五章 短章五\n" + ("戊" * 110) + "\n"
            "作者的话\n小注。\n",
            encoding="utf-8",
        )
        m_preview = self.span_json("import-txt", merged, "--preview")
        self.assertEqual(m_preview["heading_count"], 5)
        self.assertNotIn("line_samples", m_preview)
        stats = m_preview["chapter_length_stats"]
        self.assertEqual(stats["chapter_count"], 5)
        self.assertEqual(stats["max_characters"], 2001)
        self.assertEqual(stats["median_characters"], 121)
        self.assertEqual(len(stats["longest_chapters"]), 3)
        # Leading 章前文字 occupies C0001, so 第二章 is C0003.
        self.assertEqual(stats["longest_chapters"][0]["chapter"], 3)
        self.assertEqual(stats["longest_chapters"][0]["title"], "第二章 粘连超长章")
        self.assertEqual(stats["longest_chapters"][0]["source_line"], 4)
        self.assertEqual(stats["longest_chapters"][0]["characters"], 2001)
        self.assertEqual([row["title"] for row in stats["longest_chapters"]],
                         ["第二章 粘连超长章", "第四章 偏长章", "第三章 短章三"])

    def test_single_chapter_mode_symmetry_conflicts_and_persistence(self):
        oneshot_txt = self.root / "同人单篇.txt"
        oneshot_txt.write_text("雾气弥漫在港口。\n船铃响了两声。\n", encoding="utf-8")

        # Never silently falls back to single chapter.
        fail_default = self.span_process("--text", oneshot_txt, "overview", "--as-of", "all")
        self.assertNotEqual(fail_default.returncode, 0)
        self.assertIn("--single-chapter", fail_default.stderr)

        # Works symmetrically before and after subcommands for --text and import-txt.
        ov_before = self.span_json("--text", oneshot_txt, "--single-chapter", "overview", "--as-of", "all", "--format", "json")
        ov_after = self.span_json("--text", oneshot_txt, "overview", "--as-of", "all", "--single-chapter", "--format", "json")
        self.assertEqual(ov_before["scope"], ov_after["scope"])
        self.assertEqual(ov_before["chapters"], ov_after["chapters"])
        self.assertEqual(len(ov_before["chapters"]), 1)
        self.assertEqual(ov_before["chapters"][0]["title"], "同人单篇")
        self.assertEqual(ov_before["chapters"][0]["kind"], "body")
        self.assertEqual(ov_before["chapters"][0]["source_body_line"], 1)

        # Full body preserved intact.
        read_all = self.span_json("--text", oneshot_txt, "--single-chapter", "read", "--chapter", "C0001", "--as-of", "1", "--format", "json")
        self.assertEqual(read_all["fragments"][0]["text"], "雾气弥漫在港口。\n船铃响了两声。\n")

        # Import before/after + preview + duplicate rejection + conflict rejection.
        prev_before = self.span_json("--single-chapter", "import-txt", oneshot_txt, "--preview")
        prev_after = self.span_json("import-txt", oneshot_txt, "--single-chapter", "--preview")
        self.assertEqual(prev_before["heading_count"], 1)
        self.assertEqual(prev_before["parsing"]["single_chapter"], True)
        self.assertEqual(prev_before["chapter_length_stats"]["chapter_count"], 1)
        self.assertEqual(prev_before, prev_after)

        dup = self.span_process("--single-chapter", "import-txt", oneshot_txt, "--single-chapter", "--preview")
        self.assertNotEqual(dup.returncode, 0)
        self.assertIn("重复指定", dup.stderr)

        conflict_pat = self.span_process("--text", oneshot_txt, "--single-chapter", "--chapter-pattern", ".*", "overview", "--as-of", "all")
        self.assertNotEqual(conflict_pat.returncode, 0)
        self.assertIn("不能与 --chapter-pattern 同时使用", conflict_pat.stderr)

        lib = self.root / "single_lib"
        imported = self.span_json("--library", lib, "import-txt", oneshot_txt, "--single-chapter")
        self.assertEqual(imported["snapshot_id"], ov_before["scope"]["snapshot_id"])
        self.assertEqual(imported["chapters"], 1)

        # Reject --single-chapter when using --library.
        bad_lib_flag = self.span_process("--library", lib, "--single-chapter", "overview", "--snapshot", imported["snapshot_id"], "--as-of", "1")
        self.assertNotEqual(bad_lib_flag.returncode, 0)
        self.assertIn("仅用于 --text 或 import-txt", bad_lib_flag.stderr)

        # Inspect matches between --text and --library.
        insp_txt = self.span_json("--text", oneshot_txt, "--single-chapter", "overview", "--as-of", "1", "--inspect", "C0001", "--format", "json")
        insp_lib = self.span_json("--library", lib, "overview", "--snapshot", imported["snapshot_id"], "--as-of", "1", "--inspect", "C0001")
        self.assertEqual(insp_txt["inspection"]["source_lines"], insp_lib["inspection"]["source_lines"])
        self.assertEqual(insp_txt["inspection"]["parsing"], insp_lib["inspection"]["parsing"])

        # Handoff inherits single_chapter=True via --from-result without repeating --single-chapter.
        found_path = self.root / "single_find.txt"
        saved = self.span_process("--text", oneshot_txt, "--single-chapter", "--output", found_path, "find", "船铃", "--as-of", "1")
        self.assertEqual(saved.returncode, 0, saved.stderr)
        expanded = self.span_json("--text", oneshot_txt, "read", "--from-result", found_path, "--example", "1", "--format", "json")
        self.assertIn("船铃", expanded["fragments"][0]["text"])
        self.assertEqual(expanded["text_options"].get("single_chapter"), True)

        # Run persists single_chapter=True in run.json and works across commands.
        init_res = self.span_json("--text", oneshot_txt, "--single-chapter", "research", "init", self.root / "single_task", "--as-of", "1")
        run_dir = Path(init_res["run_directory"])
        manifest = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["text_options"]["single_chapter"], True)
        run_ov = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(run_ov["scope"]["snapshot_id"], imported["snapshot_id"])
        override_err = self.span_process("--run", run_dir, "--single-chapter", "overview")
        self.assertNotEqual(override_err.returncode, 0)

        # Markdown with --single-chapter uses stripped first heading as title and keeps full body intact.
        md_single = self.root / "设定稿.md"
        md_single.write_text("---\nauthor: 匿名\n---\n# 世界观总览\n\n## 第一节\n内容甲。\n", encoding="utf-8")
        md_ov = self.span_json("--text", md_single, "--single-chapter", "overview", "--as-of", "1", "--format", "json")
        self.assertEqual(md_ov["chapters"][0]["title"], "世界观总览")
        md_read = self.span_json("--text", md_single, "--single-chapter", "read", "--chapter", "C0001", "--as-of", "1", "--format", "json")
        self.assertTrue(md_read["fragments"][0]["text"].startswith("---\nauthor: 匿名\n---\n# 世界观总览"))

    def test_suffix_dispatch_and_unsupported_format_errors(self):
        content = "第一章 起\n正文内容。\n"
        for name in ("a.txt", "b.text", "no_suffix"):
            p = self.root / name
            p.write_text(content, encoding="utf-8")
            prev = self.span_json("import-txt", p, "--preview")
            self.assertEqual(prev["format"], "txt")
            self.assertEqual(prev["heading_count"], 1)

        md_content = "## 第一章 起\n正文内容。\n"
        for name in ("c.md", "d.markdown", "E.MD"):
            p = self.root / name
            p.write_text(md_content, encoding="utf-8")
            prev = self.span_json("import-txt", p, "--preview")
            self.assertEqual(prev["format"], "markdown")
            self.assertEqual(prev["heading_count"], 1)

        # Unsupported suffixes produce actionable external-tool errors.
        cases = (
            ("book.mobi", "ebook-convert"),
            ("book.azw3", "ebook-convert"),
            ("book.docx", "pandoc"),
            ("book.pdf", "不推荐"),
            ("book.rtf", "不支持的源文件后缀"),
        )
        for filename, expected_hint in cases:
            p = self.root / filename
            p.write_bytes(b"placeholder")
            res = self.span_process("import-txt", p, "--preview")
            self.assertNotEqual(res.returncode, 0, filename)
            self.assertIn(expected_hint, res.stderr, filename)

        # --heading-level rejected on TXT and out of 1..6 range.
        txt_path = self.root / "a.txt"
        bad_txt_lvl = self.span_process("--text", txt_path, "--heading-level", "2", "overview", "--as-of", "1")
        self.assertNotEqual(bad_txt_lvl.returncode, 0)
        self.assertIn("仅用于 Markdown", bad_txt_lvl.stderr)

        md_path = self.root / "c.md"
        for bad_lvl in ("0", "7"):
            res = self.span_process("--text", md_path, "--heading-level", bad_lvl, "overview", "--as-of", "1")
            self.assertNotEqual(res.returncode, 0)

    def test_markdown_atx_exclusions_classification_and_multilevel_refusal(self):
        md = self.root / "多卷草稿.md"
        md.write_text(
            "---\n"
            "title: 测试草稿\n"
            "# yaml 注释不是标题\n"
            "---\n\n"
            "# 第一卷 启程\n\n"
            "## 楔子\n"
            "楔子正文。\n\n"
            "## 第一章 雾港 ##\n"
            "正文包含 **原始Markdown** 和 #无空格标签。\n"
            "```markdown\n"
            "# 代码块内假卷\n"
            "## 第二章 假章\n"
            "```\n"
            "~~~text\n"
            "## 第三章 波浪线假章\n"
            "~~~\n"
            "    ## 第四章 四空格缩进假章\n"
            "####### 七级井号不是标题\n\n"
            "### 细节小节\n"
            "小节留在第一章原始正文中。\n\n"
            "## 作者的话\n"
            "今天更新一章。\n\n"
            "## 第二章 C# 笔记\n"
            "第二章正文。\n\n"
            "# 第二卷 远航\n\n"
            "## 第四章 跳号测试\n"
            "第四章正文。\n",
            encoding="utf-8",
        )

        # 1. Preview without --heading-level succeeds and lists all ATX levels (excluding frontmatter & fenced code).
        prev = self.span_json("import-txt", md, "--preview")
        self.assertEqual(prev["format"], "markdown")
        self.assertEqual(prev["markdown_heading_levels"], {"1": 2, "2": 5, "3": 1})
        self.assertEqual(prev["heading_count"], 0)
        self.assertIn("hint", prev)
        self.assertIn("markdown_heading_examples", prev)

        # 2. Direct --text and import without --heading-level REFUSE to guess.
        for cmd in (
            ("--text", md, "overview", "--as-of", "all"),
            ("--library", self.root / "md_refuse_lib", "import-txt", md),
        ):
            refused = self.span_process(*cmd)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("--heading-level", refused.stderr)
            self.assertIn("--preview", refused.stderr)
            self.assertIn("1 级", refused.stderr)
            self.assertIn("2 级", refused.stderr)

        # 3. Conflict with --chapter-pattern and --single-chapter rejected.
        for conflict_args in (
            ("--heading-level", "2", "--chapter-pattern", "^## .*"),
            ("--heading-level", "2", "--single-chapter"),
        ):
            res = self.span_process("--text", md, *conflict_args, "overview", "--as-of", "all")
            self.assertNotEqual(res.returncode, 0)

        # 4. Absent heading level reports available levels.
        missing_lvl = self.span_process("--text", md, "--heading-level", "5", "overview", "--as-of", "all")
        self.assertNotEqual(missing_lvl.returncode, 0)
        self.assertIn("2 级", missing_lvl.stderr)

        # 5. Preview WITH --heading-level 2 reports chapter_length_stats and 3 body chapters (excluding 楔子 & 作者的话).
        prev2 = self.span_json("import-txt", md, "--heading-level", "2", "--preview")
        self.assertEqual(prev2["heading_count"], 3)
        self.assertEqual(prev2["parsing"]["heading_level"], 2)
        self.assertEqual(prev2["chapter_length_stats"]["chapter_count"], 3)

        # 6. Overview with --heading-level 2 checks YAML frontmatter -> 章前文字, stripped titles, classification, diagnostics.
        ov = self.span_json("--text", md, "--heading-level", "2", "overview", "--as-of", "all", "--include-peripheral", "--format", "json")
        titles = [(c["chapter"], c["title"], c["kind"]) for c in ov["chapters"]]
        self.assertEqual(titles, [
            (1, "章前文字", "unclassified"),
            (2, "楔子", "unclassified"),
            (3, "第一章 雾港", "body"),
            (4, "作者的话", "peripheral"),
            (5, "第二章 C# 笔记", "body"),
            (6, "第四章 跳号测试", "body"),
        ])
        # Number gap diagnostic (2 -> 4) works seamlessly on stripped Markdown titles.
        self.assertIn("number_gap", ov["chapter_diagnostics"]["kind_counts"])

        # 7. --as-of by Chinese chapter number works on stripped title.
        ov_ch2 = self.span_json("--text", md, "overview", "--heading-level", "2", "--as-of", "第二章", "--format", "json")
        self.assertEqual(ov_ch2["scope"]["as_of"], 5)
        self.assertEqual([c["title"] for c in ov_ch2["chapters"]], ["章前文字", "楔子", "第一章 雾港", "第二章 C# 笔记"])

        # 8. Body stays raw Markdown (unrendered), preserving YAML front matter in C0001 and fenced code / ### in C0003.
        read_front = self.span_json("--text", md, "--heading-level", "2", "read", "--chapter", "C0001", "--as-of", "all", "--format", "json")
        self.assertIn("title: 测试草稿", read_front["fragments"][0]["text"])
        self.assertIn("# 第一卷 启程", read_front["fragments"][0]["text"])

        read_ch1 = self.span_json("--text", md, "--heading-level", "2", "read", "--chapter", "C0003", "--as-of", "all", "--format", "json")
        ch1_text = read_ch1["fragments"][0]["text"]
        self.assertIn("**原始Markdown**", ch1_text)
        self.assertIn("## 第二章 假章", ch1_text)
        self.assertIn("### 细节小节", ch1_text)

        # 9. Single-level Markdown auto-selects the unique ATX level and records it in parsing.
        single_lvl = self.root / "单层.md"
        single_lvl.write_text("## 第一章 甲\n正文甲。\n\n## 第二章 乙\n正文乙。\n", encoding="utf-8")
        sl_prev = self.span_json("import-txt", single_lvl, "--preview")
        self.assertEqual(sl_prev["format"], "markdown")
        self.assertEqual(sl_prev["markdown_heading_levels"], {"2": 2})
        self.assertEqual(sl_prev["parsing"]["heading_level"], 2)
        self.assertEqual(sl_prev["heading_count"], 2)
        sl_ov = self.span_json("--text", single_lvl, "overview", "--as-of", "第二章", "--format", "json")
        self.assertEqual([c["title"] for c in sl_ov["chapters"]], ["第一章 甲", "第二章 乙"])

        # 10. Explicit --chapter-pattern on .md is honored.
        custom_md = self.span_json("--text", md, "--chapter-pattern", r"^# 第.+卷 .*", "overview", "--as-of", "all", "--format", "json")
        self.assertEqual([c["title"] for c in custom_md["chapters"] if c["kind"] == "body"],
                         ["# 第一卷 启程", "# 第二卷 远航"])

    def test_markdown_cross_entry_consistency_and_handoff(self):
        md = self.root / "跨入口.md"
        md.write_text(
            "---\nmeta: 1\n---\n"
            "# 第一卷\n"
            "## 第一章 星港\n星港灯火通明，星港船帆林立。\n"
            "## 第二章 星港余晖\n星港潮起，青铜钥匙遗落码头。\n"
            "## 第三章 荒原\n荒原风声呼啸，荒原石碑林立。\n"
            "## 第四章 荒原尽头\n荒原尽头，青铜钥匙再次出现。\n",
            encoding="utf-8",
        )
        lib = self.root / "md_lib"
        imported = self.span_json("--library", lib, "import-txt", md, "--heading-level", "2")
        snap = imported["snapshot_id"]
        self.assertTrue(imported["source"].endswith(".md"))
        self.assertTrue((lib / imported["source"]).is_file())

        init_res = self.span_json("--text", md, "--heading-level", "2", "research", "init", self.root / "md_task", "--as-of", "第三章")
        self.assertEqual(init_res["snapshot_id"], snap)
        self.assertEqual(init_res["scope"]["as_of"], 4)
        run_dir = Path(init_res["run_directory"])
        manifest = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["text_options"]["heading_level"], 2)
        self.assertNotIn("chapter_pattern", manifest["source"]["text_options"])

        # Expand run to all chapters and verify overview, find, compare, read, inspect parity across all 3 entries.
        expanded_run = self.span_json("--run", run_dir, "research", "expand", "--as-of", "第四章")
        self.assertEqual(expanded_run["scope"]["as_of"], 5)

        ov_text = self.span_json("--text", md, "--heading-level", "2", "overview", "--as-of", "all", "--format", "json")
        ov_lib = self.span_json("--library", lib, "overview", "--snapshot", snap, "--as-of", "all")
        ov_run = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(ov_text["scope"], ov_lib["scope"])
        self.assertEqual(ov_text["scope"], ov_run["scope"])
        self.assertEqual(ov_text["chapters"], ov_lib["chapters"])
        self.assertEqual(ov_text["chapters"], ov_run["chapters"])

        find_text = self.span_json("--text", md, "--heading-level", "2", "find", "青铜钥匙", "--as-of", "all", "--examples", "0", "--format", "json")
        find_lib = self.span_json("--library", lib, "find", "青铜钥匙", "--snapshot", snap, "--as-of", "all", "--examples", "0")
        find_run = self.span_json("--run", run_dir, "find", "青铜钥匙", "--examples", "0", "--format", "json")
        self.assertEqual(find_text["matches"], find_lib["matches"])
        self.assertEqual(find_text["matches"], find_run["matches"])

        insp_text = self.span_json("--text", md, "--heading-level", "2", "overview", "--as-of", "all", "--inspect", "C0002", "--format", "json")
        insp_lib = self.span_json("--library", lib, "overview", "--snapshot", snap, "--as-of", "all", "--inspect", "C0002")
        insp_run = self.span_json("--run", run_dir, "overview", "--inspect", "C0002", "--format", "json")
        self.assertEqual(insp_text["inspection"], insp_lib["inspection"])
        self.assertEqual(insp_text["inspection"], insp_run["inspection"])
        self.assertEqual(insp_text["inspection"]["source_lines"][0]["text"], "## 第一章 星港")

        # Bound --run rejects overriding --heading-level.
        override_err = self.span_process("--run", run_dir, "--heading-level", "1", "overview")
        self.assertNotEqual(override_err.returncode, 0)

        # --from-result inherits heading_level=2 without repeating --heading-level.
        out_find = self.root / "md_find.txt"
        saved_find = self.span_process("--text", md, "--heading-level", "2", "--output", out_find, "find", "青铜钥匙", "--as-of", "all")
        self.assertEqual(saved_find.returncode, 0, saved_find.stderr)
        followup = self.span_json("--text", md, "read", "--from-result", out_find, "--example", "1", "--format", "json")
        self.assertIn("青铜钥匙", followup["fragments"][0]["text"])
        self.assertEqual(followup["text_options"]["heading_level"], 2)

        # Draft resume + research index on Markdown run.
        span_coord = find_run["matches"][0]["span"]
        draft = run_dir / "当前稿.md"
        draft.write_text(f"{init_res['draft_header']}# 笔记\n发现线索 [@{span_coord}]。\n", encoding="utf-8")
        resumed = self.span_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(resumed["citations"][0]["status"], "ok")
        indexed = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(indexed["citations"], 1)

    def test_backward_compatibility_old_library_and_old_run_json(self):
        # 1. Legacy SQLite library with parsing lacking "format" and archived as sources/<sha>.txt
        legacy_txt = self.root / "旧书.txt"
        legacy_txt.write_text("第一章 旧开篇\n旧正文内容。\n第二章 旧续篇\n更多旧正文。\n", encoding="utf-8")
        snap, chapters, _, _ = read_text_source_details(legacy_txt)
        raw = legacy_txt.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()

        legacy_lib = self.root / "旧书库"
        archive_source(legacy_lib, sha, raw)
        self.assertTrue((legacy_lib / "sources" / f"{sha}.txt").is_file())
        legacy_parsing = {"chapter_pattern": DEFAULT_HEADING, "paragraphs": "lines", "newlines": "LF"}
        with closing(sqlite3.connect(legacy_lib / DB_NAME)) as conn:
            conn.executescript(SCHEMA)
            with conn:
                conn.execute(
                    "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                    (snap, sha, legacy_txt.name, "utf-8-sig", json.dumps(legacy_parsing)),
                )
                conn.executemany(
                    "INSERT INTO chapters VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ((snap, c.number, c.title, c.kind, c.source_body_line, c.body, json.dumps(c.paragraph_ends))
                     for c in chapters),
                )

        snaps = list_snapshots(legacy_lib)["snapshots"]
        self.assertEqual(snaps[0]["parsing"], legacy_parsing)
        with open_scope(legacy_lib, Scope(snap, 2)) as lib:
            insp = lib.inspect_chapter(1)
            self.assertEqual(insp["title"], "第一章 旧开篇")
            self.assertEqual(insp["parsing"], legacy_parsing)

        # 2. Legacy run.json with only {encoding, chapter_pattern, paragraphs}
        run_id = "run_" + "a" * 32
        legacy_run = self.root / "旧任务" / "runs" / run_id
        (legacy_run / "results").mkdir(parents=True)
        (legacy_run / "states").mkdir(parents=True)
        (legacy_run / "calls.jsonl").write_text("", encoding="utf-8")
        legacy_manifest = {
            "run_version": RUN_VERSION,
            "run_id": run_id,
            "created_at": "2026-11-18T00:00:00.000Z",
            "tool_version": "0.4.0.dev5",
            "source": {
                "kind": "text",
                "path": str(legacy_txt.resolve()),
                "text_options": {
                    "encoding": "utf-8-sig",
                    "chapter_pattern": DEFAULT_HEADING,
                    "paragraphs": "lines",
                },
            },
            "snapshot_id": snap,
            "initial_scope": {"snapshot_id": snap, "as_of": 1, "first_chapter": 1, "include_peripheral": False},
        }
        (legacy_run / RUN_FILE).write_text(json.dumps(legacy_manifest, ensure_ascii=False), encoding="utf-8")

        loaded = load_run(legacy_run)
        self.assertEqual(loaded.snapshot_id, snap)
        ov_legacy = self.span_json("--run", legacy_run, "overview", "--format", "json")
        self.assertEqual([c["title"] for c in ov_legacy["chapters"]], ["第一章 旧开篇"])
        exp_legacy = self.span_json("--run", legacy_run, "research", "expand", "--as-of", "2")
        self.assertEqual(exp_legacy["scope"]["as_of"], 2)

        # 3. Malformed run.json text_options rejected cleanly
        for bad_opts in (
            {"encoding": "utf-8-sig", "paragraphs": "lines"},
            {"encoding": "utf-8-sig", "paragraphs": "lines", "heading_level": 2, "chapter_pattern": ".*"},
            {"encoding": "utf-8-sig", "paragraphs": "lines", "heading_level": True},
            {"encoding": "utf-8-sig", "paragraphs": "lines", "heading_level": 7},
            {"encoding": "utf-8-sig", "paragraphs": "lines", "single_chapter": "yes"},
        ):
            bad_run = self.root / f"bad_{abs(hash(str(bad_opts)))}" / "runs" / run_id
            bad_run.mkdir(parents=True)
            bad_manifest = dict(legacy_manifest, source={"kind": "text", "path": str(legacy_txt), "text_options": bad_opts})
            (bad_run / RUN_FILE).write_text(json.dumps(bad_manifest, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_run(bad_run)

    def test_stage1_docs_and_help_topics_content(self):
        self.assertEqual(len(QUERY_KEYS), 7)
        release_agents = (ROOT / "packaging" / "AGENTS.md").read_text(encoding="utf-8")
        agent_help = HELP_TOPICS["agent"][1]
        self.assertEqual(release_agents.strip(), agent_help.strip())
        self.assertIn("7 个键", release_agents)
        self.assertNotIn("6 个键", release_agents)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for keyword in ("--heading-level", "--single-chapter", "span help troubleshooting"):
            self.assertIn(keyword, readme)
        trouble = HELP_TOPICS["troubleshooting"][1]
        for keyword in (
            "opencc", "tw2s", "t2s", "charset-normalizer", "normalizer -n",
            "novel-processor", "ftfy", "--heading-level", "--single-chapter",
            "chapter_length_stats", "line_samples", "--version-note",
        ):
            self.assertIn(keyword, trouble)


if __name__ == "__main__":
    unittest.main()
