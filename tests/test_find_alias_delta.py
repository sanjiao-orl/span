"""Two-term OR results report literal presence by chapter."""

import unittest

from chapspan.model import Chapter, Scope
from chapspan.search import find_many, find_passages
from chapspan.sources import ScopedText, paragraph_ends


def scoped_text(bodies, *, first_chapter=1, as_of=None, peripheral=()):
    chapters = [
        Chapter(number, "测试", "peripheral" if number in peripheral else "body",
                1, body, paragraph_ends(body, "lines"))
        for number, body in enumerate(bodies, 1)
    ]
    return ScopedText(chapters, Scope("snap_test", len(chapters) if as_of is None else as_of, first_chapter))


class FindAliasDeltaTests(unittest.TestCase):
    def test_counts_use_visible_chapters_and_spans_use_first_exclusive_hits(self):
        library = scoped_text(
            ["甲乙", "前甲甲", "乙乙", "甲", "乙见甲", "前前甲", "甲乙", "前乙", "甲", "乙", "无", "甲乙"],
            first_chapter=2, as_of=11, peripheral=(7,),
        )
        result = find_passages(library, any_terms=["甲", "乙"], limit=1, offset=1,
                               distribution_offset=2)
        self.assertEqual(result["alias_delta"], {
            "first_only": {
                "term": "甲", "chapter_count": 4, "read_spans": ["C0002:1-2", "C0004:0-1", "C0006:2-3"],
            },
            "both": {"chapter_count": 1},
            "second_only": {
                "term": "乙", "chapter_count": 3, "read_spans": ["C0003:0-1", "C0008:1-2", "C0010:0-1"],
            },
        })
        self.assertEqual(result["match_count"], 8)
        self.assertEqual(result["hit_chapter_count"], 8)
        self.assertEqual(result["term_counts"], [{"term": "甲", "count": 6}, {"term": "乙", "count": 5}])

    def test_containment_counts_one_chapter_and_compact_batch_preserves_summary(self):
        library = scoped_text(["长名长名", "别名", "无"])
        terms = ["长名", "名"]
        result = find_passages(library, any_terms=terms, limit=1, compact=True)
        self.assertEqual(result["term_counts"], [{"term": "长名", "count": 2}, {"term": "名", "count": 3}])
        self.assertEqual(result["match_count"], 2)
        self.assertEqual(result["alias_delta"], {
            "first_only": {"term": "长名", "chapter_count": 0, "read_spans": []},
            "both": {"chapter_count": 1},
            "second_only": {"term": "名", "chapter_count": 1, "read_spans": ["C0002:1-2"]},
        })
        batch, = find_many(library, [{"any": terms, "offset": 2}], limit=1, compact=True)["queries"]
        self.assertEqual(batch["matches"], [])
        self.assertEqual(batch["alias_delta"], result["alias_delta"])

    def test_zero_counts_still_show_each_group(self):
        library = scoped_text(["无"])
        result = find_passages(library, any_terms=["甲", "乙"])
        self.assertEqual(result["alias_delta"], {
            "first_only": {"term": "甲", "chapter_count": 0, "read_spans": []},
            "both": {"chapter_count": 0},
            "second_only": {"term": "乙", "chapter_count": 0, "read_spans": []},
        })

    def test_other_query_shapes_do_not_report_alias_delta(self):
        library = scoped_text(["甲乙丙"])
        for options in (
            {"all_terms": ["甲", "乙"]},
            {"all_terms": ["丙"], "any_terms": ["甲", "乙"]},
            {"any_terms": ["甲", "乙"], "near": 1},
            {"any_terms": ["甲", "乙"], "exclude": ["丙"]},
            {"any_terms": ["甲", "甲"]},
            {"any_terms": ["甲", "乙", "丙"]},
        ):
            with self.subTest(options=options):
                self.assertNotIn("alias_delta", find_passages(library, **options))


if __name__ == "__main__":
    unittest.main()
