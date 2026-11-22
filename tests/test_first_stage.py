import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chapspan.cli import build_parser, run
from chapspan.display import format_text, output_receipt
from chapspan.documents import citation_occurrences, draft_header, read_draft
from chapspan.handoff import handoff_token, load_result, save_state, save_text_result, write_state_file
from chapspan.library import import_txt, list_snapshots, open_scope, paragraph_ends
from chapspan.model import Chapter, Scope, Span
from chapspan.reading import read_passages
from chapspan.runs import RunRecorder, load_run, run_write_lock
from chapspan.search import find_many, find_passages, minimal_windows
from chapspan.service import init_research_run


class FirstStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.book = self.root / "测试书库"
        self.source = self.root / "样本.txt"
        self.source.write_bytes((
            "\ufeff第一章 开门\r\n甲😀乙。\r\n\r\n哈哈哈。\r\n灯亮了。\r\n"
            "坏灯熄了。\r\n第二章 后续\r\n秘密灯亮了。\r\n"
            "作者有话说\r\n外围材料\r\n"
        ).encode("utf-8"))
        self.original = import_txt(self.book, self.source)
        self.snapshot = self.original["snapshot_id"]

    def save_text(self, name, result):
        path = self.root / name
        state = save_state(result, self.root / "状态")
        path.write_text(save_text_result(result, format_text(result, saved=True), state.state_id), encoding="utf-8")
        write_state_file(path.with_name(path.name + ".state.json"), state.raw)
        return state

    def span_process(self, *args, cwd=None, environ=None):
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                       "PYTHONIOENCODING": "utf-8"}
        if environ:
            environment.update(environ)
        return subprocess.run([sys.executable, "-B", "-m", "chapspan.cli", *map(str, args)],
                              cwd=cwd or self.root, env=environment, capture_output=True,
                              encoding="utf-8", check=False)

    def span_json(self, *args, cwd=None, environ=None):
        result = self.span_process(*args, cwd=cwd, environ=environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def init_run(self, name="任务", as_of="1"):
        result = self.span_json("--text", self.source, "research", "init", self.root / name, "--as-of", as_of)
        return Path(result["run_directory"]), result

    def test_import_rebuild_and_preserve_old_coordinates(self):
        self.assertEqual((self.book / self.original["source"]).read_bytes(), self.source.read_bytes())
        relocated = self.root / "副本.txt"
        relocated.write_bytes(self.source.read_bytes())
        repeated = import_txt(self.book, relocated)
        self.assertEqual(self.snapshot, repeated["snapshot_id"])
        self.assertFalse(repeated["created"])
        rebuilt = import_txt(self.root / "重建", self.source)
        self.assertEqual(self.snapshot, rebuilt["snapshot_id"])
        other_layout = import_txt(self.book, self.source, paragraphs="blank-lines")
        self.assertNotEqual(self.snapshot, other_layout["snapshot_id"])

        self.source.write_text("第一章 开门\n甲新乙。\n", encoding="utf-8")
        self.assertNotEqual(self.snapshot, import_txt(self.book, self.source)["snapshot_id"])
        with open_scope(self.book, Scope(self.snapshot, 1)) as library:
            match = find_passages(library, all_terms=["乙"], examples=1)
            self.assertEqual(match["matches"][0]["span"], "C0001:2-3")
            fragment = match["examples"]["fragments"][0]
            self.assertEqual(fragment["match_spans"], ["C0001:2-3"])
            self.assertEqual(fragment["citation"], "[@C0001:0-6]")
            self.assertEqual(library.chapter(1).body[:6], "甲😀乙。\n\n")

    def test_custom_heading_preserves_unclassified_prefix_and_original_lines(self):
        self.source.write_text("题记\n\n[一]\n初行\n折行\n\n下一段\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法识别分章"):
            import_txt(self.book, self.source)
        result = import_txt(self.book, self.source, chapter_pattern=r"\[一\]", paragraphs="blank-lines")
        self.assertEqual(result["unclassified_chapters"], 1)
        with open_scope(self.book, Scope(result["snapshot_id"], 2)) as library:
            self.assertEqual(library.chapter(1).body, "题记\n\n")
            self.assertEqual(library.chapter(2).source_body_line, 4)
            page = read_passages(library, paragraphs=[(2, 1, 1)])
            self.assertEqual(page["fragments"][0]["text"], "初行\n折行\n\n")

    def test_snapshot_listing_pages_versions_without_chapter_data(self):
        second = import_txt(self.book, self.source, paragraphs="blank-lines")
        before = (self.book / "workbench.db").read_bytes()
        first_page = list_snapshots(self.book, limit=1)
        next_page = list_snapshots(self.book, limit=1, after_snapshot=first_page["next_after_snapshot"])
        versions = first_page["snapshots"] + next_page["snapshots"]
        self.assertEqual([row["snapshot_id"] for row in versions], sorted([self.snapshot, second["snapshot_id"]]))
        self.assertIsNone(next_page["next_after_snapshot"])
        self.assertEqual({row["source_name"] for row in versions}, {self.source.name})
        self.assertEqual({row["source_sha256"] for row in versions}, {self.original["source_sha256"]})
        self.assertEqual({row["parsing"]["paragraphs"] for row in versions}, {"lines", "blank-lines"})
        self.assertEqual(set(versions[0]), {"snapshot_id", "source_name", "source_sha256", "encoding", "parsing"})
        self.assertEqual((self.book / "workbench.db").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "import-txt"):
            list_snapshots(self.root / "未知书库")

    def test_find_counts_pages_and_bounds(self):
        with open_scope(self.book, Scope(self.snapshot, 1)) as library:
            literal = find_passages(library, all_terms=["哈哈"])
            self.assertEqual(literal["match_count"], 1)
            self.assertEqual(literal["matches"][0]["span"], "C0001:6-8")
            empty = find_passages(library, all_terms=["秘密", "灯"])
            self.assertEqual(empty["match_count"], 0)
            self.assertEqual(empty["term_counts"], [{"term": "秘密", "count": 0}, {"term": "灯", "count": 2}])
            cross = find_passages(library, all_terms=["哈哈"], any_terms=["亮", "熄"], exclude=["坏"], near=2)
            self.assertEqual([m["span"] for m in cross["matches"]], ["C0001:6-16"])
            strict = find_passages(library, all_terms=["哈哈", "亮"], near=1)
            self.assertEqual(strict["match_count"], 0)
            by_chapter = find_passages(library, all_terms=["哈哈", "亮"], exclude=["坏"])
            self.assertEqual(by_chapter["match_count"], 0)
            by_chapter = find_passages(library, all_terms=["哈哈", "亮"], any_terms=["灯", "风"])
            self.assertEqual(by_chapter["match_count"], 1)
            self.assertEqual(by_chapter["matches"][0]["span"], "C0001:0-22")
            page = find_passages(library, all_terms=["灯"], limit=1)
            next_page = find_passages(library, all_terms=["灯"], limit=1, offset=page["next_offset"])
            self.assertNotEqual(page["matches"], next_page["matches"])
            self.assertEqual(next_page["match_count"], 2)
            with self.assertRaisesRegex(ValueError, "不可读取"):
                read_passages(library, [Span(2, 0, 1)])
        with open_scope(self.book, Scope(self.snapshot, 3)) as library:
            result = find_passages(library, all_terms=["灯"], limit=1, distribution_offset=1)
            self.assertEqual(result["distribution"][0]["chapter"], 2)
            self.assertIsNone(result["next_distribution_offset"])
            self.assertEqual(result["match_count"], 3)
            with self.assertRaisesRegex(ValueError, "不可读取"):
                library.chapter(3)
        with open_scope(self.book, Scope(self.snapshot, 3, 2, True)) as library:
            result = find_passages(library, all_terms=["外围"])
            self.assertEqual(result["match_count"], 1)
            self.assertEqual([row["chapter"] for row in result["distribution"]], [2, 3])

    def test_hit_chapter_summary_preserves_units_pages_and_compact_results(self):
        self.source.write_text(
            "第一章 起\n灯。\n第二章 无\n雨与秘密。\n第三章 中\n灯灯与雨。\n"
            "第四章 合\n灯与雨。\n第五章 末\n灯。\n", encoding="utf-8")
        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        with open_scope(self.book, Scope(snapshot, 5)) as library:
            literal = find_passages(library, all_terms=["灯"], limit=1)
            self.assertEqual(literal["match_count"], 5)
            self.assertEqual(literal["hit_chapter_count"], 4)
            self.assertEqual((literal["first_hit_chapter"], literal["last_hit_chapter"]), (1, 5))
            self.assertEqual(literal["hit_chapter_ranges"], [
                {"first_chapter": 1, "last_chapter": 1}, {"first_chapter": 3, "last_chapter": 5},
            ])
            following = find_passages(library, all_terms=["灯"], limit=1, offset=literal["next_offset"])
            compact = find_passages(library, all_terms=["灯"], limit=1, compact=True)
            for result in (following, compact):
                self.assertEqual(result["hit_chapter_count"], 4)
                self.assertEqual(result["hit_chapter_ranges"], literal["hit_chapter_ranges"])
            text = format_text({"scope": vars(library.scope), **literal})
            self.assertIn('"灯" 5 次 / 4 章（C0001 · C0003–C0005） · 范围 C0001–C0005', text)
            self.assertNotIn("区段（", text)

            chapter = find_passages(library, all_terms=["灯", "雨"])
            near = find_passages(library, all_terms=["灯", "雨"], near=1)
            self.assertEqual((chapter["match_count"], chapter["hit_chapter_count"]), (2, 2))
            self.assertEqual((near["match_count"], near["hit_chapter_count"]), (2, 2))
            self.assertEqual((chapter["first_hit_chapter"], chapter["last_hit_chapter"]), (3, 4))
            empty = find_passages(library, all_terms=["灯", "秘密"])
            self.assertEqual(empty["hit_chapter_count"], 0)
            self.assertIsNone(empty["first_hit_chapter"])
            self.assertIsNone(empty["last_hit_chapter"])
            self.assertEqual(empty["hit_chapter_ranges"], [])
            text = format_text({"scope": vars(library.scope), **empty})
            self.assertIn("同章内没有符合条件的匹配", text)
            self.assertIn('各词出现："灯" 5 次、"秘密" 1 次', text)

        self.source.write_text("".join(
            f"第{number}章 分布\n" + ("灯。\n" if number % 2 else "无。\n")
            for number in range(1, 16)
        ), encoding="utf-8")
        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        with open_scope(self.book, Scope(snapshot, 15)) as library:
            scattered = find_passages(library, all_terms=["灯"])
            self.assertEqual(scattered["hit_chapter_range_count"], 8)
            self.assertEqual(len(scattered["hit_chapter_ranges"]), 6)
            self.assertEqual(scattered["hit_chapter_ranges_omitted"], 2)
            self.assertIn("C0005 · … · C0011", format_text({"scope": vars(library.scope), **scattered}))

    def test_batch_read_merge_and_resume_exactly_once(self):
        spans = [Span(1, 0, 9), Span(1, 6, 15), Span(2, 0, 3)]
        with open_scope(self.book, Scope(self.snapshot, 2)) as library:
            unpaged = read_passages(library, spans, match_spans=spans[:2])
            self.assertEqual(len(unpaged["fragments"]), 2)
            self.assertEqual(unpaged["fragments"][0]["match_spans"], [s.coordinate for s in spans[:2]])
            for budget in (1, 4, 6, 10):
                cursor, actual = None, []
                while True:
                    page = read_passages(library, spans, match_spans=spans[:2], budget=budget, resume=cursor)
                    self.assertLessEqual(page["read_characters"], budget)
                    self.assertGreater(page["read_characters"], 0)
                    actual.extend(f["text"] for f in page["fragments"])
                    if page["complete"]:
                        break
                    chapter, position = page["next"][1:].split(":")
                    next_cursor = int(chapter), int(position)
                    self.assertTrue(cursor is None or cursor < next_cursor)
                    cursor = next_cursor
                self.assertEqual("".join(actual), "".join(f["text"] for f in unpaged["fragments"]))
            expanded = read_passages(library, [Span(1, 6, 7)], before=1, after=1)
            self.assertEqual(expanded["fragments"][0]["read_span"], "C0001:0-16")

    def test_compact_chapter_matches_supply_anchors_even_when_full_positions_fill(self):
        source = self.root / "词面靠后.txt"
        source.write_text("第一章 门口\n" + "开" * 50 + "\n" + "铃" * 26 + "。\n😀钥。\n第二章 后续\n铃钥。\n",
                          encoding="utf-8")
        snapshot = import_txt(self.book, source)["snapshot_id"]
        with open_scope(self.book, Scope(snapshot, 1)) as library:
            args = {"all_terms": ["铃"], "any_terms": ["钥", "未见"], "examples": 1, "budget": 40}
            full = find_passages(library, **args)
            compact = find_passages(library, compact=True, **args)
            self.assertEqual(compact["match_count"], full["match_count"])
            self.assertEqual(compact["term_counts"], full["term_counts"])
            self.assertNotIn("distribution", compact)
            match, = compact["matches"]
            self.assertEqual(match["span"], full["matches"][0]["span"])
            self.assertEqual({p["term_index"] for p in full["matches"][0]["positions"]}, {0})
            anchors = [Span.parse(value) for value in match["read_spans"]]
            self.assertEqual([f["text"] for f in read_passages(library, anchors)["fragments"]], ["铃", "钥"])
            self.assertEqual(compact["examples"]["selected_spans"], [anchors[0].coordinate])
            self.assertIn("铃", compact["examples"]["fragments"][0]["text"])
            self.assertEqual(full["examples"], compact["examples"])

            literal = find_passages(library, all_terms=["铃"], compact=True, limit=1)
            self.assertEqual(literal["next_offset"], 1)
            next_page = find_passages(library, all_terms=["铃"], compact=True, limit=1, offset=literal["next_offset"])
            self.assertNotEqual(next_page["matches"], literal["matches"])
            near, = find_passages(library, all_terms=["铃", "钥"], near=2, compact=True)["matches"]
            self.assertEqual(near["read_spans"], [near["span"]])
            empty = find_passages(library, all_terms=["未见"], compact=True, examples=1)
            self.assertEqual(empty["term_counts"], [{"term": "未见", "count": 0}])
            self.assertEqual(empty["examples"]["fragments"], [])

    def test_multi_query_scan_matches_independent_queries(self):
        self.source.write_text(
            "第一章 起\n灯与雨。\n铜钥匙出现。\n"
            "第二章 桥\n灯。\n青铜匙出现。\n"
            "第三章 转\n灯与雨。\n铜钥匙回来。\n"
            "第四章 合\n灯。\n青铜匙留下。\n",
            encoding="utf-8",
        )
        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        groups = [
            {"all": ["灯"]},
            {"any": ["铜钥匙", "青铜匙"], "exclude": ["雨"]},
            {"all": ["灯", "雨"], "near": 1, "limit": 1},
        ]
        with open_scope(self.book, Scope(snapshot, 4)) as library:
            class CountingLibrary:
                def __init__(self, wrapped):
                    self.wrapped = wrapped
                    self.calls = 0

                def chapters(self, **kwargs):
                    self.calls += 1
                    yield from self.wrapped.chapters(**kwargs)

                def __getattr__(self, name):
                    return getattr(self.wrapped, name)

            counted = CountingLibrary(library)
            combined = find_many(counted, groups, examples=2, budget=100)
            self.assertEqual(counted.calls, 1)
            self.assertLessEqual(combined["reading"]["read_characters"], 100)
            for index, (group, actual) in enumerate(zip(groups, combined["queries"]), 1):
                expected = find_passages(library, all_terms=group.get("all", ()), any_terms=group.get("any", ()),
                                         exclude=group.get("exclude", ()), near=group.get("near"),
                                         limit=group.get("limit", 20))
                self.assertEqual(actual["query"], index)
                self.assertEqual({key: value for key, value in actual.items() if key not in ("query", "examples")},
                                 expected)
                selected = find_passages(library, all_terms=group.get("all", ()), any_terms=group.get("any", ()),
                                         exclude=group.get("exclude", ()), near=group.get("near"),
                                         limit=group.get("limit", 20), examples=2)["examples"]["selected_spans"]
                self.assertEqual(actual["examples"]["selected_spans"], selected)
            references = [row["reference"] for row in combined["reading"]["example_refs"]]
            self.assertIn("Q1E1", references)
            self.assertIn("Q2E1", references)
            self.assertTrue(all(row["query"] == 2 for row in combined["queries"][1]["examples"]["delivery"]))

            spread = find_many(library, groups[:2], examples=2, budget=100, spread=True)
            self.assertEqual([row["reference"] for row in spread["reading"]["group_refs"]],
                             ["Q1E1", "Q1E2", "Q2E1", "Q2E2"])
            self.assertLessEqual(spread["reading"]["read_characters"], 100)

    def test_multi_query_examples_and_shared_continuation_survive_handoff(self):
        self.source.write_text("".join(
            f"第{number}章 场景\n前段{number}。\n灯与钥匙{number}同时出现。\n后段{number}。\n"
            for number in range(1, 4)
        ), encoding="utf-8")
        parser = build_parser()
        states = self.root / "多组状态"

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), "--state-dir", str(states), *args]))

        encoded = [json.dumps(group, ensure_ascii=False) for group in
                   ({"all": ["灯"], "limit": 1}, {"all": ["钥匙"]})]
        first_page = call("find", "--query", encoded[0], "--query", encoded[1],
                          "--as-of", "all", "--examples", "0")
        self.assertEqual(first_page["queries"][0]["next_offset"], 1)
        second_page = call("find", "--query", json.dumps({"all": ["灯"], "limit": 1, "offset": 1}),
                           "--query", encoded[1], "--as-of", "all", "--examples", "0")
        self.assertNotEqual(first_page["queries"][0]["matches"], second_page["queries"][0]["matches"])
        self.assertEqual(first_page["queries"][1]["matches"], second_page["queries"][1]["matches"])

        found = call("find", "--query", json.dumps({"all": ["灯"], "limit": 2}),
                     "--query", encoded[1], "--as-of", "all", "--examples", "2", "--budget", "6")
        self.assertEqual(found["reading"]["read_characters"], 6)
        self.assertEqual(sum(row["read_characters"] for row in found["reading"]["delivery"]), 12)
        text = format_text(found, reference="state_test")
        self.assertIn("独立查询 2 组 · 范围", text)
        self.assertIn("[Q1E1 · 未完成 6/11 字]", text)
        self.assertIn("[Q2E1 · 未完成 6/11 字]", text)
        self.assertIn("未开始：Q1E2", text)
        self.assertIn("Q2E2", text)
        self.assertIn("续全部：read --from-result state_test", text)
        self.assertIn("--continue-group Q1", text)
        receipt = output_receipt(found, "多组.txt")
        self.assertEqual(receipt["query_groups"], 2)
        self.assertNotIn("match_count", receipt)
        self.assertEqual([(row["query"], row["mode"], row["match_count"], row["hit_chapter_count"])
                          for row in receipt["query_matches"]], [(1, "literal", 3, 3), (2, "literal", 3, 3)])
        self.assertEqual(receipt["examples"], 4)
        self.assertEqual(receipt["next_action"], "read --from-result 多组.txt")

        stored = save_state(found, states)
        expanded = call("read", "--from-result", stored.state_id, "--example", "Q1E2", "--before", "1")
        self.assertEqual(expanded["fragments"][0]["match_spans"], [found["queries"][0]["examples"]["selected_spans"][1]])
        with self.assertRaisesRegex(ValueError, "Q查询号E例段号"):
            call("read", "--from-result", stored.state_id, "--example", "2")
        with self.assertRaisesRegex(ValueError, "多组查询"):
            call("read", "--from-result", stored.state_id, "--pick", "1")
        with self.assertRaisesRegex(ValueError, "不可读取"):
            call("read", "--from-result", stored.state_id, "--example", "Q1E2", "--as-of", "1")

        continued = call("read", "--from-result", stored.state_id, "--continue-group", "Q1", "--budget", "100")
        self.assertEqual(continued["group_refs"], found["reading"]["group_refs"])
        self.assertEqual(continued["example_refs"], found["reading"]["example_refs"])
        self.assertIn("本轮未续：Q2", format_text(continued, reference="state_next"))
        continued_state = save_state(continued, states)
        other_example = call("read", "--from-result", continued_state.state_id, "--example", "Q2E1")
        self.assertIn("钥匙1", other_example["fragments"][0]["text"])

        self.save_text("多组.txt", found)
        from_text = call("read", "--from-result", str(self.root / "多组.txt"), "--example", "Q2E2")
        self.assertIn("钥匙2", from_text["fragments"][0]["text"])
        structured = self.root / "多组.json"
        structured.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")
        from_json = call("read", "--from-result", str(structured), "--continue-group", "Q2", "--budget", "100")
        self.assertTrue(any(row.get("reference") == "Q1" for row in from_json["delivery"]))

        damaged = json.loads(stored.raw)
        damaged["group_refs"] = "broken"
        invalid_type = self.root / "错误引用类型.json"
        invalid_type.write_text(json.dumps(damaged, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "引用无效"):
            call("read", "--from-result", str(invalid_type))
        damaged = json.loads(stored.raw)
        damaged["example_refs"][0]["group"] = found["reading"]["group_refs"][1]["group"]
        inconsistent = self.root / "不一致例段引用.json"
        inconsistent.write_text(json.dumps(damaged, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "引用无效"):
            call("read", "--from-result", str(inconsistent), "--example", "Q1E1")

        spread = call("find", "--query", encoded[0], "--query", encoded[1], "--as-of", "all",
                      "--spread", "--examples", "1", "--budget", "2")
        spread_state = save_state(spread, states)
        one = call("read", "--from-result", spread_state.state_id, "--continue-group", "Q1E1", "--budget", "100")
        self.assertTrue(any(row.get("reference") == "Q2E1" for row in one["delivery"]))
        with self.assertRaisesRegex(ValueError, "Q1 或 Q1E1"):
            call("read", "--from-result", stored.state_id, "--continue-group", "Q1E1")

        with self.assertRaisesRegex(ValueError, "JSON 字符串数组"):
            call("find", "--query", '{"all":"灯光"}', "--as-of", "all")
        with self.assertRaisesRegex(ValueError, "不与单组条件"):
            call("find", "灯", "--query", encoded[0], "--as-of", "all")

    def test_collection_deduplication_atomicity_and_note_scope(self):
        span = Span(1, 1, 2)
        with open_scope(self.book, Scope(self.snapshot, 2), writable=True) as library:
            item, = library.add_items("专题", [span, span], "后期批注")
            duplicate, = library.add_items("专题", [span], "不覆盖")
            hidden, = library.add_items("专题", [Span(2, 0, 1)])
            self.assertFalse(duplicate["created"])
            self.assertEqual(duplicate["id"], item["id"])
            self.assertEqual(duplicate["note"], "后期批注")
        with open_scope(self.book, Scope(self.snapshot, 1), writable=True) as library:
            listed, = library.list_items("专题")["items"]
            self.assertIsNone(listed["note"])
            page = read_passages(library, item_ids=[item["id"]])
            self.assertEqual(page["fragments"][0]["text"], "😀")
            with self.assertRaises(ValueError):
                read_passages(library, item_ids=[hidden["id"]])
            with self.assertRaises(ValueError):
                library.add_items("专题", [Span(1, 0, 1), Span(2, 0, 1)])
            self.assertEqual(len(library.list_items("专题")["items"]), 1)
            library.remove_items([item["id"]])
            self.assertEqual(library.list_items("专题")["items"], [])
            self.assertEqual(read_passages(library, [span])["fragments"][0]["text"], "😀")

    def test_cli_journey(self):
        pythonpath = Path(__file__).resolve().parents[1] / "src"

        def cli(*args, success=True):
            result = subprocess.run(
                [sys.executable, "-B", "-m", "chapspan.cli", "--library", str(self.book), *args],
                cwd=self.root, env={**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "cp936"},
                capture_output=True, encoding="utf-8", check=False,
            )
            if success:
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            return result.stderr

        snapshot = cli("import-txt", str(self.source))["snapshot_id"]
        scope = ("--snapshot", snapshot, "--as-of", "1")
        directory = cli("overview", *scope, "--limit", "1")
        self.assertIsNone(directory["next_after_chapter"])
        chapter, = directory["chapters"]
        self.assertEqual(chapter["title"], "第一章 开门")
        self.assertNotIn("text", chapter)
        opening = cli("read", "--paragraph", chapter["first_paragraph"], *scope)
        self.assertTrue(opening["fragments"][0]["text"].startswith("甲😀乙。"))

        result = cli("find", "😀", *scope, "--examples", "1")
        span = result["matches"][0]["span"]
        item_id = cli("collect", "add", "一组", span, *scope)["items"][0]["id"]
        page = cli("read", "--item", str(item_id), *scope)
        self.assertEqual(page["fragments"][0]["text"], "😀")
        citation = page["fragments"][0]["citation"]
        self.assertEqual(cli("read", citation, *scope)["fragments"], page["fragments"])
        self.assertIn("不可读取", cli("read", "C0002:0-1", *scope, success=False))

        draft = self.root / "当前稿.md"
        header = f"---\nsnapshot_id: {snapshot}\nknowledge_horizon: 1\ncollection_id: 一组\n---\n"
        markdown = f"原判断。{citation}  \n`[@示例]`\n\n```text\n[@代码示例]\n```\n"
        draft.write_text(header + markdown, encoding="utf-8-sig", newline="\r\n")
        recovered = cli("resume", str(draft), "--as-of", "1")
        self.assertEqual(recovered["markdown"], markdown)
        self.assertEqual(recovered["citations"], [{"citation": citation, "status": "ok"}])
        self.assertEqual(recovered["collection_id"], "一组")
        self.assertIn("稿件形成进度超过", cli("resume", str(draft), "--as-of", "0", success=False))

        edited = markdown.replace("原判断", "人工修改") + "\n[@C0001:0-999] [@未补]"
        draft.write_text(header + edited, encoding="utf-8")
        recovered = cli("resume", str(draft), "--as-of", "1")
        self.assertEqual(recovered["markdown"], edited)
        self.assertEqual([entry["status"] for entry in recovered["citations"]],
                         ["ok", "invalid_range", "malformed"])
        draft.write_text(header + edited + " [@C0002:0-2]", encoding="utf-8")
        self.assertIn("引用超过", cli("resume", str(draft), "--as-of", "1", success=False))

    def test_direct_text_resume_matches_library_citation_statuses(self):
        other = import_txt(self.book, self.source, paragraphs="blank-lines")["snapshot_id"]
        citations = [Span(1, 0, 6).citation, Span(2, 0, 2).citation, Span(1, 0, 999).citation,
                     Span(3, 0, 2).citation, Span(1, 0, 1).source_citation(other), "[@未补]"]
        draft = self.root / "直接校稿.md"
        header = f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 1\ncollection_id: 一组\n---\n"
        markdown = "\n".join(citations)
        draft.write_text(header + markdown, encoding="utf-8")
        parser = build_parser()
        direct = run(parser.parse_args(["--text", str(self.source), "resume", str(draft), "--as-of", "all"]))
        stored = run(parser.parse_args(["--library", str(self.book), "resume", str(draft), "--as-of", "3"]))
        self.assertEqual(direct, stored)
        self.assertEqual([row["status"] for row in direct["citations"]],
                         ["ok", "beyond_draft_horizon", "invalid_range", "unavailable",
                          "snapshot_mismatch", "malformed"])
        self.assertEqual(direct["scope"]["as_of"], 3)
        self.assertEqual(direct["collection_id"], "一组")

        peripheral_draft = self.root / "外围校稿.md"
        peripheral_draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 3\n---\n{Span(3, 0, 2).citation}",
            encoding="utf-8")
        peripheral = run(parser.parse_args(["--text", str(self.source), "resume", str(peripheral_draft),
                                            "--as-of", "all", "--include-peripheral"]))
        self.assertEqual(peripheral["citations"], [{"citation": Span(3, 0, 2).citation, "status": "ok"}])
        with self.assertRaisesRegex(ValueError, "稿件形成进度超过"):
            run(parser.parse_args(["--text", str(self.source), "resume", str(draft), "--as-of", "0"]))
        limited_draft = self.root / "超范围校稿.md"
        limited_draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 1\n---\n{Span(2, 0, 2).citation}",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "引用超过"):
            run(parser.parse_args(["--text", str(self.source), "resume", str(limited_draft), "--as-of", "1"]))
        mismatched = self.root / "快照不符.md"
        mismatched.write_text(f"---\nsnapshot_id: {other}\nknowledge_horizon: 1\n---\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "快照不同"):
            run(parser.parse_args(["--text", str(self.source), "resume", str(mismatched), "--as-of", "all"]))

    def test_direct_text_resume_cli_stays_structured_and_saves_receipt(self):
        draft = self.root / "命令校稿.md"
        citation = Span(3, 0, 2).citation
        draft.write_text(f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 3\n---\n{citation}",
                         encoding="utf-8")
        pythonpath = Path(__file__).resolve().parents[1] / "src"
        environment = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "utf-8"}
        command = [sys.executable, "-B", "-m", "chapspan.cli", "--text", str(self.source)]

        def cli(*args):
            return subprocess.run([*command, *args], cwd=self.root, env=environment,
                                  capture_output=True, encoding="utf-8", check=False)

        default = cli("resume", str(draft), "--as-of", "all", "--include-peripheral")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(json.loads(default.stdout)["citations"], [{"citation": citation, "status": "ok"}])
        explicit = cli("--format", "json", "resume", str(draft), "--as-of", "all", "--include-peripheral")
        self.assertEqual(json.loads(explicit.stdout), json.loads(default.stdout))
        for index, before_command in enumerate((False, True), 1):
            output = self.root / f"校稿回执{index}.txt"
            arguments = ["resume", str(draft), "--as-of", "all", "--include-peripheral"]
            arguments = ["--output", str(output)] + arguments if before_command else arguments + ["--output", str(output)]
            saved = cli(*arguments)
            self.assertEqual(saved.returncode, 0, saved.stderr)
            self.assertEqual(json.loads(saved.stdout)["citations"], 1)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), json.loads(default.stdout))
        rejected = cli("--format", "text", "resume", str(draft), "--as-of", "all")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("--format text", rejected.stderr)

    def test_preview_reports_tight_heading_without_publishing(self):
        source = self.root / "回目.txt"
        source.write_text("第一回 起头\n叙述。\n第二回中有叙述。\n第一○回 收束\n末段。\n", encoding="utf-8")
        target = self.root / "预览书库"

        preview = import_txt(target, source, preview=True)

        self.assertFalse(target.exists())
        self.assertEqual(preview["heading_count"], 2)
        self.assertEqual(preview["tight_candidate_count"], 1)
        candidate, = preview["tight_candidate_examples"]
        self.assertEqual(candidate["source_line"], 3)
        self.assertEqual(candidate["next_heading"]["title"], "第一○回 收束")
        receipt = import_txt(target, source)
        self.assertEqual(receipt["tight_candidate_examples"], preview["tight_candidate_examples"])

    def test_direct_text_handoff_preserves_parsing_and_printed_boundary(self):
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), *args]))

        page = call("--paragraphs", "blank-lines", "read", "--as-of", "第一章",
                    "--chapter", "C0001", "--budget", "6")
        self.save_text("首页.txt", page)
        saved = self.root / "首页.txt"
        following = call("read", "--from-result", str(saved), "--budget", "100")
        self.assertEqual(following["scope"], page["scope"])
        self.assertEqual(following["text_options"], {"paragraphs": "blank-lines"})
        whole = call("--paragraphs", "blank-lines", "read", "--as-of", "第一章", "--chapter", "C0001")
        self.assertEqual(page["fragments"][0]["text"] + following["fragments"][0]["text"],
                         whole["fragments"][0]["text"])
        legacy_text = self.root / "旧文本.txt"
        legacy_state = {key: page[key] for key in ("scope", "text_options", "continuation")}
        legacy_state["fragments"] = [{"read_span": row["read_span"]} for row in page["fragments"]]
        legacy_text.write_text("寻章结果 v1\n旧正文\n寻章接续 "
                               + json.dumps(legacy_state, ensure_ascii=False), encoding="utf-8")
        legacy_text_following = call("read", "--from-result", str(legacy_text), "--budget", "100")
        self.assertEqual(legacy_text_following["fragments"], following["fragments"])
        legacy = self.root / "旧结果.json"
        legacy_result = json.loads(json.dumps(page))
        del legacy_result["continuation"]["delivery"]
        legacy.write_text(json.dumps(legacy_result, ensure_ascii=False), encoding="utf-8")
        legacy_following = call("read", "--from-result", str(legacy), "--budget", "100")
        self.assertEqual(legacy_following["fragments"], following["fragments"])
        with self.assertRaisesRegex(ValueError, "不可读取"):
            call("read", "--from-result", str(saved), "--as-of", "all", "--chapter", "C0002")

        self.source.write_text("第一章 开篇\n介绍。\n第三章错插\n后续秘密。\n第二章 当前\n正文。\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "第 3 行的疑似章题超过指定阅读进度"):
            call("read", "--as-of", "第二章", "--chapter", "C0001")

    def test_handoff_carries_selection_and_never_extends_scope(self):
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--library", str(self.book), *args]))

        def save(name, result):
            path = self.root / name
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return str(path)

        directory = call("overview", "--snapshot", self.snapshot, "--as-of", "2", "--toc")
        selected = save("章目.json", directory)
        found = call("find", "灯", "--as-of", "2", "--from-result", selected, "--compact")
        matches = save("检索.json", found)
        opening = call("read", "--from-result", matches, "--pick", "1", "--as-of", "1", "--whole-paragraphs")
        self.assertEqual(opening["scope"]["as_of"], 1)
        self.assertIn("灯", opening["fragments"][0]["text"])
        preview = call("find", "灯", "--from-result", selected, "--as-of", "1", "--examples", "1", "--budget", "1")
        next_preview = call("read", "--from-result", save("例段.json", preview), "--as-of", "1", "--budget", "20")
        self.assertTrue(next_preview["complete"])
        self.assertEqual(preview["examples"]["fragments"][0]["text"] + next_preview["fragments"][0]["text"],
                         opening["fragments"][0]["text"])
        with self.assertRaisesRegex(ValueError, "不可读取"):
            call("read", "--from-result", selected, "--pick", "2", "--as-of", "1")

        options = ("read", "--from-result", selected, "--pick", "1", "--pick", "2", "--as-of", "2", "--after", "1")
        full = call(*options, "--group-budget", "100")["passages"]
        page = call(*options, "--group-budget", "4", "--budget", "8")
        self.assertEqual([group["group"] for group in page["groups"]], [1, 2])
        self.assertTrue(all(group["fragments"] for group in page["groups"]))
        actual = {1: [], 2: []}
        for _ in range(10):
            self.assertLessEqual(page["read_characters"], 8)
            for group in page["groups"]:
                for fragment in group["fragments"]:
                    passage = page["passages"][fragment["passage"] - 1]
                    start = Span.parse(fragment["read_span"]).start - Span.parse(passage["span"]).start
                    span = Span.parse(fragment["read_span"])
                    actual[group["group"]].append(passage["text"][start:start + span.end - span.start])
            if page["complete"]:
                break
            previous = save("续读.json", page)
            page = call("read", "--from-result", previous, "--as-of", "2")
        else:
            self.fail("分组续读未在预期页数内完成")
        self.assertEqual(["".join(value) for value in actual.values()], [passage["text"] for passage in full])

        group_overlap = call("read", "C0001:0-6", "C0001:1-8", "--snapshot", self.snapshot,
                             "--as-of", "2", "--group-budget", "20")
        self.assertEqual(group_overlap["passages"], [{"span": "C0001:0-8", "text": "甲😀乙。\n\n哈哈"}])
        self.assertEqual([group["fragments"][0]["passage"] for group in group_overlap["groups"]], [1, 1])
        read_result = save("回读.json", opening)
        collected = call("collect", "add", "材料", "--from-result", read_result, "--as-of", "2")
        citation = collected["items"][0]["source_citation"]
        self.assertEqual(citation, opening["fragments"][0]["source_citation"])
        other = import_txt(self.book, self.source, paragraphs="blank-lines")["snapshot_id"]
        with self.assertRaisesRegex(ValueError, "快照"):
            call("read", citation, "--snapshot", other, "--as-of", "2")
        draft = self.root / "引文.md"
        draft.write_text(f"---\nsnapshot_id: {other}\nknowledge_horizon: 2\n---\n{citation}", encoding="utf-8")
        checked = call("resume", str(draft), "--as-of", "2")
        self.assertEqual(checked["citations"][0]["status"], "snapshot_mismatch")

    def test_scoped_chapter_inspection_for_text_and_library(self):
        self.source.write_text(
            "第一章 重复\n开篇\n第二章紧连\n中段\n"
            "第三章 重复\n后段一\n第三章 重复\n后段二\n完本感言\n外围\n",
            encoding="utf-8",
        )
        parser = build_parser()

        def direct(*args):
            return run(parser.parse_args(["--text", str(self.source), *args]))

        overview = direct("overview", "--as-of", "all", "--include-peripheral")
        diagnostics = overview["chapter_diagnostics"]
        kinds = {row["kind"] for row in diagnostics["examples"]}
        self.assertEqual(kinds, {"tight_heading", "number_gap", "duplicate_number",
                                 "duplicate_title", "trailing_peripheral"})
        self.assertEqual(diagnostics["issue_count"], 5)
        self.assertEqual(diagnostics["shown_count"], 5)
        self.assertEqual(diagnostics["kind_counts"], {kind: 1 for kind in kinds})
        self.assertEqual(diagnostics["unresolved_tight_headings"], 1)
        self.assertIsNone(diagnostics["next_diagnostic_offset"])
        direct_text = format_text(overview)
        self.assertIn("章界诊断：共 5 处", direct_text)
        self.assertIn("展示 2/5", direct_text)
        self.assertIn("第二章紧连", direct_text)
        self.assertIn("--diagnostic-offset", direct_text)
        inspected = direct("overview", "--as-of", "all", "--inspect", "C0001", "--line", "3")
        inspection = inspected["inspection"]
        self.assertEqual(inspection["target_line"], 3)
        self.assertEqual(inspection["encoding"], "utf-8-sig")
        self.assertEqual(inspection["parsing"]["paragraphs"], "lines")
        self.assertIn("第二章紧连", [row["text"] for row in inspection["source_lines"]])
        self.assertTrue(next(row for row in inspection["source_lines"] if row["line"] == 3)["target"])
        self.assertEqual([row["chapter"] for row in inspection["adjacent_chapters"]], [2])

        limited = direct("overview", "--as-of", "1", "--inspect", "C0001", "--line", "3")
        self.assertEqual(limited["inspection"]["adjacent_chapters"], [])
        self.assertNotIn("第三章 重复", json.dumps(limited["inspection"], ensure_ascii=False))
        limited_overview = direct("overview", "--as-of", "1")
        self.assertEqual(limited_overview["chapter_diagnostics"]["issue_count"], 1)
        self.assertNotIn("第三章 重复", json.dumps(limited_overview, ensure_ascii=False))
        with self.assertRaisesRegex(ValueError, "不可读取"):
            direct("overview", "--as-of", "1", "--inspect", "C0002")
        with self.assertRaisesRegex(ValueError, "核查来源行"):
            direct("overview", "--as-of", "1", "--inspect", "C0001", "--line", "5")

        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        stored = run(parser.parse_args(["--library", str(self.book), "overview", "--snapshot", snapshot,
                                        "--as-of", "4", "--include-peripheral", "--inspect", "C0001",
                                        "--line", "3"]))
        self.assertEqual(stored["inspection"]["source_lines"], inspection["source_lines"])
        self.assertEqual(stored["inspection"]["parsing"], inspection["parsing"])
        text = format_text(stored)
        self.assertIn("→ 行 3 | 第二章紧连", text)
        self.assertIn("import-txt 发布新快照", text)

        pattern = r"第[0-9〇○零一二三四五六七八九十百千万萬两兩]+[章节節回].*"
        custom = direct("--chapter-pattern", pattern, "overview", "--as-of", "all")
        self.assertNotEqual(custom["scope"]["snapshot_id"], overview["scope"]["snapshot_id"])
        self.assertEqual(custom["text_options"]["chapter_pattern"], pattern)
        saved = self.save_text("疑点.txt", custom)
        continued = direct("find", "后段", "--from-result", str(self.root / "疑点.txt"), "--examples", "0")
        self.assertEqual(continued["match_count"], 2)
        self.assertEqual(continued["text_options"]["chapter_pattern"], pattern)
        self.assertEqual(saved.state_id, save_state(custom, self.root / "状态").state_id)

    def test_inspection_never_leaks_previous_or_filtered_chapters(self):
        path = self.root / "核查边界.txt"
        path.write_text(
            "章前SECRET\n"
            "第一章 前章\nSECRET_BEFORE_SCOPE\n"
            "作者的话\nSECRET_PERIPHERAL\n"
            "第二章 当前章\n第一行正文\n第二行正文\n"
            "第三章 空章\n"
            "第四章 末章\n末章无换行",
            encoding="utf-8",
        )
        book = self.root / "核查书库"
        snapshot = import_txt(book, path)["snapshot_id"]
        parser = build_parser()

        def inspect_both(chapter, line=None, *, from_chapter=1, as_of="all"):
            txt_args = ["--text", str(path), "overview", "--as-of", str(as_of),
                        "--from-chapter", str(from_chapter), "--inspect", chapter]
            db_as_of = str(len(["章前", "一", "外", "二", "三", "四"]) if as_of == "all" else as_of)
            db_args = ["--library", str(book), "overview", "--snapshot", snapshot,
                       "--as-of", db_as_of, "--from-chapter", str(from_chapter), "--inspect", chapter]
            if line is not None:
                txt_args.extend(["--line", str(line)])
                db_args.extend(["--line", str(line)])
            txt_res = run(parser.parse_args(txt_args))["inspection"]
            db_res = run(parser.parse_args(db_args))["inspection"]
            self.assertEqual(txt_res["source_lines"], db_res["source_lines"])
            self.assertEqual(txt_res["adjacent_chapters"], db_res["adjacent_chapters"])
            return txt_res

        # Pre-chapter text stays within its own lines.
        pre = inspect_both("C0001", 1, as_of=1)
        self.assertEqual([row["line"] for row in pre["source_lines"]], [1])
        self.assertEqual(pre["source_lines"][0]["text"], "章前SECRET")

        # Narrowed first visible chapter: heading line and first two body lines must not leak prior chapter or peripheral lines.
        for line in (6, 7, 8):
            insp = inspect_both("C0004", line, from_chapter=4, as_of=4)
            lines = [row["line"] for row in insp["source_lines"]]
            self.assertTrue(all(l >= 6 for l in lines), (line, lines))
            dumped = json.dumps(insp, ensure_ascii=False)
            self.assertNotIn("SECRET_BEFORE_SCOPE", dumped)
            self.assertNotIn("SECRET_PERIPHERAL", dumped)
            self.assertNotIn("章前SECRET", dumped)
            self.assertEqual(insp["adjacent_chapters"], [])

        # Preceding filtered peripheral chapter with default from_chapter=1 must not leak either.
        insp_periph = inspect_both("C0004", 7, from_chapter=1, as_of=4)
        self.assertNotIn("SECRET_PERIPHERAL", json.dumps(insp_periph, ensure_ascii=False))
        self.assertTrue(all(row["line"] >= 6 for row in insp_periph["source_lines"]))

        # Empty chapter stays on its own heading line.
        empty_insp = inspect_both("C0005", from_chapter=4, as_of=5)
        self.assertEqual([row["line"] for row in empty_insp["source_lines"]], [9])

        # Last chapter without trailing newline ends at its own last line and does not cross backward.
        eof_insp = inspect_both("C0006", 11, from_chapter=6, as_of=6)
        self.assertEqual([(row["line"], row["text"]) for row in eof_insp["source_lines"]],
                         [(10, "第四章 末章"), (11, "末章无换行")])

    def test_chapter_diagnostics_page_without_hiding_tight_candidates(self):
        self.source.write_text("第一章 开始\n正文\n" + "".join(
            f"第{number}章紧连{number}\n候选正文\n" for number in range(2, 9)
        ) + "第九章 收束\n末段\n", encoding="utf-8")
        parser = build_parser()
        first = run(parser.parse_args(["--text", str(self.source), "overview", "--as-of", "all"]))
        diagnostics = first["chapter_diagnostics"]
        self.assertEqual(diagnostics["issue_count"], 8)
        self.assertEqual(diagnostics["kind_counts"], {"number_gap": 1, "tight_heading": 7})
        self.assertEqual(diagnostics["shown_count"], 6)
        self.assertEqual(diagnostics["next_diagnostic_offset"], 6)
        second = run(parser.parse_args(["--text", str(self.source), "overview", "--as-of", "all",
                                       "--diagnostic-offset", "6"]))
        following = second["chapter_diagnostics"]
        self.assertEqual(following["issue_count"], 8)
        self.assertEqual(following["diagnostic_offset"], 6)
        self.assertEqual(following["shown_count"], 2)
        self.assertIsNone(following["next_diagnostic_offset"])
        self.assertFalse({row["source_line"] for row in diagnostics["examples"]}
                         & {row["source_line"] for row in following["examples"]})
        self.assertEqual({row["source_line"] for row in diagnostics["examples"] + following["examples"]},
                         {3, 5, 7, 9, 11, 13, 15, 17})
        self.source.write_text("第一章 开始\n正文\n第三章 跳号\n正文\n第四章紧连\n候选\n第五章 收束\n末段\n",
                               encoding="utf-8")
        prioritized = run(parser.parse_args(["--text", str(self.source), "overview", "--as-of", "all"]))
        self.assertEqual([row["kind"] for row in prioritized["chapter_diagnostics"]["examples"]],
                         ["tight_heading", "number_gap", "number_gap"])

    def test_filtered_peripheral_gap_is_visible_in_text_and_library(self):
        self.source.write_text("第一章 开始\n正文\n请假条\n" + "假" * 218
                               + "\n第二章 后续\n后文\n", encoding="utf-8")
        parser = build_parser()

        def call(source_flag, source, *args):
            return run(parser.parse_args([source_flag, str(source), *args]))

        direct = call("--text", self.source, "overview", "--as-of", "all")
        self.assertEqual([row["chapter"] for row in direct["chapters"]], [1, 3])
        diagnostics = direct["chapter_diagnostics"]
        self.assertEqual(diagnostics["issue_count"], 0)
        self.assertEqual(diagnostics["filtered_peripheral_count"], 1)
        self.assertEqual(diagnostics["filtered_peripheral_examples"][0]["chapter"], 2)
        self.assertEqual(diagnostics["filtered_peripheral_examples"][0]["title"], "请假条")
        text = format_text(direct)
        self.assertIn("过滤外围：1 章", text)
        self.assertIn("C0002", text)
        self.assertIn("--include-peripheral", text)
        self.assertNotIn("章界诊断", text)

        included = call("--text", self.source, "overview", "--as-of", "all", "--include-peripheral")
        self.assertNotIn("chapter_diagnostics", included)
        self.assertEqual([row["chapter"] for row in included["chapters"]], [1, 2, 3])
        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        stored = call("--library", self.book, "overview", "--snapshot", snapshot, "--as-of", "3")
        self.assertEqual(stored["chapter_diagnostics"], diagnostics)
        self.assertIn("--include-peripheral", format_text(stored))

    def test_toc_covers_long_books_and_keeps_title_ambiguity(self):
        self.source.write_text("".join(f"第{number}章 回访\n正文\n" for number in range(1, 1076))
                               + "完本感言\n外围\n", encoding="utf-8")
        snapshot = import_txt(self.book, self.source)["snapshot_id"]
        with open_scope(self.book, Scope(snapshot, 1076)) as library:
            toc = library.overview(toc=True, limit=2000)
            self.assertEqual(len(toc["chapters"]), 1075)
            self.assertIsNone(toc["next_after_chapter"])
            self.assertEqual(set(toc["chapters"][0]), {"chapter", "title", "kind", "first_paragraph"})
            subset = library.overview(toc=True, title="回访", limit=1)
            self.assertEqual(subset["next_after_chapter"], 1)
            self.assertEqual(library.overview(toc=True, title="回访", limit=1, after_chapter=1)["chapters"][0]["chapter"], 2)
            self.assertEqual(library.overview(toc=True, title="完本")["chapters"], [])

    def test_delivery_survives_selective_group_continuation(self):
        self.source.write_text("".join(
            f"第{number}章 测试\n" + "".join(f"{number}-{line}：" + "字" * 20 + "\n" for line in range(1, 5))
            for number in (1, 2)
        ), encoding="utf-8")
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), *args]))

        def save(name, result):
            path = self.root / name
            path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return str(path)

        first = call("read", "--as-of", "all", "--chapter", "C0001", "--chapter", "C0002",
                     "--group-budget", "50", "--budget", "100")
        self.assertEqual([row["status"] for row in first["delivery"]], ["partial", "partial"])
        continued = call("read", "--from-result", save("只续一组.json", first), "--continue-group", "1")
        self.assertEqual([row["group"] for row in continued["continuation"]["groups"]], [2])
        delivery = {row["group"]: row for row in continued["delivery"]}
        self.assertEqual(delivery[1]["read_spans"], ["C0001:0-100"])
        self.assertEqual(delivery[1]["read_characters"], 100)
        self.assertEqual(delivery[1]["status"], "complete")
        self.assertEqual(delivery[2]["read_characters"], 50)
        self.assertEqual(delivery[2]["status"], "partial")
        continued_text = format_text(continued, reference="state_test")
        self.assertIn("本轮未续：组2", continued_text)
        self.assertIn("50/100 字", continued_text)
        self.assertIn("待续 C0002:50", continued_text)
        self.assertNotIn("已读", continued_text)
        other = call("read", "--from-result", save("再续二组.json", continued), "--continue-group", "2")
        self.assertTrue(other["complete"])
        self.assertIsNone(other["continuation"])
        self.assertEqual([row["status"] for row in other["delivery"]], ["complete", "complete"])

    def test_partial_delivery_distinguishes_current_page_and_accumulated(self):
        self.source.write_text("第一章 分页\n" + "字" * 12 + "\n", encoding="utf-8")
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), "--state-dir",
                                          str(self.root / "分页状态"), *args]))

        first = call("read", "--as-of", "all", "--chapter", "C0001", "--budget", "4")
        first_text = format_text(first, reference="state_first")
        self.assertIn("未完成 4/13 字", first_text)
        stored = save_state(first, self.root / "分页状态")
        second = call("read", "--from-result", stored.state_id, "--budget", "4")
        second_text = format_text(second, reference="state_second")
        self.assertIn("未完成 8/13 字", second_text)
        self.assertNotIn("已读", second_text)

    def test_spread_examples_expand_from_text_json_and_token(self):
        self.source.write_text("".join(
            f"第{number}章 例段\n前段{number}。\n目标{number}出现在本段。\n后段{number}。\n"
            for number in range(1, 4)
        ), encoding="utf-8")
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), *args]))

        found = call("find", "目标", "--as-of", "all", "--spread", "--examples", "3", "--budget", "300")
        selected = found["examples"]["selected_spans"]
        self.assertEqual([Span.parse(value).chapter for value in selected], [1, 2, 3])
        text = format_text(found, saved=True)
        self.assertIn("--example 1", text)
        self.assertNotIn("--example 序号", text)
        self.assertIn("例2", text)
        self.save_text("检索文本.txt", found)
        saved = self.root / "检索文本.txt"
        structured = self.root / "检索结构.json"
        structured.write_text(json.dumps(found, ensure_ascii=False), encoding="utf-8")

        for source in (str(saved), str(structured), handoff_token(found)):
            expanded = call("read", "--from-result", source, "--example", "2", "--before", "1", "--after", "1")
            self.assertEqual(expanded["fragments"][0]["match_spans"], [selected[1]])
            self.assertIn("前段2", expanded["fragments"][0]["text"])
            self.assertIn("后段2", expanded["fragments"][0]["text"])
        with self.assertRaisesRegex(ValueError, "分开使用"):
            call("read", "--from-result", str(saved), "--example", "1", "--pick", "1")

    def test_text_reports_selected_examples_and_omits_empty_reading_chrome(self):
        self.source.write_text("".join(
            f"第{number}章 城门\n灯" + "字" * 7 + "。\n"
            for number in range(1, 4)
        ), encoding="utf-8")
        parser = build_parser()

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), *args]))

        found = call("find", "灯", "--as-of", "all", "--examples", "3", "--budget", "5")
        text = format_text(found, reference="state_test")
        self.assertIn('"灯" 3 次 / 3 章（C0001–C0003）', text)
        self.assertIn("例段 3：C0001 · C0002 · C0003", text)
        self.assertIn("[例1 · 未完成 5/10 字]", text)
        self.assertNotIn("已读", text)
        self.assertIn("未开始：例2", text)
        self.assertIn("例3", text)
        self.assertIn("续读：read --from-result state_test", text)
        fragment = found["examples"]["fragments"][0]
        self.assertNotIn(fragment["source_citation"], text)
        self.assertIn(fragment["citation"], text)
        self.assertNotIn("不代表宿主", text)
        self.assertNotIn("快照 ", text)
        receipt = output_receipt(found, "查询.txt")
        self.assertEqual(receipt["examples"], 3)
        self.assertEqual(receipt["delivery"], {"partial": 1, "not_started": 2})

        self.source.write_text("第一章 合并\n灯灯。\n", encoding="utf-8")
        merged = call("find", "灯", "--as-of", "all", "--examples", "2")
        self.assertEqual(len(merged["examples"]["selected_spans"]), 2)
        self.assertEqual(len(merged["examples"]["fragments"]), 1)
        self.assertEqual(output_receipt(merged, "合并.txt")["delivery"], {"complete": 2})

        empty = call("find", "不存在", "--as-of", "all", "--examples", "3")
        empty_text = format_text(empty, reference="state_unused")
        self.assertIn('当前范围内未找到 "不存在"', empty_text)
        self.assertNotIn("取例", empty_text)
        self.assertNotIn("正文 0 字符", empty_text)
        self.assertNotIn("state_unused", empty_text)

    def test_short_state_covers_examples_picks_and_safe_recovery(self):
        self.source.write_text("".join(
            f"第{number}章 城门\n灯" + "字" * 7 + "。\n"
            for number in range(1, 4)
        ), encoding="utf-8")
        parser = build_parser()
        states = self.root / "可迁移状态"

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), "--state-dir", str(states), *args]))

        found = call("find", "灯", "--as-of", "all", "--examples", "3", "--budget", "5")
        stored = save_state(found, states)
        self.assertEqual(save_state(found, states).state_id, stored.state_id)
        other_result = call("find", "灯", "--as-of", "all", "--examples", "3", "--budget", "6")
        other = save_state(other_result, states)
        self.assertNotEqual(stored.state_id, other.state_id)
        with ThreadPoolExecutor(max_workers=8) as pool:
            concurrent = list(pool.map(lambda result: save_state(result, states), [found] * 8 + [other_result] * 8))
        self.assertEqual([item.state_id for item in concurrent], [stored.state_id] * 8 + [other.state_id] * 8)
        self.assertEqual(load_result(stored.state_id, state_dir=states)["example_spans"],
                         found["examples"]["selected_spans"])
        self.assertEqual(load_result(str(stored.path))["example_spans"], found["examples"]["selected_spans"])
        portable = self.root / "完整状态副本.json"
        portable.write_bytes(stored.raw)
        self.assertEqual(load_result(str(portable))["example_spans"], found["examples"]["selected_spans"])

        expanded = call("read", "--from-result", stored.state_id, "--example", "2", "--before", "1")
        self.assertEqual(expanded["fragments"][0]["match_spans"], [found["examples"]["selected_spans"][1]])
        continued = call("read", "--from-result", stored.state_id, "--budget", "100")
        self.assertGreater(continued["read_characters"], 0)
        toc = call("overview", "--as-of", "all", "--toc")
        toc_state = save_state(toc, states)
        picked = call("read", "--from-result", toc_state.state_id, "--pick", "2")
        self.assertIn("字", picked["fragments"][0]["text"])

        self.save_text("伴生.txt", found)
        self.assertEqual(load_result(str(self.root / "伴生.txt"))["example_spans"],
                         found["examples"]["selected_spans"])
        moved = self.root / "一起移动.txt"
        moved.write_bytes((self.root / "伴生.txt").read_bytes())
        moved.with_name(moved.name + ".state.json").write_bytes(
            (self.root / "伴生.txt.state.json").read_bytes())
        self.assertEqual(load_result(str(moved))["example_spans"], found["examples"]["selected_spans"])
        lonely = self.root / "只复制文本.txt"
        lonely.write_text((self.root / "伴生.txt").read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "伴生"):
            load_result(str(lonely))
        with self.assertRaisesRegex(ValueError, "找不到接续状态"):
            load_result("state_" + "0" * 24, state_dir=states)

        stored.path.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "损坏"):
            load_result(stored.state_id, state_dir=states)
        stored.path.write_bytes(stored.raw)
        tampered = json.loads(stored.raw)
        tampered["scope"]["as_of"] = 2
        stored.path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不符"):
            load_result(stored.state_id, state_dir=states)
        with self.assertRaisesRegex(ValueError, "不同内容"):
            save_state(found, states)
        racing = states / "竞争状态.json"
        named_temporary_file = tempfile.NamedTemporaryFile

        def write_competing_state(*args, **kwargs):
            racing.write_bytes(other.raw)
            return named_temporary_file(*args, **kwargs)

        with patch("chapspan.handoff.tempfile.NamedTemporaryFile", side_effect=write_competing_state):
            with self.assertRaisesRegex(ValueError, "不同内容"):
                write_state_file(racing, stored.raw)
        self.assertEqual(racing.read_bytes(), other.raw)

    def test_short_state_survives_processes_and_working_directories(self):
        pythonpath = Path(__file__).resolve().parents[1] / "src"
        environment = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "utf-8"}
        states = self.root / "进程状态"
        command = [sys.executable, "-B", "-m", "chapspan.cli", "--text", str(self.source),
                   "--state-dir", str(states)]
        first = subprocess.run([*command, "find", "灯", "--as-of", "1", "--examples", "2", "--budget", "1"],
                               cwd=self.root, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("span:", first.stdout)
        identifiers = set(re.findall(r"state_[0-9a-f]{24}", first.stdout))
        self.assertEqual(len(identifiers), 1, first.stdout)
        state_id = next(iter(identifiers))
        self.assertTrue((states / (state_id + ".json")).is_file())
        elsewhere = self.root / "另一工作目录"
        elsewhere.mkdir()
        following = subprocess.run([*command, "read", "--from-result", state_id, "--budget", "100"],
                                   cwd=elsewhere, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(following.returncode, 0, following.stderr)
        self.assertNotIn("span:", following.stdout)
        self.assertIn("范围 C0001–C0001", following.stdout)
        self.assertIn("灯", following.stdout)

        saved = self.root / "跨会话.txt"
        receipt = subprocess.run([*command, "--output", str(saved), "find", "灯", "--as-of", "1",
                                  "--examples", "2", "--budget", "1"], cwd=self.root, env=environment,
                                 capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(receipt.returncode, 0, receipt.stderr)
        self.assertNotIn("不代表宿主", receipt.stdout)
        self.assertIn("已选例段：2", receipt.stdout)
        self.assertNotIn("寻章接续", saved.read_text(encoding="utf-8"))
        companion = saved.with_name(saved.name + ".state.json")
        self.assertTrue(companion.is_file())
        saved_ids = re.findall(r"state_[0-9a-f]{24}", receipt.stdout)
        self.assertEqual(len(saved_ids), 1, receipt.stdout)
        self.assertEqual(companion.read_bytes(), (states / (saved_ids[0] + ".json")).read_bytes())
        from_file = subprocess.run([*command, "read", "--from-result", str(saved), "--budget", "100"],
                                   cwd=elsewhere, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(from_file.returncode, 0, from_file.stderr)

        library_command = [sys.executable, "-B", "-m", "chapspan.cli", "--library", str(self.book),
                           "--format", "text"]
        library_page = subprocess.run([*library_command, "find", "灯", "--snapshot", self.snapshot,
                                       "--as-of", "1", "--examples", "2", "--budget", "1"],
                                      cwd=self.root, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(library_page.returncode, 0, library_page.stderr)
        library_ids = set(re.findall(r"state_[0-9a-f]{24}", library_page.stdout))
        self.assertEqual(len(library_ids), 1, library_page.stdout)
        library_state_id = next(iter(library_ids))
        self.assertTrue((self.book / "states" / (library_state_id + ".json")).is_file())
        library_next = subprocess.run([*library_command, "read", "--from-result", library_state_id, "--budget", "100"],
                                      cwd=elsewhere, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(library_next.returncode, 0, library_next.stderr)

    def test_short_state_cannot_expand_scope_or_switch_snapshot(self):
        parser = build_parser()
        states = self.root / "范围状态"
        page = run(build_parser().parse_args(["--text", str(self.source), "read", "--as-of", "1",
                                                   "--chapter", "C0001", "--budget", "4"]))
        stored = save_state(page, states)

        def call(*args):
            return run(parser.parse_args(["--text", str(self.source), "--state-dir", str(states), *args]))

        narrowed = call("read", "--from-result", stored.state_id, "--as-of", "1", "--budget", "100")
        self.assertEqual(narrowed["scope"]["as_of"], 1)
        with self.assertRaisesRegex(ValueError, "不可读取"):
            call("read", "--from-result", stored.state_id, "--as-of", "all", "--chapter", "C0002")
        self.source.write_text("第一章 开门\n已经改写。\n第二章 后续\n新内容。\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "快照"):
            call("read", "--from-result", stored.state_id)

    def test_output_writes_full_result_and_prints_small_receipt(self):
        pythonpath = Path(__file__).resolve().parents[1] / "src"
        text_path = self.root / "回执.txt"
        command = [sys.executable, "-B", "-m", "chapspan.cli", "--text", str(self.source),
                   "--state-dir", str(self.root / "状态")]
        environment = {**os.environ, "PYTHONPATH": str(pythonpath), "PYTHONIOENCODING": "utf-8"}
        text = subprocess.run([*command, "--output", str(text_path), "read", "--as-of", "1",
                               "--chapter", "C0001", "--budget", "4"], cwd=self.root, env=environment,
                              capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertIn("已保存", text.stdout)
        self.assertIn("选区交付", text.stdout)
        self.assertLess(len(text.stdout), 500)
        saved_text = text_path.read_text(encoding="utf-8")
        self.assertIn("未完成 4/22 字", saved_text)
        self.assertIn("续读：", saved_text)

        json_path = self.root / "回执.json"
        structured = subprocess.run([*command, "--format", "json", "--output", str(json_path),
                                     "find", "灯", "--as-of", "1"], cwd=self.root, env=environment,
                                    capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(structured.returncode, 0, structured.stderr)
        receipt = json.loads(structured.stdout)
        self.assertEqual(receipt["match_count"], 2)
        self.assertEqual(receipt["delivery"], {"complete": 2})
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["match_count"], 2)

    def test_research_run_records_results_resume_and_failures(self):
        run_dir, created = self.init_run()
        self.assertEqual(created["scope"]["as_of"], 1)
        manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["run_version"], 1)
        self.assertEqual(manifest["source"]["kind"], "text")
        self.assertEqual(set(manifest["source"]["text_options"]),
                         {"encoding", "chapter_pattern", "paragraphs"})
        self.assertFalse((run_dir.parent / ".current_run").exists())

        found = self.span_process("--run", run_dir, "find", "灯", "--examples", "1")
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("例1", found.stdout)
        first_result_path = run_dir / json.loads((run_dir / "calls.jsonl").read_text(encoding="utf-8"))["result_path"]
        first_result = load_result(str(first_result_path))
        self.assertEqual(first_result["scope"]["as_of"], 1)
        self.assertEqual(first_result["examples"]["fragment_count"], 1)

        read = self.span_process("--run", run_dir, "read", "--chapter", "C0001", "--budget", "4")
        self.assertEqual(read.returncode, 0, read.stderr)
        state_id = re.search(r"state_[0-9a-f]{24}", read.stdout).group()
        self.assertTrue((run_dir / "states" / f"{state_id}.json").is_file())
        records = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["origin"] for row in records], ["find_examples", "read"])
        second_result = load_result(str(run_dir / records[1]["result_path"]))
        citation = second_result["fragments"][0]["source_citation"]

        draft = run_dir / "当前稿.md"
        draft.write_text(f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 1\n---\n# 开篇\n判断。{citation}\n",
                         encoding="utf-8")
        resumed = self.span_json("--run", run_dir, "resume")
        self.assertEqual(resumed["citations"], [{"citation": citation, "status": "ok"}])
        draft.write_text(draft.read_text(encoding="utf-8").replace("判断", "人工修改"), encoding="utf-8")
        self.assertIn("人工修改", self.span_json("--run", run_dir, "resume")["markdown"])

        denied = self.span_process("--run", run_dir, "read", "--chapter", "C0002", "--format", "json")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("不可读取", denied.stderr)
        binding_error = self.span_process("--run", run_dir, "--state-dir", self.root / "其他状态", "find", "灯")
        self.assertNotEqual(binding_error.returncode, 0)
        self.assertIn("states", binding_error.stderr)

        records = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["sequence"] for row in records], list(range(1, len(records) + 1)))
        self.assertEqual(records[-2]["status"], "failure")
        self.assertEqual(records[-2]["failed_stage"], "execute")
        self.assertEqual(records[-2]["scope"]["as_of"], 1)
        self.assertIsNone(records[-2]["result_path"])
        self.assertEqual(records[-1]["failed_stage"], "bind")
        self.assertIsNone(records[-1]["scope"])
        self.assertIsNone(records[-1]["origin"])
        self.assertNotIn("甲😀乙。", (run_dir / "calls.jsonl").read_text(encoding="utf-8"))
        for row in records:
            if row["result_path"] is not None:
                load_result(str(run_dir / row["result_path"]))

    def test_research_run_output_failure_and_uncommitted_result(self):
        run_dir, _ = self.init_run()
        existing = self.root / "已存在.txt"
        existing.write_text("保留", encoding="utf-8")
        failed = self.span_process("--run", run_dir, "--output", existing,
                                   "read", "--chapter", "C0001", "--budget", "4")
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(existing.read_text(encoding="utf-8"), "保留")
        record = json.loads((run_dir / "calls.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((record["status"], record["failed_stage"]), ("failure", "output"))
        self.assertIsNotNone(record["result_path"])
        load_result(str(run_dir / record["result_path"]))

        loaded = load_run(run_dir)
        recorder = RunRecorder(loaded)
        args = build_parser().parse_args(["--text", str(self.source), "read", "--chapter", "C0001", "--as-of", "1"])
        recorder.save_result(run(args))
        orphan = run_dir / recorder.result_path
        self.assertTrue(orphan.is_file())
        self.assertEqual(len(loaded.records()), 1)
        overview = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(overview["scope"]["as_of"], 1)
        records = load_run(run_dir).records()
        self.assertEqual(len(records), 2)
        self.assertNotIn(str(orphan.relative_to(run_dir)).replace("\\", "/"),
                         [row["result_path"] for row in records])
        with (run_dir / "calls.jsonl").open("ab") as file:
            file.write(b"{broken\n")
        damaged = self.span_process("--run", run_dir, "overview", "--format", "json")
        self.assertNotEqual(damaged.returncode, 0)
        self.assertIn("日志第 3 行损坏", damaged.stderr)

    def test_research_run_resolution_and_independent_isolation(self):
        first, first_created = self.init_run("任务一")
        second, second_created = self.init_run("任务二")
        self.assertEqual(first_created["snapshot_id"], second_created["snapshot_id"])
        before_first = (first / "calls.jsonl").read_bytes()
        before_second = (second / "calls.jsonl").read_bytes()
        conflict = self.span_process("--run", first, "find", "灯", environ={"SPAN_RUN_DIR": str(second)})
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("不同运行", conflict.stderr)
        self.assertEqual((first / "calls.jsonl").read_bytes(), before_first)
        self.assertEqual((second / "calls.jsonl").read_bytes(), before_second)

        elsewhere = self.root / "另一目录"
        elsewhere.mkdir()
        inherited = self.span_json("find", "灯", "--examples", "0", "--format", "json",
                                   environ={"SPAN_RUN_DIR": str(first)}, cwd=elsewhere)
        self.assertEqual(inherited["scope"]["snapshot_id"], self.snapshot)
        discovered = self.span_json("overview", "--format", "json", cwd=second, environ={"SPAN_RUN_DIR": ""})
        self.assertEqual(discovered["scope"]["as_of"], 1)
        third_created = self.span_json("--text", self.source, "research", "init", self.root / "独立重做",
                                       "--as-of", "1", environ={"SPAN_RUN_DIR": str(first)})
        third = Path(third_created["run_directory"])
        self.assertNotEqual(third, first)
        self.assertEqual((third / "calls.jsonl").read_text(encoding="utf-8"), "")
        self.assertFalse((third / "当前稿.md").exists())
        self.assertEqual(len(load_run(first).records()), 1)
        self.assertEqual(len(load_run(second).records()), 1)

        missing_snapshot = self.span_process("--library", self.book, "research", "init",
                                             self.root / "书库任务", "--as-of", "1")
        self.assertNotEqual(missing_snapshot.returncode, 0)
        stored = self.span_json("--library", self.book, "research", "init", self.root / "书库任务",
                                "--snapshot", self.snapshot, "--as-of", "1")
        stored_manifest = json.loads((Path(stored["run_directory"]) / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(stored_manifest["source"]["kind"], "library")
        stored_find = self.span_json("--run", stored["run_directory"], "find", "灯", "--examples", "0")
        self.assertEqual(stored_find["scope"]["snapshot_id"], self.snapshot)
        self.assertEqual(stored_find["match_count"], 2)

    def test_research_run_single_writer_does_not_block_other_run(self):
        first, _ = self.init_run("任务一")
        second, _ = self.init_run("任务二")
        with run_write_lock(load_run(first)):
            blocked = self.span_process("--run", first, "find", "灯", "--examples", "0", "--format", "json")
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("另一个进程", blocked.stderr)
            other = self.span_json("--run", second, "find", "灯", "--examples", "0", "--format", "json")
            self.assertEqual(other["match_count"], 2)
        self.assertEqual((first / "calls.jsonl").read_bytes(), b"")
        self.assertEqual(len(load_run(second).records()), 1)
        following = self.span_json("--run", first, "find", "灯", "--examples", "0", "--format", "json")
        self.assertEqual(following["match_count"], 2)
        self.assertEqual(len(load_run(first).records()), 1)

    def test_research_run_output_copy_and_abrupt_exit(self):
        run_dir, _ = self.init_run()
        output = self.root / "运行输出.json"
        receipt = self.span_json("--run", run_dir, "--format", "json", "--output", output,
                                 "read", "--chapter", "C0001", "--budget", "8")
        self.assertEqual(receipt["output"], str(output))
        committed = load_run(run_dir).records()[0]
        self.assertEqual(committed["status"], "success")
        self.assertEqual(load_result(str(run_dir / committed["result_path"])),
                         json.loads(output.read_text(encoding="utf-8")))

        code = (
            "import os, sys\n"
            "from chapspan.cli import build_parser, run as execute\n"
            "from chapspan.runs import RunRecorder, load_run, run_write_lock\n"
            "loaded = load_run(sys.argv[1])\n"
            "with run_write_lock(loaded):\n"
            "    recorder = RunRecorder(loaded)\n"
            "    args = build_parser().parse_args(['--text', sys.argv[2], 'find', '灯', '--as-of', '1', '--examples', '0'])\n"
            "    result = execute(args)\n"
            "    recorder.save_result(result)\n"
            "    recorder.call(args, status='success', scope=args._actual_scope)\n"
            "os._exit(17)\n"
        )
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        crashed = subprocess.run([sys.executable, "-B", "-c", code, str(run_dir), str(self.source)],
                                 cwd=self.root, env=environment, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(crashed.returncode, 17, crashed.stderr)
        records = load_run(run_dir).records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1]["status"], "success")
        load_result(str(run_dir / records[-1]["result_path"]))
        recovered = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(recovered["scope"]["as_of"], 1)
        self.assertEqual(len(load_run(run_dir).records()), 3)

    def test_research_scope_expand_narrowing_and_snapshot_boundaries(self):
        run_dir, _ = self.init_run()
        denied = self.span_process("--run", run_dir, "read", "--chapter", "C0002", "--format", "json")
        self.assertNotEqual(denied.returncode, 0)
        expanded = self.span_json("--run", run_dir, "research", "expand", "--as-of", "2")
        self.assertEqual(expanded["previous_scope"]["as_of"], 1)
        self.assertEqual(expanded["scope"]["as_of"], 2)
        second = self.span_json("--run", run_dir, "read", "--chapter", "C0002", "--format", "json")
        self.assertEqual(second["fragments"][0]["chapter_title"], "第二章 后续")
        narrowed = self.span_json("--run", run_dir, "find", "灯", "--as-of", "1", "--examples", "0",
                                  "--format", "json")
        self.assertEqual(narrowed["scope"]["as_of"], 1)
        current = self.span_json("--run", run_dir, "overview", "--format", "json")
        self.assertEqual(current["scope"]["as_of"], 2)
        shrink = self.span_process("--run", run_dir, "research", "expand", "--as-of", "1")
        self.assertNotEqual(shrink.returncode, 0)
        self.assertIn("不能收窄", shrink.stderr)
        self.assertEqual(load_run(run_dir).current_scope().as_of, 2)
        peripheral = self.span_json("--run", run_dir, "research", "expand", "--as-of", "3",
                                    "--include-peripheral")
        self.assertTrue(peripheral["scope"]["include_peripheral"])
        third = self.span_json("--run", run_dir, "read", "--chapter", "C0003", "--format", "json")
        self.assertEqual(third["fragments"][0]["text"], "外围材料\n")
        self.assertEqual(len([row for row in load_run(run_dir).records() if row["event"] == "scope_expand"]), 2)

        shifted_created = self.span_json("--text", self.source, "research", "init", self.root / "起始章任务",
                                         "--from-chapter", "2", "--as-of", "2")
        shifted = Path(shifted_created["run_directory"])
        self.assertEqual(shifted_created["scope"]["first_chapter"], 2)
        earlier = self.span_json("--run", shifted, "research", "expand", "--from-chapter", "1")
        self.assertEqual(earlier["scope"]["first_chapter"], 1)
        first = self.span_json("--run", shifted, "read", "--chapter", "C0001", "--format", "json")
        self.assertEqual(first["scope"]["first_chapter"], 1)

        self.source.write_text("第一章 开门\n内容已经改变。\n第二章 后续\n秘密灯亮了。\n", encoding="utf-8")
        changed = self.span_process("--run", run_dir, "find", "灯", "--examples", "0", "--format", "json")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("快照", changed.stderr)
        without_run = self.span_process("research", "expand", "--as-of", "2")
        self.assertNotEqual(without_run.returncode, 0)
        self.assertIn("需要", without_run.stderr)

    def test_citation_occurrences_keep_lines_paths_and_code_exclusions(self):
        markdown = (
            "文首。[@C0001:0-1]\n"
            "# 早期\n"
            "## 关键事件\n"
            "同一。[@C0001:0-2] 和 [@C0001:0-2]\n"
            "`[@C0002:0-9]`\n"
            "```text\n[@C0002:0-8]\n# 假标题\n```\n"
            "### 后续\n跨行 `代码\n[@C0002:0-7]\n结束` 正文 [@C0002:1-2]\n"
            "# 早期\n## 关键事件\n同名另一处。[@C0003:0-1]\n"
            "集合标题\n---\n集合内。[@C0003:1-2]\n"
        )
        occurrences = list(citation_occurrences(markdown, first_line=5))
        self.assertEqual([row["line"] for row in occurrences], [5, 8, 8, 17, 20, 23])
        self.assertEqual(occurrences[0]["heading_path"], ())
        self.assertEqual(occurrences[1]["heading_path"], ("早期", "关键事件"))
        self.assertEqual(occurrences[3]["heading_path"], ("早期", "关键事件", "后续"))
        self.assertEqual(occurrences[4]["heading_path"], ("早期", "关键事件"))
        self.assertNotEqual(occurrences[1]["heading_line"], occurrences[4]["heading_line"])
        self.assertEqual(occurrences[5]["heading_path"], ("早期", "关键事件"))
        self.assertNotIn("[@C0002:0-9]", [row["citation"] for row in occurrences])
        self.assertNotIn("[@C0002:0-8]", [row["citation"] for row in occurrences])
        self.assertNotIn("[@C0002:0-7]", [row["citation"] for row in occurrences])

    def test_material_index_two_views_coverage_and_draft_authority(self):
        run_dir, _ = self.init_run("索引任务", as_of="2")
        self.span_json("--run", run_dir, "read", "C0001:0-4", "--format", "json")
        self.span_json("--run", run_dir, "find", "灯", "--examples", "1", "--format", "json")
        other = import_txt(self.book, self.source, paragraphs="blank-lines")["snapshot_id"]
        old_citation = Span(1, 0, 1).source_citation(other)
        draft = run_dir / "当前稿.md"
        draft.write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 2\n---\n"
            "文首。[@C0001:0-2]\n"
            "# 早期\n## 关键事件\n"
            "部分。[@C0001:0-6]\n再次。[@C0001:0-2]\n未交付。[@C0002:0-2]\n"
            "`[@C0002:3-4]`\n```text\n[@C0002:4-5]\n```\n"
            "# 后期 | 回看\n错误。[@未补|示例]\n旧版。" + old_citation + "\n",
            encoding="utf-8")
        notes = run_dir / "研究笔记.md"
        notes.write_text("手工笔记", encoding="utf-8")
        generated = self.span_process("--run", run_dir, "research", "index")
        self.assertEqual(generated.returncode, 0, generated.stderr)
        self.assertNotIn("| 章 |", generated.stdout)
        receipt = json.loads(generated.stdout)
        self.assertEqual(receipt["citations"], 6)
        self.assertEqual(receipt["coverage"], {"covered": 2, "partial": 1, "none": 1})
        self.assertEqual(receipt["chapters"], 2)
        self.assertTrue(receipt["current_draft"])
        content = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("## 按原文章序", content)
        self.assertIn("## 按当前稿标题", content)
        self.assertIn("文首（首个 ATX 标题之前）", content)
        self.assertIn("早期 / 关键事件", content)
        self.assertIn("后期 | 回看", content)
        self.assertIn("[@未补\\|示例]", content)
        self.assertIn("快照不符", content)
        first_row = next(line for line in content.splitlines() if line.startswith("| C0001 "))
        second_row = next(line for line in content.splitlines() if line.startswith("| C0002 "))
        self.assertIn("指定读取 1 次", first_row)
        self.assertIn("检索例段 1 次", first_row)
        self.assertIn("交付覆盖 2", first_row)
        self.assertIn("部分覆盖 1", first_row)
        self.assertIn("无交付记录 1", second_row)
        self.assertIn("`read --chapter C0002`", second_row)
        self.assertNotIn("甲😀乙。", content)
        self.assertNotIn("灯亮了。", content)
        before_draft = draft.read_bytes()
        before_notes = notes.read_bytes()
        result_count = len(list((run_dir / "results").glob("*.json")))
        draft.write_text(draft.read_text(encoding="utf-8").replace("# 早期", "# 调整"), encoding="utf-8")
        rebuilt = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(rebuilt["citations"], 6)
        self.assertIn("调整 / 关键事件", (run_dir / "材料索引.md").read_text(encoding="utf-8"))
        self.assertEqual(draft.read_text(encoding="utf-8").startswith("---"), True)
        self.assertNotEqual(before_draft, draft.read_bytes())
        self.assertEqual(notes.read_bytes(), before_notes)
        self.assertEqual(len(list((run_dir / "results").glob("*.json"))), result_count)
        index_records = [row for row in load_run(run_dir).records() if row["command"] == "research index"]
        self.assertEqual(len(index_records), 2)
        self.assertTrue(all(row["status"] == "success" and row["result_path"] is None for row in index_records))

    def test_material_index_excludes_failed_orphan_and_prior_run_delivery(self):
        first, _ = self.init_run("上一运行", as_of="2")
        page = self.span_process("--run", first, "read", "--chapter", "C0001", "--budget", "4")
        self.assertEqual(page.returncode, 0, page.stderr)
        state_id = re.search(r"state_[0-9a-f]{24}", page.stdout).group()
        second, _ = self.init_run("下一运行", as_of="2")
        continued = self.span_json("--run", second, "read", "--from-result",
                                   first / "states" / f"{state_id}.json", "--budget", "4", "--format", "json")
        self.assertEqual(continued["fragments"][0]["read_span"], "C0001:4-6")
        self.assertIn("C0001:0-6", continued["delivery"][0]["read_spans"])
        existing = self.root / "不覆盖.txt"
        existing.write_text("保留", encoding="utf-8")
        failed = self.span_process("--run", second, "--format", "json", "--output", existing,
                                   "read", "--chapter", "C0002")
        self.assertNotEqual(failed.returncode, 0)
        (second / "results" / "orphan.json").write_text("{}", encoding="utf-8")
        receipt = self.span_json("--run", second, "research", "index")
        self.assertEqual(receipt["ignored"], {"unregistered_results": 1})
        self.assertEqual(receipt["citations"], 0)
        content = (second / "材料索引.md").read_text(encoding="utf-8")
        row = next(line for line in content.splitlines() if line.startswith("| C0001 "))
        self.assertIn("[@C0001:4-6]", row)
        self.assertNotIn("[@C0001:0-4]", row)
        self.assertNotIn("[@C0001:0-6]", row)
        self.assertIn("续读 1 次", row)
        self.assertFalse(any(line.startswith("| C0002 ") for line in content.splitlines()))

    def test_external_report_can_be_checked_and_reread_by_section(self):
        run_dir, created = self.init_run("改稿任务", as_of="2")
        self.assertEqual(created["draft_header"],
                         f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 2\n---\n")
        self.span_json("--run", run_dir, "read", "C0001:0-4", "--format", "json")
        draft = self.root / "外部 研究报告.md"
        repeated = "判定。[@C0001:0-2]\n" * 25
        draft.write_text("# 报告\n## 事实\n" + repeated + "### 子节\n补查。[@C0001:0-6]\n"
                         "## 其他\n" + "旁白" * 13_000 + " [@C0002:0-2]\n", encoding="utf-8")
        before = draft.read_bytes()
        full = self.span_json("--run", run_dir, "research", "index", "--draft", draft, "--draft-horizon", "2")
        self.assertEqual(full["draft"], str(draft.resolve()))
        self.assertEqual(full["citations"], 27)
        index = Path(full["index"])
        index_before = index.read_bytes()
        args = ("--run", run_dir, "research", "index", "--draft", draft, "--draft-horizon", "2", "--section", "2")
        first = self.span_json(*args)
        self.assertEqual(first["citation_count"], 26)
        self.assertEqual(first["next_offset"], 24)
        self.assertEqual(first["read_spans"], ["C0001:0-2"])
        second = self.span_json(*args, "--offset", first["next_offset"])
        self.assertEqual(second["section"], {"line": 2, "heading_path": ["报告", "事实"]})
        self.assertEqual([row["line"] for row in second["citations"]], [27, 29])
        self.assertEqual([row["coverage"] for row in second["citations"]], ["covered", "partial"])
        self.assertIsNone(second["next_offset"])
        read = self.span_json("--run", run_dir, "read", *second["read_spans"], "--format", "json")
        self.assertEqual(read["fragments"][0]["read_span"], "C0001:0-6")
        resumed = self.span_json("--run", run_dir, "resume", draft, "--draft-horizon", "2", "--citations-only")
        self.assertNotIn("markdown", resumed)
        self.assertEqual(len(resumed["citations"]), 3)
        self.assertEqual(draft.read_bytes(), before)
        self.assertEqual(index.read_bytes(), index_before)

        recorded = next(row for row in load_run(run_dir).records() if row["command"] == "read")
        (run_dir / recorded["result_path"]).unlink()
        self.assertEqual(self.span_json(*args)["ignored"], {"unreadable_results": 1})

        draft.write_text(draft.read_text(encoding="utf-8") + "[@C0003:0-2]\n", encoding="utf-8")
        failed = self.span_process(*args)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("引用超过当前阅读上限", failed.stderr)
        draft.write_text(created["draft_header"] + "# 稿\n", encoding="utf-8")
        override = self.span_process("--run", run_dir, "resume", draft, "--draft-horizon", "1")
        self.assertIn("已有 YAML 文件头", override.stderr)
        missing = self.span_process("--run", run_dir, "research", "index", "--draft", self.root / "缺失.md")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("稿件不存在", missing.stderr)

    def test_report_inline_citations_require_opt_in_and_keep_scope(self):
        run_dir, _ = self.init_run("行内引用")
        draft = self.root / "反引号稿.md"
        draft.write_text("# 主题\n甲 `[@C0001:0-1] [@C0001:1-2]`\n"
                         "乙 `read [@C0002:0-1]`\n```text\n[@C0002:0-1]\n```\n", encoding="utf-8")
        arguments = ("--run", run_dir, "resume", draft, "--draft-horizon", "1", "--citations-only")
        self.assertEqual(self.span_json(*arguments)["citations"], [])
        checks = self.span_json(*arguments, "--include-inline-citations")["citations"]
        self.assertEqual([row["citation"] for row in checks], ["[@C0001:0-1]", "[@C0001:1-2]"])
        self.assertTrue(all(row["status"] == "ok" for row in checks))
        indexed = self.span_json("--run", run_dir, "research", "index", "--draft", draft, "--draft-horizon", "1",
                                 "--section", "1", "--include-inline-citations")
        self.assertEqual(indexed["read_spans"], ["C0001:0-1", "C0001:1-2"])
        self.assertEqual([row["line"] for row in indexed["citations"]], [2, 2])
        draft.write_text(draft.read_text(encoding="utf-8") + "甲 `[@C0002:0-1]`\n", encoding="utf-8")
        failed = self.span_process(*arguments, "--include-inline-citations")
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("引用超过当前阅读上限", failed.stderr)

        unfinished = "[@" * 100_000 + "\n有效。[@C0001:0-1]\n"
        self.assertEqual([row["citation"] for row in citation_occurrences(unfinished)], ["[@C0001:0-1]"])

    def test_material_index_filters_other_snapshot_and_out_of_scope_results(self):
        run_dir, _ = self.init_run("筛选索引")
        self.span_json("--run", run_dir, "read", "C0001:0-2", "--format", "json")
        loaded = load_run(run_dir)
        with run_write_lock(loaded):
            recorder = RunRecorder(loaded)
            other_args = build_parser().parse_args(["--text", str(self.source), "--paragraphs", "blank-lines",
                                                    "read", "C0001:0-2", "--as-of", "1"])
            other_result = run(other_args)
            recorder.save_result(other_result)
            recorder.call(other_args, status="success", scope=other_args._actual_scope)
            outside_args = build_parser().parse_args(["--text", str(self.source), "read", "C0002:0-2",
                                                      "--as-of", "2"])
            outside_result = run(outside_args)
            recorder.save_result(outside_result)
            recorder.call(outside_args, status="success", scope=outside_args._actual_scope)
        receipt = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(receipt["ignored"], {"other_snapshot_results": 1, "out_of_scope_results": 1})
        self.assertEqual(receipt["chapters"], 1)
        content = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("其他快照结果 1", content)
        self.assertIn("超范围结果 1", content)
        self.assertFalse(any(line.startswith("| C0002 ") for line in content.splitlines()))

    def test_material_index_empty_invalid_draft_and_unreadable_result(self):
        run_dir, _ = self.init_run("空索引")
        empty = self.span_json("--run", run_dir, "research", "index")
        self.assertFalse(empty["current_draft"])
        self.assertEqual(empty["chapters"], 0)
        self.assertEqual(empty["delivery_characters"], 0)
        path = run_dir / "材料索引.md"
        self.assertIn("当前稿未创建", path.read_text(encoding="utf-8"))
        before = path.read_bytes()
        (run_dir / "当前稿.md").write_text("没有文件头", encoding="utf-8")
        invalid = self.span_process("--run", run_dir, "research", "index")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("YAML", invalid.stderr)
        self.assertEqual(path.read_bytes(), before)
        failure = load_run(run_dir).records()[-1]
        self.assertEqual((failure["command"], failure["status"], failure["failed_stage"]),
                         ("research index", "failure", "execute"))
        (run_dir / "当前稿.md").unlink()
        self.span_json("--run", run_dir, "read", "C0001:0-2", "--format", "json")
        record = next(row for row in load_run(run_dir).records() if row["result_path"] is not None)
        (run_dir / record["result_path"]).unlink()
        unreadable = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(unreadable["ignored"], {"unreadable_results": 1})
        self.assertEqual(unreadable["chapters"], 0)
        no_run = self.span_process("research", "index")
        self.assertNotEqual(no_run.returncode, 0)

    def test_research_conventions_roundtrip_precedence_conflict_and_index(self):
        conventions = {
            "purpose": '核实：第一行"因果"\n第二行：\'补充\'',
            "audience": "网文追更读者",
            "evaluation_framework": "叙事与节奏：看铺垫",
            "external_policy": "先文本取材，禁止提前搜索剧透",
            "version_note": "连载修订版（含章末感言）",
        }
        created = self.span_json(
            "--text", self.source, "research", "init", self.root / "约定任务",
            "--as-of", "2",
            "--purpose", conventions["purpose"],
            "--audience", conventions["audience"],
            "--evaluation-framework", conventions["evaluation_framework"],
            "--external-policy", conventions["external_policy"],
            "--version-note", conventions["version_note"],
        )
        run_dir = Path(created["run_directory"])
        self.assertEqual(created["conventions"], conventions)
        self.assertEqual(load_run(run_dir).conventions, conventions)

        # Round-trip special characters (Chinese, colons, quotes, newlines) through draft_header & read_draft
        probe_draft = self.root / "probe.md"
        probe_draft.write_text(created["draft_header"] + "# 标题\n正文\n", encoding="utf-8")
        parsed_probe = read_draft(probe_draft)
        self.assertEqual(parsed_probe["conventions"], conventions)

        # No current draft yet: research index echoes run conventions
        no_draft_index = self.span_json("--run", run_dir, "research", "index")
        self.assertFalse(no_draft_index["current_draft"])
        self.assertEqual(no_draft_index["conventions"], {
            "effective": conventions,
            "sources": {key: "run" for key in conventions},
            "conflicts": [],
        })
        index_md_no_draft = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("## 研究约定", index_md_no_draft)
        self.assertIn("网文追更读者", index_md_no_draft)

        # Draft omits `audience` (inherits run), overrides `evaluation_framework` (reports conflict),
        # and carries an arbitrary unknown YAML key that must NOT pass through.
        self.span_json("--run", run_dir, "read", "C0001:0-4", "--format", "json")
        override_header = draft_header(
            self.snapshot, 2,
            conventions={
                "purpose": conventions["purpose"],
                "evaluation_framework": "改为：作者技法拆解",
                "external_policy": conventions["external_policy"],
                "version_note": conventions["version_note"],
            },
        )
        injected = override_header.replace("---\n", "---\ncustom_ignored_key: should_not_leak\n", 1)
        draft_path = run_dir / "当前稿.md"
        draft_path.write_text(injected + "# 第一节\n分析。[@C0001:0-4]\n", encoding="utf-8")

        resumed = self.span_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(resumed["scope"], {"snapshot_id": self.snapshot, "as_of": 2, "first_chapter": 1, "include_peripheral": False})
        self.assertEqual(resumed["citation_count"], 1)
        self.assertNotIn("custom_ignored_key", resumed["conventions"]["effective"])
        self.assertEqual(resumed["conventions"]["effective"]["audience"], "网文追更读者")
        self.assertEqual(resumed["conventions"]["sources"]["audience"], "run")
        self.assertEqual(resumed["conventions"]["effective"]["evaluation_framework"], "改为：作者技法拆解")
        self.assertEqual(resumed["conventions"]["sources"]["evaluation_framework"], "draft")
        self.assertEqual(resumed["conventions"]["conflicts"], [{
            "field": "evaluation_framework",
            "draft": "改为：作者技法拆解",
            "run": "叙事与节奏：看铺垫",
        }])

        # research index and --section echo merged conventions & render conflict in 材料索引.md
        full_index = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(full_index["conventions"], resumed["conventions"])
        index_md = (run_dir / "材料索引.md").read_text(encoding="utf-8")
        self.assertIn("改为：作者技法拆解", index_md)
        self.assertIn("运行原设：叙事与节奏：看铺垫", index_md)
        self.assertIn("来源：运行", index_md)

        sec_line = draft_path.read_text(encoding="utf-8").splitlines().index("# 第一节") + 1
        sec_index = self.span_json("--run", run_dir, "research", "index", "--section", str(sec_line))
        self.assertEqual(sec_index["conventions"], resumed["conventions"])

        # Standalone resume only reads draft header (does not inherit run conventions)
        standalone = self.span_json("--text", self.source, "resume", draft_path, "--as-of", "2", "--citations-only")
        self.assertNotIn("audience", standalone["conventions"]["effective"])
        self.assertEqual(standalone["conventions"]["sources"]["evaluation_framework"], "draft")
        self.assertEqual(standalone["conventions"]["conflicts"], [])

    def test_research_conventions_legacy_compat_validation_and_service_api(self):
        # Legacy run without conventions key in run.json + legacy draft without convention keys
        run_dir, created = self.init_run("兼容任务", as_of="2")
        self.assertEqual(created["conventions"], {})
        manifest_path = run_dir / "run.json"
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertNotIn("conventions", raw_manifest)
        self.assertEqual(load_run(run_dir).conventions, {})

        draft_path = run_dir / "当前稿.md"
        draft_path.write_text(created["draft_header"] + "# 旧稿\n引文。[@C0001:0-2]\n", encoding="utf-8")
        resumed = self.span_json("--run", run_dir, "resume", "--citations-only")
        self.assertEqual(resumed["conventions"], {"effective": {}, "sources": {}, "conflicts": []})
        indexed = self.span_json("--run", run_dir, "research", "index")
        self.assertEqual(indexed["conventions"], {"effective": {}, "sources": {}, "conflicts": []})

        # Explicit-parameter service function (without argparse.Namespace) works directly
        outcome = init_research_run(
            self.root / "API任务",
            as_of=2,
            text=self.source,
            conventions={"purpose": "API 调用验证"},
        )
        self.assertEqual(outcome.result["conventions"], {"purpose": "API 调用验证"})

        # Validation rejects blank, over-long, and non-string YAML values
        blank = self.span_process("--text", self.source, "research", "init", self.root / "bad1", "--as-of", "2", "--purpose", "   ")
        self.assertNotEqual(blank.returncode, 0)
        too_long = self.span_process("--text", self.source, "research", "init", self.root / "bad2", "--as-of", "2", "--audience", "字" * 501)
        self.assertNotEqual(too_long.returncode, 0)

        bad_draft = self.root / "bad_yaml_type.md"
        bad_draft.write_text(f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 2\npurpose: 123\n---\n# 稿\n", encoding="utf-8")
        bad_resume = self.span_process("--text", self.source, "resume", bad_draft, "--as-of", "2")
        self.assertNotEqual(bad_resume.returncode, 0)
        self.assertIn("purpose", bad_resume.stderr)

        bool_draft = self.root / "bad_yaml_bool.md"
        bool_draft.write_text(f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 2\nexternal_policy: true\n---\n# 稿\n", encoding="utf-8")
        self.assertNotEqual(self.span_process("--text", self.source, "resume", bool_draft, "--as-of", "2").returncode, 0)

        # Explicit YAML null behaves as undeclared and falls back to run convention
        run_with_null_test = self.span_json(
            "--text", self.source, "research", "init", self.root / "null_fallback",
            "--as-of", "2", "--purpose", "运行默认用途",
        )
        null_run_dir = Path(run_with_null_test["run_directory"])
        (null_run_dir / "当前稿.md").write_text(
            f"---\nsnapshot_id: {self.snapshot}\nknowledge_horizon: 2\npurpose: null\n---\n# 稿\n引。[@C0001:0-2]\n",
            encoding="utf-8",
        )
        null_resumed = self.span_json("--run", null_run_dir, "resume", "--citations-only")
        self.assertEqual(null_resumed["conventions"], {
            "effective": {"purpose": "运行默认用途"},
            "sources": {"purpose": "run"},
            "conflicts": [],
        })


class WindowTests(unittest.TestCase):
    def test_minimal_windows_match_exhaustive_oracle(self):
        for lines in itertools.product(("a", "b", "ab", "x"), repeat=4):
            text = "\n".join(lines)
            ends = paragraph_ends(text, "lines")
            chapter = Chapter(1, "测试", "body", 1, text, ends)
            for terms in ((["a"], ["b"], ["x"]), (["a\nb"], [], ["x"]), ([], ["ab", "x"], [])):
                all_terms, any_terms, exclude = terms
                for near in (1, 2, 4):
                    candidates = []
                    for index, start in enumerate((0, *ends[:-1])):
                        for end in ends[index:index + near]:
                            part = text[start:end]
                            if (all(t in part for t in all_terms)
                                    and (not any_terms or any(t in part for t in any_terms))
                                    and not any(t in part for t in exclude)):
                                candidates.append((start, end))
                    expected = [(a, b) for a, b in candidates if not any(
                        c >= a and d <= b and (a, b) != (c, d) for c, d in candidates
                    )]
                    self.assertEqual(list(minimal_windows(chapter, *terms, near)), expected)
