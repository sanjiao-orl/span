import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from chapspan.cli import build_parser, execute_command, run
from chapspan.display import format_text
from chapspan.library import import_txt
from chapspan.model import Chapter, Scope, Span
from chapspan.reading import read_passages
from chapspan.runs import call_origin
from chapspan.search import find_many, find_passages
from chapspan.sources import ScopedText, paragraph_ends


class FindChapterViewTests(unittest.TestCase):
    def setUp(self):
        bodies = {
            23: "a😀锚锚灯。\n灯坏。\n灯灯。\n",
            24: "灯。\n",
            27: "锚灯。\n",
            29: "雨灯。\n",
            30: "灯。\n",
        }
        self.chapters = []
        for number in range(1, 31):
            body = bodies.get(number, "无。\n")
            kind = "peripheral" if number == 24 else "body"
            self.chapters.append(Chapter(number, f"第{number}章 章题", kind, 1, body, paragraph_ends(body, "lines")))
        self.library = ScopedText(self.chapters, Scope("snap_test", 30))

    def test_late_sparse_hits_and_paging_count_chapters(self):
        options = {"all_terms": ["锚"], "limit": 1}
        first = find_passages(self.library, **options, hit_chapters=True)
        self.assertEqual(first["hit_chapters"], [
            {"chapter": 23, "title": "第23章 章题", "kind": "body", "match_count": 2, "read_span": "C0023:2-3"},
        ])
        self.assertEqual((first["match_count"], first["hit_chapter_count"], first["next_distribution_offset"]),
                         (3, 2, 1))
        next_page = find_passages(self.library, **options, hit_chapters=True,
                                  distribution_offset=first["next_distribution_offset"])
        self.assertEqual(next_page["hit_chapters"][0]["chapter"], 27)
        self.assertIsNone(next_page["next_distribution_offset"])
        self.assertTrue({"distribution", "matches", "next_offset", "examples"}.isdisjoint(first))

        full = find_passages(self.library, **options)
        self.assertEqual(full["distribution"][0]["chapter"], 1)
        self.assertEqual(full["distribution"][0]["match_count"], 0)
        self.assertEqual(full["matches"][0]["span"], "C0023:2-3")
        self.assertEqual(full["next_distribution_offset"], 1)
        self.assertNotIn("hit_chapters", full)

    def test_zero_distribution_text_keeps_gaps_and_distinguishes_page_from_scope(self):
        page = find_passages(self.library, all_terms=["锚"])
        text = format_text({"scope": vars(self.library.scope), **page}, distribution=True)
        self.assertIn("C0001–C0020 · 无匹配", text)
        self.assertIn("--hit-chapters", text)
        self.assertNotIn("本页匹配序号", text)
        absent = find_passages(self.library, all_terms=["未出现"], distribution_offset=20)
        text = format_text({"scope": vars(self.library.scope), **absent}, distribution=True)
        self.assertIn('当前范围内未找到 "未出现"', text)
        self.assertIn("C0021–C0023 · 无匹配", text)
        self.assertIn("C0025–C0030 · 无匹配", text)
        self.assertIn("跳过的章号未计入零值", text)

    def test_near_exclusion_and_chapter_conditions_keep_matching_units(self):
        conditions = {"all_terms": ["灯"], "any_terms": ["雨", "锚"], "exclude": ["坏"]}
        chapter = find_passages(self.library, **conditions, hit_chapters=True)
        self.assertEqual([(row["chapter"], row["match_count"]) for row in chapter["hit_chapters"]], [(27, 1), (29, 1)])
        self.assertEqual(chapter["hit_chapters"][0]["read_span"], "C0027:1-2")

        near = find_passages(self.library, **conditions, near=1, hit_chapters=True)
        self.assertEqual([(row["chapter"], row["match_count"]) for row in near["hit_chapters"]],
                         [(23, 1), (27, 1), (29, 1)])
        span = Span.parse(near["hit_chapters"][0]["read_span"])
        self.assertEqual(read_passages(self.library, [span])["fragments"][0]["text"], "a😀锚锚灯。\n")

        near_repeat = find_passages(self.library, all_terms=["灯"], near=1, hit_chapters=True)
        self.assertEqual(near_repeat["hit_chapters"][0]["match_count"], 3)
        self.assertEqual(near_repeat["term_counts"], [{"term": "灯", "count": 7}])

    def test_scope_empty_page_and_zero_matches(self):
        library = ScopedText(self.chapters, Scope("snap_test", 29, 24))
        result = find_passages(library, all_terms=["灯"], hit_chapters=True)
        self.assertEqual([row["chapter"] for row in result["hit_chapters"]], [27, 29])
        result = find_passages(library, all_terms=["灯"], hit_chapters=True, distribution_offset=3)
        self.assertEqual(result["hit_chapters"], [])
        self.assertEqual(result["hit_chapter_count"], 2)
        self.assertIsNone(result["next_distribution_offset"])
        result = find_passages(library, all_terms=["坏"], hit_chapters=True)
        self.assertEqual(result["hit_chapters"], [])
        self.assertEqual(result["hit_chapter_count"], 0)
        self.assertIsNone(result["next_distribution_offset"])

    def test_grouped_offsets_are_independent(self):
        groups = [
            {"all": ["灯"], "limit": 1, "distribution_offset": 1},
            {"any": ["雨", "锚"], "limit": 2},
            {"all": ["坏"], "distribution_offset": 1},
        ]
        batch = find_many(self.library, groups, hit_chapters=True)
        self.assertEqual(set(batch), {"queries"})
        self.assertEqual([[row["chapter"] for row in result["hit_chapters"]] for result in batch["queries"]],
                         [[27], [23, 27], []])
        self.assertEqual([result["next_distribution_offset"] for result in batch["queries"]], [2, 2, None])
        with self.assertRaisesRegex(ValueError, "distribution_offset"):
            find_many(self.library, [{"all": ["灯"], "offset": 1}], hit_chapters=True)

    def test_cli_without_examples_and_coordinate_handoff(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "书.txt"
            source.write_text("第一章 起\n起。\n第二章 灯\n灯灯。\n", encoding="utf-8")
            parser = build_parser()
            base = ["--text", str(source), "find", "灯", "--as-of", "all", "--hit-chapters"]
            args = parser.parse_args(base)
            result = run(args)
            self.assertNotIn("examples", result)
            self.assertEqual(call_origin(args), "find")
            self.assertEqual(run(parser.parse_args([*base, "--examples", "0"])), result)
            for options, message in [(["--compact"], "--compact"), (["--distribution"], "--distribution"),
                                     (["--spread"], "--spread"), (["--examples", "1"], "--examples"),
                                     (["--offset", "1"], "--distribution-offset")]:
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, message):
                    run(parser.parse_args([*base, *options]))

            saved = root / "命中章.json"
            saved.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            read = ["--text", str(source), "read", "--from-result", str(saved)]
            selected = run(parser.parse_args([*read, "--pick", "1"]))
            self.assertEqual(selected["fragments"][0]["selection_origins"], ["hit_chapter_anchor"])
            passage = run(parser.parse_args([*read, result["hit_chapters"][0]["read_span"]]))
            self.assertEqual(passage["fragments"][0]["text"], "灯")

    def test_saved_page_picks_use_only_current_hit_chapter_rows(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "书.txt"
            source.write_text("第一章 起\n起。\n第二章 次\n甲灯灯。\n第三章 末\n乙灯。\n", encoding="utf-8")
            library = root / "书库"
            snapshot = import_txt(library, source)["snapshot_id"]
            parser = build_parser()
            store = ["--library", str(library)]
            find = [*store, "find", "灯", "--snapshot", snapshot, "--as-of", "3", "--hit-chapters",
                    "--limit", "1", "--distribution-offset", "1"]

            for format_, suffix in [("json", "json"), ("text", "txt")]:
                with self.subTest(format=format_):
                    path = root / f"命中章.{suffix}"
                    execute_command(parser.parse_args([*find, "--format", format_, "--output", str(path)]))
                    if format_ == "text":
                        companion = json.loads((root / "命中章.txt.state.json").read_text(encoding="utf-8"))
                        self.assertEqual(companion["hit_chapters"], [{"read_span": "C0003:1-2"}])
                        self.assertNotIn("matches", companion)

                    previous = ["--from-result", str(path)]
                    read = [*store, "read", *previous]
                    with self.assertRaisesRegex(ValueError, "条目序号"):
                        run(parser.parse_args([*read, "--pick", "2"]))
                    with self.assertRaisesRegex(ValueError, "不可读取"):
                        run(parser.parse_args([*read, "--pick", "1", "--as-of", "2"]))
                    selected = run(parser.parse_args([*read, "--pick", "1"]))
                    self.assertEqual(selected["fragments"][0]["text"].strip(), "乙灯。")
                    self.assertEqual(selected["fragments"][0]["match_spans"], ["C0003:1-2"])
                    self.assertEqual(selected["fragments"][0]["selection_origins"], ["hit_chapter_anchor"])

                    collect = [*store, "collect", "add", format_, *previous]
                    with self.assertRaisesRegex(ValueError, "--pick"):
                        run(parser.parse_args(collect))
                    item, = run(parser.parse_args([*collect, "--pick", "1"]))["items"]
                    self.assertEqual(item["span"], "C0003:1-2")
                    self.assertEqual(item["selection_origins"], ["hit_chapter_anchor"])

                    grouped = root / f"多组命中章.{suffix}"
                    grouped_find = [*store, "find", "--snapshot", snapshot, "--as-of", "3", "--hit-chapters",
                                    "--query", '{"all":["灯"]}', "--query", '{"all":["乙"]}']
                    execute_command(parser.parse_args([*grouped_find, "--format", format_, "--output", str(grouped)]))
                    previous = ["--from-result", str(grouped)]
                    with self.assertRaisesRegex(ValueError, "多组查询的 --pick"):
                        run(parser.parse_args([*store, "read", *previous, "--pick", "1"]))
                    with self.assertRaises(ValueError):
                        run(parser.parse_args([*store, "collect", "add", format_, *previous, "--pick", "1"]))
                    explicit = run(parser.parse_args([*store, "read", *previous, "C0003:1-2"]))
                    self.assertEqual(explicit["fragments"][0]["text"], "灯")


if __name__ == "__main__":
    unittest.main()
