from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from admins import parse_admin_ids  # noqa: E402


class AdminIdsTests(unittest.TestCase):
    def test_parses_common_separators(self) -> None:
        self.assertEqual(parse_admin_ids("2834040317"), (2834040317,))
        self.assertEqual(parse_admin_ids("1, 2;3  4"), (1, 2, 3, 4))

    def test_ignores_garbage_and_duplicates(self) -> None:
        self.assertEqual(parse_admin_ids(" 42, x, -1, 0, 42 "), (42,))

    def test_empty(self) -> None:
        self.assertEqual(parse_admin_ids(""), ())


if __name__ == "__main__":
    unittest.main()
