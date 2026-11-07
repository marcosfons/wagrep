import datetime as dt
import unittest

from src import search
from src.index import fold


class ParseWhen(unittest.TestCase):
    def test_absolute_forms_are_local_time(self):
        expected = dt.datetime(2026, 8, 1, 9, 30).timestamp()
        for text in ("2026-08-01 09:30", "2026-08-01T09:30", "01/08/2026 09:30"):
            self.assertEqual(search.parse_when(text, "-s"), expected, text)

    def test_month_means_its_first_day(self):
        self.assertEqual(search.parse_when("2026-08", "-s"),
                         dt.datetime(2026, 8, 1).timestamp())

    def test_relative_spans(self):
        now = dt.datetime.now().timestamp()
        self.assertAlmostEqual(search.parse_when("7d", "-s"), now - 7 * 86400, delta=5)
        self.assertAlmostEqual(search.parse_when("-3 hours", "-s"), now - 3 * 3600, delta=5)

    def test_bad_date_is_an_error_not_an_empty_result(self):
        with self.assertRaises(search.QueryError):
            search.parse_when("next tuesday", "-s")

    def test_day_after_is_next_midnight(self):
        start = search.parse_when("2026-11-05 15:00", "-d")
        self.assertEqual(search.day_after(start), dt.datetime(2026, 11, 6).timestamp())


class LoosePattern(unittest.TestCase):
    def test_codes_match_with_or_without_separators(self):
        import re
        pattern, prefilter = search._loose_pattern("la 3021")
        self.assertEqual(prefilter, "3021")
        for text in ("la3021", "la-3021", "voo la 3021 ok"):
            self.assertTrue(re.search(pattern, text), text)

    def test_plain_words_are_not_loose(self):
        self.assertIsNone(search._loose_pattern("boleto"))
        self.assertIsNone(search._loose_pattern("2026"))


class TextClause(unittest.TestCase):
    def test_terms_are_folded_and_prefiltered_by_trigrams(self):
        sql, params = search.text_clause(search.Query(terms=["Márcio", "50%"]))
        self.assertIn("fts MATCH ?", sql)
        self.assertIn('"mar" AND "rci"', params[0])
        self.assertIn("%marcio%", params)
        self.assertIn("%50\\%%", params)  # LIKE wildcards in terms are literal

    def test_short_terms_have_no_prefilter(self):
        sql, params = search.text_clause(search.Query(terms=["ok"]))
        self.assertNotIn("MATCH", sql)
        self.assertEqual(params[0], "%ok%")

    def test_any_with_an_unfilterable_term_scans(self):
        sql, _ = search.text_clause(search.Query(terms=["proposta", "ok"], any_term=True))
        self.assertNotIn("MATCH", sql)
        self.assertIn(" OR (", sql)

    def test_fuzzy_expansions_become_alternatives(self):
        q = search.Query(terms=["churasco"],
                         expansions={"churasco": ["churasco", "churrasco"]})
        sql, params = search.text_clause(q)
        self.assertIn(") OR (", params[0])
        self.assertEqual(params[1:5], ["%churasco%", "%churasco%",
                                       "%churrasco%", "%churrasco%"])


class SqlFunctions(unittest.TestCase):
    def test_docmatch_reads_compressed_documents(self):
        import zlib
        doc = zlib.compress("Nota Fiscal Nº 123".encode())
        self.assertEqual(search.docmatch('["s", ["fiscal"]]', doc), 1)
        self.assertEqual(search.docmatch('["s", ["boleto"]]', doc), 0)
        self.assertEqual(search.docmatch('["r", "n. 1\\\\d+"]', doc), 1)

    def test_codes_need_word_boundaries(self):
        pattern, _ = search._loose_pattern("la 3021")
        self.assertEqual(search.regexp(pattern, "voo la-3021 amanha"), 1)
        self.assertEqual(search.regexp(pattern, "rua vila 3021"), 0)


class Fuzzy(unittest.TestCase):
    def test_distance(self):
        self.assertEqual(search.distance("churasco", "churrasco", 2), 1)
        self.assertEqual(search.distance("planilha", "planila", 2), 1)
        self.assertEqual(search.distance("abc", "xyzxyz", 1), 2)  # gave up

    def test_typos_allowed_grow_with_length(self):
        self.assertEqual(search.max_typos("que"), 0)
        self.assertEqual(search.max_typos("festa"), 1)
        self.assertEqual(search.max_typos("electrical"), 2)


class Fold(unittest.TestCase):
    def test_accent_pattern_matches_unfolded_text(self):
        import re
        from src.index import accent_pattern
        self.assertTrue(re.search(accent_pattern("acao"), "Nova AÇÃO", re.I))
        self.assertFalse(re.search(accent_pattern("acao"), "Nova AÇO", re.I))

    def test_accents_and_case(self):
        self.assertEqual(fold("Márcio AÇÃO Ñandú"), "marcio acao nandu")
        self.assertEqual(fold("emoji 👍 stays"), "emoji 👍 stays")


if __name__ == "__main__":
    unittest.main()
