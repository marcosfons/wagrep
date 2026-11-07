import http.server
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from src import index, transcribe


class FakeWhisper(http.server.BaseHTTPRequestHandler):
    """An OpenAI-compatible endpoint: answers 429 once, then verbose_json."""
    calls = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeWhisper.calls.append((self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer good":
            self.send_response(401)
            self.end_headers()
            return
        if len(FakeWhisper.calls) == 1:
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        answer = {"text": " Chego às três. ", "language": "pt",
                  "segments": [{"avg_logprob": -0.1}, {"avg_logprob": -0.3}]}
        data = json.dumps(answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeWhisper)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/v1"
        cls.audio = tempfile.NamedTemporaryFile(suffix=".opus", delete=False)
        cls.audio.write(b"OggS fake audio")
        cls.audio.close()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.remove(cls.audio.name)

    def setUp(self):
        FakeWhisper.calls = []

    def test_retries_when_busy_and_parses_verbose_json(self):
        cfg = {"url": self.url, "key": "good", "language": "pt"}
        result = transcribe.api_transcribe(cfg, self.audio.name, "Ana, Bruno.", 10)
        self.assertEqual(result["text"], "Chego às três.")
        self.assertAlmostEqual(result["confidence"], -0.2)
        self.assertEqual(len(FakeWhisper.calls), 2)
        body = FakeWhisper.calls[-1][1]
        self.assertIn(b'name="prompt"\r\n\r\nAna, Bruno.', body)
        self.assertIn(b'name="language"\r\n\r\npt', body)
        self.assertIn(b"OggS fake audio", body)

    def test_rejected_key_is_a_clear_error(self):
        with self.assertRaisesRegex(transcribe.TranscribeError, "key rejected"):
            transcribe.api_transcribe({"url": self.url, "key": "bad"}, self.audio.name, "", 5)


class Prompt(unittest.TestCase):
    def test_lead_sentence_plus_people_in_the_chat(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(index.SCHEMA)
        rows = [(1, 10, 7, "Ana"), (2, 20, 7, "me"), (3, 30, 7, "Bruno"),
                (4, 40, 8, "Carla"), (5, 50, 7, "+55 11 91234-5678")]
        conn.executemany("INSERT INTO msgs (id, ts, chat_id, sender, kind) "
                         "VALUES (?, ?, ?, ?, 'text')", rows)
        prompt = transcribe.build_prompt(conn, {"chat_id": 7}, {"prompt": "Áudio do WhatsApp."})
        conn.close()
        self.assertEqual(prompt, "Áudio do WhatsApp. Bruno, Ana.")


class Config(unittest.TestCase):
    def test_round_trip_is_private_and_env_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = index.CONFIG
            index.CONFIG = os.path.join(tmp, "wagrep", "config.ini")
            try:
                transcribe.save_config({"backend": "api", "url": "http://x/v1", "key": "k"})
                self.assertEqual(os.stat(index.CONFIG).st_mode & 0o777, 0o600)
                os.environ["WAGREP_WHISPER_MODEL"] = "whisper-large"
                cfg = transcribe.load_config()
                self.assertEqual((cfg["backend"], cfg["key"], cfg["model"]),
                                 ("api", "k", "whisper-large"))
                transcribe.save_config({"key": None})
                self.assertNotIn("key", transcribe.load_config())
            finally:
                index.CONFIG = old
                os.environ.pop("WAGREP_WHISPER_MODEL", None)


if __name__ == "__main__":
    unittest.main()
