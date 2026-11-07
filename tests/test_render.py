import unittest

from src import render


class Formatting(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(render.human_size(None), "")
        self.assertEqual(render.human_size(512), "512B")
        self.assertEqual(render.human_size(2166412), "2.1M")

    def test_durations(self):
        self.assertEqual(render.human_duration(11), "0:11")
        self.assertEqual(render.human_duration(3725), "1:02:05")
        self.assertEqual(render.human_duration(0), "")


class Budget(unittest.TestCase):
    blocks = [["a" * 9], ["b" * 9], ["c" * 9]]  # 10 chars each with newline

    def test_fit_keeps_the_newest_blocks(self):
        self.assertEqual(render.fit(self.blocks, 25), 1)
        self.assertEqual(render.fit(self.blocks, 0), 0)
        self.assertEqual(render.fit(self.blocks, 5), 2)  # always one

    def test_fit_oldest_keeps_the_oldest_blocks(self):
        self.assertEqual(render.fit_oldest(self.blocks, 25), 2)
        self.assertEqual(render.fit_oldest(self.blocks, 0), 3)
        self.assertEqual(render.fit_oldest(self.blocks, 5), 1)


class Summary(unittest.TestCase):
    def test_days_collapse_to_months_when_many(self):
        days = [(f"2026-{m:02d}-{d:02d}", 1) for m in (1, 2) for d in range(1, 29)]
        text = render._days(days)
        self.assertTrue(text.startswith("2026-01 28, 2026-02 28"))

    def test_days_drop_repeated_year(self):
        self.assertEqual(render._days([("2026-10-01", 3), ("2026-10-02", 4)]),
                         "2026-10-01 3, 10-02 4")


if __name__ == "__main__":
    unittest.main()
