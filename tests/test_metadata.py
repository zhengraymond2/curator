from __future__ import annotations

import unittest
from unittest.mock import patch

from curator.metadata import capture_timestamps, parse_exif_date, parse_mdls_date

from tests.helpers import unique_case_dir


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

    def test_capture_timestamps_uses_valid_cache_entry(self) -> None:
        case = unique_case_dir("metadata-cache-hit")
        media = case / "DSC_0001.JPG"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"fake jpg")
        cache_path = case / ".curator" / "metadata-cache.json"

        with patch(
            "curator.metadata.exiftool_capture_dates",
            return_value=[(media, "DateTimeOriginal", "2026:05:24 03:31:56")],
        ) as first_exiftool:
            first = capture_timestamps([media], cache_path=cache_path)

        self.assertEqual(first[media].source, "exiftool:DateTimeOriginal")
        first_exiftool.assert_called_once()
        self.assertTrue(cache_path.exists())

        with patch("curator.metadata.exiftool_capture_dates") as second_exiftool:
            second = capture_timestamps([media], cache_path=cache_path)

        second_exiftool.assert_not_called()
        self.assertEqual(second[media], first[media])

    def test_capture_timestamps_invalidates_cache_when_file_changes(self) -> None:
        case = unique_case_dir("metadata-cache-stale")
        media = case / "DSC_0001.JPG"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"old jpg")
        cache_path = case / ".curator" / "metadata-cache.json"

        with patch(
            "curator.metadata.exiftool_capture_dates",
            return_value=[(media, "DateTimeOriginal", "2026:05:24 03:31:56")],
        ):
            capture_timestamps([media], cache_path=cache_path)

        media.write_bytes(b"new jpg with different size")

        with patch(
            "curator.metadata.exiftool_capture_dates",
            return_value=[(media, "DateTimeOriginal", "2027:06:25 04:32:10")],
        ) as exiftool:
            refreshed = capture_timestamps([media], cache_path=cache_path)

        exiftool.assert_called_once()
        self.assertEqual(refreshed[media].raw, "2027:06:25 04:32:10")


if __name__ == "__main__":
    unittest.main()
