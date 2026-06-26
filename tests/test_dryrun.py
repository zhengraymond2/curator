from __future__ import annotations

import unittest

from curator.dryrun import render_destination_tree, write_dryrun_file
from curator.plan import Operation, make_plan

from tests.helpers import unique_case_dir


class DryRunTests(unittest.TestCase):
    def test_render_destination_tree_relative_to_originals(self) -> None:
        case = unique_case_dir("dryrun-render")
        library = case / "library"
        plan = make_plan(
            run_id="dryrun-test",
            description="dry run tree",
            metadata={"kind": "organize", "library": str(library)},
            operations=[
                Operation(type="copy", src="/src/a.NEF", dest=str(library / "Originals" / "Italy" / "Rome" / "DSC_0001.NEF")),
                Operation(type="copy", src="/src/b.NEF", dest=str(library / "Originals" / "Italy" / "Rome" / "DSC_0001.NEF")),
                Operation(type="copy", src="/src/c.NEF", dest=str(library / "Originals" / "Italy" / "Cinque Torri" / "DSC_0002.NEF")),
                Operation(type="copy", src="/src/d.NEF", dest=str(library / "Originals" / "Iceland" / "Laugavegur" / "DSC_0003.NEF")),
            ],
        )

        self.assertEqual(
            render_destination_tree(plan),
            "\n".join(
                [
                    "Iceland/",
                    "    Laugavegur/",
                    "        DSC_0003.NEF",
                    "Italy/",
                    "    Cinque Torri/",
                    "        DSC_0002.NEF",
                    "    Rome/",
                    "        DSC_0001.NEF",
                    "        DSC_0001.NEF",
                    "",
                ]
            ),
        )

    def test_write_dryrun_file_writes_to_source_folder(self) -> None:
        case = unique_case_dir("dryrun-write")
        source = case / "originalFolder"
        library = case / "library"
        source.mkdir()
        plan = make_plan(
            run_id="dryrun-test",
            description="dry run tree",
            metadata={"kind": "organize", "library": str(library)},
            operations=[
                Operation(type="copy", src=str(source / "DSC_0001.NEF"), dest=str(library / "Originals" / "Italy" / "Rome" / "DSC_0001.NEF")),
            ],
        )

        path = write_dryrun_file(plan, source)

        self.assertEqual(path, source / "DRYRUN.txt")
        self.assertEqual(path.read_text(encoding="utf-8"), "Italy/\n    Rome/\n        DSC_0001.NEF\n")


if __name__ == "__main__":
    unittest.main()
