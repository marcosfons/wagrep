import os
import tempfile
import unittest

from src import index


class Scope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = index.CONFIG
        index.CONFIG = os.path.join(self.tmp.name, "config.ini")

    def tearDown(self):
        index.CONFIG = self.old
        self.tmp.cleanup()

    def test_spans(self):
        self.assertEqual(index.parse_span("60d"), 60 * 86400)
        self.assertEqual(index.parse_span(" 1Y "), 365 * 86400)
        self.assertEqual(index.parse_span("all"), "all")
        with self.assertRaises(ValueError):
            index.parse_span("forever")

    def test_everything_is_indexed_without_a_config(self):
        scopes = index.chat_scopes([{"id": 1, "jid": "a@s", "archived": 1}], 1000.0)
        self.assertEqual(scopes, {1: ("all", None)})

    def test_chat_rule_beats_archived_beats_default(self):
        index.save_config("scope", {"default": "10d", "archived": "skip"})
        index.save_config("chats", {"kept@s.whatsapp.net": "all  # Ana",
                                    "old@g.us": "1y  # Old group"})
        chats = [{"id": 1, "jid": "kept@s.whatsapp.net", "archived": 1},
                 {"id": 2, "jid": "x@g.us", "archived": 1},
                 {"id": 3, "jid": "y@g.us", "archived": 0},
                 {"id": 4, "jid": "old@g.us", "archived": 0}]
        now = 100 * 86400.0
        scopes = index.chat_scopes(chats, now)
        self.assertEqual(scopes[1], ("all", None))
        self.assertEqual(scopes[2], ("skip", float("inf")))
        self.assertEqual(scopes[3], ("10d", now - 10 * 86400))
        self.assertEqual(scopes[4][0], "1y")


if __name__ == "__main__":
    unittest.main()
