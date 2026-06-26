from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from curator.metadata import CaptureTimestamp
from curator.organize import build_organize_plan
from curator.place_identification import OpenRouterError, PlaceIdentification, PreparedImage
from curator.review_ui import FinalReviewResult, ReviewState

from tests.helpers import unique_case_dir


class OrganizeTests(unittest.TestCase):
    def test_organize_uses_country_top_level_layout_without_year(self) -> None:
        case = unique_case_dir("organize")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            plan = build_organize_plan(case / "CRG", library, mode="migration")

        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].type, "copy")
        self.assertEqual(plan.operations[0].expected_size, media.stat().st_size)
        dest = plan.operations[0].dest
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertIn("/library/Unsorted/103NCZ_6/DSC_0001.NEF", dest)
        self.assertNotIn("/Originals/2026/", dest)
        self.assertEqual(plan.metadata["layout"], "Country/Album")
        self.assertEqual(plan.metadata["organization_root"], str(library.resolve()))
        self.assertEqual(plan.metadata["transfer"], "copy")
        self.assertEqual(plan.operations[0].metadata["timestamp_source"], "exiftool:DateTimeOriginal")

    def test_organize_can_plan_moves_when_explicitly_requested(self) -> None:
        case = unique_case_dir("organize-move")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")

        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            plan = build_organize_plan(case / "CRG", library, mode="migration", transfer="move")

        self.assertEqual(plan.operations[0].type, "move")
        self.assertIsNone(plan.operations[0].expected_size)
        self.assertEqual(plan.metadata["transfer"], "move")

    def test_organize_rejects_missing_source(self) -> None:
        case = unique_case_dir("organize-missing")

        with self.assertRaises(ValueError):
            build_organize_plan(case / "missing", case / "library", mode="migration")

    def test_organize_uses_place_identification_for_country_and_album(self) -> None:
        case = unique_case_dir("organize-identified")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        identification = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Italy",
            place_name="Cinque Torri",
            confidence=0.82,
            is_unknown=False,
            rationale="Visible mountain landmark.",
            visual_evidence=("dolomite towers",),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            plan = build_organize_plan(
                case / "CRG",
                library,
                mode="migration",
                place_identifications={"103NCZ_6::01": identification},
            )

        dest = plan.operations[0].dest
        self.assertIsNotNone(dest)
        assert dest is not None
        self.assertIn("/library/Italy/Cinque Torri/DSC_0001.NEF", dest)
        self.assertEqual(plan.metadata["identified_bundle_count"], 1)
        self.assertEqual(plan.operations[0].metadata["identified_country_or_region"], "Italy")

    def test_organize_identify_places_calls_identifier(self) -> None:
        case = unique_case_dir("organize-identify-calls")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        identification = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Iceland",
            place_name="Laugavegur",
            confidence=0.75,
            is_unknown=False,
            rationale="Visible trail landscape.",
            visual_evidence=("trail",),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.identify_bundle_places", return_value={"103NCZ_6::01": identification}) as mocked:
                plan = build_organize_plan(case / "CRG", library, mode="migration", identify_places=True)

        mocked.assert_called_once()
        self.assertIn("/library/Iceland/Laugavegur/DSC_0001.NEF", str(plan.operations[0].dest))

    def test_organize_identify_places_does_not_swallow_openrouter_errors(self) -> None:
        case = unique_case_dir("organize-identify-error")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1_779_606_716.0, source="exiftool:DateTimeOriginal", raw="2026:05:24 03:31:56")
        prepared = PreparedImage(
            source_path=media,
            data_url="data:image/jpeg;base64,anBlZw==",
            captured_at=None,
            encoded_bytes=4,
            original_size=(10, 10),
            prepared_size=(10, 10),
        )

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images):
                raise OpenRouterError("missing key")

        class FakePreprocessor:
            def prepare(self, photo):
                return prepared

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with self.assertRaises(OpenRouterError):
                    build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        place_identifier=FakeIdentifier(),
                    )

    def test_organize_keeps_same_identified_place_separate_across_source_folders(self) -> None:
        case = unique_case_dir("organize-folder-boundaries")
        library = case / "library"
        media_a = case / "CRG" / "103NCZ_6" / "DSC_0001.NEF"
        media_b = case / "CRG" / "104NCZ_6" / "DSC_0002.NEF"
        media_a.parent.mkdir(parents=True)
        media_b.parent.mkdir(parents=True)
        media_a.write_bytes(b"a")
        media_b.write_bytes(b"b")
        timestamps = {
            media_a: CaptureTimestamp(epoch=1000.0, source="test"),
            media_b: CaptureTimestamp(epoch=1000.0, source="test"),
        }
        identifications = {
            group_id: PlaceIdentification(
                group_id=group_id,
                country_or_region="Italy",
                place_name="Rome",
                confidence=0.9,
                is_unknown=False,
                rationale="test",
                visual_evidence=(),
                alternate_guesses=(),
                sampled_paths=(),
                raw_response={},
            )
            for group_id in ("103NCZ_6::01", "104NCZ_6::01")
        }

        with patch("curator.organize.capture_timestamps", return_value=timestamps):
            plan = build_organize_plan(
                case / "CRG",
                library,
                mode="migration",
                place_identifications=identifications,
            )

        parents = {str(__import__("pathlib").Path(operation.dest).parent) for operation in plan.operations}
        self.assertEqual(len(parents), 2)
        self.assertIn(str(library / "Italy" / "Rome"), parents)
        self.assertIn(str(library / "Italy" / "Rome - 104NCZ_6"), parents)

    def test_organize_filename_adjacency_prevents_skipping_existing_names(self) -> None:
        case = unique_case_dir("organize-name-adjacency")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        files = {
            "DSC_0001.NEF": 1000.0,
            "DSC_0002.NEF": 1010.0,
            "DSC_0004.NEF": 1030.0,
            "DSC_0007.NEF": 1020.0,
        }
        timestamps = {}
        for name, epoch in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode("utf-8"))
            timestamps[path] = CaptureTimestamp(epoch=epoch, source="test")

        with patch("curator.organize.capture_timestamps", return_value=timestamps):
            plan = build_organize_plan(case / "CRG", library, mode="migration")

        parent_by_name = {
            __import__("pathlib").Path(operation.dest).name: __import__("pathlib").Path(operation.dest).parent.name
            for operation in plan.operations
        }
        self.assertEqual(parent_by_name["DSC_0001.NEF"], "103NCZ_6")
        self.assertEqual(parent_by_name["DSC_0002.NEF"], "103NCZ_6")
        self.assertEqual(parent_by_name["DSC_0004.NEF"], "103NCZ_6-02")
        self.assertEqual(parent_by_name["DSC_0007.NEF"], "103NCZ_6-02")

    def test_organize_review_unknown_places_uses_reviewer_response(self) -> None:
        case = unique_case_dir("organize-review-unknown")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1000.0, source="test")
        prepared = PreparedImage(
            source_path=media,
            data_url="data:image/jpeg;base64,anBlZw==",
            captured_at=None,
            encoded_bytes=4,
            original_size=(10, 10),
            prepared_size=(10, 10),
        )
        unknown = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Unsorted",
            place_name="unknown beach",
            confidence=0.2,
            is_unknown=True,
            rationale="test",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )
        reviewed = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Costa Rica",
            place_name="Manuel Antonio",
            confidence=1.0,
            is_unknown=False,
            rationale="user",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images):
                return unknown

        class FakePreprocessor:
            def prepare(self, photo):
                return prepared

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                plan = build_organize_plan(
                    case / "CRG",
                    library,
                    mode="migration",
                    identify_places=True,
                    review_unknown_places=True,
                    place_identifier=FakeIdentifier(),
                    unknown_place_reviewer=lambda identification, images: reviewed,
                )

        self.assertIn("/library/Costa Rica/Manuel Antonio/DSC_0001.NEF", str(plan.operations[0].dest))

    def test_organize_review_ui_updates_identification_for_all_reviewed_groups(self) -> None:
        case = unique_case_dir("organize-review-ui")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1000.0, source="test")
        prepared = PreparedImage(
            source_path=media,
            data_url="data:image/jpeg;base64,anBlZw==",
            captured_at=None,
            encoded_bytes=4,
            original_size=(10, 10),
            prepared_size=(10, 10),
        )
        guess = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Unsorted",
            place_name="unknown beach",
            confidence=0.2,
            is_unknown=True,
            rationale="test",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )
        reviewed = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Costa Rica",
            place_name="Manuel Antonio",
            confidence=1.0,
            is_unknown=False,
            rationale="user",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(media,),
            raw_response={},
        )

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                return guess

        class FakePreprocessor:
            def prepare(self, photo):
                return prepared

        def fake_review(items, **kwargs):
            return FinalReviewResult(decisions={reviewed.group_id: reviewed}, image_locations={})

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    plan = build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        self.assertIn("/library/Costa Rica/Manuel Antonio/DSC_0001.NEF", str(plan.operations[0].dest))

    def test_review_ui_receives_all_bundle_images(self) -> None:
        case = unique_case_dir("organize-review-ui-all-images")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media_a = source / "DSC_0001.NEF"
        media_b = source / "DSC_0002.NEF"
        media_a.parent.mkdir(parents=True)
        media_a.write_bytes(b"a")
        media_b.write_bytes(b"b")
        timestamps = {
            media_a: CaptureTimestamp(epoch=1000.0, source="test"),
            media_b: CaptureTimestamp(epoch=1010.0, source="test"),
        }
        guess = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Unsorted",
            place_name="unknown beach",
            confidence=0.2,
            is_unknown=True,
            rationale="test",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(),
            raw_response={},
        )

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                return guess

        class FakePreprocessor:
            def prepare(self, photo):
                return PreparedImage(
                    source_path=photo.path,
                    data_url="data:image/jpeg;base64,anBlZw==",
                    captured_at=None,
                    encoded_bytes=4,
                    original_size=(10, 10),
                    prepared_size=(10, 10),
                )

        def fake_review(items, **kwargs):
            state = ReviewState(items)
            kwargs["state_ready"](state)
            group_id = items[0].identification.group_id
            for _ in range(100):
                with state.lock:
                    images = state.image_data.get(group_id, ())
                    images_loading = group_id in state.image_loading
                if len(images) == 2 and not images_loading:
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("review UI did not receive the full prepared gallery")

            reviewed = PlaceIdentification(
                group_id=group_id,
                country_or_region="Costa Rica",
                place_name="Manuel Antonio",
                confidence=1.0,
                is_unknown=False,
                rationale="user",
                visual_evidence=(),
                alternate_guesses=(),
                sampled_paths=(),
                raw_response={},
            )
            return FinalReviewResult(decisions={reviewed.group_id: reviewed}, image_locations={})

        with patch("curator.organize.capture_timestamps", return_value=timestamps):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    plan = build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        self.assertEqual(len(plan.operations), 2)

    def test_review_ui_guesses_country_after_album_naming(self) -> None:
        case = unique_case_dir("organize-review-ui-country-guess")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1000.0, source="test")

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                return PlaceIdentification(
                    group_id=group_id,
                    country_or_region="Unsorted",
                    place_name="unknown mountain",
                    confidence=0.2,
                    is_unknown=True,
                    rationale="test",
                    visual_evidence=(),
                    alternate_guesses=(),
                    sampled_paths=(),
                    raw_response={},
                )

            def identify_country_for_album(self, group_id, album_name, prepared_images, prompt=None):
                return PlaceIdentification(
                    group_id=group_id,
                    country_or_region="Italy",
                    place_name=album_name,
                    confidence=0.8,
                    is_unknown=False,
                    rationale="album name",
                    visual_evidence=(),
                    alternate_guesses=(),
                    sampled_paths=(),
                    raw_response={},
                )

        class FakePreprocessor:
            def prepare(self, photo):
                return PreparedImage(
                    source_path=photo.path,
                    data_url="data:image/jpeg;base64,anBlZw==",
                    captured_at=None,
                    encoded_bytes=4,
                    original_size=(10, 10),
                    prepared_size=(10, 10),
                )

        def fake_review(items, **kwargs):
            state = ReviewState(items)
            kwargs["state_ready"](state)
            group_id = items[0].identification.group_id
            for _ in range(100):
                with state.lock:
                    images = state.image_data.get(group_id, ())
                if images:
                    break
                threading.Event().wait(0.01)
            state.decide("", "Cinque Torri")
            for _ in range(100):
                with state.lock:
                    loading = bool(state.country_guess_loading)
                    country = state.decisions[group_id].country_or_region
                if not loading and country == "Italy":
                    break
                threading.Event().wait(0.01)
            else:
                self.fail("review UI did not apply country guess")
            return state.final_result()

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    plan = build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        self.assertIn("/library/Italy/Cinque Torri/DSC_0001.NEF", str(plan.operations[0].dest))

    def test_review_ui_does_not_wait_for_llm_result_before_reviewing(self) -> None:
        case = unique_case_dir("organize-review-ui-async")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media = source / "DSC_0001.NEF"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"fake raw")
        timestamp = CaptureTimestamp(epoch=1000.0, source="test")
        started = threading.Event()
        release = threading.Event()
        returned = threading.Event()

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                started.set()
                release.wait(timeout=2)
                returned.set()
                return PlaceIdentification(
                    group_id=group_id,
                    country_or_region="Unsorted",
                    place_name="unknown beach",
                    confidence=0.2,
                    is_unknown=True,
                    rationale="test",
                    visual_evidence=(),
                    alternate_guesses=(),
                    sampled_paths=(),
                    raw_response={},
                )

        class FakePreprocessor:
            def prepare(self, photo):
                return PreparedImage(
                    source_path=photo.path,
                    data_url="data:image/jpeg;base64,anBlZw==",
                    captured_at=None,
                    encoded_bytes=4,
                    original_size=(10, 10),
                    prepared_size=(10, 10),
                )

        def fake_review(items, **kwargs):
            state = ReviewState(items)
            kwargs["state_ready"](state)
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(returned.is_set())
            payload = state.payload()
            self.assertTrue(payload["llm_loading"])
            release.set()
            item = items[0]
            reviewed = PlaceIdentification(
                group_id=item.identification.group_id,
                country_or_region="Costa Rica",
                place_name="Manuel Antonio",
                confidence=1.0,
                is_unknown=False,
                rationale="user",
                visual_evidence=(),
                alternate_guesses=(),
                sampled_paths=(),
                raw_response={},
            )
            return FinalReviewResult(decisions={reviewed.group_id: reviewed}, image_locations={})

        with patch("curator.organize.capture_timestamps", return_value={media: timestamp}):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    plan = build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        self.assertIn("/library/Costa Rica/Manuel Antonio/DSC_0001.NEF", str(plan.operations[0].dest))

    def test_review_ui_final_image_moves_split_bundle_destinations(self) -> None:
        case = unique_case_dir("organize-review-ui-image-move")
        source = case / "CRG" / "103NCZ_6"
        library = case / "library"
        media_a = source / "DSC_0001.NEF"
        media_b = source / "DSC_0002.NEF"
        media_a.parent.mkdir(parents=True)
        media_a.write_bytes(b"a")
        media_b.write_bytes(b"b")
        timestamps = {
            media_a: CaptureTimestamp(epoch=1000.0, source="test"),
            media_b: CaptureTimestamp(epoch=1010.0, source="test"),
        }
        guess = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Unsorted",
            place_name="unknown beach",
            confidence=0.2,
            is_unknown=True,
            rationale="test",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(),
            raw_response={},
        )
        manuel = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Costa Rica",
            place_name="Manuel Antonio",
            confidence=1.0,
            is_unknown=False,
            rationale="user",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(),
            raw_response={},
        )
        corcovado = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Costa Rica",
            place_name="Corcovado",
            confidence=1.0,
            is_unknown=False,
            rationale="user moved image",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(),
            raw_response={},
        )

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                return guess

        class FakePreprocessor:
            def prepare(self, photo):
                return PreparedImage(
                    source_path=photo.path,
                    data_url="data:image/jpeg;base64,anBlZw==",
                    captured_at=None,
                    encoded_bytes=4,
                    original_size=(10, 10),
                    prepared_size=(10, 10),
                )

        def fake_review(items, **kwargs):
            return FinalReviewResult(
                decisions={manuel.group_id: manuel},
                image_locations={str(media_a): manuel, str(media_b): corcovado},
            )

        with patch("curator.organize.capture_timestamps", return_value=timestamps):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    plan = build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        parents = {__import__("pathlib").Path(operation.dest).parent.name for operation in plan.operations}
        self.assertEqual(parents, {"Manuel Antonio", "Corcovado"})

    def test_review_ui_starts_llm_for_all_groups_before_review_returns(self) -> None:
        case = unique_case_dir("organize-review-ui-prestarts")
        library = case / "library"
        media_a = case / "CRG" / "103NCZ_6" / "DSC_0001.NEF"
        media_b = case / "CRG" / "104NCZ_6" / "DSC_0002.NEF"
        media_a.parent.mkdir(parents=True)
        media_b.parent.mkdir(parents=True)
        media_a.write_bytes(b"a")
        media_b.write_bytes(b"b")
        timestamps = {
            media_a: CaptureTimestamp(epoch=1000.0, source="test"),
            media_b: CaptureTimestamp(epoch=2000.0, source="test"),
        }
        groups_started = []
        all_groups_started = threading.Event()

        class FakeIdentifier:
            def identify_prepared_images(self, group_id, prepared_images, prompt=None):
                groups_started.append(group_id)
                if len(groups_started) >= 2:
                    all_groups_started.set()
                return PlaceIdentification(
                    group_id=group_id,
                    country_or_region="Unsorted",
                    place_name="unknown mountain",
                    confidence=0.2,
                    is_unknown=True,
                    rationale="test",
                    visual_evidence=(),
                    alternate_guesses=(),
                    sampled_paths=(),
                    raw_response={},
                )

        class FakePreprocessor:
            def prepare(self, photo):
                return PreparedImage(
                    source_path=photo.path,
                    data_url="data:image/jpeg;base64,anBlZw==",
                    captured_at=None,
                    encoded_bytes=4,
                    original_size=(10, 10),
                    prepared_size=(10, 10),
                )

        def fake_review(items, **kwargs):
            state = ReviewState(items)
            kwargs["state_ready"](state)
            self.assertTrue(all_groups_started.wait(timeout=1))
            decisions = {
                item.identification.group_id: PlaceIdentification(
                    group_id=item.identification.group_id,
                    country_or_region="Costa Rica",
                    place_name=f"Reviewed {index}",
                    confidence=1.0,
                    is_unknown=False,
                    rationale="user",
                    visual_evidence=(),
                    alternate_guesses=(),
                    sampled_paths=(),
                    raw_response={},
                )
                for index, item in enumerate(items, start=1)
            }
            return FinalReviewResult(decisions=decisions, image_locations={})

        with patch("curator.organize.capture_timestamps", return_value=timestamps):
            with patch("curator.organize.ImagePreprocessor", return_value=FakePreprocessor()):
                with patch("curator.organize.review_place_identifications_in_browser", fake_review):
                    build_organize_plan(
                        case / "CRG",
                        library,
                        mode="migration",
                        identify_places=True,
                        review_ui=True,
                        place_identifier=FakeIdentifier(),
                    )

        self.assertEqual(set(groups_started), {"103NCZ_6::01", "104NCZ_6::01"})


if __name__ == "__main__":
    unittest.main()
