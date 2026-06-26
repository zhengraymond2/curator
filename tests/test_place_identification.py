from __future__ import annotations

import base64
import json
import os
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from curator.metadata import CaptureTimestamp
from curator.place_identification import (
    AlbumCountryContext,
    OpenRouterPlaceIdentifier,
    PhotoCandidate,
    PreparedImage,
    PlaceIdentification,
    load_batch_country_identification_prompt,
    identify_places_for_groups,
    load_place_identification_prompt,
    select_place_identification_samples,
)


class FakePreprocessor:
    def prepare(self, photo: PhotoCandidate) -> PreparedImage:
        data = base64.b64encode(b"jpeg").decode("ascii")
        return PreparedImage(
            source_path=photo.path,
            data_url=f"data:image/jpeg;base64,{data}",
            captured_at=photo.captured_at,
            encoded_bytes=4,
            original_size=(4000, 3000),
            prepared_size=(1536, 1152),
        )


class CapturingTransport:
    def __init__(self) -> None:
        self.payload = None
        self.headers = None

    def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> dict[str, object]:
        self.headers = headers
        self.payload = json.loads(body.decode("utf-8"))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "country_or_region": "Unsorted",
                                "place_name": "unknown park",
                                "confidence": 0.35,
                                "is_unknown": True,
                                "rationale": "Outdoor scene with paths but no readable landmark.",
                                "visual_evidence": ["trees", "walking path"],
                                "alternate_guesses": ["unknown trail"],
                            }
                        )
                    }
                }
            ]
        }


class BatchCountryTransport:
    def __init__(self) -> None:
        self.payload = None

    def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> dict[str, object]:
        self.payload = json.loads(body.decode("utf-8"))
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "albums": [
                                    {
                                        "group_id": "a::01",
                                        "album_name": "Cinque Torri",
                                        "country_or_region": "Italy",
                                        "confidence": 0.88,
                                        "rationale": "Dolomite album name and prior mountain context.",
                                    },
                                    {
                                        "group_id": "b::01",
                                        "album_name": "Lago di Braies",
                                        "country_or_region": "Italy",
                                        "confidence": 0.9,
                                        "rationale": "Nearby Italian lake name and sequence context.",
                                    },
                                ]
                            }
                        )
                    }
                }
            ]
        }


class PlaceIdentificationTests(unittest.TestCase):
    def test_sampler_prefers_max_timestamp_distance(self) -> None:
        photos = [
            PhotoCandidate(Path("middle.jpg"), datetime(2024, 6, 1, 12, 0, 0)),
            PhotoCandidate(Path("latest.jpg"), datetime(2024, 6, 1, 19, 0, 0)),
            PhotoCandidate(Path("earliest.jpg"), datetime(2024, 6, 1, 8, 0, 0)),
        ]

        selected = select_place_identification_samples(photos, rng=random.Random(1))

        self.assertEqual([photo.path.name for photo in selected], ["earliest.jpg", "latest.jpg"])

    def test_prompt_is_checked_in_and_contains_unknown_fallback(self) -> None:
        prompt = load_place_identification_prompt()

        self.assertIn("unknown XYZ", prompt)
        self.assertIn("Return exactly one JSON object", prompt)

    def test_batch_country_prompt_is_checked_in(self) -> None:
        prompt = load_batch_country_identification_prompt()

        self.assertIn("Albums usually do not alternate countries", prompt)
        self.assertIn("Preserve each group_id and album_name exactly", prompt)

    def test_openrouter_payload_uses_gpt_54_mini_and_image_parts(self) -> None:
        transport = CapturingTransport()
        identifier = OpenRouterPlaceIdentifier(api_key="test-key", transport=transport)
        data = base64.b64encode(b"jpeg").decode("ascii")
        prepared = [
            PreparedImage(
                source_path=Path("sample.jpg"),
                data_url=f"data:image/jpeg;base64,{data}",
                captured_at=datetime(2024, 6, 1, 8, 0, 0),
                encoded_bytes=4,
                original_size=(4000, 3000),
                prepared_size=(1536, 1152),
            )
        ]

        result = identifier.identify_prepared_images("group-a", prepared)

        self.assertEqual(result.place_name, "unknown park")
        self.assertEqual(result.country_or_region, "Unsorted")
        self.assertEqual(transport.payload["model"], "openai/gpt-5.4-mini")
        content = transport.payload["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertTrue(content[-1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(transport.headers["Authorization"], "Bearer test-key")

    def test_openrouter_api_key_can_be_loaded_from_dotenv(self) -> None:
        original_cwd = os.getcwd()
        original_key = os.environ.pop("OPENROUTER_API_KEY", None)
        transport = CapturingTransport()

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, ".env").write_text("OPENROUTER_API_KEY=dotenv-key\n", encoding="utf-8")
            os.chdir(temp_dir)
            try:
                identifier = OpenRouterPlaceIdentifier(transport=transport)
                data = base64.b64encode(b"jpeg").decode("ascii")
                identifier.identify_prepared_images(
                    "group-a",
                    [
                        PreparedImage(
                            source_path=Path("sample.jpg"),
                            data_url=f"data:image/jpeg;base64,{data}",
                            captured_at=datetime(2024, 6, 1, 8, 0, 0),
                            encoded_bytes=4,
                            original_size=(4000, 3000),
                            prepared_size=(1536, 1152),
                        )
                    ],
                )
            finally:
                os.chdir(original_cwd)
                os.environ.pop("OPENROUTER_API_KEY", None)
                if original_key is not None:
                    os.environ["OPENROUTER_API_KEY"] = original_key

        self.assertEqual(transport.headers["Authorization"], "Bearer dotenv-key")

    def test_openrouter_batch_country_payload_uses_prior_context_without_images(self) -> None:
        transport = BatchCountryTransport()
        identifier = OpenRouterPlaceIdentifier(api_key="test-key", transport=transport)
        prior = PlaceIdentification(
            group_id="a::01",
            country_or_region="Unsorted",
            place_name="unknown mountain",
            confidence=0.2,
            is_unknown=True,
            rationale="Jagged mountain ridge and alpine lake.",
            visual_evidence=("mountain ridge", "lake"),
            alternate_guesses=("Dolomites",),
            sampled_paths=(Path("DSC_0001.NEF"),),
            raw_response={},
        )

        guesses = identifier.identify_countries_for_albums(
            (
                AlbumCountryContext("a::01", "Cinque Torri", prior),
                AlbumCountryContext("b::01", "Lago di Braies", prior),
            )
        )

        self.assertEqual([guess.country_or_region for guess in guesses], ["Italy", "Italy"])
        self.assertEqual(transport.payload["response_format"]["json_schema"]["name"], "album_country_identification")
        content = transport.payload["messages"][0]["content"]
        self.assertEqual([part["type"] for part in content], ["text", "text"])
        payload_text = content[1]["text"]
        self.assertIn("Cinque Torri", payload_text)
        self.assertIn("Jagged mountain ridge", payload_text)
        self.assertNotIn("image_url", json.dumps(content))

    def test_identify_places_for_groups_samples_two_images_per_group(self) -> None:
        transport = CapturingTransport()
        identifier = OpenRouterPlaceIdentifier(api_key="test-key", transport=transport)

        with patch(
            "curator.place_identification.capture_timestamps",
            return_value={
                Path("early.jpg"): CaptureTimestamp(epoch=1_717_249_200, source="exiftool:DateTimeOriginal"),
                Path("late.jpg"): CaptureTimestamp(epoch=1_717_292_800, source="exiftool:DateTimeOriginal"),
                Path("middle.jpg"): CaptureTimestamp(epoch=1_717_263_600, source="exiftool:DateTimeOriginal"),
            },
        ):
            results = identify_places_for_groups(
                {
                    "trip": [
                        {"path": "early.jpg"},
                        {"path": "late.jpg"},
                        {"path": "middle.jpg"},
                    ]
                },
                identifier=identifier,
                preprocessor=FakePreprocessor(),
                rng=random.Random(2),
            )

        sampled = [path.name for path in results["trip"].sampled_paths]
        self.assertEqual(sampled, ["early.jpg", "late.jpg"])


if __name__ == "__main__":
    unittest.main()
