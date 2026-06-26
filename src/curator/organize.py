from __future__ import annotations

import base64
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .metadata import capture_timestamps, metadata_cache_path
from .paths import is_relative_to, safe_component, safe_human_component
from .place_identification import (
    DEFAULT_JPEG_QUALITY,
    DEFAULT_MAX_IMAGE_SIDE,
    ImagePreprocessor,
    ImagePreparationError,
    OpenRouterPlaceIdentifier,
    PhotoCandidate,
    PlaceIdentification,
    PreparedImage,
    load_place_identification_prompt,
    select_place_identification_samples,
)
from .plan import Operation, Plan, make_plan, new_run_id
from .progress import ProgressReporter
from .review_ui import BrowserReviewSession, FinalReviewResult, LocationSuggestion, ReviewItem, SequentialReviewState
from .scan import MediaFile, scan_media

SHOOT_GAP_SECONDS = 60 * 60
PLACE_IMAGE_EXTENSIONS = {
    ".arw",
    ".cr2",
    ".cr3",
    ".dng",
    ".heic",
    ".jpeg",
    ".jpg",
    ".nef",
    ".orf",
    ".png",
    ".raf",
    ".rw2",
    ".tif",
    ".tiff",
}


@dataclass(frozen=True)
class MediaBundle:
    group_id: str
    source_parent: Path
    fallback_name: str
    media: tuple[MediaFile, ...]


UnknownPlaceReviewer = Callable[[PlaceIdentification, Sequence[PreparedImage]], PlaceIdentification]


def build_organize_plan(
    source: Path,
    library: Path,
    *,
    mode: str,
    transfer: str = "copy",
    identify_places: bool = False,
    review_unknown_places: bool = False,
    review_ui: bool = False,
    unknown_place_reviewer: UnknownPlaceReviewer | None = None,
    place_identifier: OpenRouterPlaceIdentifier | None = None,
    place_identifications: Mapping[str, PlaceIdentification] | None = None,
    progress: ProgressReporter | None = None,
) -> Plan:
    progress = progress or ProgressReporter.disabled()
    if mode not in {"ongoing", "migration"}:
        raise ValueError("mode must be 'ongoing' or 'migration'")
    if transfer not in {"copy", "move"}:
        raise ValueError("transfer must be 'copy' or 'move'")

    source = source.expanduser().resolve()
    library = library.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"organize source must be a directory: {source}")
    originals = library / "Originals"
    run_id = new_run_id(f"organize-{mode}")
    with progress.step(
        f"Scanning {source} for media files",
        done=lambda: f"Found {len(files)} media file(s)",
    ):
        files = scan_media(source, hash_files=False)
    operations: list[Operation] = []

    timestamped_files = []
    timestamp_cache = metadata_cache_path(library)
    with progress.step(
        "Filtering media that needs organizing",
        done=lambda: f"Planning {len(timestamped_files)} media file(s)",
        debug=True,
    ):
        for media in files:
            if mode == "ongoing" and is_relative_to(media.path, originals):
                continue
            timestamped_files.append(media)
    timestamps = capture_timestamps(
        [media.path for media in timestamped_files],
        cache_path=timestamp_cache,
        progress=progress,
    )
    with progress.step(
        "Bundling media into shoots",
        done=lambda: f"Built {len(bundles)} bundle(s)",
        debug=True,
    ):
        bundles = build_media_bundles(source, timestamped_files, timestamps)

    identifications = dict(place_identifications or {})
    image_identifications: dict[str, PlaceIdentification] = {}
    if identify_places:
        identified_places = identify_bundle_places(
            bundles,
            timestamps,
            identifier=place_identifier,
            review_unknown_places=review_unknown_places,
            review_ui=review_ui,
            unknown_place_reviewer=unknown_place_reviewer,
            progress=progress,
        )
        final_decisions = getattr(identified_places, "decisions", None)
        if isinstance(identified_places, FinalReviewResult) or isinstance(final_decisions, dict):
            identifications.update(final_decisions or {})
            image_identifications.update(getattr(identified_places, "image_locations", {}) or {})
        else:
            identifications.update(identified_places)

    with progress.step(
        "Building organize operations",
        done=lambda: f"Planned {len(operations)} file operation(s)",
    ):
        bundle_destinations = assign_bundle_destination_names(bundles, identifications)
        for bundle in bundles:
            for media in bundle.media:
                image_identification = image_identifications.get(str(media.path))
                if image_identification is None:
                    country_name, folder_name = bundle_destinations[bundle.group_id]
                else:
                    country_name, folder_name = bundle_destination_names(bundle, image_identification)
                captured = timestamps[media.path]
                dest = originals / country_name / folder_name / media.path.name
                place_identification = image_identification or identifications.get(bundle.group_id)
                operations.append(
                    Operation(
                        type=transfer,
                        src=str(media.path),
                        dest=str(dest),
                        reason=f"organize-{mode}",
                        expected_size=media.size if transfer == "copy" else None,
                        metadata={
                            "bundle_id": bundle.group_id,
                            "bundle_fallback_name": bundle.fallback_name,
                            "timestamp_source": captured.source,
                            "timestamp_raw": captured.raw,
                            "capture_epoch": captured.epoch,
                            "source_parent": str(bundle.source_parent),
                            "identified_country_or_region": place_identification.country_or_region
                            if place_identification
                            else None,
                            "identified_place_name": place_identification.place_name if place_identification else None,
                            "identified_confidence": place_identification.confidence if place_identification else None,
                            "identified_is_unknown": place_identification.is_unknown if place_identification else None,
                        },
                    )
                )

    return make_plan(
        run_id=run_id,
        description=f"organize {source} into {originals} ({mode}, {transfer})",
        operations=operations,
        metadata={
            "kind": "organize",
            "mode": mode,
            "transfer": transfer,
            "source": str(source),
            "library": str(library),
            "files_planned": len(operations),
            "bundle_count": len(bundles),
            "identified_bundle_count": len(identifications),
            "layout": "Originals/Country/Album",
            "unknown_country": "Unsorted",
            "timestamp_source": "exiftool_then_sips_then_mdls_then_filesystem_mtime",
            "metadata_cache": str(timestamp_cache),
        },
    )


def build_media_bundles(
    source: Path,
    files: list[MediaFile],
    timestamps: Mapping[Path, object],
) -> list[MediaBundle]:
    by_parent: dict[Path, list[MediaFile]] = defaultdict(list)
    for media in files:
        by_parent[media.path.parent].append(media)

    bundles: list[MediaBundle] = []
    for parent, group in sorted(by_parent.items(), key=lambda item: str(item[0])):
        sorted_group = sorted(group, key=lambda media: timestamps[media.path].epoch)  # type: ignore[attr-defined]
        name_ranks = filename_ranks(group)
        shoot_index = 1
        previous_ts: float | None = None
        grouped: list[MediaFile] = []
        rank_min: int | None = None
        rank_max: int | None = None

        def flush() -> None:
            nonlocal rank_min, rank_max
            if not grouped:
                return
            fallback_name = safe_component(parent.name or "Shoot")
            if shoot_index > 1:
                fallback_name = f"{fallback_name}-{shoot_index:02d}"
            relative_parent = relative_parent_id(parent, source)
            bundles.append(
                MediaBundle(
                    group_id=f"{relative_parent}::{shoot_index:02d}",
                    source_parent=parent,
                    fallback_name=fallback_name,
                    media=tuple(grouped),
                )
            )
            grouped.clear()
            rank_min = None
            rank_max = None

        for media in sorted_group:
            captured = timestamps[media.path]  # type: ignore[index]
            rank = name_ranks[media.path]
            if previous_ts is not None and (
                captured.epoch - previous_ts > SHOOT_GAP_SECONDS
                or not rank_is_adjacent(rank, rank_min, rank_max)
            ):
                flush()
                shoot_index += 1
            previous_ts = captured.epoch
            grouped.append(media)
            rank_min = rank if rank_min is None else min(rank_min, rank)
            rank_max = rank if rank_max is None else max(rank_max, rank)
        flush()

    return bundles


def filename_ranks(group: list[MediaFile]) -> dict[Path, int]:
    ordered = sorted(group, key=lambda media: filename_sequence_key(media.path.name))
    return {media.path: index for index, media in enumerate(ordered)}


def filename_sequence_key(name: str) -> tuple[int, object, str]:
    stem = Path(name).stem
    match = re.search(r"(\d+)$", stem)
    if match:
        return (0, int(match.group(1)), name.casefold())
    return (1, natural_name_key(name), name.casefold())


def natural_name_key(name: str) -> tuple[tuple[int, object], ...]:
    parts: list[tuple[int, object]] = []
    for part in re.split(r"(\d+)", name.casefold()):
        if part.isdigit():
            parts.append((0, int(part)))
        elif part:
            parts.append((1, part))
    return tuple(parts)


def rank_is_adjacent(rank: int, rank_min: int | None, rank_max: int | None) -> bool:
    if rank_min is None or rank_max is None:
        return True
    return rank_min - 1 <= rank <= rank_max + 1


def relative_parent_id(parent: Path, source: Path) -> str:
    try:
        relative = parent.relative_to(source)
    except ValueError:
        relative = Path(parent.name)
    return safe_component(relative.as_posix(), "root")


def bundle_destination_names(
    bundle: MediaBundle,
    identification: PlaceIdentification | None,
) -> tuple[str, str]:
    if identification is None:
        return "Unsorted", bundle.fallback_name

    country = safe_human_component(identification.country_or_region, "Unsorted")
    if identification.is_unknown or country.casefold() in {"unknown", "unknown country", "unknown location"}:
        country = "Unsorted"

    place = safe_human_component(identification.place_name, bundle.fallback_name)
    return country, place


def assign_bundle_destination_names(
    bundles: list[MediaBundle],
    identifications: Mapping[str, PlaceIdentification],
) -> dict[str, tuple[str, str]]:
    assigned: dict[str, tuple[str, str]] = {}
    used: set[tuple[str, str]] = set()
    for bundle in bundles:
        country, folder = bundle_destination_names(bundle, identifications.get(bundle.group_id))
        candidate = (country, folder)
        if candidate in used:
            suffix = safe_human_component(bundle.fallback_name, "Bundle")
            candidate = (country, f"{folder} - {suffix}")
            counter = 2
            while candidate in used:
                candidate = (country, f"{folder} - {suffix} {counter}")
                counter += 1
        used.add(candidate)
        assigned[bundle.group_id] = candidate
    return assigned


def identify_bundle_places(
    bundles: list[MediaBundle],
    timestamps: Mapping[Path, object],
    *,
    identifier: OpenRouterPlaceIdentifier | None = None,
    review_unknown_places: bool = False,
    review_ui: bool = False,
    unknown_place_reviewer: UnknownPlaceReviewer | None = None,
    progress: ProgressReporter | None = None,
) -> dict[str, PlaceIdentification] | FinalReviewResult:
    progress = progress or ProgressReporter.disabled()
    if review_ui:
        return identify_bundle_places_with_review_ui(bundles, timestamps, identifier=identifier, progress=progress)

    identifier = identifier or OpenRouterPlaceIdentifier()
    preprocessor = ImagePreprocessor()
    reviewer = unknown_place_reviewer or review_unknown_place_interactively
    results: dict[str, PlaceIdentification] = {}

    identifiable_bundles = [bundle for bundle in bundles if place_photo_candidates(bundle, timestamps)]
    for index, bundle in enumerate(identifiable_bundles, start=1):
        photos = place_photo_candidates(bundle, timestamps)
        try:
            with progress.step(
                f"Identifying place for bundle {index}/{len(identifiable_bundles)} ({bundle.group_id})",
                done=lambda bundle=bundle: f"Identified place for {bundle.group_id}",
            ):
                samples = select_place_identification_samples(photos)
                prepared = [preprocessor.prepare(photo) for photo in samples]
                identification = identifier.identify_prepared_images(bundle.group_id, prepared)
        except ImagePreparationError:
            progress.log(f"Skipped place identification for {bundle.group_id}; image preparation failed")
            continue
        if review_unknown_places and identification.is_unknown:
            identification = reviewer(identification, prepared)
        results[bundle.group_id] = identification

    return results


def identify_bundle_places_with_review_ui(
    bundles: list[MediaBundle],
    timestamps: Mapping[Path, object],
    *,
    identifier: OpenRouterPlaceIdentifier | None = None,
    progress: ProgressReporter | None = None,
) -> FinalReviewResult:
    progress = progress or ProgressReporter.disabled()
    identifier = identifier or OpenRouterPlaceIdentifier()
    model_preprocessor = ImagePreprocessor()
    gallery_preprocessor = ImagePreprocessor(
        max_side=max(256, DEFAULT_MAX_IMAGE_SIDE // 2),
        jpeg_quality=max(1, min(95, DEFAULT_JPEG_QUALITY)),
    )
    results: dict[str, PlaceIdentification] = {}
    accepted: list[LocationSuggestion] = []
    base_prompt = load_place_identification_prompt()

    progress.log(f"Opening browser review UI for {len(bundles)} bundle(s)")
    with BrowserReviewSession(total=len(bundles)) as session:
        for index, bundle in enumerate(bundles):
            photos = place_photo_candidates(bundle, timestamps)
            if not photos:
                continue
            try:
                with progress.step(
                    f"Preparing browser review item {index + 1}/{len(bundles)} ({bundle.group_id})",
                    done=lambda bundle=bundle: f"Prepared browser review item for {bundle.group_id}",
                    debug=True,
                ):
                    samples = select_place_identification_samples(photos)
                    prepared_samples = [model_preprocessor.prepare(photo) for photo in samples]
                    prompt = place_prompt_with_context(base_prompt, accepted)
                    start_llm_identification(
                        session.state,
                        identifier=identifier,
                        group_id=bundle.group_id,
                        prepared_images=tuple(prepared_samples),
                        prompt=prompt,
                        progress=progress,
                    )
                    gallery_images = prepare_gallery_images(photos, gallery_preprocessor)
            except ImagePreparationError:
                progress.log(f"Skipped browser review item for {bundle.group_id}; image preparation failed")
                continue

            progress.log(f"Waiting for browser review {index + 1}/{len(bundles)} ({bundle.group_id})", debug=True)
            reviewed = session.review(
                ReviewItem(
                    identification=pending_place_identification(bundle, prepared_samples),
                    prepared_images=tuple(gallery_images or prepared_samples),
                    file_count=len(bundle.media),
                    suggestions=tuple(accepted),
                    context_summary=active_context_summary(accepted),
                    llm_pending=True,
                ),
                index=index,
            )
            results[bundle.group_id] = reviewed
            add_location_suggestion(accepted, reviewed)
            progress.log(
                f"Accepted location for {bundle.group_id}: {reviewed.country_or_region} / {reviewed.place_name}",
                debug=True,
            )

        progress.log("Waiting for final browser review approval")
        final_review = session.finalize()

    return final_review


def place_photo_candidates(bundle: MediaBundle, timestamps: Mapping[Path, object]) -> list[PhotoCandidate]:
    candidates: list[PhotoCandidate] = []
    for media in bundle.media:
        if media.path.suffix.casefold() not in PLACE_IMAGE_EXTENSIONS:
            continue
        captured = timestamps[media.path]  # type: ignore[index]
        candidates.append(
            PhotoCandidate(
                path=media.path,
                captured_at=datetime.fromtimestamp(captured.epoch),  # type: ignore[attr-defined]
                timestamp_source=captured.source,  # type: ignore[attr-defined]
            )
        )
    return candidates


def prepare_gallery_images(
    photos: Sequence[PhotoCandidate],
    preprocessor: ImagePreprocessor,
) -> list[PreparedImage]:
    prepared: list[PreparedImage] = []
    for photo in photos:
        try:
            prepared.append(preprocessor.prepare(photo))
        except ImagePreparationError:
            continue
    return prepared


def start_llm_identification(
    state: SequentialReviewState,
    *,
    identifier: OpenRouterPlaceIdentifier,
    group_id: str,
    prepared_images: tuple[PreparedImage, ...],
    prompt: str,
    progress: ProgressReporter,
) -> threading.Thread:
    def identify() -> None:
        try:
            identification = identifier.identify_prepared_images(group_id, prepared_images, prompt=prompt)
        except Exception as exc:
            state.store_llm_error(group_id, exc)
            progress.log(f"LLM place identification failed for {group_id}: {exc}")
            return

        state.store_llm_result(identification)
        progress.log(f"LLM place identification ready for {group_id}")

    thread = threading.Thread(
        target=identify,
        name=f"curator-llm-{safe_component(group_id, 'group')}",
        daemon=True,
    )
    thread.start()
    return thread


def pending_place_identification(
    bundle: MediaBundle,
    prepared_samples: Sequence[PreparedImage],
) -> PlaceIdentification:
    return PlaceIdentification(
        group_id=bundle.group_id,
        country_or_region="Unsorted",
        place_name=bundle.fallback_name,
        confidence=0.0,
        is_unknown=True,
        rationale="LLM data was not available when this album was reviewed.",
        visual_evidence=(),
        alternate_guesses=(),
        sampled_paths=tuple(image.source_path for image in prepared_samples),
        raw_response={},
    )


def add_location_suggestion(
    suggestions: list[LocationSuggestion],
    identification: PlaceIdentification,
) -> None:
    country = identification.country_or_region.strip()
    place = identification.place_name.strip()
    if not country or not place:
        return
    candidate = LocationSuggestion(country, place)
    normalized = (country.casefold(), place.casefold())
    for existing in suggestions:
        if (existing.country_or_region.casefold(), existing.place_name.casefold()) == normalized:
            return
    suggestions.append(candidate)


def active_context_summary(suggestions: Sequence[LocationSuggestion]) -> str:
    if not suggestions:
        return "No reviewed locations yet."
    active_country = suggestions[-1].country_or_region
    active = [suggestion.place_name for suggestion in suggestions if suggestion.country_or_region == active_country]
    return f"Active context: {active_country} / {', '.join(active[-8:])}"


def place_prompt_with_context(base_prompt: str, suggestions: Sequence[LocationSuggestion]) -> str:
    if not suggestions:
        return base_prompt

    active_country = suggestions[-1].country_or_region
    active_places = [suggestion.place_name for suggestion in suggestions if suggestion.country_or_region == active_country]
    history = "\n".join(
        f"- {index + 1}. {suggestion.country_or_region} / {suggestion.place_name}"
        for index, suggestion in enumerate(suggestions[-20:])
    )
    active = "\n".join(f"- {place}" for place in active_places[-12:])

    return (
        f"{base_prompt}\n\n"
        "Previously reviewed album locations, in processing order:\n"
        f"{history}\n\n"
        f"Current active country/region from the most recent user-reviewed album: {active_country}\n"
        "Recent accepted places in this active country/region:\n"
        f"{active}\n\n"
        "Use this history as trip context. Nearby albums are often from the same country or area, "
        "so if the current images are generic but visually compatible with the active context, prefer "
        "a plausible location in that active context. However, context can switch: if the current images "
        "or user-reviewed history indicate a new country/region, do not force the older country/region. "
        "Country changes should reset which local context is considered most relevant."
    )


def review_unknown_place_interactively(
    identification: PlaceIdentification,
    prepared_images: Sequence[PreparedImage],
) -> PlaceIdentification:
    with tempfile.TemporaryDirectory(prefix="curator-place-review-") as temp_dir:
        paths = write_prepared_gallery_images(prepared_images, Path(temp_dir))
        if paths:
            print(
                f"\nUnknown place for {identification.group_id}. Opening sample gallery; close it with Esc."
            )
            if shutil.which("qlmanage"):
                subprocess.run(
                    ["qlmanage", "-p", *(str(path) for path in paths)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                for path in paths:
                    print(path)

        print(f"Model guess: {identification.country_or_region} / {identification.place_name}")
        entered = input("Location (Country/Place, Place only, or blank to keep): ").strip()
        if not entered:
            return identification

        if "/" in entered:
            country, place = [part.strip() for part in entered.split("/", 1)]
        else:
            country = identification.country_or_region
            place = entered
        if not country:
            country = "Unsorted"
        if not place:
            place = identification.place_name

        return replace(
            identification,
            country_or_region=country,
            place_name=place,
            confidence=1.0,
            is_unknown=False,
            rationale=f"User provided location: {entered}",
        )


def write_prepared_gallery_images(prepared_images: Sequence[PreparedImage], directory: Path) -> list[Path]:
    paths: list[Path] = []
    for index, image in enumerate(prepared_images, start=1):
        if "," not in image.data_url:
            continue
        encoded = image.data_url.split(",", 1)[1]
        path = directory / f"{index:02d}-{safe_component(image.source_path.stem, 'sample')}.jpg"
        path.write_bytes(base64.b64decode(encoded))
        paths.append(path)
    return paths
