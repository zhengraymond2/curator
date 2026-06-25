from __future__ import annotations

import unittest

from curator.catalog import connect_catalog, fetch_counts, record_scan_run, upsert_file

from tests.helpers import unique_case_dir


class CatalogTests(unittest.TestCase):
    def test_catalog_initializes_and_records_scan_file(self) -> None:
        case = unique_case_dir("catalog")
        library = case / "library"
        connection = connect_catalog(library)
        try:
            record_scan_run(connection, run_id="scan-test", root=case, mode="test")
            media = case / "DSC_0001.NEF"
            media.write_bytes(b"fake")
            upsert_file(
                connection,
                path=media,
                name=media.name,
                size=media.stat().st_size,
                sha256="abc123",
                run_id="scan-test",
            )
            counts = fetch_counts(connection)
        finally:
            connection.close()

        self.assertEqual(counts["scan_runs"], 1)
        self.assertEqual(counts["files"], 1)


if __name__ == "__main__":
    unittest.main()

