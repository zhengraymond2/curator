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


if __name__ == "__main__":
    unittest.main()
