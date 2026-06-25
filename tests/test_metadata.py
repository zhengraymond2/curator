from __future__ import annotations

import unittest

from curator.metadata import parse_exif_date, parse_mdls_date


class MetadataTests(unittest.TestCase):
    def test_parse_mdls_date(self) -> None:
        parsed = parse_mdls_date("2026-05-24 10:31:56 +0000")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 5)
        self.assertEqual(parsed.day, 24)

    def test_parse_mdls_date_returns_none_for_null(self) -> None:
        self.assertIsNone(parse_mdls_date("(null)"))

    def test_parse_exif_date_with_camera_format(self) -> None:
        parsed = parse_exif_date("2026:05:24 03:31:56")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.hour, 3)

    def test_parse_exif_date_with_timezone(self) -> None:
        parsed = parse_exif_date("2026:05:24 03:31:56-0700")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main()
