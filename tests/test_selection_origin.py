import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path

from chapspan.cli import build_parser, main, run
from chapspan.display import format_text
from chapspan.handoff import handoff_token, save_state, save_text_result, write_state_file
from chapspan.library import DB_NAME, import_txt
from chapspan.model import Scope, Span
from chapspan.runs import create_run


class SelectionOriginTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.book = self.root / "来历.txt"
        self.book.write_text("第一章 早期\n前段。\n钥匙接着是一段较长的内容。\n后段。\n"
                             "第二章 中期\n这里也提到钥匙。\n"
                             "第三章 未来标题\n钥匙后接未来正文。\n", encoding="utf-8")
        self.library = self.root / "书库"
        self.snapshot = import_txt(self.library, self.book)["snapshot_id"]
        self.parser = build_parser()

    def call(self, *args):
        return run(self.parser.parse_args(["--library", str(self.library), *map(str, args)]))

    def find(self, *args):
        return self.call("find", "钥匙", "--snapshot", self.snapshot, "--as-of", "3", *args)

    def save_json(self, name, result):
        path = self.root / name
        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return path

    def test_find_example_origins_work_across_result_formats(self):
        cases = (((), 2, "match_order"), (("--spread",), 1, "first_match"),
                 (("--spread",), 3, "even_hit_chapters"))
        for options, count, origin in cases:
            with self.subTest(origin=origin):
                found = self.find(*options, "--examples", count)
                self.assertEqual(found["examples"]["selection_origin"], origin)
                state = save_state(found, self.library / "states")
                text = self.root / f"{origin}.txt"
                text.write_text(save_text_result(found, format_text(found, saved=True), state.state_id),
                                encoding="utf-8")
                write_state_file(text.with_name(text.name + ".state.json"), state.raw)
                for source in (self.save_json(f"{origin}.json", found), text, state.state_id, handoff_token(found)):
                    read = self.call("read", "--from-result", source, "--example", 1, "--before", 1, "--as-of", 1)
                    self.assertEqual(read["fragments"][0]["selection_origins"], [origin])
                    self.assertIn("选材", format_text(read))
                    output = json.dumps(read, ensure_ascii=False)
                    self.assertNotIn("未来", output)
                    self.assertNotIn("C0003", output)

    def test_multi_query_continuation_only_exposes_delivered_reasons(self):
        found = self.call("find", "--snapshot", self.snapshot, "--as-of", 3, "--spread", "--examples", 2,
                          "--query", '{"all":["钥匙"]}', "--query", '{"all":["前段"]}', "--budget", 4)
        previous = save_state(found, self.library / "states").state_id
        example = self.call("read", "--from-result", previous, "--example", "Q2E1", "--as-of", 1)
        self.assertEqual(example["fragments"][0]["selection_origins"], ["first_match"])
        page = self.call("read", "--from-result", previous, "--continue-group", "Q1E1", "--as-of", 1)
        self.assertEqual(page["groups"][0]["fragments"][0]["selection_origins"], ["even_hit_chapters"])
        self.assertNotIn("Q1E2", json.dumps(page))
        self.assertNotIn("C0003", json.dumps(page))

    def test_mixed_picks_collection_and_legacy_library(self):
        match = self.find("--examples", 0)
        saved = save_state(match, self.library / "states").state_id
        picked = self.call("read", "C0002:0-2", "--from-result", saved, "--pick", 1, "--as-of", 2)
        first, manual = picked["fragments"]
        self.assertEqual(first["selection_origins"], ["match_order"])
        self.assertNotIn("selection_origins", manual)

        scope = ("--snapshot", self.snapshot, "--as-of", 2)
        self.call("collect", "add", "旧收藏", manual["read_span"], *scope)
        with closing(sqlite3.connect(self.library / DB_NAME)) as connection, connection:
            connection.execute("ALTER TABLE items DROP COLUMN selection_origins")
        old = self.call("collect", "list", "旧收藏", *scope)
        self.assertNotIn("selection_origins", old["items"][0])
        old_read = self.call("read", "--item", old["items"][0]["id"], *scope)
        self.assertNotIn("selection_origins", old_read["fragments"][0])
        with closing(sqlite3.connect(self.library / DB_NAME)) as connection:
            self.assertNotIn("selection_origins", [row[1] for row in connection.execute("PRAGMA table_info(items)")])

        reading = save_state(picked, self.library / "states").state_id
        added = self.call("collect", "add", "旧收藏", "--from-result", reading)
        self.assertEqual(added["items"][0]["selection_origins"], ["match_order"])
        self.assertFalse(added["items"][1]["created"])
        self.assertNotIn("selection_origins", added["items"][1])
        narrowed = self.call("collect", "list", "旧收藏", "--snapshot", self.snapshot, "--as-of", 1)
        self.assertEqual([item["selection_origins"] for item in narrowed["items"]], [["match_order"]])
        item = added["items"][0]
        first_page = self.call("read", "--item", item["id"], "--item", item["id"], "--budget", 1, *scope)
        self.assertEqual(first_page["fragments"][0]["selection_origins"], ["match_order"])
        next_page = self.call("read", "--from-result", self.save_json("收藏续读.json", first_page))
        self.assertEqual(next_page["fragments"][0]["selection_origins"], ["match_order"])
        manual = self.call("read", item["span"], *scope)
        self.assertNotIn("selection_origins", manual["fragments"][0])

    def test_manual_tail_of_merged_window_has_no_origin(self):
        scope = ("--snapshot", self.snapshot, "--as-of", 1)
        last = self.call("read", "--paragraph", "C0001:3", *scope)["fragments"][0]
        tail = Span.parse(last["read_span"])
        manual = Span(tail.chapter, tail.start - 1, tail.end)
        found = self.save_json("匹配页.json", self.find("--examples", 0))
        first = self.call("read", manual.coordinate, "--from-result", found, "--pick", 1,
                          "--as-of", 1, "--before", 1, "--budget", tail.start)
        self.assertEqual(first["fragments"][0]["selection_origins"], ["match_order"])
        second = self.call("read", "--from-result", self.save_json("合并窗.json", first), "--budget", 100)
        self.assertEqual(second["fragments"][0]["read_span"], tail.coordinate)
        self.assertNotIn("selection_origins", second["fragments"][0])

    def test_old_results_and_direct_coordinates_make_no_inference(self):
        legacy = self.find("--examples", 1)
        legacy["examples"].pop("selection_origin")
        legacy.pop("match_selection_origin")
        path = self.save_json("旧结果.json", legacy)
        for selector in ("--example", "--pick"):
            read = self.call("read", "--from-result", path, selector, 1)
            self.assertNotIn("selection_origins", read["fragments"][0])
        coordinate = legacy["examples"]["selected_spans"][0]
        read = self.call("read", coordinate, "--from-result", path, "--as-matches")
        self.assertNotIn("selection_origins", read["fragments"][0])

    def test_material_index_collects_reasons_from_scoped_physical_delivery(self):
        scope = Scope(self.snapshot, 3)
        research = create_run(self.root / "任务", source={"kind": "library", "path": str(self.library)},
                              snapshot_id=self.snapshot, initial_scope=scope)
        result = self.find("--examples", 2, "--spread", "--budget", 30)
        previous = self.save_json("查询.json", result)
        run_args = ["--run", str(research.root)]
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*run_args, "read", "--from-result", str(previous), "--example", "1", "--as-of", "1"]), 0)
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main([*run_args, "research", "index"]), 0)
        indexed = json.loads(output.getvalue())
        index = Path(indexed["index"]).read_text(encoding="utf-8")
        self.assertIn("按命中章等距 1 次", index)
        self.assertIn("结果扩读 1 次", index)
        self.assertNotIn("未来标题", index)


if __name__ == "__main__":
    unittest.main()
