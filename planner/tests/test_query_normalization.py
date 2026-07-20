from django.test import SimpleTestCase

from planner.services.query_normalization import normalize_query


class NormalizeQueryTests(SimpleTestCase):
    def test_comma_spacing_variants_all_normalize_the_same(self):
        variants = ["Chicago, IL", "Chicago,IL", "Chicago,   IL", "  Chicago , IL  "]
        normalized = {normalize_query(v) for v in variants}
        self.assertEqual(len(normalized), 1, f"expected one canonical form, got {normalized}")

    def test_case_is_folded(self):
        self.assertEqual(normalize_query("CHICAGO, IL"), normalize_query("chicago, il"))

    def test_internal_whitespace_runs_are_collapsed(self):
        self.assertEqual(normalize_query("New   York, NY"), normalize_query("New York, NY"))

    def test_distinct_places_remain_distinct(self):
        self.assertNotEqual(normalize_query("Chicago, IL"), normalize_query("Denver, CO"))
