"""Regression tests for sparse paragraph-window searches."""

from collections import Counter
import unittest

from chapspan.model import Chapter, Scope
from chapspan.search import find_many, find_passages, minimal_windows
from chapspan.sources import ScopedText, paragraph_ends


class MeteredText(str):
    def __new__(cls, value):
        result = super().__new__(cls, value)
        result.find_calls = Counter()
        return result

    def find(self, term, start=0, end=None):
        self.find_calls[term] += 1
        return super().find(term, start, len(self) if end is None else end)


class CoreScanRegressions(unittest.TestCase):
    def chapter(self, number, body):
        return Chapter(number, "测试", "body", 1, body, paragraph_ends(body, "lines"))

    def test_sparse_positive_does_not_rescan_the_same_gap(self):
        body = MeteredText("甲\n" * 1000 + "终点\n")
        chapter = self.chapter(1, body)
        self.assertEqual(list(minimal_windows(chapter, ["终点"], [], [], 1001)),
                         [(2000, len(body))])
        self.assertLessEqual(body.find_calls["终点"], 2)

    def test_missing_exclusion_does_not_rescan_every_window(self):
        body = MeteredText("甲\n" * 1000)
        chapter = self.chapter(1, body)
        self.assertEqual(len(list(minimal_windows(chapter, ["甲"], [], ["无"], 1000))), 1000)
        self.assertLessEqual(body.find_calls["无"], 2)

    def test_cached_future_matches_respect_window_end_and_cross_paragraph_terms(self):
        chapter = self.chapter(1, "甲\n乙\n丙\n甲\n乙\n")
        self.assertEqual(list(minimal_windows(chapter, ["甲\n乙"], [], [], 1)), [])
        self.assertEqual(list(minimal_windows(chapter, ["甲\n乙"], [], [], 2)), [(0, 4), (6, 10)])
        self.assertEqual(list(minimal_windows(chapter, ["甲"], ["丙", "乙"], ["丙"], 3)),
                         [(0, 4), (6, 10)])

    def test_sparse_find_single_and_many_keep_identical_pages(self):
        chapters = [self.chapter(1, "甲\n" * 100 + "终点\n"), self.chapter(2, "终点\n")]
        library = ScopedText(chapters, Scope("snap_test", 2))
        queries = [{"all": ["终点"], "near": 101, "limit": 1},
                   {"all": ["终点"], "near": 101, "limit": 1, "offset": 1}]
        multi = find_many(library, queries)
        for query, actual in zip(queries, multi["queries"]):
            expected = find_passages(library, all_terms=query["all"], near=query["near"],
                                     offset=query.get("offset", 0), limit=query["limit"])
            self.assertEqual({key: value for key, value in actual.items() if key != "query"}, expected)


if __name__ == "__main__":
    unittest.main()
