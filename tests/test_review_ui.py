from __future__ import annotations

import unittest
from pathlib import Path

from curator.place_identification import PlaceIdentification, PreparedImage
from curator.review_ui import HTML, ReviewItem, ReviewState, review_item_payload


def sample_identification(
    *,
    group_id: str = "103NCZ_6::01",
    country_or_region: str = "Unsorted",
    place_name: str = "unknown beach",
) -> PlaceIdentification:
    return PlaceIdentification(
        group_id=group_id,
        country_or_region=country_or_region,
        place_name=place_name,
        confidence=0.25,
        is_unknown=place_name.casefold().startswith("unknown"),
        rationale="sand and water",
        visual_evidence=("sand", "water"),
        alternate_guesses=("unknown coast",),
        sampled_paths=(Path("DSC_0001.NEF"),),
        raw_response={},
    )


def sample_image(filename: str = "DSC_0001.NEF") -> PreparedImage:
    return PreparedImage(
        source_path=Path(filename),
        data_url="data:image/jpeg;base64,anBlZw==",
        captured_at=None,
        encoded_bytes=4,
        original_size=(10, 10),
        prepared_size=(10, 10),
    )


class ReviewUiTests(unittest.TestCase):
    def test_review_item_payload_includes_gallery_and_text_fields(self) -> None:
        item = ReviewItem(sample_identification(), (sample_image(),), file_count=42)

        payload = review_item_payload(item, index=0, total=3)

        self.assertEqual(payload["country_or_region"], "Unsorted")
        self.assertEqual(payload["place_name"], "unknown beach")
        self.assertEqual(payload["file_count"], 42)
        self.assertEqual(payload["images"][0]["filename"], "DSC_0001.NEF")

    def test_pending_review_item_payload_uses_llm_data_when_available(self) -> None:
        fallback = PlaceIdentification(
            group_id="103NCZ_6::01",
            country_or_region="Unsorted",
            place_name="103NCZ_6",
            confidence=0.0,
            is_unknown=True,
            rationale="pending",
            visual_evidence=(),
            alternate_guesses=(),
            sampled_paths=(Path("DSC_0001.NEF"),),
            raw_response={},
        )
        item = ReviewItem(fallback, (sample_image(),), file_count=42, llm_pending=True)

        loading = review_item_payload(item, index=0, total=3)
        loaded = review_item_payload(
            item,
            index=0,
            total=3,
            llm_data={"103NCZ_6::01": sample_identification()},
        )

        self.assertTrue(loading["llm_loading"])
        self.assertEqual(loading["place_name"], "")
        self.assertEqual(loading["images"][0]["filename"], "DSC_0001.NEF")
        self.assertFalse(loaded["llm_loading"])
        self.assertEqual(loaded["place_name"], "unknown beach")

    def test_pending_review_item_payload_uses_image_data_when_available(self) -> None:
        item = ReviewItem(sample_identification(), (), file_count=42, images_pending=True)

        loading = review_item_payload(item, index=0, total=3)
        loaded = review_item_payload(
            item,
            index=0,
            total=3,
            image_data={"103NCZ_6::01": (sample_image(),)},
            image_loading=set(),
            image_versions={"103NCZ_6::01": 1},
            image_url_builder=lambda group_id, index, version: f"/image/{group_id}/{index}/{version}",
        )

        self.assertTrue(loading["images_loading"])
        self.assertEqual(loading["images"], [])
        self.assertFalse(loaded["images_loading"])
        self.assertEqual(loaded["images"][0]["src"], "/image/103NCZ_6::01/0/1")

    def test_review_state_serves_prepared_image_bytes(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])

        response = state.image_response("103NCZ_6::01", 0)

        self.assertIsNotNone(response)
        assert response is not None
        body, content_type = response
        self.assertEqual(body, b"jpeg")
        self.assertEqual(content_type, "image/jpeg")

    def test_review_state_records_decision_and_advances(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])

        result = state.decide("Costa Rica", "Manuel Antonio")

        self.assertTrue(result["final_review"])
        reviewed = state.decisions["103NCZ_6::01"]
        self.assertEqual(reviewed.country_or_region, "Costa Rica")
        self.assertEqual(reviewed.place_name, "Manuel Antonio")
        self.assertFalse(reviewed.is_unknown)
        self.assertEqual(result["albums"][0]["place_name"], "Manuel Antonio")

        approved = state.approve_final_review()

        self.assertTrue(approved["done"])

    def test_review_state_can_wait_for_final_cli_validation(self) -> None:
        state = ReviewState(
            [ReviewItem(sample_identification(), (sample_image(),), file_count=1)],
            wait_for_final_validation=True,
        )
        state.decide("Costa Rica", "Manuel Antonio")

        approved = state.approve_final_review()

        self.assertTrue(approved["final_validation"])
        self.assertEqual(approved["validation_status"], "pending")
        self.assertTrue(state.done.is_set())

        state.complete_final_validation(
            success=False,
            title="Validation failed",
            message="Do not delete the original source folder.",
            summary="Checksum comparison: FAILED",
            details="Missing final files:\n  DSC_0001.NEF",
        )
        payload = state.payload()

        self.assertEqual(payload["validation_status"], "failed")
        self.assertIn("Checksum comparison: FAILED", payload["validation_summary"])
        self.assertIn("Missing final files", payload["validation_details"])
        self.assertTrue(state.final_validation_seen.is_set())

    def test_review_state_accepts_country_and_place_in_single_location_field(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])

        result = state.decide("", "Guatemala/Antigua")

        self.assertTrue(result["final_review"])
        reviewed = state.decisions["103NCZ_6::01"]
        self.assertEqual(reviewed.country_or_region, "Guatemala")
        self.assertEqual(reviewed.place_name, "Antigua")

    def test_review_state_renames_final_album(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])
        result = state.decide("Costa Rica", "Manuel Antonio")
        album_key = result["albums"][0]["key"]

        renamed = state.rename_album(str(album_key), "Corcovado")

        self.assertEqual(renamed["albums"][0]["place_name"], "Corcovado")
        self.assertEqual(state.decisions["103NCZ_6::01"].place_name, "Corcovado")

    def test_review_state_moves_images_between_final_albums(self) -> None:
        state = ReviewState(
            [
                ReviewItem(sample_identification(group_id="a::01"), (sample_image("a.NEF"),), file_count=1),
                ReviewItem(sample_identification(group_id="b::01"), (sample_image("b.NEF"),), file_count=1),
            ]
        )
        state.decide("Costa Rica", "Manuel Antonio")
        result = state.decide("Costa Rica", "Corcovado")
        corcovado_key = next(album["key"] for album in result["albums"] if album["place_name"] == "Corcovado")

        moved = state.move_images(["a.NEF"], str(corcovado_key), "")

        albums = {album["place_name"]: album for album in moved["albums"]}
        self.assertNotIn("Manuel Antonio", albums)
        self.assertEqual(len(albums["Corcovado"]["images"]), 2)
        self.assertEqual(state.image_locations["a.NEF"].place_name, "Corcovado")

    def test_review_state_moves_images_to_new_final_album(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])
        state.decide("Costa Rica", "Manuel Antonio")

        moved = state.move_images(["DSC_0001.NEF"], "", "Costa Rica/Tamarindo")

        album = moved["albums"][0]
        self.assertEqual(album["country_or_region"], "Costa Rica")
        self.assertEqual(album["place_name"], "Tamarindo")
        self.assertEqual(state.image_locations["DSC_0001.NEF"].place_name, "Tamarindo")

    def test_review_html_uses_single_location_textbox(self) -> None:
        self.assertNotIn('id="country"', HTML)
        self.assertIn('id="place"', HTML)
        self.assertIn('placeholder="Location or album name"', HTML)
        self.assertIn('Edit folder name', HTML)
        self.assertIn('Looks good', HTML)
        self.assertIn('position: fixed', HTML)
        self.assertIn('Move to...', HTML)
        self.assertIn('Deselect', HTML)
        self.assertIn('renderFinalValidation', HTML)
        self.assertIn('Waiting for CLI validation', HTML)
        self.assertIn('Do not delete the original source folder', HTML)
        self.assertIn('album-select-checkbox', HTML)
        self.assertIn('image-select-checkbox', HTML)
        self.assertIn('albumImagesSelected', HTML)
        self.assertIn('toggleAlbumSelection', HTML)
        self.assertIn('deselectFinalImages', HTML)
        self.assertIn('confirmMoveTo(item.album.place_name)', HTML)
        self.assertIn('--finder-blue: #0a84ff', HTML)
        self.assertIn('var(--finder-blue)', HTML)
        self.assertNotIn("country.textContent = album.country_or_region", HTML)

    def test_review_html_keeps_typed_album_as_first_suggestion(self) -> None:
        self.assertIn("suggestionDraft", HTML)
        self.assertIn("kind: 'typed'", HTML)
        self.assertIn("activeSuggestionIndex = 0", HTML)
        self.assertNotIn("Create New Album:", HTML)
        self.assertNotIn("createNewAlbum", HTML)

    def test_review_html_supports_suggestion_arrow_navigation(self) -> None:
        self.assertIn("event.key !== 'ArrowDown' && event.key !== 'ArrowUp'", HTML)
        self.assertIn("moveSuggestionSelection(event.key === 'ArrowDown' ? 1 : -1)", HTML)
        self.assertIn("applySuggestionSelection(nextIndex)", HTML)

    def test_review_html_paginates_gallery_images(self) -> None:
        self.assertIn("const GALLERY_PAGE_SIZE", HTML)
        self.assertIn("IntersectionObserver", HTML)
        self.assertIn("appendGalleryPage", HTML)
        self.assertIn("galleryRenderedCount < images.length", HTML)

    def test_review_html_expands_selected_images_with_escape_close(self) -> None:
        self.assertIn("openExpandedImage", HTML)
        self.assertIn("closeExpandedImage(true)", HTML)
        self.assertIn("expandedTargetRect", HTML)
        self.assertIn("isSpaceKey(event)", HTML)
        self.assertIn("event.key === 'Escape'", HTML)

    def test_review_html_spacebar_toggles_expanded_preview(self) -> None:
        self.assertIn("isSpaceKey(event) && expandedView", HTML)
        self.assertIn("event.stopPropagation()", HTML)

    def test_review_html_has_album_progress_bar(self) -> None:
        self.assertIn('id="progress-count"', HTML)
        self.assertIn('id="progress-fill"', HTML)
        self.assertIn("${progress.current}/${progress.total}", HTML)
        self.assertIn("fill.style.width", HTML)


if __name__ == "__main__":
    unittest.main()
