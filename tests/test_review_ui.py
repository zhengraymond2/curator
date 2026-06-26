from __future__ import annotations

import unittest
from pathlib import Path

from curator.place_identification import PlaceIdentification, PreparedImage
from curator.review_ui import HTML, ReviewItem, ReviewState, review_item_payload


def sample_identification() -> PlaceIdentification:
    return PlaceIdentification(
        group_id="103NCZ_6::01",
        country_or_region="Unsorted",
        place_name="unknown beach",
        confidence=0.25,
        is_unknown=True,
        rationale="sand and water",
        visual_evidence=("sand", "water"),
        alternate_guesses=("unknown coast",),
        sampled_paths=(Path("DSC_0001.NEF"),),
        raw_response={},
    )


def sample_image() -> PreparedImage:
    return PreparedImage(
        source_path=Path("DSC_0001.NEF"),
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

    def test_review_state_records_decision_and_advances(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])

        result = state.decide("Costa Rica", "Manuel Antonio")

        self.assertTrue(result["done"])
        reviewed = state.decisions["103NCZ_6::01"]
        self.assertEqual(reviewed.country_or_region, "Costa Rica")
        self.assertEqual(reviewed.place_name, "Manuel Antonio")
        self.assertFalse(reviewed.is_unknown)

    def test_review_state_accepts_country_and_place_in_single_location_field(self) -> None:
        state = ReviewState([ReviewItem(sample_identification(), (sample_image(),), file_count=1)])

        result = state.decide("", "Guatemala/Antigua")

        self.assertTrue(result["done"])
        reviewed = state.decisions["103NCZ_6::01"]
        self.assertEqual(reviewed.country_or_region, "Guatemala")
        self.assertEqual(reviewed.place_name, "Antigua")

    def test_review_html_uses_single_location_textbox(self) -> None:
        self.assertNotIn('id="country"', HTML)
        self.assertIn('id="place"', HTML)
        self.assertIn('placeholder="Location or album name"', HTML)

    def test_review_html_offers_create_new_album_suggestion(self) -> None:
        self.assertIn("Create New Album:", HTML)
        self.assertIn("createNewAlbum", HTML)

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

    def test_review_html_has_album_progress_bar(self) -> None:
        self.assertIn('id="progress-count"', HTML)
        self.assertIn('id="progress-fill"', HTML)
        self.assertIn("${progress.current}/${progress.total}", HTML)
        self.assertIn("fill.style.width", HTML)


if __name__ == "__main__":
    unittest.main()
