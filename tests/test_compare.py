import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest import mock

from chapspan.compare import (
    CONTRAST_POOL_LIMIT,
    HAN_CLAUSE,
    MAX_EXPLAIN_TERMS,
    compare_intervals,
    parse_interval_spec,
    _g2_contrast,
    _matches_direction,
    _prune_subsumed_fragments,
)
from chapspan.display import format_text
from chapspan.library import ScopedText, import_txt, read_text_source, read_text_source_details
from chapspan.model import Chapter, Scope
from chapspan.runs import load_run


class CompareTests(unittest.TestCase):
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

    def _build_fixture(self):
        source = self.root / "双区对比.txt"
        chapters = [
            "第一章 启程\n旧式傀儡站立在城门下。\n共通用语照常通行。\n旧式傀儡转动齿轮，共通用语再次响起。\n",
            "第二章 守城\n旧式傀儡列阵巡逻，旧式傀儡修复装甲。\n共通用语稳定传递。\n旧式傀儡维持阵型，共通用语不曾改变。\n",
            "第三章 告别\n旧式傀儡缓缓退场，旧式傀儡沉入地底。\n共通用语依旧沿用。\n旧式傀儡留下痕迹，共通用语平淡收尾。\n",
            "作者有话说\n" + "".join(
                f"{p}幕后杂谈{s}。\n"
                for p, s in zip(
                    ("记录", "谈及", "整理", "公开", "补充", "写下", "汇总", "回忆", "梳理", "分享"),
                    ("笔录", "设定", "灵感", "大纲", "年表", "人物", "旧稿", "线索", "构思", "花絮"),
                )
            ),
            "第五章 转折\n星辉引擎点亮穹顶。\n共通用语照常通行。\n星辉引擎驱动巨舰，共通用语再次响起。\n",
            "第六章 远航\n星辉引擎稳定输出，星辉引擎照亮星海。\n共通用语稳定传递。\n星辉引擎持续加速，共通用语不曾改变。\n",
            "第七章 彼岸\n星辉引擎抵达新域，星辉引擎共振共鸣。\n共通用语依旧沿用。\n星辉引擎完成跃迁，共通用语平淡收尾。\n",
            "第八章 后续机密\n" + "超纲秘宝封存深处。\n" * 15,
        ]
        source.write_text("".join(chapters), encoding="utf-8")
        book = self.root / "书库"
        snap = import_txt(book, source)["snapshot_id"]
        return source, book, snap

    def test_duplicate_title_hints_respect_lower_bound_and_peripheral_filter(self):
        chapters = [Chapter(number, "相同章题", kind, 1, "文", (1,))
                    for number, kind in enumerate(("body", "peripheral", "body", "body"), 1)]
        for include_peripheral in (False, True):
            with self.subTest(include_peripheral=include_peripheral), self.assertRaises(ValueError) as caught:
                parse_interval_spec("相同章题", chapters, Scope("snap_test", 4, 2, include_peripheral))
            message = str(caught.exception)
            self.assertNotIn("C0001", message)
            self.assertEqual("C0002" in message, include_peripheral)
            self.assertIn("C0003", message)
            self.assertIn("C0004", message)

    def test_scope_isolation_and_txt_sqlite_parity(self):
        source, book, snap = self._build_fixture()
        txt_res = self.span_json(
            "--text", source, "compare", "--left", "1-3", "--right", "5-7", "--as-of", "7",
            "--min-support", "5", "--format", "json",
        )
        db_res = self.span_json(
            "--library", book, "compare", "--snapshot", snap, "--left", "C0001-C0003", "--right", "C0005-C0007",
            "--as-of", "7", "--min-support", "5", "--format", "json",
        )
        self.assertEqual(txt_res["left_interval"], db_res["left_interval"])
        self.assertEqual(txt_res["right_interval"], db_res["right_interval"])
        self.assertEqual(txt_res["candidates"], db_res["candidates"])

        terms = [row["term"] for row in txt_res["candidates"]]
        self.assertEqual(set(terms), {"星辉引擎", "旧式傀儡"})
        self.assertNotIn("幕后杂谈", terms)
        self.assertNotIn("超纲秘宝", terms)

        # Out-of-scope right endpoint must fail cleanly without leaking future chapters.
        leak = self.span_process("--text", source, "compare", "--left", "1-3", "--right", "5-8", "--as-of", "7")
        self.assertNotEqual(leak.returncode, 0)
        self.assertIn("超出当前允许范围", leak.stderr)

        # Printed boundary specs strictly respect --as-of boundary.
        printed_ok = self.span_json(
            "--text", source, "compare", "--left", "第一章..第三章", "--right", "第五章..第七章",
            "--as-of", "第七章", "--min-support", "5", "--format", "json",
        )
        self.assertEqual([row["term"] for row in printed_ok["candidates"]], terms)
        printed_fail = self.span_process(
            "--text", source, "compare", "--left", "第一章..第三章", "--right", "第五章..第八章", "--as-of", "第七章"
        )
        self.assertNotEqual(printed_fail.returncode, 0)
        self.assertNotIn("第八章", printed_fail.stderr)

        # Opting into --include-peripheral exposes author notes in range.
        with_periph = self.span_json(
            "--library", book, "compare", "--snapshot", snap, "--left", "1-4", "--right", "5-7",
            "--as-of", "7", "--include-peripheral", "--min-support", "5", "--format", "json",
        )
        self.assertIn("幕后杂谈", [row["term"] for row in with_periph["candidates"]])

    def test_asymmetric_interval_lengths_rank_rate_shift_above_constant_background(self):
        source = self.root / "非对称长度.txt"
        left_body = "稳定巡逻记录日常。\n" * 2 + "甲乙丙丁戊己庚辛。\n" * 8
        right_block = "稳定巡逻记录日常。\n" * 2 + "甲乙丙丁戊己庚辛。\n" * 8
        right_parts = []
        for idx in range(5):
            # 5 chapters -> 5x left length; '稳定巡逻' keeps exact same rate (10 vs 2), while '新锐法阵' appears 6x only on right.
            right_parts.append(f"第{idx + 2}章 右章\n{right_block}新锐法阵激活。\n新锐法阵运转。\n" if idx < 3
                               else f"第{idx + 2}章 右章\n{right_block}")
        source.write_text("第一章 左章\n" + left_body + "".join(right_parts), encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 6))
        res = compare_intervals(library, (1, 1), (2, 6), min_support=2, limit=5)
        terms = [row["term"] for row in res["candidates"]]
        self.assertIn("新锐法阵", terms)
        self.assertEqual(terms[0], "新锐法阵")
        for row in res["candidates"]:
            if row["term"] == "稳定巡逻":
                self.assertLess(row["g2_score"], res["candidates"][0]["g2_score"])

    def test_uncovered_masking_resolves_compound_shift_and_directions(self):
        source = self.root / "复合词剥离.txt"
        left_verbs = ("拔出", "擦拭", "收起", "挥动", "打造", "佩戴", "借来", "握紧")
        right_verbs = ("祭出", "催动", "横斩", "收回", "温养", "御使", "重铸", "引爆", "亮出", "举起", "镇压", "刺出")
        left = "第一章 旧器\n" + "".join(f"铁匠{v}铁剑。\n" for v in left_verbs)
        right = "第二章 新器\n" + "".join(f"剑修{v}玄铁剑。\n" for v in right_verbs)
        source.write_text(left + right, encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 2))

        both = compare_intervals(library, (1, 1), (2, 2), min_support=5)
        self.assertEqual(both["parameters"]["counting_basis"], "raw_interval_stats_with_uncovered_selection")
        by_term = {row["term"]: row for row in both["candidates"]}
        self.assertIn("玄铁剑", by_term)
        self.assertIn("铁剑", by_term)
        # Raw counts/rates/spans/trend keep real occurrences; masking exposes uncovered support & selection G2.
        self.assertEqual(by_term["玄铁剑"]["trend"], "right_only")
        self.assertEqual(by_term["玄铁剑"]["right"]["count"], 12)
        self.assertEqual(by_term["铁剑"]["trend"], "down")
        self.assertEqual(by_term["铁剑"]["left"]["count"], 8)
        self.assertEqual(by_term["铁剑"]["right"]["count"], 12)
        self.assertEqual(by_term["铁剑"]["left"]["uncovered_count"], 8)
        self.assertEqual(by_term["铁剑"]["right"]["uncovered_count"], 0)
        self.assertEqual(by_term["铁剑"]["selection"]["left_uncovered_count"], 8)
        self.assertEqual(by_term["铁剑"]["selection"]["right_uncovered_count"], 0)
        self.assertTrue(by_term["铁剑"]["left"]["read_spans"])
        self.assertTrue(by_term["铁剑"]["right"]["read_spans"])

        envelope = {
            "scope": {"snapshot_id": snap, "as_of": 2, "first_chapter": 1, "include_peripheral": False},
            **both,
        }
        rendered = format_text(envelope)
        self.assertNotIn("计数口径", rendered)
        self.assertIn("去重剩余 左 8/右 0", rendered)

        # Saved legacy compare JSON without uncovered_count/counting_basis stays readable.
        legacy = json.loads(json.dumps(envelope, ensure_ascii=False))
        legacy["parameters"].pop("counting_basis")
        for row in legacy["candidates"]:
            row.pop("selection")
            row["left"].pop("uncovered_count")
            row["left"].pop("uncovered_chapters")
            row["right"].pop("uncovered_count")
            row["right"].pop("uncovered_chapters")
        self.assertIn("铁剑", format_text(legacy))

        # Direction filters align with raw counts/trend (never emit 'down' under direction='right').
        right_only = compare_intervals(library, (1, 1), (2, 2), min_support=5, direction="right")
        right_terms = [row["term"] for row in right_only["candidates"]]
        self.assertIn("玄铁剑", right_terms)
        self.assertNotIn("铁剑", right_terms)
        self.assertTrue(all(row["trend"] in ("up", "right_only") for row in right_only["candidates"]))
        left_only = compare_intervals(library, (1, 1), (2, 2), min_support=5, direction="left")
        left_terms = [row["term"] for row in left_only["candidates"]]
        self.assertIn("铁剑", left_terms)
        self.assertNotIn("玄铁剑", left_terms)
        self.assertTrue(all(row["trend"] in ("down", "left_only") for row in left_only["candidates"]))

    def test_handoff_read_from_result_snapshot_and_scope_intersection(self):
        source, book, snap = self._build_fixture()
        cmp_text = self.span_process(
            "--text", source, "compare", "--left", "1-3", "--right", "5-7", "--as-of", "7", "--min-support", "5",
        )
        self.assertEqual(cmp_text.returncode, 0, cmp_text.stderr)
        state_id = re.search(r"read --from-result (state_[^\s]+)", cmp_text.stdout).group(1)

        # Pick rank 1 (say 星辉引擎 / 旧式傀儡) and read via state handle.
        picked = self.span_json(
            "--text", source, "read", "--from-result", state_id, "--pick", "1",
            "--group-budget", "1200", "--before", "1", "--after", "1", "--format", "json",
        )
        self.assertTrue(picked["complete"])
        self.assertGreater(picked["read_characters"], 0)

        # Snapshot mismatch is rejected.
        other_txt = self.root / "另一本.txt"
        other_txt.write_text("第一章 不同书\n全新正文内容。\n", encoding="utf-8")
        mismatch = self.span_process("--text", other_txt, "read", "--from-result", state_id, "--pick", "1")
        self.assertNotEqual(mismatch.returncode, 0)

        # Cannot pick an item whose span lies outside narrowed --as-of.
        cmp_file = self.root / "cmp.json"
        self.span_process(
            "--library", book, "--output", cmp_file,
            "compare", "--snapshot", snap, "--left", "1-3", "--right", "5-7", "--as-of", "7",
            "--min-support", "5", "--format", "json",
        )
        cmp_data = json.loads(cmp_file.read_text(encoding="utf-8"))
        right_idx = next(row["rank"] for row in cmp_data["candidates"] if row["term"] == "星辉引擎")
        left_idx = next(row["rank"] for row in cmp_data["candidates"] if row["term"] == "旧式傀儡")

        out_of_narrow = self.span_process(
            "--library", book, "read", "--from-result", cmp_file, "--pick", str(right_idx), "--as-of", "3"
        )
        self.assertNotEqual(out_of_narrow.returncode, 0)

        # Narrowing within left-only candidate's range succeeds, and attempting --as-of 8 intersects back to 7.
        narrow_ok = self.span_json(
            "--library", book, "read", "--from-result", cmp_file, "--pick", str(left_idx),
            "--as-of", "8", "--format", "json",
        )
        self.assertEqual(narrow_ok["scope"]["as_of"], 7)

    def test_run_integration_calls_jsonl_and_exact_material_index_accounting(self):
        source, book, snap = self._build_fixture()
        init_res = self.span_json("--text", source, "research", "init", self.root / "研究任务", "--as-of", "6")
        run_dir = Path(init_res["run_directory"])
        self.assertEqual(init_res["snapshot_id"], snap)

        # Interval exceeding active run scope must fail cleanly.
        bad = self.span_process("--run", run_dir, "compare", "--left", "1-3", "--right", "5-7")
        self.assertNotEqual(bad.returncode, 0)

        # Expand to 7, then run stats-only compare (0 delivered body chars).
        self.span_json("--run", run_dir, "research", "expand", "--as-of", "7")
        stats_cmp = self.span_json("--run", run_dir, "compare", "--left", "1-3", "--right", "5-7",
                                   "--min-support", "5", "--format", "json")
        self.assertEqual(stats_cmp["scope"]["as_of"], 7)
        self.assertNotIn("reading", stats_cmp)

        idx_after_stats = self.span_json("--run", run_dir, "research", "index")
        index_md = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertEqual(idx_after_stats["chapters"], 0)
        self.assertFalse(any(line.startswith("| C") for line in index_md.splitlines()))

        # Next: compare WITH --examples 1 & small budget counts ONLY delivered fragments in 材料索引.md.
        ex_cmp = self.span_json("--run", run_dir, "compare", "--left", "1-3", "--right", "5-7",
                                "--min-support", "5", "--examples", "1", "--budget", "60", "--format", "json")
        self.assertIn("reading", ex_cmp)
        delivered_frags = [frag for group in ex_cmp["reading"]["groups"] for frag in group["fragments"]]
        self.assertGreater(len(delivered_frags), 0)
        self.assertEqual(sum(len(p["text"]) for p in ex_cmp["reading"]["passages"]), ex_cmp["reading"]["read_characters"])

        idx_after_ex = self.span_json("--run", run_dir, "research", "index")
        index_after_md = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        delivered_rows = [line for line in index_after_md.splitlines() if line.startswith("| C")]
        self.assertEqual(idx_after_ex["chapters"], len({frag["read_span"].split(":")[0] for frag in delivered_frags}))
        self.assertEqual(len(delivered_rows), idx_after_ex["chapters"])
        self.assertIn("区间对比例段 1 次", delivered_rows[0])
        self.assertIn(delivered_frags[0]["read_span"], delivered_rows[0])

        # Follow up via read --from-result saved run result + check calls.jsonl protocol fields only.
        calls = [row for row in load_run(run_dir).records() if "command" in row]
        compare_calls = [row for row in calls if row["command"] == "compare"]
        self.assertEqual([row["status"] for row in compare_calls], ["failure", "success", "success"])
        self.assertEqual(compare_calls[1]["origin"], "compare")
        self.assertEqual(compare_calls[2]["origin"], "compare_examples")
        r_ex_path = compare_calls[2]["result_path"]

        reread = self.span_json("--run", run_dir, "read", "--from-result", run_dir / r_ex_path,
                                "--pick", "2", "--group-budget", "1200", "--format", "json")
        self.assertGreater(reread["read_characters"], 0)
        calls_after = [row for row in load_run(run_dir).records() if "command" in row]
        self.assertEqual(calls_after[-1]["origin"], "result_expansion")

    def test_large_corpus_exact_min_len_and_cross_side_extension(self):
        source = self.root / "长文本矿区.txt"
        # Exceed 50,000 Han characters so use_seen_filter activates on min_len=2.
        filler = ("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥。\n" * 60)
        left_parts = [f"第{i}章 左章\n{filler}" for i in range(1, 22)]
        left_parts[0] += "靐龗出现。\n星辰铁刃试锋。\n"
        left_parts[5] += "靐龗再现。\n"
        left_parts[10] += "靐龗三次。\n星辰铁刃收鞘。\n"
        right_parts = [f"第{i}章 右章\n{filler}" for i in range(22, 43)]
        right_suffixes = ["锋", "寒", "亮", "啸", "鸣", "震", "劈", "破", "裂", "断"]
        for idx in range(10):
            right_parts[idx] += f"星辰铁刃{right_suffixes[idx]}芒闪耀。\n"
        source.write_text("".join(left_parts + right_parts), encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, len(chapters)))
        res = compare_intervals(library, (1, 21), (22, 42), min_support=3, min_len=2, max_len=6, limit=20)
        by_term = {row["term"]: row for row in res["candidates"]}
        self.assertIn("靐龗", by_term)
        self.assertEqual(by_term["靐龗"]["left"]["count"], 3)
        self.assertEqual(by_term["靐龗"]["right"]["count"], 0)
        self.assertIn("星辰铁刃", by_term)
        self.assertEqual(by_term["星辰铁刃"]["left"]["count"], 2)
        self.assertEqual(by_term["星辰铁刃"]["right"]["count"], 10)
        self.assertEqual(by_term["星辰铁刃"]["trend"], "up")

    def test_compare_examples_group_labels_and_interval_title_colon(self):
        source, book, snap = self._build_fixture()
        states = self.root / "状态"
        cmp_res = self.span_process(
            "--text", source, "--state-dir", states,
            "compare", "--left", "1-3", "--right", "5-7", "--as-of", "7",
            "--min-support", "5", "--examples", "1", "--budget", "4",
        )
        self.assertEqual(cmp_res.returncode, 0, cmp_res.stderr)
        self.assertRegex(cmp_res.stdout, r"\[组\d+ · 「[^」]+」(?:左|右) · 未完成")
        state_id = re.search(r"read --from-result (state_[0-9a-f]{24})", cmp_res.stdout).group(1)

        continued = self.span_process(
            "--text", source, "--state-dir", states,
            "read", "--from-result", state_id, "--budget", "4000",
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertRegex(continued.stdout, r"\[组\d+ · 「[^」]+」(?:左|右)\]")

        titled = self.root / "冒号章题.txt"
        titled.write_text("第一章 序:上\n前段内容。\n第二章 尾声\n后段内容。\n", encoding="utf-8")
        tsnap, tchapters, _ = read_text_source(titled)
        self.assertEqual(parse_interval_spec("第一章 序:上", tchapters, Scope(tsnap, 2)), (1, 1))

    def _brute_force_greedy_oracle(self, chapters, left_range, right_range, *,
                                   min_support=3, min_chapters=1, min_len=2, max_len=4,
                                   direction="all", limit=20):
        left_chars = right_chars = 0
        raw_occ = defaultdict(lambda: ({}, {}))
        counts = defaultdict(lambda: [0, 0])
        for ch in chapters:
            side = 0 if left_range[0] <= ch.number <= left_range[1] else 1 if right_range[0] <= ch.number <= right_range[1] else None
            if side is None:
                continue
            if side == 0:
                left_chars += len(ch.body)
            else:
                right_chars += len(ch.body)
            for match in HAN_CLAUSE.finditer(ch.body):
                base, text = match.start(), match.group(0)
                n = len(text)
                for length in range(min_len, max_len + 1):
                    for i in range(n - length + 1):
                        gram = text[i:i + length]
                        counts[gram][side] += 1
                        raw_occ[gram][side].setdefault(ch.number, []).append((base + i, base + i + length))
        eligible = {g: (c[0], c[1]) for g, c in counts.items() if max(c) >= min_support}
        promoted = _prune_subsumed_fragments(eligible, min_support)
        pool = {g for g, (lc, rc) in promoted.items() if _g2_contrast(lc, rc, left_chars, right_chars) >= 1.0}
        for term in list(pool):
            for s in range(len(term)):
                for e in range(s + min_len, len(term) + 1):
                    if term[s:e] in promoted:
                        pool.add(term[s:e])

        covered = defaultdict(list)
        remaining = set(pool)
        out = []
        while remaining and len(out) < limit:
            best = None
            for term in sorted(remaining):
                l_occ, r_occ = raw_occ[term]
                l_f = {ch: [sp for sp in sps if not any(sp[0] < ce and sp[1] > cs for cs, ce in covered[ch])]
                       for ch, sps in l_occ.items()}
                l_f = {ch: sps for ch, sps in l_f.items() if sps}
                r_f = {ch: [sp for sp in sps if not any(sp[0] < ce and sp[1] > cs for cs, ce in covered[ch])]
                       for ch, sps in r_occ.items()}
                r_f = {ch: sps for ch, sps in r_f.items() if sps}
                lc = sum(len(sps) for sps in l_f.values())
                rc = sum(len(sps) for sps in r_f.values())
                if not ((lc >= min_support and len(l_f) >= min_chapters) or (rc >= min_support and len(r_f) >= min_chapters)):
                    continue
                score = _g2_contrast(lc, rc, left_chars, right_chars)
                if score < 1.0:
                    continue
                key = (-score, -max(lc, rc), -len(term), term)
                if best is None or key < best[0]:
                    best = (key, term, score, l_f, r_f)
            if best is None:
                break
            _, term, score, l_f, r_f = best
            remaining.remove(term)
            for ch, sps in (*l_f.items(), *r_f.items()):
                covered[ch].extend(sps)
            raw_lc = sum(len(sps) for sps in raw_occ[term][0].values())
            raw_rc = sum(len(sps) for sps in raw_occ[term][1].values())
            if _matches_direction(raw_lc, raw_rc, left_chars, right_chars, direction):
                out.append((term, round(score, 4)))
        return out

    def test_greedy_heap_matches_full_recompute_oracle(self):
        corpora = {
            "suffix": (
                "第一章 左\n" + ("甲把铁剑收好。\n" * 8) + ("路人乙丙闲谈。\n" * 3) + ("背景铺垫废字。\n" * 6),
                "第二章 右\n" + ("剑修玄铁剑出。\n" * 12) + ("路人乙丙闲谈。\n" * 9) + ("背景铺垫废字。\n" * 6),
            ),
            "prefix": (
                "第一章 左\n" + ("甲把铁剑收好。\n" * 8) + ("路人乙丙闲谈。\n" * 3) + ("背景铺垫废字。\n" * 6),
                "第二章 右\n" + ("剑修铁剑玄出。\n" * 12) + ("路人乙丙闲谈。\n" * 9) + ("背景铺垫废字。\n" * 6),
            ),
            # Partial overlap: '甲乙' (right-only, highest G2) partially overlaps '乙戊' inside '甲乙戊'.
            # Masking '甲乙' removes '乙戊' on right, jumping '乙戊' above intermediate '丙丁' (monotone breakdown check).
            "partial_overlap": (
                "第一章 左\n" + ("他取乙戊来用。\n" * 9) + ("此处丙丁独存。\n" * 6) + ("无关填充句子。\n" * 12),
                "第二章 右\n" + ("此地甲乙戊盛。\n" * 12) + ("此地甲乙木盛。\n" * 4) + ("无关填充句子。\n" * 12),
            ),
        }
        for name, (left_text, right_text) in corpora.items():
            path = self.root / f"{name}.txt"
            path.write_text(left_text + right_text, encoding="utf-8")
            snap, chapters, _ = read_text_source(path)
            library = ScopedText(chapters, Scope(snap, 2))
            for direction in ("all", "left", "right"):
                for limit in (1, 2, 3, 20):
                    actual = [
                        (row["term"], row["g2_score"])
                        for row in compare_intervals(
                            library, (1, 1), (2, 2),
                            min_support=3, min_len=2, max_len=4, direction=direction, limit=limit,
                        )["candidates"]
                    ]
                    expected = self._brute_force_greedy_oracle(
                        chapters, (1, 1), (2, 2),
                        min_support=3, min_len=2, max_len=4, direction=direction, limit=limit,
                    )
                    self.assertEqual(actual, expected, f"Mismatch for {name} direction={direction} limit={limit}")

    def test_compare_preserves_valid_edge_particle_words(self):
        # 1) Minimal two-chapter regression for words starting/ending with structural particles (e.g. 地球).
        two_chap = self.root / "山河地球.txt"
        two_chap.write_text("第一章 前段\n" + ("山河。" * 40) + "\n第二章 后段\n" + ("地球。" * 40), encoding="utf-8")
        snap2, chaps2, _ = read_text_source(two_chap)
        terms2 = [row["term"] for row in compare_intervals(ScopedText(chaps2, Scope(snap2, 2)), (1, 1), (2, 2))["candidates"]]
        self.assertEqual(set(terms2), {"山河", "地球"})

        # 2) Valid entities beginning or ending with 的/了/着/得/地/之 must not be dropped.
        source = self.root / "合法边缘字词.txt"
        left = "第一章 旧卷\n" + "".join(
            f"第{n}天队伍安营生火，巡夜清点物资。\n" for n in ("一", "二", "三", "四", "五", "六")
        )
        right_lines = [
            "第二章 新卷\n",
            "基地警报响起，飞船准备着陆，地方指挥所已就位。\n",
            "基地守军确认得到充足补给，飞船顺利着陆。\n",
            "基地前沿终于得到回应，地方代表迎接降落，运输艇再次着陆。\n",
            "新建基地稳定运转，大家得到宝贵休整时间。\n",
            "众人返回基地与地方驻军汇合，确认得到增援并安全着陆。\n",
            "当地地方防线稳固，前哨不再动摇。\n",
            "祭起白银之手，守卫南门。\n",
            "借着白银之手，破开迷雾。\n",
            "依靠白银之手，稳住阵脚。\n",
            "祭出白银之手，护住营地。\n",
            "催动白银之手，汇聚微光。\n",
        ]
        source.write_text(left + "".join(right_lines), encoding="utf-8")
        snap, chapters, _ = read_text_source(source)
        library = ScopedText(chapters, Scope(snap, 2))
        res = compare_intervals(library, (1, 1), (2, 2), min_support=3, min_len=2, max_len=6, limit=20)
        terms = [row["term"] for row in res["candidates"]]
        for valid in ("基地", "着陆", "得到", "地方", "白银之手"):
            self.assertIn(valid, terms)
        self.assertNotIn("白银之", terms)

    def test_base_view_preserves_subsumed_and_equal_rate_terms_with_paging_and_reread(self):
        # Construct equal-length left (2 chapters) and right (2 chapters):
        # - "常见表达" appears 40 times per side (equal rate -> flat, G2 = 0)
        # - "白骑士" 9 left / 18 right, plus 1 standalone "骑士" left / 2 standalone "骑士" right -> "骑士" 10/20
        source = self.root / "基础候选.txt"
        # Make both sides identical in body length (left has 5+4=9 白骑士 + 1 老骑士; right has 18 白骑士 + 2 老骑士 -> 50 char diff).
        # Single-Han filler "填。\n" (3 chars * 16 + "填。\n" -> pad with non-extractable single chars).
        left_1 = "第一章 左一\n" + ("常见表达。\n" * 20) + ("白骑士。\n" * 5) + "老骑士。\n"
        left_2 = "第二章 左二\n" + ("常见表达。\n" * 20) + ("白骑士。\n" * 4) + ("填。\n" * 16) + "。\n"
        right_1 = "第三章 右一\n" + ("常见表达。\n" * 20) + ("白骑士。\n" * 9) + "老骑士。\n"
        right_2 = "第四章 右二\n" + ("常见表达。\n" * 20) + ("白骑士。\n" * 9) + "老骑士。\n"
        source.write_text(left_1 + left_2 + right_1 + right_2, encoding="utf-8")
        book = self.root / "基础书库"
        snap = import_txt(book, source)["snapshot_id"]

        contrast = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--min-support", "3", "--format", "json",
        )
        contrast_terms = [row["term"] for row in contrast["candidates"]]
        self.assertIn("白骑士", contrast_terms)
        self.assertNotIn("骑士", contrast_terms)
        self.assertNotIn("常见表达", contrast_terms)

        base_all = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--sort", "total", "--min-support", "3", "--format", "json",
        )
        db_base = self.span_json(
            "--library", book, "compare", "--snapshot", snap, "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--sort", "total", "--min-support", "3", "--format", "json",
        )
        self.assertEqual(base_all["candidates"], db_base["candidates"])
        self.assertEqual(base_all["mode"], "base_candidates")
        self.assertEqual(base_all["parameters"]["counting_basis"], "raw_interval_stats")

        by_term = {row["term"]: row for row in base_all["candidates"]}
        self.assertIn("常见表达", by_term)
        self.assertEqual(by_term["常见表达"]["trend"], "flat")
        self.assertEqual(by_term["常见表达"]["left"]["count"], 40)
        self.assertEqual(by_term["常见表达"]["right"]["count"], 40)
        self.assertEqual(by_term["常见表达"]["left"]["hit_chapters"], 2)
        self.assertEqual(by_term["常见表达"]["right"]["hit_chapters"], 2)
        self.assertIn("骑士", by_term)
        self.assertEqual(by_term["骑士"]["total_count"], 30)
        self.assertEqual(by_term["骑士"]["left"]["count"], 10)
        self.assertEqual(by_term["骑士"]["right"]["count"], 20)
        self.assertNotIn("selection", by_term["骑士"])
        self.assertNotIn("uncovered_count", by_term["骑士"]["left"])

        # --min-chapters 2 evaluated against raw hit chapters preserves 骑士 (2 left, 2 right) and 白骑士.
        chap2 = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--contains", "骑士", "--min-support", "3", "--min-chapters", "2", "--format", "json",
        )
        self.assertEqual([row["term"] for row in chap2["candidates"]], ["骑士", "白骑士"])
        self.assertEqual(chap2["total_candidates"], 2)
        self.assertIsNone(chap2["next_offset"])

        # Pagination stability + --pick alignment under offset > 0 + reread via --from-result.
        p1 = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--contains", "骑士", "--offset", "0", "--limit", "1", "--format", "json",
        )
        p2_txt = self.root / "base_p2.txt"
        p2_proc = self.span_process(
            "--text", source, "--output", p2_txt, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--contains", "骑士", "--offset", "1", "--limit", "1",
        )
        self.assertEqual(p2_proc.returncode, 0, p2_proc.stderr)
        self.assertEqual(p1["next_offset"], 1)
        self.assertEqual(p1["candidates"][0]["term"], "骑士")
        self.assertIn("候选总数：2", p2_proc.stdout)
        p2_rendered = p2_txt.read_text(encoding="utf-8")
        self.assertIn("[1 · 第2名] 「白骑士」", p2_rendered)

        reread = self.span_json(
            "--text", source, "read", "--from-result", p2_txt, "--pick", "1",
            "--group-budget", "400", "--before", "0", "--after", "0", "--format", "json",
        )
        self.assertEqual(len(reread["groups"]), 2)
        self.assertIn("白骑士", reread["passages"][0]["text"])

        # Out-of-range offset returns empty safely, and --contains/--sort under contrast are rejected.
        empty_page = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--view", "base", "--contains", "骑士", "--offset", "10", "--format", "json",
        )
        self.assertEqual(empty_page["candidates"], [])
        self.assertIsNone(empty_page["next_offset"])

        bad_contains = self.span_process(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4", "--contains", "骑士",
        )
        self.assertNotEqual(bad_contains.returncode, 0)
        self.assertIn("--view base", bad_contains.stderr)
        bad_sort = self.span_process(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4", "--sort", "total",
        )
        self.assertNotEqual(bad_sort.returncode, 0)
        self.assertIn("--view base", bad_sort.stderr)

    def test_contrast_pool_decoupled_from_limit_and_supports_offset(self):
        # Generate > 500 distinct non-overlapping 2-Han words occurring 4x on right only.
        alphabet = "一乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥甲木火土金水风雷云电霜雪山河"
        pairs = [f"{a}{b}" for idx, a in enumerate(alphabet) for b in alphabet[idx + 1:]][:560]
        left_body = "甲。\n" * 3000
        right_body = "".join(f"{word}。\n" * 4 for word in pairs)
        source = self.root / "池解耦.txt"
        source.write_text(f"第一章 左\n{left_body}第二章 右\n{right_body}", encoding="utf-8")

        res20 = self.span_json(
            "--text", source, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--limit", "20", "--format", "json",
        )
        res40 = self.span_json(
            "--text", source, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--limit", "40", "--format", "json",
        )
        res60 = self.span_json(
            "--text", source, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--limit", "60", "--format", "json",
        )
        page2 = self.span_json(
            "--text", source, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--offset", "20", "--limit", "20", "--format", "json",
        )

        self.assertEqual(res20["parameters"]["pool_limit"], CONTRAST_POOL_LIMIT)
        self.assertTrue(res20["pool_truncated"])
        self.assertGreaterEqual(res20["scored_pool_count"], 560)
        self.assertEqual(res20["next_offset"], 20)

        self.assertEqual([r["term"] for r in res20["candidates"]], [r["term"] for r in res60["candidates"][:20]])
        self.assertEqual([r["term"] for r in page2["candidates"]], [r["term"] for r in res40["candidates"][20:40]])
        self.assertEqual([r["rank"] for r in page2["candidates"]], list(range(21, 41)))

        text_out = self.span_process(
            "--text", source, "compare", "--left", "1", "--right", "2", "--as-of", "2", "--min-support", "3",
        )
        self.assertEqual(text_out.returncode, 0, text_out.stderr)
        self.assertIn("池截断：", text_out.stdout)
        self.assertIn(f"→前{CONTRAST_POOL_LIMIT}", text_out.stdout)
        self.assertIn("下一页：--offset 20", text_out.stdout)

    def test_explain_reports_extract_support_subsumption_chapters_direction_and_paging(self):
        source = self.root / "复核阶段.txt"
        left_1 = (
            "第一章 左一\n"
            + ("常见表达。\n" * 20)
            + ("白骑士。\n" * 5) + "老骑士。\n"
            + ("旧城门。\n" * 10)
            + ("铜枪。\n" * 3)
            + ("单章词。\n" * 8)
            + ("填。\n" * 30)
        )
        left_2 = (
            "第二章 左二\n"
            + ("常见表达。\n" * 20)
            + ("白骑士。\n" * 4)
            + ("旧城门。\n" * 10)
            + ("单。\n" * 20)
        )
        right_1 = (
            "第三章 右一\n"
            + ("常见表达。\n" * 20)
            + ("白骑士。\n" * 9) + "老骑士。\n"
            + ("青铜枪。\n" * 5)
        )
        right_2 = (
            "第四章 右二\n"
            + ("常见表达。\n" * 20)
            + ("白骑士。\n" * 9) + "老骑士。\n"
            + ("青铜枪。\n" * 5)
            + ("微弱词。\n" * 2)
        )
        left_body = left_1 + left_2
        right_body = right_1 + right_2
        l_content_len = len(left_body) - len("第一章 左一\n第二章 左二\n")
        r_content_len = len(right_body) - len("第三章 右一\n第四章 右二\n")
        if l_content_len > r_content_len:
            right_body += "。\n" * ((l_content_len - r_content_len) // 2) + ("。" * ((l_content_len - r_content_len) % 2))
        elif r_content_len > l_content_len:
            left_2 += "。\n" * ((r_content_len - l_content_len) // 2) + ("。" * ((r_content_len - l_content_len) % 2))
            left_body = left_1 + left_2
        source.write_text(left_body + right_body, encoding="utf-8")

        res = self.span_json(
            "--text", source, "compare", "--left", "1-2", "--right", "3-4", "--as-of", "4",
            "--min-support", "5", "--min-chapters", "2", "--direction", "right", "--limit", "1",
            "--explain", "AWSL", "--explain", "233", "--explain", "单",
            "--explain", "星河", "--explain", "微弱词",
            "--explain", "骑士", "--explain", "常见表达", "--explain", "单章词",
            "--explain", "铜枪", "--explain", "旧城门", "--explain", "白骑士",
            "--format", "json",
        )
        by_term = {row["term"]: row for row in res["explanations"]}

        self.assertEqual((by_term["AWSL"]["stage"], by_term["AWSL"]["reason"]), ("extract", "not_extracted"))
        self.assertEqual((by_term["233"]["stage"], by_term["233"]["reason"]), ("extract", "not_extracted"))
        self.assertEqual((by_term["单"]["stage"], by_term["单"]["reason"]), ("extract", "not_extracted"))

        self.assertEqual((by_term["星河"]["stage"], by_term["星河"]["reason"]), ("support", "below_min_support"))
        self.assertIn("零出现", by_term["星河"]["note"])
        self.assertEqual(by_term["微弱词"]["reason"], "below_min_support")
        self.assertEqual(by_term["微弱词"]["raw"]["right_count"], 2)

        self.assertEqual((by_term["骑士"]["stage"], by_term["骑士"]["reason"]), ("subsumption", "subsumed_by_superstring"))
        self.assertEqual(by_term["骑士"]["representative_term"], "白骑士")
        self.assertTrue(by_term["骑士"]["representative_read_spans"])

        self.assertEqual((by_term["常见表达"]["stage"], by_term["常见表达"]["reason"]), ("contrast_score", "below_min_g2"))
        self.assertEqual((by_term["单章词"]["stage"], by_term["单章词"]["reason"]), ("chapters", "below_min_chapters"))

        self.assertEqual((by_term["铜枪"]["stage"], by_term["铜枪"]["reason"]), ("greedy_mask", "masked_below_threshold"))
        self.assertIn("青铜枪", by_term["铜枪"]["masked_by"])
        self.assertTrue(by_term["铜枪"]["masked_by_read_spans"])

        self.assertEqual((by_term["旧城门"]["stage"], by_term["旧城门"]["reason"]), ("direction", "direction_filtered"))
        self.assertEqual(by_term["白骑士"]["reason"], "beyond_page_limit")

    def test_explain_reports_masked_below_min_g2_and_before_offset(self):
        mask_txt = self.root / "掩蔽低G2.txt"
        l_body = "第一章 左\n" + ("铁剑。\n" * 3) + ("甲。\n" * 60)
        r_body = "第二章 右\n" + ("玄铁剑。\n" * 12) + ("铁剑。\n" * 3) + ("乙。\n" * 9)
        l_len = len(l_body) - len("第一章 左\n")
        r_len = len(r_body) - len("第二章 右\n")
        if r_len > l_len:
            l_body += "甲。\n" * ((r_len - l_len) // 3)
        elif l_len > r_len:
            r_body += "丙。\n" * ((l_len - r_len) // 3)
        mask_txt.write_text(l_body + r_body, encoding="utf-8")

        g2_drop = self.span_json(
            "--text", mask_txt, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--offset", "0", "--limit", "1",
            "--explain", "玄铁剑", "--explain", "铁剑", "--format", "json",
        )
        g2_map = {row["term"]: row for row in g2_drop["explanations"]}
        self.assertEqual(g2_map["玄铁剑"]["reason"], "selected")
        self.assertEqual(g2_map["玄铁剑"]["candidate_rank"], 1)
        self.assertEqual(g2_map["铁剑"]["reason"], "masked_below_min_g2")
        self.assertIn("玄铁剑", g2_map["铁剑"]["masked_by"])

        p2_explain = self.span_json(
            "--text", mask_txt, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--offset", "1", "--limit", "1",
            "--explain", "玄铁剑", "--format", "json",
        )
        self.assertEqual(p2_explain["explanations"][0]["reason"], "before_current_offset")

    def test_explain_reports_pool_truncated_and_boundary_guards(self):
        alphabet = "一乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥甲木火土金水风雷云电霜雪山河"
        pairs = [f"{a}{b}" for idx, a in enumerate(alphabet) for b in alphabet[idx + 1:]][:560]
        pool_src = self.root / "池复核.txt"
        left_fill = "甲。\n" * 3000
        # Put 550 words at 5x and one word at 3x so the 3x word lands below rank 500.
        right_pool = "".join(f"{w}。\n" * 5 for w in pairs[:550]) + (f"{pairs[550]}。\n" * 3)
        pool_src.write_text(f"第一章 左\n{left_fill}第二章 右\n{right_pool}", encoding="utf-8")

        trunc_txt = self.span_process(
            "--text", pool_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--explain", pairs[550],
        )
        self.assertEqual(trunc_txt.returncode, 0, trunc_txt.stderr)
        self.assertIn("筛选复核（1 项）", trunc_txt.stdout)
        self.assertIn("pool_truncated", trunc_txt.stdout)
        trunc_json = self.span_json(
            "--text", pool_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-support", "3", "--explain", pairs[550], "--format", "json",
        )
        self.assertEqual(trunc_json["explanations"][0]["reason"], "pool_truncated")
        self.assertGreater(trunc_json["explanations"][0]["scored_pool_rank"], CONTRAST_POOL_LIMIT)

        bad_view = self.span_process(
            "--text", pool_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--view", "base", "--explain", pairs[0],
        )
        self.assertNotEqual(bad_view.returncode, 0)
        self.assertIn("--explain", bad_view.stderr)

        too_many = []
        for idx in range(MAX_EXPLAIN_TERMS + 1):
            too_many.extend(("--explain", f"词{idx}"))
        bad_count = self.span_process(
            "--text", pool_src, "compare", "--left", "1", "--right", "2", "--as-of", "2", *too_many,
        )
        self.assertNotEqual(bad_count.returncode, 0)
        self.assertIn("1 到 16", bad_count.stderr)

    def test_explain_g2_threshold_neighborhood_and_candidate_invariance(self):
        # Slightly below 1.0 (0.9999938...) vs slightly above 1.0 via real CLI, plus exact 1.0 boundary.
        below_src = self.root / "门槛略低.txt"
        below_src.write_text(
            "第一章 左\n" + ("甲乙。" * 16).ljust(388, ".") + "\n"
            + "第二章 右\n" + ("甲乙。" * 22).ljust(388, ".") + "\n",
            encoding="utf-8",
        )
        self.assertLess(_g2_contrast(16, 22, 389, 389), 1.0)
        base_args = ["--text", below_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
                     "--min-len", "2", "--max-len", "2", "--format", "json"]
        reg_below = self.span_json(*base_args)
        exp_below = self.span_json(*base_args, "--explain", "甲乙")
        self.assertEqual(reg_below["candidates"], [])
        self.assertEqual(exp_below["candidates"], reg_below["candidates"])
        self.assertEqual(exp_below["explanations"][0]["reason"], "below_min_g2")
        self.assertNotIn("1.00 < 1.00", exp_below["explanations"][0]["note"])
        self.assertIn("0.999994 < 1", exp_below["explanations"][0]["note"])
        exp_below_text = self.span_process(
            "--text", below_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-len", "2", "--max-len", "2", "--explain", "甲乙", "--format", "text",
        )
        self.assertEqual(exp_below_text.returncode, 0, exp_below_text.stderr)
        self.assertIn("0.999994 < 1", exp_below_text.stdout)
        self.assertNotIn("G2 1.00", exp_below_text.stdout)

        above_src = self.root / "门槛略高.txt"
        above_src.write_text(
            "第一章 左\n" + ("甲乙。" * 16).ljust(388, ".") + "\n"
            + "第二章 右\n" + ("甲乙。" * 23).ljust(388, ".") + "\n",
            encoding="utf-8",
        )
        self.assertGreater(_g2_contrast(16, 23, 389, 389), 1.0)
        reg_above = self.span_json(
            "--text", above_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-len", "2", "--max-len", "2", "--format", "json",
        )
        exp_above = self.span_json(
            "--text", above_src, "compare", "--left", "1", "--right", "2", "--as-of", "2",
            "--min-len", "2", "--max-len", "2", "--explain", "甲乙", "--format", "json",
        )
        self.assertEqual(len(reg_above["candidates"]), 1)
        self.assertEqual(exp_above["candidates"], reg_above["candidates"])
        self.assertEqual(exp_above["explanations"][0]["reason"], "selected")

        snap, chapters, _, details = read_text_source_details(below_src)
        lib = ScopedText(chapters, Scope(snap, 2), details)
        for score_val, expected_reason in ((1.0 - 1e-12, "below_min_g2"), (1.0, "selected"), (1.0 + 1e-12, "selected")):
            with mock.patch("chapspan.compare._g2_contrast", return_value=score_val):
                plain = compare_intervals(lib, (1, 1), (2, 2), min_len=2, max_len=2)
                explained = compare_intervals(lib, (1, 1), (2, 2), min_len=2, max_len=2, explain=["甲乙"])
            self.assertEqual(explained["candidates"], plain["candidates"])
            self.assertEqual(explained["explanations"][0]["reason"], expected_reason)
            if expected_reason == "below_min_g2":
                self.assertEqual(explained["candidates"], [])
                self.assertIn("略低于门槛 1", explained["explanations"][0]["note"])
            else:
                self.assertEqual(len(explained["candidates"]), 1)

    def test_explain_consistency_across_base_candidates(self):
        corpora = [
            (
                "合成甲.txt",
                "第一章 左\n" + ("常见词。\n" * 16) + ("白骑士。\n" * 4) + ("青铜枪。\n" * 3) + ("甲乙。" * 16).ljust(120, ".") + "\n",
                "第二章 右\n" + ("常见词。\n" * 16) + ("白骑士。\n" * 12) + ("老骑士。\n" * 3) + ("铜枪。\n" * 4) + ("甲乙。" * 22).ljust(120, ".") + "\n",
                {"min_support": 3, "limit": 3, "offset": 0},
            ),
            (
                "合成乙.txt",
                "第一章 左一\n" + ("星港。\n" * 10) + ("旧门。\n" * 6) + ("木窗。\n" * 5) + "\n"
                + "第二章 左二\n" + ("星港。\n" * 8) + ("旧门。\n" * 5) + ("青瓦。\n" * 4) + "\n",
                "第三章 右一\n" + ("星舰母舰。\n" * 9) + ("铁门。\n" * 6) + ("木窗。\n" * 5) + "\n"
                + "第四章 右二\n" + ("星舰母舰。\n" * 9) + ("铁门。\n" * 5) + ("青瓦。\n" * 4) + "\n",
                {"min_support": 3, "limit": 2, "offset": 1},
            ),
        ]
        for filename, left_text, right_text, opts in corpora:
            path = self.root / filename
            path.write_text(left_text + right_text, encoding="utf-8")
            snap, chapters, _, details = read_text_source_details(path)
            total_chaps = len(chapters)
            mid = total_chaps // 2
            lib = ScopedText(chapters, Scope(snap, total_chaps), details)

            base_res = compare_intervals(
                lib, (1, mid), (mid + 1, total_chaps),
                min_support=opts["min_support"], view="base", limit=100,
            )
            base_terms = [row["term"] for row in base_res["candidates"]]
            self.assertGreater(len(base_terms), 0)

            ref_contrast = compare_intervals(
                lib, (1, mid), (mid + 1, total_chaps),
                min_support=opts["min_support"], limit=opts["limit"], offset=opts["offset"],
            )
            page_ranks = {row["term"]: row["rank"] for row in ref_contrast["candidates"]}

            probe_terms = [*base_terms, "X9", "零无词"]
            observed_selected = {}
            for i in range(0, len(probe_terms), MAX_EXPLAIN_TERMS):
                batch = probe_terms[i:i + MAX_EXPLAIN_TERMS]
                explained = compare_intervals(
                    lib, (1, mid), (mid + 1, total_chaps),
                    min_support=opts["min_support"], limit=opts["limit"], offset=opts["offset"],
                    explain=batch,
                )
                self.assertEqual(explained["candidates"], ref_contrast["candidates"])
                for entry in explained["explanations"]:
                    self.assertNotIn("1.00 < 1.00", entry["note"])
                    if entry["reason"] == "selected":
                        observed_selected[entry["term"]] = entry["candidate_rank"]
            self.assertEqual(observed_selected, page_ranks)


if __name__ == "__main__":
    unittest.main()
