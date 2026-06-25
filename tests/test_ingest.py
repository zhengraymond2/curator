from __future__ import annotations

import json
import unittest

from curator.checksums import sha256_file
from curator.ingest import build_ingest_plan
from curator.safety import apply_plan

from tests.helpers import unique_case_dir


class IngestTests(unittest.TestCase):
    def test_ingest_copies_card_tree_and_writes_checksum_manifest(self) -> None:
        case = unique_case_dir("ingest")
        source = case / "card"
        dest = case / "ssd"
        (source / "DCIM" / "100NCZ_6").mkdir(parents=True)
        (source / "PRIVATE").mkdir()
        raw = source / "DCIM" / "100NCZ_6" / "DSC_0001.NEF"
        video_meta = source / "PRIVATE" / "CLIP.MPL"
        raw.write_bytes(b"fake raw bytes")
        video_meta.write_bytes(b"fake video metadata")

        plan = build_ingest_plan(source, dest, name="CARD-001")
        self.assertEqual(plan.metadata["file_count"], 2)
        self.assertEqual(len([op for op in plan.operations if op.type == "copy"]), 2)

        apply_plan(plan, log_root=dest / ".curator" / "logs")

        copied_raw = dest / "Ingests" / "CARD-001" / "DCIM" / "100NCZ_6" / "DSC_0001.NEF"
        copied_meta = dest / "Ingests" / "CARD-001" / "PRIVATE" / "CLIP.MPL"
        manifest_path = dest / "Ingests" / "CARD-001" / ".curator" / "manifest.json"
        checksums_path = dest / "Ingests" / "CARD-001" / ".curator" / "checksums.sha256"

        self.assertTrue(raw.exists(), "ingest must leave the source card copy alone")
        self.assertTrue(copied_raw.exists())
        self.assertTrue(copied_meta.exists())
        self.assertEqual(sha256_file(raw), sha256_file(copied_raw))
        self.assertTrue(checksums_path.exists())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["checksum_algorithm"], "sha256")


if __name__ == "__main__":
    unittest.main()
