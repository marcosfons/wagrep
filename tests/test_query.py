"""End to end over an in-memory index: SQL, functions and filters together."""
import json
import sqlite3
import unittest
import zlib

from src import index, search

ROWS = [  # id, ts, chat, sender, kind, text, filename, extra
    (1, 1000, 1, "Ana", "text", "Vamos no churrasco sábado?", None, None),
    (2, 2000, 1, "Bruno", "text", "Reservei o voo LA-3021", None, None),
    (3, 3000, 2, "me", "document", None, "booking.pdf", None),
    (4, 4000, 2, "Carla", "poll", "Onde?", None, {"poll": [["Praia", 2], ["Sítio", 1]]}),
    (5, 5000, 1, "Ana", "system", "Ana entrou", None, None),
]


class Query(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(index.SCHEMA)
        self.conn.execute("ATTACH DATABASE ':memory:' AS tr")
        self.conn.executescript(index.TRANSCRIPTS_SCHEMA)
        search.register(self.conn)
        for pk, ts, chat, sender, kind, text, filename, extra in ROWS:
            msg = {"text": text, "filename": filename, "extra": extra}
            norm = index.fts_body(msg)
            self.conn.execute(
                "INSERT INTO msgs (id, ts, chat_id, sender, kind, text, norm, filename, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pk, ts, chat, sender, kind, text, norm, filename,
                 json.dumps(extra) if extra else None))
            body = norm + ("\n" + index.fold("Flight LA-3021 seat 14C") if pk == 3 else "")
            self.conn.execute("INSERT INTO fts (rowid, text) VALUES (?, ?)", (pk, body))
        self.conn.execute("INSERT INTO docs VALUES (3, 'ok', 2, 23, ?)",
                          (zlib.compress(b"Flight LA-3021 seat 14C"),))
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def ids(self, **kw):
        rows, total = search.run(self.conn, search.Query(**kw), 0)
        self.assertEqual(total, len(rows))
        return [r["id"] for r in rows]

    def test_accent_insensitive_substring(self):
        self.assertEqual(self.ids(terms=["sabado"]), [1])
        self.assertEqual(self.ids(terms=["CHURRAS"]), [1])

    def test_codes_match_in_messages_and_documents(self):
        self.assertEqual(self.ids(terms=["la 3021"]), [2, 3])

    def test_document_text_and_poll_options_are_searched(self):
        self.assertEqual(self.ids(terms=["seat 14c"]), [3])
        self.assertEqual(self.ids(terms=["sitio"]), [4])

    def test_any_and_short_terms(self):
        self.assertEqual(self.ids(terms=["praia", "voo"], any_term=True), [2, 4])
        self.assertEqual(self.ids(terms=["14"]), [3])

    def test_filters_and_system_rows(self):
        self.assertEqual(self.ids(senders=["ana"]), [1])  # system row hidden
        self.assertEqual(self.ids(chat_ids=[2], since=3500), [4])
        self.assertEqual(search.count(self.conn, search.Query(kinds=["system"])), 1)

    def test_transcripts_are_searched(self):
        self.conn.execute("INSERT INTO msgs (id, ts, chat_id, sender, kind) "
                          "VALUES (6, 6000, 1, 'Bruno', 'audio')")
        index.save_transcript(self.conn, 6, {"text": "Chego às três da tarde"}, "api", 1.0)
        self.assertEqual(self.ids(terms=["tres da tarde"]), [6])

    def test_summary_and_context(self):
        s = search.summary(self.conn, search.Query(chat_ids=[1]))
        self.assertEqual(s["total"], 2)
        self.assertEqual([r["id"] for r in search.around(self.conn, 2, 5, 5)], [1, 2])


if __name__ == "__main__":
    unittest.main()
