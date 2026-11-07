import unittest

from src import wa


def varint(n):
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field(number, value):
    """Encode one protobuf field (int -> varint, bytes/str -> length-delimited)."""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


class Protobuf(unittest.TestCase):
    def test_decodes_varints_and_bytes(self):
        fields = wa.pb(field(1, 150) + field(2, "hi") + field(2, "yo"))
        self.assertEqual(fields[1], [150])
        self.assertEqual(fields[2], [b"hi", b"yo"])

    def test_truncated_input_does_not_raise(self):
        self.assertEqual(dict(wa.pb(b"\x0a\x10ab")), {1: [b"ab"]})


class ReceiptInfo(unittest.TestCase):
    def test_edit_reactions_and_poll(self):
        edit = field(10, field(1, 3) + field(3, "new text") + field(4, 1742511618273))
        reaction = field(7, field(1, field(2, "5531@s.whatsapp.net") + field(3, "👍")))
        poll = field(8, field(2, "Qual dia?")
                     + field(3, field(1, "Terça")) + field(3, field(1, "Quarta"))
                     + field(5, field(1, 1) + field(4, "a@s"))
                     + field(5, field(1, 1) + field(4, "b@s")))
        info = wa.receipt_info(edit + reaction + poll)
        self.assertAlmostEqual(info["edited"], 1742511618.273)
        self.assertEqual(info["reactions"], [("👍", "5531@s.whatsapp.net")])
        self.assertEqual(info["poll"], {"question": "Qual dia?",
                                        "options": [["Terça", 0], ["Quarta", 2]]})


class ContextInfo(unittest.TestCase):
    def test_reply_mentions_and_timer(self):
        quoted = field(1, "original message")
        blob = (field(5, "STANZA1") + field(6, "5531@s.whatsapp.net")
                + field(8, "5532@s.whatsapp.net") + field(19, quoted)
                + field(29, 604800))
        ctx = wa.context_info(blob)
        self.assertEqual(ctx["reply"]["text"], "original message")
        self.assertEqual(ctx["reply"]["stanza"], "STANZA1")
        self.assertEqual(ctx["mentions"], ["5532@s.whatsapp.net"])
        self.assertEqual(ctx["ephemeral"], 604800)


class Harvest(unittest.TestCase):
    def test_template_text_skips_urls_and_binary(self):
        blob = field(24, field(1, field(3, field(1, "https://mmg.whatsapp.net/x y"))
                                + field(6, "Olá, Ana. Tudo bem?")
                                + field(8, field(1, field(1, "Ver Detalhes")))
                                + field(9, b"\x00\x01\xff binary")))
        self.assertEqual(wa.harvest_text(blob), ["Olá, Ana. Tudo bem?", "Ver Detalhes"])


class Names(unittest.TestCase):
    def test_clean_name_removes_bidi_marks(self):
        self.assertEqual(wa.clean_name("\u202a+55\xa011\xa091234\u20115678\u202c"),
                         "+55 11 91234-5678")

    def test_filename_restores_extension(self):
        self.assertEqual(wa.filename_for("Result_5", "Media/x/1/2/uuid.xlsx"), "Result_5.xlsx")
        self.assertEqual(wa.filename_for(None, "Media/x/1/2/uuid.pdf"), "uuid.pdf")

    def test_chat_kind(self):
        self.assertEqual(wa.chat_kind("123@g.us", 1), "group")
        self.assertEqual(wa.chat_kind("123@newsletter", 1), "channel")
        self.assertEqual(wa.chat_kind("5531@s.whatsapp.net", 0), "dm")


if __name__ == "__main__":
    unittest.main()
