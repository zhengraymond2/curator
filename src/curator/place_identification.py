from __future__ import annotations

import base64
import json
import os
import random
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, Union

from importlib import resources

from .metadata import capture_timestamps

try:
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError:  # pragma: no cover - exercised only without installed deps
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    UnidentifiedImageError = Exception  # type: ignore[assignment]


DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.4-mini"
DEFAULT_PROMPT_NAME = "prompt_001_identify_place.txt"
DEFAULT_MAX_IMAGE_SIDE = 1536
DEFAULT_JPEG_QUALITY = 82
DEFAULT_ENV_FILE_NAME = ".env"

RAW_EXTENSIONS = {
    ".3fr",
    ".arw",
    ".cr2",
    ".cr3",
    ".dcr",
    ".dng",
    ".erf",
    ".fff",
    ".iiq",
    ".k25",
    ".kdc",
    ".mef",
    ".mos",
    ".mrw",
    ".nef",
    ".nrw",
    ".orf",
    ".pef",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
}

PLACE_RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "place_identification",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "place_name": {"type": "string"},
                "confidence": {"type": "number"},
                "is_unknown": {"type": "boolean"},
                "rationale": {"type": "string"},
                "visual_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "alternate_guesses": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [
                "place_name",
                "confidence",
                "is_unknown",
                "rationale",
                "visual_evidence",
                "alternate_guesses",
            ],
        },
    },
}


class PlaceIdentificationError(RuntimeError):
    """Base error for place identification failures."""


class ImagePreparationError(PlaceIdentificationError):
    """Raised when a source image cannot be converted for LLM input."""


class OpenRouterError(PlaceIdentificationError):
    """Raised when OpenRouter returns an unusable response."""


@dataclass(frozen=True)
class PhotoCandidate:
    path: Path
    captured_at: datetime | None = None
    timestamp_source: str | None = None


PhotoInput = Union[str, Path, PhotoCandidate, Mapping[str, Any]]


@dataclass(frozen=True)
class PreparedImage:
    source_path: Path
    data_url: str
    captured_at: datetime | None
    encoded_bytes: int
    original_size: tuple[int, int] | None
    prepared_size: tuple[int, int] | None


@dataclass(frozen=True)
class PlaceIdentification:
    group_id: str
    place_name: str
    confidence: float
    is_unknown: bool
    rationale: str
    visual_evidence: tuple[str, ...]
    alternate_guesses: tuple[str, ...]
    sampled_paths: tuple[Path, ...]
    raw_response: Mapping[str, Any]


def load_place_identification_prompt(prompt_name: str = DEFAULT_PROMPT_NAME) -> str:
    return resources.files("curator").joinpath("prompts", prompt_name).read_text(encoding="utf-8")


def load_dotenv_file(env_path: str | Path | None = None) -> Path | None:
    path = Path(env_path) if env_path is not None else find_dotenv_path()
    if path is None or not path.exists():
        return None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, strip_env_value(value.strip()))
    return path


def find_dotenv_path(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for root in [current, *current.parents]:
        candidate = root / DEFAULT_ENV_FILE_NAME
        if candidate.exists():
            return candidate
    return None


def strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_capture_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
        for date_format in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, date_format)
            except ValueError:
                continue
    return None


def coerce_photo_candidates(photos: Sequence[PhotoInput]) -> list[PhotoCandidate]:
    candidates = [coerce_photo_candidate(photo) for photo in photos]
    missing = [candidate.path for candidate in candidates if candidate.captured_at is None]
    if not missing:
        return candidates

    inferred = capture_timestamps(missing)
    hydrated: list[PhotoCandidate] = []
    for candidate in candidates:
        timestamp = inferred.get(candidate.path)
        if candidate.captured_at is None and timestamp is not None:
            hydrated.append(
                PhotoCandidate(
                    path=candidate.path,
                    captured_at=datetime.fromtimestamp(timestamp.epoch),
                    timestamp_source=timestamp.source,
                )
            )
        else:
            hydrated.append(candidate)
    return hydrated


def coerce_photo_candidate(photo: PhotoInput) -> PhotoCandidate:
    if isinstance(photo, PhotoCandidate):
        return photo
    if isinstance(photo, Mapping):
        path_value = photo.get("path") or photo.get("filepath") or photo.get("file")
        if not path_value:
            raise ValueError("Photo mapping must contain a path, filepath, or file key.")
        return PhotoCandidate(
            path=Path(path_value),
            captured_at=parse_capture_time(
                photo.get("captured_at")
                or photo.get("capture_time")
                or photo.get("timestamp")
                or photo.get("datetime")
            ),
            timestamp_source=str(photo.get("timestamp_source")) if photo.get("timestamp_source") else None,
        )
    return PhotoCandidate(path=Path(photo))


def select_place_identification_samples(
    photos: Sequence[PhotoCandidate],
    max_samples: int = 2,
    rng: random.Random | None = None,
) -> list[PhotoCandidate]:
    if max_samples < 1:
        raise ValueError("max_samples must be at least 1.")
    if not photos:
        return []

    rng = rng or random.Random()
    unique_photos = list(dict.fromkeys(photos))
    sample_count = min(max_samples, len(unique_photos))
    if sample_count == 1:
        return [rng.choice(unique_photos)]

    timestamped = [photo for photo in unique_photos if photo.captured_at is not None]
    if len(timestamped) >= 2:
        min_timestamp = min(photo.captured_at for photo in timestamped if photo.captured_at is not None)
        max_timestamp = max(photo.captured_at for photo in timestamped if photo.captured_at is not None)
        earliest = [photo for photo in timestamped if photo.captured_at == min_timestamp]
        latest = [photo for photo in timestamped if photo.captured_at == max_timestamp]
        first = rng.choice(earliest)
        second_pool = [photo for photo in latest if photo != first]
        if second_pool:
            return [first, rng.choice(second_pool)]

    return rng.sample(unique_photos, sample_count)


class ImagePreprocessor:
    def __init__(
        self,
        max_side: int = DEFAULT_MAX_IMAGE_SIDE,
        jpeg_quality: int = DEFAULT_JPEG_QUALITY,
    ) -> None:
        if max_side < 256:
            raise ValueError("max_side should be at least 256 pixels.")
        if not 1 <= jpeg_quality <= 95:
            raise ValueError("jpeg_quality must be between 1 and 95.")
        self.max_side = max_side
        self.jpeg_quality = jpeg_quality

    def prepare(self, photo: PhotoCandidate) -> PreparedImage:
        if Image is None or ImageOps is None:
            raise ImagePreparationError("Pillow is required to prepare image inputs.")

        image = self._open_image(photo.path)
        try:
            image = ImageOps.exif_transpose(image)
            original_size = image.size
            if image.mode != "RGB":
                image = image.convert("RGB")

            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            image.thumbnail((self.max_side, self.max_side), resampling)

            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=self.jpeg_quality,
                optimize=True,
                progressive=True,
            )
            payload = output.getvalue()
            prepared_size = image.size
        finally:
            image.close()

        encoded = base64.b64encode(payload).decode("ascii")
        return PreparedImage(
            source_path=photo.path,
            data_url=f"data:image/jpeg;base64,{encoded}",
            captured_at=photo.captured_at,
            encoded_bytes=len(payload),
            original_size=original_size,
            prepared_size=prepared_size,
        )

    def _open_image(self, path: Path) -> Any:
        try:
            return Image.open(path)
        except UnidentifiedImageError as exc:
            if path.suffix.casefold() in RAW_EXTENSIONS:
                return self._open_raw_image(path)
            raise ImagePreparationError(f"Cannot decode image: {path}") from exc

    def _open_raw_image(self, path: Path) -> Any:
        try:
            import rawpy  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional rawpy
            raise ImagePreparationError(
                f"RAW photo support requires rawpy. Install the raw extra before processing {path}."
            ) from exc

        with rawpy.imread(str(path)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
        return Image.fromarray(rgb)


Transport = Callable[[str, Mapping[str, str], bytes, float], Mapping[str, Any]]


class OpenRouterPlaceIdentifier:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        endpoint: str = DEFAULT_OPENROUTER_ENDPOINT,
        timeout_seconds: float = 60,
        app_title: str = "Curator",
        http_referer: str | None = None,
        image_detail: str = "auto",
        transport: Transport | None = None,
    ) -> None:
        load_dotenv_file()
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self.model = model or os.getenv("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.app_title = app_title
        self.http_referer = http_referer or os.getenv("OPENROUTER_HTTP_REFERER")
        self.image_detail = image_detail
        self.transport = transport or self._urlopen_transport

    def identify_prepared_images(
        self,
        group_id: str,
        prepared_images: Sequence[PreparedImage],
        prompt: str | None = None,
    ) -> PlaceIdentification:
        if not prepared_images:
            raise ValueError("At least one prepared image is required.")

        payload = self._build_payload(group_id, prepared_images, prompt or load_place_identification_prompt())
        response = self._post(payload)
        content = self._extract_message_content(response)
        parsed = self._parse_json_content(content)

        return PlaceIdentification(
            group_id=group_id,
            place_name=str(parsed["place_name"]),
            confidence=float(parsed["confidence"]),
            is_unknown=bool(parsed["is_unknown"]),
            rationale=str(parsed["rationale"]),
            visual_evidence=tuple(str(item) for item in parsed["visual_evidence"]),
            alternate_guesses=tuple(str(item) for item in parsed["alternate_guesses"]),
            sampled_paths=tuple(image.source_path for image in prepared_images),
            raw_response=response,
        )

    def _build_payload(
        self,
        group_id: str,
        prepared_images: Sequence[PreparedImage],
        prompt_text: str,
    ) -> Mapping[str, Any]:
        metadata_lines = [f"group_id: {group_id}", "sampled_images:"]
        for index, image in enumerate(prepared_images, start=1):
            timestamp = image.captured_at.isoformat() if image.captured_at else "unknown"
            metadata_lines.append(
                f"- image_{index}: file={image.source_path.name}; captured_at={timestamp}; "
                f"prepared_size={format_size(image.prepared_size)}; jpeg_bytes={image.encoded_bytes}"
            )

        content: list[Mapping[str, Any]] = [
            {"type": "text", "text": prompt_text},
            {"type": "text", "text": "\n".join(metadata_lines)},
        ]
        for image in prepared_images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image.data_url,
                        "detail": self.image_detail,
                    },
                }
            )

        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": PLACE_RESPONSE_SCHEMA,
            "temperature": 0.1,
            "max_tokens": 400,
        }

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is required to call OpenRouter.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": self.app_title,
        }
        if self.http_referer:
            headers["HTTP-Referer"] = self.http_referer

        body = json.dumps(payload).encode("utf-8")
        return self.transport(self.endpoint, headers, body, self.timeout_seconds)

    @staticmethod
    def _urlopen_transport(
        endpoint: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        request = urllib.request.Request(endpoint, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise OpenRouterError(f"OpenRouter HTTP {exc.code}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise OpenRouterError("OpenRouter returned invalid JSON.") from exc

    @staticmethod
    def _extract_message_content(response: Mapping[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterError(f"OpenRouter response missing message content: {response}") from exc
        if not isinstance(content, str):
            raise OpenRouterError(f"OpenRouter message content was not text: {content!r}")
        return content

    @staticmethod
    def _parse_json_content(content: str) -> Mapping[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenRouterError(f"Model returned non-JSON content: {content}") from exc

        required = {
            "place_name",
            "confidence",
            "is_unknown",
            "rationale",
            "visual_evidence",
            "alternate_guesses",
        }
        missing = required.difference(parsed)
        if missing:
            raise OpenRouterError(f"Model response missing keys: {sorted(missing)}")
        return parsed


def identify_places_for_groups(
    groups: Mapping[str, Sequence[PhotoInput]],
    identifier: OpenRouterPlaceIdentifier | None = None,
    preprocessor: ImagePreprocessor | None = None,
    max_samples_per_group: int = 2,
    rng: random.Random | None = None,
) -> dict[str, PlaceIdentification]:
    identifier = identifier or OpenRouterPlaceIdentifier()
    preprocessor = preprocessor or ImagePreprocessor()
    results: dict[str, PlaceIdentification] = {}

    for group_id, photos in groups.items():
        candidates = coerce_photo_candidates(photos)
        samples = select_place_identification_samples(
            candidates,
            max_samples=max_samples_per_group,
            rng=rng,
        )
        prepared = [preprocessor.prepare(photo) for photo in samples]
        results[group_id] = identifier.identify_prepared_images(group_id, prepared)

    return results


def format_size(size: tuple[int, int] | None) -> str:
    if not size:
        return "unknown"
    return f"{size[0]}x{size[1]}"
