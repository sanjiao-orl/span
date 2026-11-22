import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from chapspan.display import format_text
from chapspan.library import ScopedText, import_txt, open_scope, read_text_source, text_chapter_issues
from chapspan.model import Scope, Span
from chapspan.reading import ReadSelection, read_selections, reclaim_budgets, sample_openings


class BatchOneTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def span_process(self, *args, cwd=None):
        environment = {
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "PYTHONIOENCODING": "utf-8",
        }
        return subprocess.run(
            [sys.executable, "-B", "-m", "chapspan.cli", *map(str, args)],
            cwd=cwd or self.root,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )

    def span_json(self, *args, cwd=None):
        result = self.span_process(*args, cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_reclaim_budgets_and_grouped_surplus_reclamation(self):
        self.assertEqual(reclaim_budgets([300, 300, 300, 1800], 4000, 2000), [300, 300, 300, 1800])
        self.assertEqual(reclaim_budgets([300, 300, 300, 2600], 4000, 2000), [300, 300, 300, 2000])

        source = self.root / "配额验证.txt"
        short_body = "甲" * 299 + "\n"
        long_paragraphs = ("乙" * 299 + "\n") * 6  # 1800 chars
        extra_paragraphs = ("丙" * 299 + "\n") * 8 + ("丁" * 199 + "\n")  # 2600 chars
        source.write_text(
            f"第一章 短一\n{short_body}"
            f"第二章 短二\n{short_body}"
            f"第三章 短三\n{short_body}"
            f"第四章 刚好长\n{long_paragraphs}"
            f"第五章 超限长\n{extra_paragraphs}",
            encoding="utf-8",
        )
        book = self.root / "书库"
        snap = import_txt(book, source)["snapshot_id"]
        with open_scope(book, Scope(snap, 5)) as library:
            groups = [ReadSelection(chapter_numbers=(n,), group=n) for n in (1, 2, 3, 4)]
            full = read_selections(library, groups, budget=4000, group_budget=2000)
            self.assertTrue(full["complete"])
            self.assertEqual(full["read_characters"], 2700)
            self.assertEqual([g["read_characters"] for g in full["groups"]], [300, 300, 300, 1800])

            capped_groups = [ReadSelection(chapter_numbers=(n,), group=n) for n in (1, 2, 3, 5)]
            capped = read_selections(library, capped_groups, budget=4000, group_budget=2000)
            self.assertFalse(capped["complete"])
            self.assertEqual([g["read_characters"] for g in capped["groups"]], [300, 300, 300, 1800])
            self.assertLessEqual(capped["groups"][3]["read_characters"], 2000)
            self.assertEqual(capped["continuation"]["groups"][0]["next"], "C0005:1800")

    def test_grouped_pages_paragraph_stall_falls_back_to_sequential_probing(self):
        source = self.root / "段落阻塞.txt"
        short = "一" * 99 + "\n"
        p700 = "二" * 699 + "\n"
        p700_b = "三" * 699 + "\n"
        source.write_text(
            f"第一章 短章\n{short}"
            f"第二章 双大段甲\n{p700}{p700}"
            f"第三章 双大段乙\n{p700_b}{p700_b}",
            encoding="utf-8",
        )
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 3))
        groups = [ReadSelection(chapter_numbers=(n,), group=n) for n in (1, 2, 3)]
        page = read_selections(library, groups, budget=2300, group_budget=1500)
        # Initial equal split = 766; after C1 uses 100 and C2/C3 each read 700, 800 remains.
        # Adding 400 each stalls at 1100 < 1400; sequential fallback gives 800 to C2 -> reaches 1400.
        self.assertEqual([g["read_characters"] for g in page["groups"]], [100, 1400, 700])
        self.assertEqual(page["read_characters"], 2200)
        self.assertEqual([row["status"] for row in page["delivery"]], ["complete", "complete", "partial"])

    def test_sample_openings_reclaims_surplus_for_long_chapters(self):
        source = self.root / "不均抽读.txt"
        c1 = "甲" * 119 + "\n"
        c2 = "乙" * 119 + "\n"
        c3 = ("丙" * 299 + "\n") * 5  # 1500 chars
        source.write_text(f"第一章 短首\n{c1}第二章 短次\n{c2}第三章 长尾\n{c3}", encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 3))
        sampled = sample_openings(library, 3, budget=2400, group_budget=1200)
        self.assertEqual([g["read_characters"] for g in sampled["groups"]], [120, 120, 1200])
        self.assertEqual(sampled["read_characters"], 1440)

    def test_continue_group_multi_page_no_duplication_or_omission_and_prebody_warnings(self):
        source = self.root / "并读续读.txt"
        c1 = "".join(f"甲{i:03d}" + "文" * 195 + "\n" for i in range(6))  # 6 * 200 = 1200
        c2 = "".join(f"乙{i:03d}" + "字" * 195 + "\n" for i in range(6))  # 6 * 200 = 1200
        source.write_text(f"第一章 甲篇\n{c1}第二章 乙篇\n{c2}", encoding="utf-8")

        page1 = self.span_process(
            "--text", source, "read", "--chapter", "1", "--chapter", "2",
            "--budget", "1200", "--group-budget", "600", "--as-of", "2",
        )
        self.assertEqual(page1.returncode, 0, page1.stderr)
        out1 = page1.stdout
        state1 = re.search(r"read --from-result (state_[^\s（]+)", out1).group(1)
        self.assertIn("未完成 600/1200 字", out1)
        self.assertIn("[组1 · 未完成 600/1200 字]", out1)
        self.assertIn("[组2 · 未完成 600/1200 字]", out1)
        self.assertGreater(out1.index("---"), out1.index("[组2 ·"))
        self.assertIn("续全部：", out1[out1.index("---"):])
        self.assertIn("--continue-group", out1[out1.index("---"):])

        page2 = self.span_process("--text", source, "read", "--from-result", state1, "--continue-group", "2")
        self.assertEqual(page2.returncode, 0, page2.stderr)
        out2 = page2.stdout
        state2 = re.search(r"read --from-result (state_[^\s（]+)", out2).group(1)
        self.assertIn("[组2]", out2)
        self.assertNotIn("[组2 · 未完成", out2)
        self.assertGreater(out2.index("本轮未续："), out2.index("[组2]"))
        self.assertIn("600/1200 字，待续 C0001:600", out2[out2.index("---"):])

        page3 = self.span_json("--text", source, "read", "--from-result", state2, "--format", "json")
        self.assertTrue(page3["complete"])

        # Reconstruct chapter texts through JSON pages to verify exact coverage without gap or duplication.
        j1 = self.span_json(
            "--text", source, "read", "--chapter", "1", "--chapter", "2",
            "--budget", "1200", "--group-budget", "600", "--as-of", "2", "--format", "json",
        )
        p1_file = self.root / "p1.json"
        p1_file.write_text(json.dumps(j1, ensure_ascii=False), encoding="utf-8")
        j2 = self.span_json("--text", source, "read", "--from-result", p1_file, "--continue-group", "2", "--format", "json")
        p2_file = self.root / "p2.json"
        p2_file.write_text(json.dumps(j2, ensure_ascii=False), encoding="utf-8")
        j3 = self.span_json("--text", source, "read", "--from-result", p2_file, "--format", "json")

        by_chapter = {1: [], 2: []}
        for page in (j1, j2, j3):
            for group in page["groups"]:
                for frag in group["fragments"]:
                    span = Span.parse(frag["read_span"])
                    passage = page["passages"][frag["passage"] - 1]
                    by_chapter[span.chapter].append((span.start, span.end, passage["text"]))
        for ch, expected in ((1, c1), (2, c2)):
            pieces = sorted(by_chapter[ch])
            self.assertEqual(pieces[0][0], 0)
            for prev, curr in zip(pieces, pieces[1:]):
                self.assertEqual(prev[1], curr[0])
            self.assertEqual("".join(text for _, _, text in pieces), expected)

    def test_ungrouped_multi_selection_shows_not_started_and_parallel_hint_before_body(self):
        source = self.root / "单组阻塞.txt"
        body1 = "甲" * 199 + "\n" + "乙" * 199 + "\n"  # 400 chars
        body2 = "丙" * 199 + "\n"
        source.write_text(f"第一章 甲章\n{body1}第二章 乙章\n{body2}", encoding="utf-8")
        proc = self.span_process("--text", source, "read", "--chapter", "1", "--chapter", "2", "--budget", "250", "--as-of", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        state_id = re.search(r"read --from-result (state_[^\s（]+)", out).group(1)
        self.assertEqual(out.count(state_id), 1)
        body_start = out.index("甲甲")
        self.assertLess(out.index("（未完成 200/400 字）"), body_start)
        footer_start = out.index("---")
        self.assertGreater(footer_start, body_start)
        self.assertIn("未开始：C0002:0-200", out[footer_start:])
        self.assertIn(f"read --from-result {state_id}", out[footer_start:])
        self.assertNotIn("并读未开始选区", out)

        formatted = format_text(
            self.span_json("--text", source, "read", "--chapter", "1", "--chapter", "2", "--budget", "250", "--as-of", "2", "--format", "json"),
            reference=state_id,
        )
        self.assertEqual(formatted.count(state_id), 1)

    def test_peripheral_category_summary_and_unclassified_visibility_parity(self):
        source = self.root / "外围分类.txt"
        sections = [
            "序章 起点\n序文内容。\n",
            "第一章 初见\n正文第一段。\n",
        ]
        for idx in range(1, 7):
            sections.append(f"请假条 {idx}\n今天休息。\n")
        for idx in range(1, 4):
            sections.append(f"作者有话说 {idx}\n作者唠叨。\n")
        sections.extend([
            "上架感言\n明天上架。\n",
            "完本感言\n感谢订阅。\n",
            "后记\n尾声随笔。\n",
            "第二章 终章\n正篇收尾。\n",
            "请假条 越界\n范围之外。\n",
            "后记 越界\n不可泄露。\n",
        ])
        source.write_text("".join(sections), encoding="utf-8")

        book = self.root / "书库"
        snap = import_txt(book, source)["snapshot_id"]
        _, all_chapters, candidates = read_text_source(source)
        # Scope ends at chapter 15 ("第二章 终章"), excluding chapters 16-17.
        scope = Scope(snap, 15, include_peripheral=False)
        diag = text_chapter_issues(all_chapters, candidates, scope)
        expected_counts = {"afterword": 1, "author_note": 3, "ending_note": 1, "launch_note": 1, "leave_notice": 6}
        self.assertEqual(diag["filtered_peripheral_count"], 12)
        self.assertEqual(diag["filtered_peripheral_category_counts"], expected_counts)
        self.assertEqual({row["category"] for row in diag["filtered_peripheral_examples"]}, set(expected_counts))

        txt_ov = self.span_json("--text", source, "overview", "--as-of", "15", "--format", "json")
        db_ov = self.span_json("--library", book, "overview", "--snapshot", snap, "--as-of", "15", "--format", "json")
        for ov in (txt_ov, db_ov):
            self.assertEqual(ov["chapter_kind_counts"], {"body": 2, "unclassified": 1})
            self.assertEqual([row["kind"] for row in ov["chapters"]], ["unclassified", "body", "body"])
            self.assertEqual(ov["chapter_diagnostics"]["filtered_peripheral_category_counts"], expected_counts)
            self.assertEqual(
                {row["category"] for row in ov["chapter_diagnostics"]["filtered_peripheral_examples"]},
                set(expected_counts),
            )

        txt_view = self.span_process("--text", source, "overview", "--as-of", "15", "--format", "text").stdout
        db_view = self.span_process("--library", book, "overview", "--snapshot", snap, "--as-of", "15", "--format", "text").stdout
        self.assertEqual(txt_view, db_view)
        self.assertIn("[未分类]", txt_view)
        self.assertIn("范围可见 正篇 2 · 未分类 1", txt_view)
        self.assertIn("过滤外围：12 章（后记 1 · 作者的话 3 · 完本感言 1 · 上架感言 1 · 请假条 6）", txt_view)
        for keyword in ("上架感言", "完本感言", "后记", "作者有话说", "请假条"):
            self.assertIn(keyword, txt_view)
        self.assertNotIn("越界", txt_view)

        with_peripheral = self.span_process(
            "--library", book, "overview", "--snapshot", snap, "--as-of", "15", "--include-peripheral", "--format", "text"
        ).stdout
        self.assertIn("[外围]", with_peripheral)

    def test_grouped_pages_reclaims_union_budget_and_zero_cost_advancement(self):
        source = self.root / "重叠预算.txt"
        body1 = "0123456789abcdefghij" * 8 + "\n"
        body2 = "甲乙丙丁戊己庚辛。\n" * 6
        source.write_text(f"第一章 重叠章\n{body1}第二章 独立章\n{body2}", encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 2))

        # 1) Reviewer sample: 0-10 & 5-15 with budget=10, group_budget=10 advances group 1 to complete at 0 marginal cost.
        res1 = read_selections(
            library,
            [ReadSelection(spans=(Span(1, 0, 10),), group=1), ReadSelection(spans=(Span(1, 5, 15),), group=2)],
            budget=10,
            group_budget=10,
        )
        self.assertEqual(res1["read_characters"], 10)
        self.assertEqual(res1["groups"][0]["read_characters"], 10)
        self.assertTrue(res1["groups"][0]["complete"])
        self.assertEqual(res1["groups"][1]["read_characters"], 5)
        self.assertFalse(res1["groups"][1]["complete"])

        # 2) Partial overlap when remaining hits 0: 0-20 & 5-25 with budget=20, group_budget=20.
        res2 = read_selections(
            library,
            [ReadSelection(spans=(Span(1, 0, 20),), group=1), ReadSelection(spans=(Span(1, 5, 25),), group=2)],
            budget=20,
            group_budget=20,
        )
        self.assertEqual(res2["read_characters"], 20)
        self.assertEqual(res2["groups"][0]["read_characters"], 20)
        self.assertTrue(res2["groups"][0]["complete"])
        self.assertEqual(res2["groups"][1]["read_characters"], 15)

        # 3) Identical overlapping spans plus disjoint group complete without artificial budget stall.
        res3 = read_selections(
            library,
            [
                ReadSelection(spans=(Span(1, 0, 100),), group=1),
                ReadSelection(spans=(Span(1, 0, 100),), group=2),
                ReadSelection(spans=(Span(2, 0, 30),), group=3),
            ],
            budget=130,
            group_budget=130,
        )
        self.assertEqual(res3["read_characters"], 130)
        self.assertTrue(res3["complete"])
        self.assertEqual([g["read_characters"] for g in res3["groups"]], [100, 100, 30])

        # 4) Whole-paragraph overlap + sequential fallback still reaches full union budget cleanly.
        para_src = self.root / "重叠整段.txt"
        para_src.write_text("第一章 段\n一句。\n二句。\n第三长句子啊。\n", encoding="utf-8")
        psnap, pchap, _ = read_text_source(para_src)
        plib = ScopedText(pchap, Scope(psnap, 1))
        para_res = read_selections(
            plib,
            [ReadSelection(spans=(Span(1, 0, 16),), group=1), ReadSelection(spans=(Span(1, 0, 16),), group=2)],
            whole_paragraphs=True,
            budget=16,
            group_budget=16,
        )
        self.assertEqual(para_res["read_characters"], 16)
        self.assertTrue(para_res["complete"])


if __name__ == "__main__":
    unittest.main()
