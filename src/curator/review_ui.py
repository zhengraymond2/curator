from __future__ import annotations

import base64
import binascii
import json
import threading
import time
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlparse

from .place_identification import AlbumCountryContext, AlbumCountryGuess, PlaceIdentification, PreparedImage


@dataclass(frozen=True)
class LocationSuggestion:
    country_or_region: str
    place_name: str

    @property
    def label(self) -> str:
        return f"{self.country_or_region}/{self.place_name}"


@dataclass(frozen=True)
class ReviewItem:
    identification: PlaceIdentification
    prepared_images: tuple[PreparedImage, ...]
    file_count: int
    suggestions: tuple[LocationSuggestion, ...] = ()
    context_summary: str = ""
    llm_pending: bool = False
    images_pending: bool = False


ImageUrlBuilder = Callable[[str, int, int], str]
CountryBatchGuessStarter = Callable[[tuple[AlbumCountryContext, ...]], None]


@dataclass(frozen=True)
class ReviewedAlbum:
    item: ReviewItem
    decision: PlaceIdentification


@dataclass(frozen=True)
class FinalReviewResult:
    decisions: dict[str, PlaceIdentification]
    image_locations: dict[str, PlaceIdentification]
    validation_reporter: FinalValidationReporter | None = None


@dataclass(frozen=True)
class FinalValidationProgress:
    completed: int
    total: int
    started_at: float


class ReviewState:
    def __init__(
        self,
        items: Sequence[ReviewItem],
        *,
        wait_for_final_validation: bool = False,
        country_batch_guess_starter: CountryBatchGuessStarter | None = None,
    ) -> None:
        self.items = list(items)
        self.items_by_group_id = {item.identification.group_id: item for item in self.items}
        self.index = 0
        self.wait_for_final_validation = wait_for_final_validation
        self.decisions: dict[str, PlaceIdentification] = {}
        self.llm_data: dict[str, PlaceIdentification] = {}
        self.llm_errors: dict[str, str] = {}
        self.country_context_data: dict[str, PlaceIdentification] = {}
        self.image_data: dict[str, tuple[PreparedImage, ...]] = {}
        self.image_errors: dict[str, str] = {}
        self.image_loading: set[str] = {
            item.identification.group_id for item in self.items if item.images_pending
        }
        self.image_versions: dict[str, int] = {}
        self.accepted_suggestions: list[LocationSuggestion] = []
        self.reviewed: list[ReviewedAlbum] = []
        self.image_locations: dict[str, PlaceIdentification] = {}
        self.country_batch_guess_starter = country_batch_guess_starter
        self.country_guess_loading: set[str] = set()
        self.country_guess_pending: set[str] = set()
        self.country_batch_started = False
        self.country_locked: set[str] = set()
        self.final_ready = False
        self.final_validation_status: str | None = None
        self.final_validation_title = ""
        self.final_validation_message = ""
        self.final_validation_summary = ""
        self.final_validation_details = ""
        self.final_validation_dryrun_path = ""
        self.final_validation_progress: FinalValidationProgress | None = None
        self.final_validation_seen = threading.Event()
        self.commit_requested = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()

    def payload(self, image_url_builder: ImageUrlBuilder | None = None) -> Mapping[str, object]:
        batch_request: tuple[AlbumCountryContext, ...] = ()
        with self.lock:
            if self.final_validation_status is not None:
                payload = final_validation_payload(
                    status=self.final_validation_status,
                    title=self.final_validation_title,
                    message=self.final_validation_message,
                    summary=self.final_validation_summary,
                    details=self.final_validation_details,
                    dryrun_path=self.final_validation_dryrun_path,
                    progress=self.final_validation_progress_payload(),
                    index=self.index,
                    total=len(self.items),
                )
                if self.final_validation_status in {"passed", "failed"}:
                    self.final_validation_seen.set()
                return payload
            if self.final_ready:
                batch_request = self.country_batch_request_locked()
                payload = final_review_payload(
                    self.reviewed,
                    self.image_locations,
                    self.index,
                    len(self.items),
                    image_url_builder=image_url_builder,
                    image_versions=self.image_versions,
                    images_loading=self.image_loading,
                    country_guesses_loading=self.country_guess_loading,
                )
            else:
                if self.index >= len(self.items):
                    return {"done": True, "index": self.index, "total": len(self.items)}
                item = self.item_with_current_context(self.items[self.index])
                return review_item_payload(
                    item,
                    self.index,
                    len(self.items),
                    llm_data=self.llm_data,
                    llm_errors=self.llm_errors,
                    image_data=self.image_data,
                    image_errors=self.image_errors,
                    image_loading=self.image_loading,
                    image_versions=self.image_versions,
                    image_url_builder=image_url_builder,
                )
        start_batch_country_guess(self.country_batch_guess_starter, batch_request)
        return payload

    def decide(
        self,
        country_or_region: str | None,
        place_name: str | None,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        batch_request: tuple[AlbumCountryContext, ...] = ()
        with self.lock:
            if self.index >= len(self.items):
                self.done.set()
                return {"done": True}

            item = self.item_with_current_context(self.items[self.index])
            original = self.llm_data.get(item.identification.group_id, item.identification)
            self.country_context_data[original.group_id] = original
            country_was_explicit = country_supplied_by_user(country_or_region, place_name)
            suggestions = [*item.suggestions, *self.accepted_suggestions]
            country, place = resolve_location(
                country_or_region,
                place_name,
                original=original,
                suggestions=suggestions,
            )
            reviewed = replace(
                original,
                country_or_region=country,
                place_name=place,
                confidence=1.0 if (country, place) != (original.country_or_region, original.place_name) else original.confidence,
                is_unknown=False if place and not place.casefold().startswith("unknown") else original.is_unknown,
                rationale=f"User reviewed location: {country}/{place}",
            )
            self.decisions[original.group_id] = reviewed
            if country_was_explicit:
                self.country_locked.add(original.group_id)
                self.country_guess_pending.discard(original.group_id)
                self.country_guess_loading.discard(original.group_id)
            elif self.country_batch_guess_starter and place:
                self.country_locked.discard(original.group_id)
                self.country_guess_pending.add(original.group_id)
            self.reviewed.append(ReviewedAlbum(item=item, decision=reviewed))
            set_initial_image_locations(self.image_locations, item, reviewed)
            add_location_suggestion(self.accepted_suggestions, reviewed)
            self.index += 1
            if self.index >= len(self.items):
                self.final_ready = True
                batch_request = self.country_batch_request_locked()
                payload = final_review_payload(
                    self.reviewed,
                    self.image_locations,
                    self.index,
                    len(self.items),
                    image_url_builder=image_url_builder,
                    image_versions=self.image_versions,
                    images_loading=self.image_loading,
                    country_guesses_loading=self.country_guess_loading,
                )
            else:
                item = self.item_with_current_context(self.items[self.index])
                payload = review_item_payload(
                    item,
                    self.index,
                    len(self.items),
                    llm_data=self.llm_data,
                    llm_errors=self.llm_errors,
                    image_data=self.image_data,
                    image_errors=self.image_errors,
                    image_loading=self.image_loading,
                    image_versions=self.image_versions,
                    image_url_builder=image_url_builder,
                )
        start_batch_country_guess(self.country_batch_guess_starter, batch_request)
        return payload

    def store_llm_result(self, identification: PlaceIdentification) -> None:
        batch_request: tuple[AlbumCountryContext, ...] = ()
        with self.lock:
            self.llm_data[identification.group_id] = identification
            self.country_context_data[identification.group_id] = identification
            self.llm_errors.pop(identification.group_id, None)
            if self.final_ready:
                batch_request = self.country_batch_request_locked()
        start_batch_country_guess(self.country_batch_guess_starter, batch_request)

    def store_llm_error(self, group_id: str, error: Exception) -> None:
        batch_request: tuple[AlbumCountryContext, ...] = ()
        with self.lock:
            self.llm_errors[group_id] = str(error)
            if self.final_ready:
                batch_request = self.country_batch_request_locked()
        start_batch_country_guess(self.country_batch_guess_starter, batch_request)

    def store_images(
        self,
        group_id: str,
        images: Sequence[PreparedImage],
        *,
        complete: bool,
    ) -> None:
        with self.lock:
            prepared = tuple(images)
            self.image_data[group_id] = prepared
            self.image_errors.pop(group_id, None)
            if complete:
                self.image_loading.discard(group_id)
            else:
                self.image_loading.add(group_id)
            self.image_versions[group_id] = self.image_versions.get(group_id, 0) + 1
            self.refresh_reviewed_images(group_id, prepared)

    def store_image_error(self, group_id: str, error: Exception) -> None:
        with self.lock:
            self.image_errors[group_id] = str(error)
            self.image_loading.discard(group_id)
            self.image_versions[group_id] = self.image_versions.get(group_id, 0) + 1

    def country_batch_request_locked(self) -> tuple[AlbumCountryContext, ...]:
        if self.country_batch_started or not self.country_batch_guess_starter:
            return ()
        group_ids = [
            album.decision.group_id
            for album in self.reviewed
            if album.decision.group_id in self.country_guess_pending and album.decision.group_id not in self.country_locked
        ]
        if not group_ids:
            self.country_guess_loading.clear()
            return ()

        self.country_guess_loading = set(group_ids)
        waiting = [
            group_id
            for group_id in group_ids
            if self.items_by_group_id.get(group_id, None) is not None
            and self.items_by_group_id[group_id].llm_pending
            and group_id not in self.llm_data
            and group_id not in self.llm_errors
        ]
        if waiting:
            return ()

        contexts = []
        for album in self.reviewed:
            group_id = album.decision.group_id
            if group_id not in group_ids:
                continue
            context = self.llm_data.get(group_id) or self.country_context_data.get(group_id) or album.decision
            contexts.append(
                AlbumCountryContext(
                    group_id=group_id,
                    album_name=album.decision.place_name,
                    llm_response=context,
                )
            )
        self.country_batch_started = True
        return tuple(contexts)

    def store_country_batch_guesses(self, guesses: Sequence[AlbumCountryGuess]) -> None:
        guesses_by_group = {guess.group_id: guess for guess in guesses}
        with self.lock:
            for group_id in list(self.country_guess_loading):
                guess = guesses_by_group.get(group_id)
                if guess is None:
                    self.country_guess_loading.discard(group_id)
                    self.country_guess_pending.discard(group_id)
                    continue
                if group_id in self.country_locked:
                    self.country_guess_loading.discard(group_id)
                    self.country_guess_pending.discard(group_id)
                    continue
                for index, reviewed_album in enumerate(self.reviewed):
                    decision = reviewed_album.decision
                    if decision.group_id != group_id or normalize_text(decision.place_name) != normalize_text(guess.album_name):
                        continue
                    updated = updated_final_decision(
                        decision,
                        guess.country_or_region,
                        decision.place_name,
                        rationale_prefix="LLM guessed country after batch album review",
                        confidence=guess.confidence,
                    )
                    self.reviewed[index] = ReviewedAlbum(item=reviewed_album.item, decision=updated)
                    for path, image_decision in list(self.image_locations.items()):
                        if image_decision.group_id == group_id and normalize_text(image_decision.place_name) == normalize_text(guess.album_name):
                            self.image_locations[path] = updated_final_decision(
                                image_decision,
                                guess.country_or_region,
                                image_decision.place_name,
                                rationale_prefix="LLM guessed country after batch album review",
                                confidence=guess.confidence,
                            )
                    self.country_guess_loading.discard(group_id)
                    self.country_guess_pending.discard(group_id)
                    break
            self.decisions = reviewed_decisions(self.reviewed)

    def store_country_batch_error(self, error: Exception) -> None:
        with self.lock:
            self.country_guess_loading.clear()

    def image_url(self, group_id: str, index: int, version: int) -> str:
        return f"/api/image?{urlencode({'group_id': group_id, 'index': index, 'v': version})}"

    def image_response(self, group_id: str, index: int) -> tuple[bytes, str] | None:
        with self.lock:
            images = self.images_for_group(group_id)
            if index < 0 or index >= len(images):
                return None
            return prepared_image_response(images[index])

    def images_for_group(self, group_id: str) -> tuple[PreparedImage, ...]:
        if group_id in self.image_data:
            return self.image_data[group_id]
        item = self.items_by_group_id.get(group_id)
        return item.prepared_images if item else ()

    def item_with_current_context(self, item: ReviewItem) -> ReviewItem:
        group_id = item.identification.group_id
        suggestions = merge_location_suggestions(item.suggestions, self.accepted_suggestions)
        return replace(
            item,
            prepared_images=self.images_for_group(group_id),
            suggestions=tuple(suggestions),
            context_summary=review_context_summary(suggestions),
        )

    def refresh_reviewed_images(self, group_id: str, images: Sequence[PreparedImage]) -> None:
        for index, reviewed_album in enumerate(self.reviewed):
            if reviewed_album.decision.group_id != group_id:
                continue
            item = replace(reviewed_album.item, prepared_images=tuple(images))
            self.reviewed[index] = ReviewedAlbum(item=item, decision=reviewed_album.decision)
            set_initial_image_locations(self.image_locations, item, reviewed_album.decision)

    def final_result(self, validation_reporter: FinalValidationReporter | None = None) -> FinalReviewResult:
        with self.lock:
            return FinalReviewResult(
                decisions=reviewed_decisions(self.reviewed),
                image_locations=dict(self.image_locations),
                validation_reporter=validation_reporter,
            )

    def approve_final_review(self) -> Mapping[str, object]:
        with self.lock:
            if self.wait_for_final_validation:
                self.final_ready = False
                self.final_validation_status = "staging"
                self.final_validation_title = "Preparing commit"
                self.final_validation_message = "Curator is writing DRYRUN.txt in the source folder."
                self.final_validation_summary = ""
                self.final_validation_details = ""
                self.final_validation_dryrun_path = ""
                self.final_validation_progress = None
                self.final_validation_seen.clear()
                self.done.set()
                return final_validation_payload(
                    status=self.final_validation_status,
                    title=self.final_validation_title,
                    message=self.final_validation_message,
                    summary=self.final_validation_summary,
                    details=self.final_validation_details,
                    dryrun_path=self.final_validation_dryrun_path,
                    progress=self.final_validation_progress_payload(),
                    index=self.index,
                    total=len(self.items),
                )
            self.final_ready = False
            self.done.set()
            return {"done": True, "index": self.index, "total": len(self.items)}

    def start_final_validation(self, message: str) -> None:
        with self.lock:
            self.final_validation_status = "pending"
            self.final_validation_title = "Copying and validating"
            self.final_validation_message = message
            self.final_validation_summary = ""
            self.final_validation_details = ""
            self.final_validation_progress = None
            self.final_validation_seen.clear()

    def ready_to_commit(self, dryrun_path: Path) -> None:
        with self.lock:
            self.final_validation_status = "ready"
            self.final_validation_title = "Ready to commit"
            self.final_validation_message = "DRYRUN.txt is staged in the source folder. Click Commit to copy into the destination."
            self.final_validation_summary = ""
            self.final_validation_details = ""
            self.final_validation_dryrun_path = str(dryrun_path)
            self.final_validation_progress = None
            self.final_validation_seen.clear()

    def request_commit(self) -> Mapping[str, object]:
        with self.lock:
            if self.final_validation_status == "ready":
                self.final_validation_status = "pending"
                self.final_validation_title = "Copying and validating"
                self.final_validation_message = "Curator is copying files, then checking checksums, file totals, and filenames."
                self.final_validation_summary = ""
                self.final_validation_details = ""
                self.final_validation_progress = None
                self.final_validation_seen.clear()
                self.commit_requested.set()
            return final_validation_payload(
                status=self.final_validation_status or "pending",
                title=self.final_validation_title,
                message=self.final_validation_message,
                summary=self.final_validation_summary,
                details=self.final_validation_details,
                dryrun_path=self.final_validation_dryrun_path,
                progress=self.final_validation_progress_payload(),
                index=self.index,
                total=len(self.items),
            )

    def update_final_validation_progress(self, completed: int, total: int) -> None:
        with self.lock:
            total = max(0, total)
            completed = max(0, completed)
            if total:
                completed = min(completed, total)
            else:
                completed = 0
            existing = self.final_validation_progress
            if existing is not None and existing.total == total and completed >= existing.completed:
                started_at = existing.started_at
            else:
                started_at = time.monotonic()
            self.final_validation_progress = FinalValidationProgress(
                completed=completed,
                total=total,
                started_at=started_at,
            )

    def final_validation_progress_payload(self) -> Mapping[str, object] | None:
        progress = self.final_validation_progress
        if progress is None:
            return None
        elapsed = max(0.0, time.monotonic() - progress.started_at)
        remaining = max(0, progress.total - progress.completed)
        estimated_remaining: float | None = None
        if progress.completed >= progress.total:
            estimated_remaining = 0.0
        elif progress.completed > 0:
            estimated_remaining = elapsed * remaining / progress.completed
        return {
            "completed": progress.completed,
            "total": progress.total,
            "remaining": remaining,
            "elapsed_seconds": elapsed,
            "estimated_remaining_seconds": estimated_remaining,
        }

    def complete_final_validation(
        self,
        *,
        success: bool,
        title: str,
        message: str,
        summary: str,
        details: str = "",
    ) -> None:
        with self.lock:
            self.final_validation_status = "passed" if success else "failed"
            self.final_validation_title = title
            self.final_validation_message = message
            self.final_validation_summary = summary
            self.final_validation_details = details
            self.final_validation_seen.clear()

    def rename_album(
        self,
        target_key: str,
        country_or_region: str,
        folder_name: str | None = None,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        if folder_name is None:
            country_or_region, folder_name = "", country_or_region
        with self.lock:
            rename_reviewed_album(self.reviewed, self.image_locations, target_key, country_or_region, folder_name)
            self.decisions = reviewed_decisions(self.reviewed)
            lock_country_for_album(self.country_locked, self.reviewed, country_or_region, folder_name)
            return final_review_payload(
                self.reviewed,
                self.image_locations,
                self.index,
                len(self.items),
                image_url_builder=image_url_builder,
                image_versions=self.image_versions,
                images_loading=self.image_loading,
                country_guesses_loading=self.country_guess_loading,
            )

    def move_images(
        self,
        paths: Sequence[str],
        target_key: str,
        folder_name: str,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        with self.lock:
            move_reviewed_images(self.image_locations, paths, target_key, folder_name)
            return final_review_payload(
                self.reviewed,
                self.image_locations,
                self.index,
                len(self.items),
                image_url_builder=image_url_builder,
                image_versions=self.image_versions,
                images_loading=self.image_loading,
                country_guesses_loading=self.country_guess_loading,
            )


class FinalValidationReporter:
    def __init__(
        self,
        state: ReviewState,
        server: ThreadingHTTPServer,
        thread: threading.Thread,
        *,
        seen_timeout_seconds: float = 30.0,
    ) -> None:
        self.state = state
        self.server = server
        self.thread = thread
        self.seen_timeout_seconds = seen_timeout_seconds
        self._closed = False
        self._lock = threading.Lock()

    def start(self, message: str) -> None:
        self.state.start_final_validation(message)

    def operation_progress(self, completed: int, total: int) -> None:
        self.state.update_final_validation_progress(completed, total)

    def ready_to_commit(self, dryrun_path: Path) -> None:
        self.state.ready_to_commit(dryrun_path)

    def wait_for_commit(self) -> None:
        self.state.commit_requested.wait()

    def succeed(self, summary: str) -> None:
        self.state.complete_final_validation(
            success=True,
            title="Validation passed",
            message="Curator verified the final copy against the source.",
            summary=summary,
        )
        self.close_after_browser_sees_result()

    def fail(self, summary: str, details: str = "") -> None:
        self.state.complete_final_validation(
            success=False,
            title="Validation failed",
            message="Do not delete the original source folder. Curator found files or metadata that did not line up.",
            summary=summary,
            details=details,
        )
        self.close_after_browser_sees_result()

    def close_after_browser_sees_result(self) -> None:
        self.state.final_validation_seen.wait(timeout=self.seen_timeout_seconds)
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def review_place_identifications_in_browser(
    items: Sequence[ReviewItem],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    state_ready: Callable[[ReviewState], None] | None = None,
    wait_for_final_validation: bool = False,
) -> FinalReviewResult:
    if not items:
        return FinalReviewResult(decisions={}, image_locations={})

    return _review_place_identifications_in_browser(
        items,
        host=host,
        port=port,
        open_browser=open_browser,
        state_ready=state_ready,
        wait_for_final_validation=wait_for_final_validation,
    )


def _review_place_identifications_in_browser(
    items: Sequence[ReviewItem],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    state_ready: Callable[[ReviewState], None] | None = None,
    wait_for_final_validation: bool = False,
) -> FinalReviewResult:
    if not items:
        return FinalReviewResult(decisions={}, image_locations={})

    state = ReviewState(items, wait_for_final_validation=wait_for_final_validation)
    handler = make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://{actual_host}:{actual_port}/"
    shutdown_server = True
    validation_reporter: FinalValidationReporter | None = None

    try:
        if state_ready:
            state_ready(state)
        if open_browser:
            webbrowser.open(url)
        print(f"Review UI: {url}")
        state.done.wait()
        if wait_for_final_validation:
            validation_reporter = FinalValidationReporter(state, server, thread)
            shutdown_server = False
    finally:
        if shutdown_server:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    return state.final_result(validation_reporter=validation_reporter)


class BrowserReviewSession:
    def __init__(
        self,
        *,
        total: int,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
    ) -> None:
        self.state = SequentialReviewState(total)
        self.handler = make_handler(self.state)
        self.server = ThreadingHTTPServer((host, port), self.handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        actual_host, actual_port = self.server.server_address
        self.url = f"http://{actual_host}:{actual_port}/"
        self.open_browser = open_browser

    def __enter__(self) -> "BrowserReviewSession":
        self.thread.start()
        if self.open_browser:
            webbrowser.open(self.url)
        print(f"Review UI: {self.url}")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.state.finish()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def review(self, item: ReviewItem, *, index: int) -> PlaceIdentification:
        return self.state.review(item, index=index)

    def finalize(self) -> FinalReviewResult:
        return self.state.finalize()


class SequentialReviewState:
    def __init__(self, total: int) -> None:
        self.total = total
        self.index = 0
        self.current: ReviewItem | None = None
        self.decision: PlaceIdentification | None = None
        self.llm_data: dict[str, PlaceIdentification] = {}
        self.llm_errors: dict[str, str] = {}
        self.image_data: dict[str, tuple[PreparedImage, ...]] = {}
        self.image_errors: dict[str, str] = {}
        self.image_loading: set[str] = set()
        self.image_versions: dict[str, int] = {}
        self.reviewed: list[ReviewedAlbum] = []
        self.image_locations: dict[str, PlaceIdentification] = {}
        self.final_ready = False
        self.final_done = threading.Event()
        self.item_ready = threading.Event()
        self.item_done = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()

    def review(self, item: ReviewItem, *, index: int) -> PlaceIdentification:
        with self.lock:
            self.index = index
            self.current = item
            self.decision = None
            self.item_done.clear()
            if item.images_pending:
                self.image_loading.add(item.identification.group_id)
            self.item_ready.set()
        self.item_done.wait()
        with self.lock:
            if self.decision is None:
                raise RuntimeError("review UI closed before a decision was recorded")
            return self.decision

    def payload(self, image_url_builder: ImageUrlBuilder | None = None) -> Mapping[str, object]:
        with self.lock:
            if self.done.is_set():
                return {"done": True, "index": self.index, "total": self.total}
            if self.final_ready:
                return final_review_payload(
                    self.reviewed,
                    self.image_locations,
                    self.index,
                    self.total,
                    image_url_builder=image_url_builder,
                    image_versions=self.image_versions,
                    images_loading=self.image_loading,
                )
            if self.current is None:
                return {"loading": True, "done": False, "index": self.index, "total": self.total}
            return review_item_payload(
                self.current,
                self.index,
                self.total,
                llm_data=self.llm_data,
                llm_errors=self.llm_errors,
                image_data=self.image_data,
                image_errors=self.image_errors,
                image_loading=self.image_loading,
                image_versions=self.image_versions,
                image_url_builder=image_url_builder,
            )

    def decide(
        self,
        country_or_region: str | None,
        place_name: str | None,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        with self.lock:
            if self.current is None:
                return {"loading": True, "done": False, "index": self.index, "total": self.total}
            item = self.current
            original = self.llm_data.get(item.identification.group_id, item.identification)
            country, place = resolve_location(
                country_or_region,
                place_name,
                original=original,
                suggestions=item.suggestions,
            )
            self.decision = replace(
                original,
                country_or_region=country,
                place_name=place,
                confidence=1.0 if (country, place) != (original.country_or_region, original.place_name) else original.confidence,
                is_unknown=False if place and not place.casefold().startswith("unknown") else original.is_unknown,
                rationale=f"User reviewed location: {country}/{place}",
            )
            self.reviewed.append(ReviewedAlbum(item=item, decision=self.decision))
            set_initial_image_locations(self.image_locations, item, self.decision)
            self.current = None
            self.item_ready.clear()
            self.item_done.set()
            self.index += 1
            return {"loading": True, "done": False, "index": self.index, "total": self.total}

    def store_llm_result(self, identification: PlaceIdentification) -> None:
        with self.lock:
            self.llm_data[identification.group_id] = identification
            self.llm_errors.pop(identification.group_id, None)

    def store_llm_error(self, group_id: str, error: Exception) -> None:
        with self.lock:
            self.llm_errors[group_id] = str(error)

    def store_images(
        self,
        group_id: str,
        images: Sequence[PreparedImage],
        *,
        complete: bool,
    ) -> None:
        with self.lock:
            self.image_data[group_id] = tuple(images)
            self.image_errors.pop(group_id, None)
            if complete:
                self.image_loading.discard(group_id)
            else:
                self.image_loading.add(group_id)
            self.image_versions[group_id] = self.image_versions.get(group_id, 0) + 1

    def store_image_error(self, group_id: str, error: Exception) -> None:
        with self.lock:
            self.image_errors[group_id] = str(error)
            self.image_loading.discard(group_id)
            self.image_versions[group_id] = self.image_versions.get(group_id, 0) + 1

    def image_url(self, group_id: str, index: int, version: int) -> str:
        return f"/api/image?{urlencode({'group_id': group_id, 'index': index, 'v': version})}"

    def image_response(self, group_id: str, index: int) -> tuple[bytes, str] | None:
        with self.lock:
            images = self.images_for_group(group_id)
            if index < 0 or index >= len(images):
                return None
            return prepared_image_response(images[index])

    def images_for_group(self, group_id: str) -> tuple[PreparedImage, ...]:
        if group_id in self.image_data:
            return self.image_data[group_id]
        if self.current and self.current.identification.group_id == group_id:
            return self.current.prepared_images
        return ()

    def finalize(self) -> FinalReviewResult:
        with self.lock:
            self.current = None
            self.index = self.total
            self.final_ready = True
            self.item_ready.set()
        self.final_done.wait()
        with self.lock:
            return FinalReviewResult(
                decisions=reviewed_decisions(self.reviewed),
                image_locations=dict(self.image_locations),
            )

    def approve_final_review(self) -> Mapping[str, object]:
        with self.lock:
            self.final_ready = False
            self.done.set()
            self.final_done.set()
            self.item_ready.set()
            self.item_done.set()
            return {"done": True, "index": self.index, "total": self.total}

    def rename_album(
        self,
        target_key: str,
        country_or_region: str,
        folder_name: str | None = None,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        if folder_name is None:
            country_or_region, folder_name = "", country_or_region
        with self.lock:
            rename_reviewed_album(self.reviewed, self.image_locations, target_key, country_or_region, folder_name)
            return final_review_payload(
                self.reviewed,
                self.image_locations,
                self.index,
                self.total,
                image_url_builder=image_url_builder,
                image_versions=self.image_versions,
                images_loading=self.image_loading,
            )

    def move_images(
        self,
        paths: Sequence[str],
        target_key: str,
        folder_name: str,
        image_url_builder: ImageUrlBuilder | None = None,
    ) -> Mapping[str, object]:
        with self.lock:
            move_reviewed_images(self.image_locations, paths, target_key, folder_name)
            return final_review_payload(
                self.reviewed,
                self.image_locations,
                self.index,
                self.total,
                image_url_builder=image_url_builder,
                image_versions=self.image_versions,
                images_loading=self.image_loading,
            )

    def finish(self) -> None:
        with self.lock:
            self.current = None
            self.final_ready = False
            self.done.set()
            self.final_done.set()
            self.item_ready.set()
            self.item_done.set()


def review_item_payload(
    item: ReviewItem,
    index: int,
    total: int,
    *,
    llm_data: Mapping[str, PlaceIdentification] | None = None,
    llm_errors: Mapping[str, str] | None = None,
    image_data: Mapping[str, tuple[PreparedImage, ...]] | None = None,
    image_errors: Mapping[str, str] | None = None,
    image_loading: set[str] | None = None,
    image_versions: Mapping[str, int] | None = None,
    image_url_builder: ImageUrlBuilder | None = None,
) -> Mapping[str, object]:
    group_id = item.identification.group_id
    llm_data = llm_data or {}
    llm_errors = llm_errors or {}
    image_data = image_data or {}
    image_errors = image_errors or {}
    image_loading = image_loading or set()
    image_versions = image_versions or {}
    identification = llm_data.get(group_id, item.identification)
    llm_loading = item.llm_pending and group_id not in llm_data and group_id not in llm_errors
    llm_error = llm_errors.get(group_id, "")
    images = image_data.get(group_id, item.prepared_images)
    images_loading = item.images_pending and group_id not in image_errors and (
        group_id not in image_data or group_id in image_loading
    )
    image_error = image_errors.get(group_id, "")
    image_version = image_versions.get(group_id, 0)
    return {
        "done": False,
        "index": index,
        "total": total,
        "group_id": group_id,
        "country_or_region": "" if llm_loading else identification.country_or_region,
        "place_name": "" if llm_loading else identification.place_name,
        "confidence": None if llm_loading else identification.confidence,
        "is_unknown": False if llm_loading else identification.is_unknown,
        "rationale": "" if llm_loading else identification.rationale,
        "visual_evidence": [] if llm_loading else list(identification.visual_evidence),
        "alternate_guesses": [] if llm_loading else list(identification.alternate_guesses),
        "llm_loading": llm_loading,
        "llm_error": llm_error,
        "images_loading": images_loading,
        "image_error": image_error,
        "images_version": image_version,
        "file_count": item.file_count,
        "context_summary": item.context_summary,
        "suggestions": [
            {
                "country_or_region": suggestion.country_or_region,
                "place_name": suggestion.place_name,
                "label": suggestion.label,
            }
            for suggestion in item.suggestions
        ],
        "images": [
            {
                "src": image_url_builder(group_id, image_index, image_version)
                if image_url_builder
                else image.data_url,
                "filename": image.source_path.name,
                "path": str(image.source_path),
                "prepared_size": image.prepared_size,
                "encoded_bytes": image.encoded_bytes,
            }
            for image_index, image in enumerate(images)
        ],
    }


def final_review_payload(
    reviewed: Sequence[ReviewedAlbum],
    image_locations: Mapping[str, PlaceIdentification],
    index: int,
    total: int,
    *,
    image_url_builder: ImageUrlBuilder | None = None,
    image_versions: Mapping[str, int] | None = None,
    images_loading: set[str] | None = None,
    country_guesses_loading: set[str] | None = None,
) -> Mapping[str, object]:
    albums: dict[tuple[str, str], dict[str, object]] = {}
    image_versions = image_versions or {}
    for reviewed_album in reviewed:
        group_id = reviewed_album.item.identification.group_id
        image_version = image_versions.get(group_id, 0)
        for image_index, image in enumerate(reviewed_album.item.prepared_images):
            image_path = str(image.source_path)
            decision = image_locations.get(image_path, reviewed_album.decision)
            key = (decision.country_or_region, decision.place_name)
            album = albums.setdefault(
                key,
                {
                    "key": album_key(*key),
                    "country_or_region": decision.country_or_region,
                    "place_name": decision.place_name,
                    "images": [],
                },
            )
            images = album["images"]
            assert isinstance(images, list)
            images.append(
                {
                    "src": image_url_builder(group_id, image_index, image_version)
                    if image_url_builder
                    else image.data_url,
                    "filename": image.source_path.name,
                    "path": image_path,
                    "group_id": decision.group_id,
                    "album_key": album["key"],
                    "prepared_size": image.prepared_size,
                    "encoded_bytes": image.encoded_bytes,
                }
            )
    return {
        "done": False,
        "final_review": True,
        "index": index,
        "total": total,
        "images_loading": bool(images_loading),
        "country_guesses_loading": bool(country_guesses_loading),
        "albums": list(albums.values()),
    }


def final_validation_payload(
    *,
    status: str,
    title: str,
    message: str,
    summary: str,
    details: str,
    dryrun_path: str,
    progress: Mapping[str, object] | None = None,
    index: int,
    total: int,
) -> Mapping[str, object]:
    payload = {
        "done": False,
        "final_validation": True,
        "validation_status": status,
        "validation_title": title,
        "validation_message": message,
        "validation_summary": summary,
        "validation_details": details,
        "validation_dryrun_path": dryrun_path,
        "index": index,
        "total": total,
    }
    if progress is not None:
        payload["validation_progress"] = progress
    return payload


def reviewed_decisions(reviewed: Sequence[ReviewedAlbum]) -> dict[str, PlaceIdentification]:
    return {album.decision.group_id: album.decision for album in reviewed}


def start_batch_country_guess(
    starter: CountryBatchGuessStarter | None,
    contexts: tuple[AlbumCountryContext, ...],
) -> None:
    if starter and contexts:
        starter(contexts)


def lock_country_for_album(
    country_locked: set[str],
    reviewed: Sequence[ReviewedAlbum],
    country_or_region: str,
    folder_name: str,
) -> None:
    if not country_or_region.strip():
        return
    target = (normalize_text(country_or_region), normalize_text(folder_name))
    for album in reviewed:
        if (normalize_text(album.decision.country_or_region), normalize_text(album.decision.place_name)) == target:
            country_locked.add(album.decision.group_id)


def set_initial_image_locations(
    image_locations: dict[str, PlaceIdentification],
    item: ReviewItem,
    decision: PlaceIdentification,
) -> None:
    for image in item.prepared_images:
        image_locations.setdefault(str(image.source_path), decision)


def rename_reviewed_album(
    reviewed: list[ReviewedAlbum],
    image_locations: dict[str, PlaceIdentification],
    target_key: str,
    country_or_region: str,
    folder_name: str,
) -> None:
    for index, reviewed_album in enumerate(reviewed):
        decision = reviewed_album.decision
        if album_key(decision.country_or_region, decision.place_name) != target_key:
            continue
        country, place = resolve_album_location(country_or_region, folder_name, decision)
        updated = updated_final_decision(decision, country, place)
        reviewed[index] = ReviewedAlbum(item=reviewed_album.item, decision=updated)
    for path, decision in list(image_locations.items()):
        if album_key(decision.country_or_region, decision.place_name) != target_key:
            continue
        country, place = resolve_album_location(country_or_region, folder_name, decision)
        image_locations[path] = updated_final_decision(decision, country, place)


def move_reviewed_images(
    image_locations: dict[str, PlaceIdentification],
    paths: Sequence[str],
    target_key: str,
    folder_name: str,
) -> None:
    selected = [path for path in paths if path in image_locations]
    if not selected:
        return
    target = location_for_album_key(image_locations.values(), target_key)
    if target is None:
        target = location_for_album_name(image_locations.values(), folder_name)
    if target is None:
        country, place = resolve_album_location("", folder_name, image_locations[selected[0]])
    else:
        country, place = target
    for path in selected:
        image_locations[path] = updated_final_decision(
            image_locations[path],
            country,
            place,
            rationale_prefix="User moved image in final review",
        )


def location_for_album_key(decisions: Iterable[PlaceIdentification], target_key: str) -> tuple[str, str] | None:
    if not target_key:
        return None
    for decision in decisions:
        if not isinstance(decision, PlaceIdentification):
            continue
        if album_key(decision.country_or_region, decision.place_name) == target_key:
            return decision.country_or_region, decision.place_name
    return None


def location_for_album_name(decisions: Iterable[PlaceIdentification], folder_name: str) -> tuple[str, str] | None:
    normalized = normalize_text(folder_name)
    if not normalized:
        return None
    for decision in decisions:
        if not isinstance(decision, PlaceIdentification):
            continue
        label = f"{decision.country_or_region}/{decision.place_name}"
        if normalize_text(decision.place_name) == normalized or normalize_text(label) == normalized:
            return decision.country_or_region, decision.place_name
    return None


def updated_final_decision(
    decision: PlaceIdentification,
    country: str,
    place: str,
    *,
    rationale_prefix: str = "User renamed final album",
    confidence: float = 1.0,
) -> PlaceIdentification:
    return replace(
        decision,
        country_or_region=country,
        place_name=place,
        confidence=confidence,
        is_unknown=False if place and not place.casefold().startswith("unknown") else decision.is_unknown,
        rationale=f"{rationale_prefix}: {country}/{place}",
    )


def resolve_album_location(
    country_or_region: str | None,
    folder_name: str,
    original: PlaceIdentification,
) -> tuple[str, str]:
    country = (country_or_region or "").strip()
    place = folder_name.strip()
    if not country and "/" in place:
        parsed_country, _, parsed_place = place.partition("/")
        if parsed_country.strip() and parsed_place.strip():
            country = parsed_country.strip()
            place = parsed_place.strip()
    return country or original.country_or_region, place or original.place_name


def resolve_album_name(folder_name: str, original: PlaceIdentification) -> tuple[str, str]:
    return resolve_album_location("", folder_name, original)


def album_key(country_or_region: str, place_name: str) -> str:
    return f"{normalize_text(country_or_region)}\n{normalize_text(place_name)}"


def resolve_location(
    country_or_region: str | None,
    place_name: str | None,
    *,
    original: PlaceIdentification,
    suggestions: Sequence[LocationSuggestion],
) -> tuple[str, str]:
    country = (country_or_region or "").strip()
    place = (place_name or "").strip()
    if not country and "/" in place:
        parsed_country, _, parsed_place = place.partition("/")
        if parsed_country.strip() and parsed_place.strip():
            country = parsed_country.strip()
            place = parsed_place.strip()
    country = country or original.country_or_region
    place = place or original.place_name

    normalized_country = normalize_text(country)
    normalized_place = normalize_text(place)
    for suggestion in suggestions:
        if normalize_text(suggestion.country_or_region) == normalized_country and normalize_text(suggestion.place_name) == normalized_place:
            return suggestion.country_or_region, suggestion.place_name
    return country, place


def country_supplied_by_user(country_or_region: str | None, place_name: str | None) -> bool:
    if (country_or_region or "").strip():
        return True
    return bool(split_country_place(place_name))


def split_country_place(value: str | None) -> tuple[str, str] | None:
    text = (value or "").strip()
    if "/" not in text:
        return None
    country, _, place = text.partition("/")
    country = country.strip()
    place = place.strip()
    if not country or not place:
        return None
    return country, place


def normalize_text(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def add_location_suggestion(
    suggestions: list[LocationSuggestion],
    identification: PlaceIdentification,
) -> None:
    country = identification.country_or_region.strip()
    place = identification.place_name.strip()
    if not country or not place:
        return
    normalized = (country.casefold(), place.casefold())
    for existing in suggestions:
        if (existing.country_or_region.casefold(), existing.place_name.casefold()) == normalized:
            return
    suggestions.append(LocationSuggestion(country, place))


def merge_location_suggestions(
    base: Sequence[LocationSuggestion],
    accepted: Sequence[LocationSuggestion],
) -> list[LocationSuggestion]:
    merged: list[LocationSuggestion] = []
    for suggestion in [*base, *accepted]:
        normalized = (suggestion.country_or_region.casefold(), suggestion.place_name.casefold())
        if any(
            (existing.country_or_region.casefold(), existing.place_name.casefold()) == normalized
            for existing in merged
        ):
            continue
        merged.append(suggestion)
    return merged


def review_context_summary(suggestions: Sequence[LocationSuggestion]) -> str:
    if not suggestions:
        return "No reviewed locations yet."
    active_country = suggestions[-1].country_or_region
    active = [suggestion.place_name for suggestion in suggestions if suggestion.country_or_region == active_country]
    return f"Active context: {active_country} / {', '.join(active[-8:])}"


def prepared_image_response(image: PreparedImage) -> tuple[bytes, str] | None:
    if "," not in image.data_url:
        return None
    header, encoded = image.data_url.split(",", 1)
    content_type = "image/jpeg"
    if header.startswith("data:") and ";" in header:
        content_type = header.removeprefix("data:").split(";", 1)[0] or content_type
    try:
        return base64.b64decode(encoded), content_type
    except (ValueError, binascii.Error):
        return None


def make_handler(state: ReviewState) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/" or self.path.startswith("/?"):
                self.send_text(HTML, content_type="text/html")
                return
            if parsed.path == "/api/state":
                self.send_json(state.payload(image_url_builder=state.image_url))
                return
            if parsed.path == "/api/image":
                query = parse_qs(parsed.query)
                group_id = (query.get("group_id") or [""])[0]
                try:
                    index = int((query.get("index") or ["-1"])[0])
                except ValueError:
                    self.send_error(400, "invalid image index")
                    return
                response = state.image_response(group_id, index)
                if response is None:
                    self.send_error(404)
                    return
                body, content_type = response
                self.send_bytes(
                    body,
                    content_type=content_type,
                    cache_control="private, max-age=31536000",
                )
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/final/approve":
                self.send_json(state.approve_final_review())
                return
            if self.path == "/api/final/commit":
                self.send_json(state.request_commit())
                return

            if self.path not in {"/api/decision", "/api/final/album", "/api/final/move"}:
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                self.send_error(400, "invalid JSON")
                return
            if self.path == "/api/final/album":
                self.send_json(
                    state.rename_album(
                        str(payload.get("album_key") or ""),
                        str(payload.get("country_or_region") or ""),
                        str(payload.get("place_name") or ""),
                        image_url_builder=state.image_url,
                    )
                )
                return
            if self.path == "/api/final/move":
                raw_paths = payload.get("paths")
                paths = [str(path) for path in raw_paths] if isinstance(raw_paths, list) else []
                self.send_json(
                    state.move_images(
                        paths,
                        str(payload.get("album_key") or ""),
                        str(payload.get("place_name") or ""),
                        image_url_builder=state.image_url,
                    )
                )
                return
            self.send_json(
                state.decide(
                    str(payload.get("country_or_region") or ""),
                    str(payload.get("place_name") or ""),
                    image_url_builder=state.image_url,
                )
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def send_json(self, payload: Mapping[str, object]) -> None:
            self.send_text(json.dumps(payload).encode("utf-8"), content_type="application/json")

        def send_text(
            self,
            body: str | bytes,
            *,
            content_type: str,
            cache_control: str = "no-store",
        ) -> None:
            encoded = body.encode("utf-8") if isinstance(body, str) else body
            self.send_bytes(encoded, content_type=content_type, cache_control=cache_control)

        def send_bytes(
            self,
            body: bytes,
            *,
            content_type: str,
            cache_control: str = "no-store",
        ) -> None:
            self.send_response(200)
            if content_type.startswith("text/") or content_type == "application/json":
                content_type = f"{content_type}; charset=utf-8"
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ReviewHandler


HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Curator Review</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f5f2;
      --ink: #1f2933;
      --muted: #667085;
      --line: #d7d4cc;
      --accent: #0f766e;
      --accent-ink: #ffffff;
      --progress: #16a34a;
      --progress-track: #dceade;
      --success: #15803d;
      --danger: #b91c1c;
      --danger-bg: #fef2f2;
      --finder-blue: #0a84ff;
      --finder-blue-ring: rgba(10, 132, 255, 0.34);
      --panel: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 3;
      display: grid;
      gap: 10px;
      padding: 14px 18px;
      background: rgba(246, 245, 242, 0.96);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }
    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h1 {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
    }
    #progress {
      display: grid;
      grid-template-columns: auto minmax(120px, 220px);
      align-items: center;
      gap: 10px;
      color: var(--muted);
      font-size: 14px;
      white-space: nowrap;
    }
    .progress-track {
      width: 100%;
      height: 8px;
      overflow: hidden;
      border: 1px solid #c8dccd;
      border-radius: 999px;
      background: var(--progress-track);
    }
    .progress-fill {
      display: block;
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: var(--progress);
      transition: width 240ms ease;
    }
    form {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 8px;
      align-items: center;
    }
    input {
      width: 100%;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }
    button {
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      background: #fff;
      color: var(--ink);
      cursor: pointer;
    }
    button.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: var(--accent-ink);
    }
    .place-wrap { position: relative; }
    .suggestions {
      position: absolute;
      top: 42px;
      left: 0;
      right: 0;
      z-index: 5;
      max-height: 220px;
      overflow: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 6px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.14);
    }
    .suggestion {
      display: grid;
      gap: 2px;
      padding: 8px 10px;
      cursor: pointer;
      border-bottom: 1px solid #eeeae2;
    }
    .suggestion:last-child { border-bottom: 0; }
    .suggestion.active {
      background: #e8f3f1;
    }
    .suggestion strong { font-size: 14px; }
    .suggestion span { color: var(--muted); font-size: 12px; }
    main {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 18px;
      padding: 18px;
    }
    .gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
      gap: 8px;
      align-content: start;
    }
    .final-review {
      display: block;
      padding: 20px 18px 84px;
    }
    .final-review > h1 {
      margin: 0 0 4px;
      font-size: 22px;
    }
    .final-review > p {
      margin: 0 0 18px;
      color: var(--muted);
    }
    .album-section {
      margin: 0 0 28px;
    }
    .album-heading {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 10px;
      flex-wrap: wrap;
    }
    .album-location-fields {
      display: grid;
      grid-template-columns: minmax(120px, 220px) auto minmax(160px, 340px);
      align-items: center;
      gap: 6px;
      min-width: min(100%, 560px);
    }
    .album-country-wrap {
      position: relative;
    }
    .album-location-input {
      height: 34px;
      font-size: 18px;
      font-weight: 650;
    }
    .album-separator {
      color: var(--muted);
      font-size: 18px;
      font-weight: 650;
    }
    .album-select-checkbox,
    .image-select-checkbox {
      width: 16px;
      height: 16px;
      accent-color: var(--finder-blue);
      flex: 0 0 auto;
    }
    .final-actions {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 10;
    }
    .looks-good {
      min-width: 128px;
      border-color: var(--success);
      background: var(--success);
      color: #fff;
      box-shadow: 0 12px 28px rgba(21, 128, 61, 0.25);
    }
    .validation-screen {
      max-width: 880px;
      margin: 64px auto;
      padding: 0 18px;
      display: block;
    }
    .validation-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 24px;
    }
    .validation-panel h1 {
      font-size: 24px;
      margin: 0 0 8px;
    }
    .validation-panel p {
      margin: 0 0 16px;
      color: var(--muted);
    }
    .validation-status {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 28px;
      margin: 0 0 18px;
      font-weight: 650;
    }
    .validation-progress {
      display: grid;
      gap: 8px;
      max-width: 560px;
      margin: -2px 0 18px;
    }
    .validation-progress-row {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    .validation-progress-count {
      color: var(--ink);
      font-size: 14px;
      font-weight: 650;
    }
    .validation-progress .progress-track {
      max-width: none;
      height: 10px;
    }
    .validation-screen.failed .validation-panel {
      border-color: rgba(185, 28, 28, 0.42);
      background: var(--danger-bg);
    }
    .validation-screen.failed .validation-status,
    .validation-screen.failed h1 {
      color: var(--danger);
    }
    .validation-screen.passed .validation-status,
    .validation-screen.passed h1 {
      color: var(--success);
    }
    .validation-output {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin: 12px 0 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.72);
      color: var(--ink);
      font: 13px ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }
    .move-controls {
      position: fixed;
      left: 18px;
      bottom: 18px;
      z-index: 10;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .move-panel {
      position: relative;
      width: min(360px, calc(100vw - 36px));
    }
    .move-panel input {
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.16);
    }
    .move-suggestions {
      top: auto;
      bottom: 42px;
    }
    .gallery figure {
      margin: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      cursor: pointer;
      transition: border-color 150ms ease, box-shadow 150ms ease, transform 150ms ease;
    }
    .gallery figure:focus {
      outline: none;
    }
    .gallery figure:focus-visible {
      box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.22);
    }
    .gallery figure.selected {
      border-color: var(--finder-blue);
      box-shadow: 0 0 0 3px var(--finder-blue-ring);
    }
    .gallery figure:hover {
      transform: translateY(-1px);
    }
    figure.image-tile {
      cursor: pointer;
      outline: 0;
    }
    figure.image-tile:hover,
    figure.image-tile:focus {
      border-color: var(--accent);
    }
    figure.image-tile.selected {
      border-color: var(--finder-blue);
      box-shadow: 0 0 0 4px var(--finder-blue-ring);
      background: #eef6ff;
    }
    .gallery figure img {
      display: block;
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: contain;
      background: #151515;
    }
    .gallery figcaption {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }
    .image-filename {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .gallery-sentinel {
      grid-column: 1 / -1;
      height: 1px;
    }
    .expanded-backdrop {
      position: fixed;
      inset: 0;
      z-index: 20;
      background: rgba(15, 23, 42, 0.72);
      opacity: 0;
      transition: opacity 220ms ease;
    }
    .expanded-backdrop.visible {
      opacity: 1;
    }
    .expanded-figure {
      position: fixed;
      z-index: 21;
      margin: 0;
      overflow: visible;
      border-radius: 8px;
      transition:
        left 240ms cubic-bezier(0.2, 0.8, 0.2, 1),
        top 240ms cubic-bezier(0.2, 0.8, 0.2, 1),
        width 240ms cubic-bezier(0.2, 0.8, 0.2, 1),
        height 240ms cubic-bezier(0.2, 0.8, 0.2, 1),
        opacity 180ms ease;
    }
    .expanded-figure img {
      display: block;
      width: 100%;
      height: 100%;
      object-fit: contain;
      border-radius: 8px;
      background: #111111;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.38);
    }
    .expanded-figure figcaption {
      position: absolute;
      top: calc(100% + 8px);
      left: 0;
      right: 0;
      padding: 0;
      color: #ffffff;
      font-size: 13px;
      text-align: center;
      text-shadow: 0 1px 3px rgba(0, 0, 0, 0.42);
      opacity: 0;
      transition: opacity 160ms ease;
      overflow-wrap: anywhere;
    }
    .expanded-figure.visible figcaption {
      opacity: 1;
      transition-delay: 120ms;
    }
    body.expanded-open {
      overflow: hidden;
    }
    aside {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      align-self: start;
    }
    dl { margin: 0; display: grid; gap: 10px; }
    dt { font-size: 12px; color: var(--muted); }
    dd { margin: 2px 0 0; overflow-wrap: anywhere; }
    ul { margin: 4px 0 0 18px; padding: 0; color: var(--muted); }
    .done {
      max-width: 640px;
      margin: 80px auto;
      padding: 24px;
      text-align: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .loading {
      max-width: 640px;
      margin: 80px auto;
      padding: 24px;
      text-align: center;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      color: var(--muted);
    }
    .gallery .loading {
      grid-column: 1 / -1;
      max-width: none;
      margin: 0;
    }
    .inline-loading {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
    }
    .spinner {
      width: 16px;
      height: 16px;
      border: 2px solid #d7d4cc;
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    @media (max-width: 820px) {
      form { grid-template-columns: 1fr; }
      main { grid-template-columns: 1fr; }
      .album-location-fields { grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr); }
      #progress {
        grid-template-columns: auto minmax(90px, 1fr);
        white-space: normal;
      }
    }
  </style>
</head>
<body>
  <header>
    <div class="topline">
      <h1>Curator Review</h1>
      <div id="progress" aria-label="Album naming progress">
        <span id="progress-count">0/0</span>
        <span class="progress-track" aria-hidden="true"><span class="progress-fill" id="progress-fill"></span></span>
      </div>
    </div>
    <form id="review-form">
      <div class="place-wrap">
        <input id="place" autocomplete="off" placeholder="Location or album name" aria-autocomplete="list" aria-controls="suggestions">
        <div class="suggestions" id="suggestions" role="listbox" hidden></div>
      </div>
      <button class="primary" type="submit">Save / Continue</button>
    </form>
  </header>
  <main id="content">
    <section class="gallery" id="gallery"></section>
    <aside>
      <dl>
        <div><dt>Group</dt><dd id="group"></dd></div>
        <div><dt>Files</dt><dd id="file-count"></dd></div>
        <div><dt>Confidence</dt><dd id="confidence"></dd></div>
        <div><dt>Rationale</dt><dd id="rationale"></dd></div>
        <div><dt>Context</dt><dd id="context"></dd></div>
        <div><dt>Evidence</dt><dd><ul id="evidence"></ul></dd></div>
        <div><dt>Alternates</dt><dd><ul id="alternates"></ul></dd></div>
      </dl>
    </aside>
  </main>
  <script>
    const GALLERY_PAGE_SIZE = 36;
    const COUNTRY_OPTIONS = [
      'Afghanistan', 'Albania', 'Algeria', 'Andorra', 'Angola', 'Antigua and Barbuda',
      'Argentina', 'Armenia', 'Australia', 'Austria', 'Azerbaijan', 'Bahamas', 'Bahrain',
      'Bangladesh', 'Barbados', 'Belarus', 'Belgium', 'Belize', 'Benin', 'Bhutan',
      'Bolivia', 'Bosnia and Herzegovina', 'Botswana', 'Brazil', 'Brunei', 'Bulgaria',
      'Burkina Faso', 'Burundi', 'Cabo Verde', 'Cambodia', 'Cameroon', 'Canada',
      'Central African Republic', 'Chad', 'Chile', 'China', 'Colombia', 'Comoros',
      'Congo', 'Costa Rica', 'Croatia', 'Cuba', 'Cyprus', 'Czechia',
      'Democratic Republic of the Congo', 'Denmark', 'Djibouti', 'Dominica',
      'Dominican Republic', 'Ecuador', 'Egypt', 'El Salvador', 'Equatorial Guinea',
      'Eritrea', 'Estonia', 'Eswatini', 'Ethiopia', 'Fiji', 'Finland', 'France',
      'Gabon', 'Gambia', 'Georgia', 'Germany', 'Ghana', 'Greece', 'Grenada',
      'Guatemala', 'Guinea', 'Guinea-Bissau', 'Guyana', 'Haiti', 'Honduras',
      'Hungary', 'Iceland', 'India', 'Indonesia', 'Iran', 'Iraq', 'Ireland',
      'Israel', 'Italy', 'Jamaica', 'Japan', 'Jordan', 'Kazakhstan', 'Kenya',
      'Kiribati', 'Kuwait', 'Kyrgyzstan', 'Laos', 'Latvia', 'Lebanon', 'Lesotho',
      'Liberia', 'Libya', 'Liechtenstein', 'Lithuania', 'Luxembourg', 'Madagascar',
      'Malawi', 'Malaysia', 'Maldives', 'Mali', 'Malta', 'Marshall Islands',
      'Mauritania', 'Mauritius', 'Mexico', 'Micronesia', 'Moldova', 'Monaco',
      'Mongolia', 'Montenegro', 'Morocco', 'Mozambique', 'Myanmar', 'Namibia',
      'Nauru', 'Nepal', 'Netherlands', 'New Zealand', 'Nicaragua', 'Niger',
      'Nigeria', 'North Korea', 'North Macedonia', 'Norway', 'Oman', 'Pakistan',
      'Palau', 'Panama', 'Papua New Guinea', 'Paraguay', 'Peru', 'Philippines',
      'Poland', 'Portugal', 'Qatar', 'Romania', 'Russia', 'Rwanda',
      'Saint Kitts and Nevis', 'Saint Lucia', 'Saint Vincent and the Grenadines',
      'Samoa', 'San Marino', 'Sao Tome and Principe', 'Saudi Arabia', 'Senegal',
      'Serbia', 'Seychelles', 'Sierra Leone', 'Singapore', 'Slovakia', 'Slovenia',
      'Solomon Islands', 'Somalia', 'South Africa', 'South Korea', 'South Sudan',
      'Spain', 'Sri Lanka', 'Sudan', 'Suriname', 'Sweden', 'Switzerland', 'Syria',
      'Taiwan', 'Tajikistan', 'Tanzania', 'Thailand', 'Timor-Leste', 'Togo',
      'Tonga', 'Trinidad and Tobago', 'Tunisia', 'Turkey', 'Turkmenistan', 'Tuvalu',
      'Uganda', 'Ukraine', 'United Arab Emirates', 'United Kingdom', 'United States',
      'Uruguay', 'Uzbekistan', 'Vanuatu', 'Vatican City', 'Venezuela', 'Vietnam',
      'Yemen', 'Zambia', 'Zimbabwe'
    ];

    let current = null;
    let activeSuggestion = null;
    let suggestionDraft = '';
    let suggestionOptions = [];
    let activeSuggestionIndex = -1;
    let lastGroupId = null;
    let lastImagesVersion = null;
    let placeDirty = false;
    let galleryObserver = null;
    let galleryRenderedCount = 0;
    let selectedImageIndex = -1;
    let expandedView = null;
    let selectedPaths = new Set();
    let lastSelectedIndex = null;
    let movePanelOpen = false;
    let activeMoveSuggestion = null;

    async function loadState() {
      const response = await fetch('/api/state');
      current = await response.json();
      render();
    }

    function renderList(id, items) {
      const root = document.getElementById(id);
      root.innerHTML = '';
      for (const item of items || []) {
        const li = document.createElement('li');
        li.textContent = item;
        root.appendChild(li);
      }
    }

    function render() {
      if (!current || current.done) {
        closeExpandedImage(false);
        resetGalleryObserver();
        document.body.innerHTML = '<div class="done"><h1>All groups reviewed</h1><p>You can return to the terminal.</p></div>';
        return;
      }
      if (current.final_validation) {
        closeExpandedImage(false);
        resetGalleryObserver();
        renderFinalValidation();
        if (current.validation_status === 'staging' || current.validation_status === 'pending') {
          setTimeout(loadState, 900);
        }
        return;
      }
      if (current.final_review) {
        renderFinalReview();
        if (current.images_loading || current.country_guesses_loading) {
          setTimeout(loadState, 900);
        }
        return;
      }
      if (current.loading) {
        closeExpandedImage(false);
        resetGalleryObserver();
        lastGroupId = null;
        lastImagesVersion = null;
        placeDirty = false;
        resetSuggestionState();
        updateProgress();
        const gallery = document.getElementById('gallery');
        if (gallery) gallery.innerHTML = '<div class="loading">Preparing the next album suggestion...</div>';
        for (const id of ['group', 'file-count', 'confidence', 'rationale', 'context']) {
          const node = document.getElementById(id);
          if (node) node.textContent = '';
        }
        renderList('evidence', []);
        renderList('alternates', []);
        setTimeout(loadState, 900);
        return;
      }
      const groupChanged = current.group_id !== lastGroupId;
      if (groupChanged) {
        closeExpandedImage(false);
        resetGalleryObserver();
        lastGroupId = current.group_id;
        lastImagesVersion = null;
        placeDirty = false;
        resetSuggestionState();
      }
      const imageVersionChanged = current.images_version !== lastImagesVersion;
      if (imageVersionChanged) {
        lastImagesVersion = current.images_version;
      }
      updateProgress();
      const placeInput = document.getElementById('place');
      if (!placeDirty) {
        placeInput.value = current.place_name || '';
        suggestionDraft = placeInput.value;
        activeSuggestionIndex = suggestionDraft.trim() ? 0 : -1;
        activeSuggestion = null;
      }
      document.getElementById('group').textContent = current.group_id;
      document.getElementById('file-count').textContent = current.file_count;
      if (current.llm_loading) {
        document.getElementById('confidence').innerHTML = '<span class="inline-loading"><span class="spinner" aria-hidden="true"></span><span>Loading suggestion...</span></span>';
        document.getElementById('rationale').textContent = '';
      } else if (current.llm_error) {
        document.getElementById('confidence').textContent = 'Unavailable';
        document.getElementById('rationale').textContent = current.llm_error;
      } else {
        document.getElementById('confidence').textContent = `${Math.round((current.confidence || 0) * 100)}%${current.is_unknown ? ' · unknown' : ''}`;
        document.getElementById('rationale').textContent = current.rationale || '';
      }
      document.getElementById('context').textContent = current.context_summary || '';
      renderList('evidence', current.visual_evidence);
      renderList('alternates', current.alternate_guesses);

      if (groupChanged || imageVersionChanged || current.images_loading) {
        renderGallery();
      }
      if (groupChanged) {
        placeInput.focus();
        placeInput.select();
      }
      renderSuggestions();
      if (current.llm_loading || current.images_loading) {
        setTimeout(loadState, 900);
      }
    }

    function progressState() {
      if (!current) return {current: 0, total: 0};
      const total = Number(current.total || 0);
      const currentNumber = current.done ? total : Math.min(total, Number(current.index || 0) + 1);
      return {current: currentNumber, total};
    }

    function updateProgress() {
      const progress = progressState();
      const count = document.getElementById('progress-count');
      const fill = document.getElementById('progress-fill');
      if (!count || !fill) return;
      count.textContent = `${progress.current}/${progress.total}`;
      const percent = progress.total ? Math.max(0, Math.min(100, (progress.current / progress.total) * 100)) : 0;
      fill.style.width = `${percent}%`;
    }

    function renderValidationProgress(panel) {
      const progress = current.validation_progress || null;
      const total = Math.max(0, Number((progress && progress.total) || 0));
      if (!total) return;

      const completed = Math.max(0, Math.min(total, Number(progress.completed || 0)));
      const remaining = Math.max(0, Number(progress.remaining || total - completed));
      const elapsed = Math.max(0, Number(progress.elapsed_seconds || 0));
      const estimated = progress.estimated_remaining_seconds;
      const percent = Math.max(0, Math.min(100, (completed / total) * 100));

      const root = document.createElement('div');
      root.className = 'validation-progress';
      root.setAttribute('aria-label', 'Operation progress');

      const row = document.createElement('div');
      row.className = 'validation-progress-row';

      const count = document.createElement('span');
      count.className = 'validation-progress-count';
      count.textContent = `${completed}/${total} ${pluralizeWord(total, 'operation')}`;
      row.appendChild(count);

      const meta = document.createElement('span');
      const hasEstimate = estimated !== null && estimated !== undefined && Number.isFinite(Number(estimated));
      const etaText = hasEstimate
        ? `est. ${formatDuration(Number(estimated))} remaining`
        : 'est. calculating';
      meta.textContent = `${remaining} ${pluralizeWord(remaining, 'operation')} remaining · ${formatDuration(elapsed)} elapsed · ${etaText}`;
      row.appendChild(meta);

      const track = document.createElement('div');
      track.className = 'progress-track';
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', String(total));
      track.setAttribute('aria-valuenow', String(completed));
      const fill = document.createElement('span');
      fill.className = 'progress-fill';
      fill.style.width = `${percent}%`;
      track.appendChild(fill);

      root.appendChild(row);
      root.appendChild(track);
      panel.appendChild(root);
    }

    function pluralizeWord(count, word) {
      return `${word}${count === 1 ? '' : 's'}`;
    }

    function formatDuration(seconds) {
      const totalSeconds = Math.max(0, Math.round(Number(seconds) || 0));
      const hours = Math.floor(totalSeconds / 3600);
      const minutes = Math.floor((totalSeconds % 3600) / 60);
      const remainingSeconds = totalSeconds % 60;
      if (hours) return `${hours}h ${minutes}m ${remainingSeconds}s`;
      if (minutes) return `${minutes}m ${remainingSeconds}s`;
      return `${remainingSeconds}s`;
    }

    function resetGalleryObserver() {
      if (galleryObserver) {
        galleryObserver.disconnect();
        galleryObserver = null;
      }
    }

    function renderGallery() {
      resetGalleryObserver();
      const gallery = document.getElementById('gallery');
      gallery.innerHTML = '';
      galleryRenderedCount = 0;
      selectedImageIndex = -1;
      appendGalleryPage();
      renderGalleryStatus();
    }

    function appendGalleryPage() {
      resetGalleryObserver();
      const gallery = document.getElementById('gallery');
      if (!gallery || !current) return;
      const previousSentinel = gallery.querySelector('.gallery-sentinel');
      if (previousSentinel) previousSentinel.remove();

      const images = current.images || [];
      const start = galleryRenderedCount;
      const end = Math.min(start + GALLERY_PAGE_SIZE, images.length);
      for (let index = start; index < end; index += 1) {
        gallery.appendChild(createImageFigure(images[index], index));
      }
      galleryRenderedCount = end;

      if (galleryRenderedCount < images.length) {
        const sentinel = document.createElement('div');
        sentinel.className = 'gallery-sentinel';
        gallery.appendChild(sentinel);
        galleryObserver = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) appendGalleryPage();
        });
        galleryObserver.observe(sentinel);
      }
    }

    function renderGalleryStatus() {
      const gallery = document.getElementById('gallery');
      if (!gallery || !current) return;
      const previous = gallery.querySelector('.gallery-loading');
      if (previous) previous.remove();
      if (current.images_loading) {
        const loading = document.createElement('div');
        loading.className = 'loading gallery-loading';
        loading.innerHTML = '<span class="inline-loading"><span class="spinner" aria-hidden="true"></span><span>Loading images...</span></span>';
        gallery.appendChild(loading);
      } else if (current.image_error && !(current.images || []).length) {
        const error = document.createElement('div');
        error.className = 'loading gallery-loading';
        error.textContent = current.image_error;
        gallery.appendChild(error);
      }
    }

    function createImageFigure(image, index) {
      const figure = document.createElement('figure');
      figure.tabIndex = 0;
      figure.dataset.index = String(index);
      figure.setAttribute('role', 'button');
      figure.setAttribute('aria-label', `Open ${image.filename}`);

      const img = document.createElement('img');
      img.src = image.src;
      img.alt = image.filename;
      const caption = document.createElement('figcaption');
      caption.textContent = image.filename;

      figure.appendChild(img);
      figure.appendChild(caption);
      figure.addEventListener('click', () => {
        selectImage(index, figure);
        figure.focus({preventScroll: true});
      });
      figure.addEventListener('dblclick', () => openExpandedImage(image, figure));
      figure.addEventListener('keydown', (event) => {
        if (isSpaceKey(event)) {
          event.preventDefault();
          event.stopPropagation();
          if (expandedView) {
            closeExpandedImage(true);
            return;
          }
          selectImage(index, figure);
          openExpandedImage(image, figure);
        }
      });
      return figure;
    }

    function selectImage(index, figure) {
      selectedImageIndex = index;
      document.querySelectorAll('.gallery figure.selected').forEach((node) => node.classList.remove('selected'));
      figure.classList.add('selected');
    }

    function openSelectedImage() {
      if (!current || selectedImageIndex < 0) return;
      const image = (current.images || [])[selectedImageIndex];
      const figure = document.querySelector(`.gallery figure[data-index="${selectedImageIndex}"]`);
      if (image && figure) openExpandedImage(image, figure);
    }

    function openExpandedImage(image, figure) {
      closeExpandedImage(false);
      selectImage(Number(figure.dataset.index), figure);

      const originImage = figure.querySelector('img');
      const origin = originImage.getBoundingClientRect();
      const target = expandedTargetRect(image);
      const backdrop = document.createElement('div');
      backdrop.className = 'expanded-backdrop';
      backdrop.addEventListener('click', () => closeExpandedImage(true));

      const expandedFigure = document.createElement('figure');
      expandedFigure.className = 'expanded-figure';
      expandedFigure.style.left = `${origin.left}px`;
      expandedFigure.style.top = `${origin.top}px`;
      expandedFigure.style.width = `${origin.width}px`;
      expandedFigure.style.height = `${origin.height}px`;

      const expandedImage = document.createElement('img');
      expandedImage.src = image.src;
      expandedImage.alt = image.filename;
      const caption = document.createElement('figcaption');
      caption.textContent = image.filename;

      expandedFigure.appendChild(expandedImage);
      expandedFigure.appendChild(caption);
      document.body.appendChild(backdrop);
      document.body.appendChild(expandedFigure);
      document.body.classList.add('expanded-open');
      expandedView = {backdrop, figure: expandedFigure, originFigure: figure};

      requestAnimationFrame(() => {
        backdrop.classList.add('visible');
        expandedFigure.classList.add('visible');
        expandedFigure.style.left = `${target.left}px`;
        expandedFigure.style.top = `${target.top}px`;
        expandedFigure.style.width = `${target.width}px`;
        expandedFigure.style.height = `${target.height}px`;
      });
    }

    function closeExpandedImage(animate = true) {
      if (!expandedView) return;
      const view = expandedView;
      expandedView = null;

      const cleanup = () => {
        view.backdrop.remove();
        view.figure.remove();
        document.body.classList.remove('expanded-open');
      };

      if (!animate) {
        cleanup();
        return;
      }

      const originImage = view.originFigure && view.originFigure.querySelector('img');
      const origin = originImage ? originImage.getBoundingClientRect() : {
        left: window.innerWidth / 2,
        top: window.innerHeight / 2,
        width: 1,
        height: 1,
      };
      view.backdrop.classList.remove('visible');
      view.figure.classList.remove('visible');
      view.figure.style.left = `${origin.left}px`;
      view.figure.style.top = `${origin.top}px`;
      view.figure.style.width = `${origin.width}px`;
      view.figure.style.height = `${origin.height}px`;

      let cleaned = false;
      const finish = () => {
        if (cleaned) return;
        cleaned = true;
        cleanup();
      };
      view.figure.addEventListener('transitionend', finish, {once: true});
      setTimeout(finish, 320);
    }

    function expandedTargetRect(image) {
      const margin = Math.max(18, Math.min(48, window.innerWidth * 0.04));
      const maxWidth = window.innerWidth - margin * 2;
      const maxHeight = window.innerHeight - margin * 2 - 34;
      const preparedSize = Array.isArray(image.prepared_size) ? image.prepared_size : null;
      const naturalWidth = Number(preparedSize && preparedSize[0]) || 4;
      const naturalHeight = Number(preparedSize && preparedSize[1]) || 3;
      const ratio = naturalWidth / naturalHeight;
      let width = maxWidth;
      let height = width / ratio;
      if (height > maxHeight) {
        height = maxHeight;
        width = height * ratio;
      }
      return {
        left: (window.innerWidth - width) / 2,
        top: (window.innerHeight - height) / 2,
        width,
        height,
      };
    }

    function isSpaceKey(event) {
      return event.key === ' ' || event.key === 'Spacebar' || event.code === 'Space';
    }

    function isTextInputTarget(target) {
      return target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable);
    }

    function renderFinalReview() {
      document.body.innerHTML = '';
      pruneSelectedPaths();
      const main = document.createElement('main');
      main.className = 'final-review';

      const title = document.createElement('h1');
      title.textContent = 'Final review';
      main.appendChild(title);

      const summary = document.createElement('p');
      const albumCount = (current.albums || []).length;
      summary.textContent = `${albumCount} album${albumCount === 1 ? '' : 's'} ready`;
      main.appendChild(summary);

      let imageIndex = 0;
      for (const album of current.albums || []) {
        const section = document.createElement('section');
        section.className = 'album-section';

        const heading = document.createElement('div');
        heading.className = 'album-heading';
        const albumCheckbox = document.createElement('input');
        albumCheckbox.type = 'checkbox';
        albumCheckbox.className = 'album-select-checkbox';
        albumCheckbox.checked = albumImagesSelected(album);
        albumCheckbox.disabled = !(album.images || []).length;
        albumCheckbox.title = `Select all in ${album.place_name || 'this album'}`;
        albumCheckbox.setAttribute('aria-label', `Select all in ${album.place_name || 'this album'}`);
        albumCheckbox.addEventListener('click', (event) => {
          event.stopPropagation();
        });
        albumCheckbox.addEventListener('change', () => toggleAlbumSelection(album));
        const fields = createAlbumLocationFields(album);
        heading.appendChild(albumCheckbox);
        heading.appendChild(fields);
        section.appendChild(heading);

        const gallery = document.createElement('div');
        gallery.className = 'gallery';
        for (const image of album.images || []) {
          const currentIndex = imageIndex;
          imageIndex += 1;
          const figure = document.createElement('figure');
          figure.className = `image-tile${selectedPaths.has(image.path) ? ' selected' : ''}`;
          figure.tabIndex = 0;
          figure.addEventListener('click', (event) => {
            toggleImageSelection(image.path, currentIndex, event.shiftKey);
          });
          figure.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              toggleImageSelection(image.path, currentIndex, event.shiftKey);
            }
          });
          const img = document.createElement('img');
          img.src = image.src;
          img.alt = image.filename;
          const caption = document.createElement('figcaption');
          const checkbox = document.createElement('input');
          checkbox.type = 'checkbox';
          checkbox.className = 'image-select-checkbox';
          checkbox.checked = selectedPaths.has(image.path);
          checkbox.setAttribute('aria-label', `Select ${image.filename}`);
          checkbox.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleImageSelection(image.path, currentIndex, event.shiftKey);
          });
          const filename = document.createElement('span');
          filename.className = 'image-filename';
          filename.textContent = image.filename;
          caption.appendChild(checkbox);
          caption.appendChild(filename);
          figure.appendChild(img);
          figure.appendChild(caption);
          gallery.appendChild(figure);
        }
        section.appendChild(gallery);
        main.appendChild(section);
      }

      const actions = document.createElement('div');
      actions.className = 'final-actions';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'looks-good';
      button.textContent = 'Looks good';
      button.addEventListener('click', approveFinalReview);
      actions.appendChild(button);
      main.appendChild(actions);
      renderMoveControls(main);
      document.body.appendChild(main);
    }

    function renderFinalValidation() {
      document.body.innerHTML = '';
      const status = current.validation_status || 'pending';
      const main = document.createElement('main');
      main.className = `validation-screen ${status}`;

      const panel = document.createElement('section');
      panel.className = 'validation-panel';

      const title = document.createElement('h1');
      title.textContent = current.validation_title || 'Validating final copy';
      panel.appendChild(title);

      const message = document.createElement('p');
      message.textContent = current.validation_message || '';
      panel.appendChild(message);

      const statusLine = document.createElement('div');
      statusLine.className = 'validation-status';
      if (status === 'staging') {
        statusLine.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Writing DRYRUN.txt...</span>';
      } else if (status === 'ready') {
        statusLine.textContent = 'Staged and ready';
      } else if (status === 'pending') {
        statusLine.innerHTML = '<span class="spinner" aria-hidden="true"></span><span>Waiting for CLI validation...</span>';
      } else if (status === 'passed') {
        statusLine.textContent = 'All final checks passed';
      } else {
        statusLine.textContent = 'Do not delete the original source folder';
      }
      panel.appendChild(statusLine);

      if (status === 'pending') {
        renderValidationProgress(panel);
      }

      if (current.validation_dryrun_path) {
        const dryrun = document.createElement('pre');
        dryrun.className = 'validation-output';
        dryrun.textContent = current.validation_dryrun_path;
        panel.appendChild(dryrun);
      }

      if (status === 'ready') {
        const actions = document.createElement('div');
        actions.className = 'final-actions';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'looks-good';
        button.textContent = 'Commit';
        button.addEventListener('click', commitFinalReview);
        actions.appendChild(button);
        main.appendChild(actions);
      }

      if (current.validation_summary) {
        const summary = document.createElement('pre');
        summary.className = 'validation-output';
        summary.textContent = current.validation_summary;
        panel.appendChild(summary);
      }

      if (current.validation_details) {
        const details = document.createElement('pre');
        details.className = 'validation-output';
        details.textContent = current.validation_details;
        panel.appendChild(details);
      }

      main.appendChild(panel);
      document.body.appendChild(main);
    }

    function finalImages() {
      const images = [];
      for (const album of current.albums || []) {
        for (const image of album.images || []) {
          images.push(image);
        }
      }
      return images;
    }

    function albumImagesSelected(album) {
      const images = album.images || [];
      return images.length > 0 && images.every((image) => selectedPaths.has(image.path));
    }

    function pruneSelectedPaths() {
      const visible = new Set(finalImages().map((image) => image.path));
      selectedPaths = new Set([...selectedPaths].filter((path) => visible.has(path)));
      if (!selectedPaths.size) {
        movePanelOpen = false;
        activeMoveSuggestion = null;
      }
    }

    function toggleImageSelection(path, index, shiftKey) {
      const images = finalImages();
      if (shiftKey && lastSelectedIndex !== null) {
        const start = Math.min(lastSelectedIndex, index);
        const end = Math.max(lastSelectedIndex, index);
        for (let i = start; i <= end; i += 1) {
          selectedPaths.add(images[i].path);
        }
      } else if (selectedPaths.has(path)) {
        selectedPaths.delete(path);
      } else {
        selectedPaths.add(path);
      }
      lastSelectedIndex = index;
      movePanelOpen = false;
      activeMoveSuggestion = null;
      renderFinalReview();
    }

    function toggleAlbumSelection(album) {
      const paths = (album.images || []).map((image) => image.path);
      if (!paths.length) return;
      if (paths.every((path) => selectedPaths.has(path))) {
        for (const path of paths) selectedPaths.delete(path);
      } else {
        for (const path of paths) selectedPaths.add(path);
      }
      lastSelectedIndex = null;
      movePanelOpen = false;
      activeMoveSuggestion = null;
      renderFinalReview();
    }

    function deselectFinalImages() {
      selectedPaths.clear();
      lastSelectedIndex = null;
      movePanelOpen = false;
      activeMoveSuggestion = null;
      renderFinalReview();
    }

    function renderMoveControls(root) {
      if (!selectedPaths.size) return;
      const controls = document.createElement('div');
      controls.className = 'move-controls';

      if (!movePanelOpen) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Move to...';
        button.addEventListener('click', () => {
          movePanelOpen = true;
          renderFinalReview();
        });
        controls.appendChild(button);
        controls.appendChild(createDeselectButton());
        root.appendChild(controls);
        return;
      }

      const panel = document.createElement('div');
      panel.className = 'move-panel';
      const input = document.createElement('input');
      input.id = 'move-input';
      input.autocomplete = 'off';
      input.placeholder = 'Album name';
      const suggestions = document.createElement('div');
      suggestions.className = 'suggestions move-suggestions';
      suggestions.id = 'move-suggestions';
      suggestions.hidden = true;
      input.addEventListener('input', () => renderMoveSuggestions(input.value));
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          confirmMoveTo(input.value);
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          movePanelOpen = false;
          activeMoveSuggestion = null;
          renderFinalReview();
        }
      });
      panel.appendChild(input);
      panel.appendChild(suggestions);
      controls.appendChild(panel);
      controls.appendChild(createDeselectButton());
      root.appendChild(controls);
      setTimeout(() => {
        input.focus();
        input.select();
        renderMoveSuggestions(input.value);
      }, 0);
    }

    function createDeselectButton() {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = 'Deselect';
      button.addEventListener('click', deselectFinalImages);
      return button;
    }

    function fuzzyAlbumScore(query, album) {
      const q = normalize(query);
      const label = normalize(`${album.country_or_region}/${album.place_name}`);
      const place = normalize(album.place_name);
      if (!q) return 10;
      if (place === q || label === q) return 100;
      if (place.startsWith(q)) return 80;
      if (label.startsWith(q)) return 70;
      if (place.includes(q)) return 50;
      if (label.includes(q)) return 40;
      let cursor = 0;
      for (const char of q) {
        cursor = label.indexOf(char, cursor);
        if (cursor === -1) return -1;
        cursor += 1;
      }
      return 20;
    }

    function renderMoveSuggestions(query) {
      const root = document.getElementById('move-suggestions');
      if (!root) return;
      const ranked = (current.albums || [])
        .map((album) => ({album, score: fuzzyAlbumScore(query, album)}))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || a.album.place_name.localeCompare(b.album.place_name))
        .slice(0, 8);
      if (!ranked.length) {
        root.hidden = true;
        activeMoveSuggestion = null;
        return;
      }
      activeMoveSuggestion = ranked[0].album;
      root.innerHTML = '';
      ranked.forEach((item, index) => {
        const div = document.createElement('div');
        div.className = `suggestion${index === 0 ? ' active' : ''}`;
        div.innerHTML = `<strong>${item.album.place_name}</strong><span>${item.album.country_or_region}</span>`;
        div.addEventListener('mousedown', (event) => {
          event.preventDefault();
          activeMoveSuggestion = item.album;
          const input = document.getElementById('move-input');
          input.value = item.album.place_name;
          root.hidden = true;
          confirmMoveTo(item.album.place_name);
        });
        root.appendChild(div);
      });
      root.hidden = false;
    }

    async function confirmMoveTo(placeName) {
      const suggestion = activeMoveSuggestion;
      const response = await fetch('/api/final/move', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          paths: [...selectedPaths],
          album_key: suggestion ? suggestion.key : '',
          place_name: suggestion ? suggestion.place_name : placeName
        })
      });
      current = await response.json();
      selectedPaths.clear();
      lastSelectedIndex = null;
      movePanelOpen = false;
      activeMoveSuggestion = null;
      render();
    }

    function createAlbumLocationFields(album) {
      const fields = document.createElement('div');
      fields.className = 'album-location-fields';

      const countryWrap = document.createElement('div');
      countryWrap.className = 'album-country-wrap';
      const country = document.createElement('input');
      country.className = 'album-location-input album-country-input';
      country.value = album.country_or_region || '';
      country.autocomplete = 'off';
      country.placeholder = 'Country';
      country.title = 'Edit country or region';
      country.setAttribute('aria-label', `Country or region for ${album.place_name || 'this album'}`);
      const suggestions = document.createElement('div');
      suggestions.className = 'suggestions';
      suggestions.hidden = true;
      country.addEventListener('input', () => renderCountrySuggestions(country, suggestions, album));
      country.addEventListener('focus', () => renderCountrySuggestions(country, suggestions, album));
      country.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          renameAlbum(album.key, country.value, albumInput.value);
        }
        if (event.key === 'Escape') suggestions.hidden = true;
      });
      country.addEventListener('blur', () => {
        setTimeout(() => { suggestions.hidden = true; }, 100);
        renameAlbum(album.key, country.value, albumInput.value);
      });
      countryWrap.appendChild(country);
      countryWrap.appendChild(suggestions);

      const separator = document.createElement('span');
      separator.className = 'album-separator';
      separator.textContent = ':';

      const albumInput = document.createElement('input');
      albumInput.className = 'album-location-input album-name-input';
      albumInput.value = album.place_name || '';
      albumInput.placeholder = 'Album';
      albumInput.title = 'Edit folder name';
      albumInput.setAttribute('aria-label', `Album name for ${album.country_or_region || 'this country'}`);
      albumInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          renameAlbum(album.key, country.value, albumInput.value);
        }
      });
      albumInput.addEventListener('blur', () => renameAlbum(album.key, country.value, albumInput.value));

      fields.appendChild(countryWrap);
      fields.appendChild(separator);
      fields.appendChild(albumInput);
      return fields;
    }

    function fuzzyCountryScore(query, country) {
      const q = normalize(query);
      const value = normalize(country);
      if (!q) return 10;
      if (value === q) return 100;
      if (value.startsWith(q)) return 80;
      if (value.includes(q)) return 50;
      let cursor = 0;
      for (const char of q) {
        cursor = value.indexOf(char, cursor);
        if (cursor === -1) return -1;
        cursor += 1;
      }
      return 20;
    }

    function countrySuggestionOptions(query, currentCountry) {
      const custom = (query || '').trim();
      const options = [...COUNTRY_OPTIONS];
      if (currentCountry && !options.some((country) => normalize(country) === normalize(currentCountry))) {
        options.unshift(currentCountry);
      }
      return options
        .map((country) => ({country, score: fuzzyCountryScore(query, country)}))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || a.country.localeCompare(b.country))
        .slice(0, custom ? 8 : 10);
    }

    function renderCountrySuggestions(input, root, album) {
      const options = countrySuggestionOptions(input.value, album.country_or_region);
      if (!options.length) {
        root.hidden = true;
        return;
      }
      root.innerHTML = '';
      options.forEach((option, index) => {
        const item = document.createElement('div');
        item.className = `suggestion${index === 0 ? ' active' : ''}`;
        item.setAttribute('role', 'option');
        const label = document.createElement('strong');
        label.textContent = option.country;
        item.appendChild(label);
        item.addEventListener('mousedown', (event) => {
          event.preventDefault();
          input.value = option.country;
          root.hidden = true;
          renameAlbum(album.key, input.value, input.closest('.album-location-fields').querySelector('.album-name-input').value);
        });
        root.appendChild(item);
      });
      root.hidden = false;
    }

    async function renameAlbum(albumKey, countryOrRegion, placeName) {
      const response = await fetch('/api/final/album', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({album_key: albumKey, country_or_region: countryOrRegion, place_name: placeName})
      });
      current = await response.json();
      render();
    }

    async function approveFinalReview() {
      const response = await fetch('/api/final/approve', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      current = await response.json();
      render();
    }

    async function commitFinalReview() {
      const response = await fetch('/api/final/commit', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: '{}'
      });
      current = await response.json();
      render();
    }

    async function submitDecision(place) {
      let country = '';
      const typedLocation = splitLocation(place);
      if (typedLocation) {
        country = typedLocation.country;
        place = typedLocation.place;
      }
      if (activeSuggestion && normalize(place) === normalize(activeSuggestion.place_name)) {
        country = activeSuggestion.country_or_region;
        place = activeSuggestion.place_name;
      }
      const response = await fetch('/api/decision', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({country_or_region: country, place_name: place})
      });
      current = await response.json();
      render();
    }

    function normalize(value) {
      return (value || '').trim().toLocaleLowerCase();
    }

    function splitLocation(value) {
      const parts = (value || '').split('/');
      if (parts.length < 2) return null;
      const country = parts[0].trim();
      const place = parts.slice(1).join('/').trim();
      if (!country || !place) return null;
      return {country, place};
    }

    function fuzzyScore(query, suggestion) {
      const q = normalize(query);
      const label = normalize(`${suggestion.country_or_region}/${suggestion.place_name}`);
      const place = normalize(suggestion.place_name);
      const country = normalize(suggestion.country_or_region);
      const countryInput = normalize(current && current.country_or_region);
      if (!q) return country === countryInput ? 40 : 10;
      let score = 0;
      if (country === countryInput) score += 60;
      if (place === q || label === q) score += 100;
      else if (place.startsWith(q)) score += 80;
      else if (label.startsWith(q)) score += 70;
      else if (place.includes(q)) score += 50;
      else if (label.includes(q)) score += 40;
      else {
        let cursor = 0;
        for (const char of q) {
          cursor = label.indexOf(char, cursor);
          if (cursor === -1) return -1;
          cursor += 1;
        }
        score += 20;
      }
      return score;
    }

    function resetSuggestionState() {
      activeSuggestion = null;
      suggestionDraft = '';
      suggestionOptions = [];
      activeSuggestionIndex = -1;
    }

    function renderSuggestions() {
      const root = document.getElementById('suggestions');
      if (!current) {
        root.hidden = true;
        resetSuggestionState();
        return;
      }
      const query = suggestionDraft;
      const hasDraft = query.trim().length > 0;
      const ranked = (current.suggestions || [])
        .map((suggestion) => ({suggestion, score: fuzzyScore(query, suggestion)}))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || a.suggestion.place_name.localeCompare(b.suggestion.place_name))
        .slice(0, hasDraft ? 7 : 8);
      suggestionOptions = [
        ...(hasDraft ? [{kind: 'typed', value: query, detail: current.country_or_region || '', suggestion: null}] : []),
        ...ranked.map((item) => ({
          kind: 'existing',
          value: item.suggestion.place_name,
          detail: item.suggestion.country_or_region,
          suggestion: item.suggestion,
        })),
      ];
      if (!suggestionOptions.length) {
        root.hidden = true;
        activeSuggestion = null;
        activeSuggestionIndex = -1;
        return;
      }
      if (activeSuggestionIndex < 0 || activeSuggestionIndex >= suggestionOptions.length) {
        activeSuggestionIndex = 0;
      }
      root.innerHTML = '';
      suggestionOptions.forEach((option, index) => {
        const item = document.createElement('div');
        item.className = 'suggestion';
        item.id = `suggestion-${index}`;
        item.setAttribute('role', 'option');

        const label = document.createElement('strong');
        label.textContent = option.value;
        item.appendChild(label);
        if (option.detail) {
          const detail = document.createElement('span');
          detail.textContent = option.detail;
          item.appendChild(detail);
        }

        item.addEventListener('mousedown', (event) => {
          event.preventDefault();
          applySuggestionSelection(index);
          root.hidden = true;
        });
        root.appendChild(item);
      });
      root.hidden = false;
      applySuggestionSelection(activeSuggestionIndex, {updateInput: false});
    }

    function applySuggestionSelection(index, options = {}) {
      if (!suggestionOptions.length) {
        activeSuggestionIndex = -1;
        activeSuggestion = null;
        return;
      }
      const updateInput = options.updateInput !== false;
      activeSuggestionIndex = Math.max(0, Math.min(index, suggestionOptions.length - 1));
      const option = suggestionOptions[activeSuggestionIndex];
      activeSuggestion = option.suggestion || null;
      const placeInput = document.getElementById('place');
      if (updateInput) {
        placeInput.value = option.value;
        placeDirty = true;
      }
      placeInput.setAttribute('aria-activedescendant', `suggestion-${activeSuggestionIndex}`);
      const root = document.getElementById('suggestions');
      Array.from(root.children).forEach((node, nodeIndex) => {
        const active = nodeIndex === activeSuggestionIndex;
        node.classList.toggle('active', active);
        node.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      const activeNode = root.children[activeSuggestionIndex];
      if (activeNode && updateInput) {
        activeNode.scrollIntoView({block: 'nearest'});
      }
    }

    function moveSuggestionSelection(delta) {
      if (!suggestionOptions.length) return;
      const currentIndex = activeSuggestionIndex < 0 ? 0 : activeSuggestionIndex;
      const nextIndex = (currentIndex + delta + suggestionOptions.length) % suggestionOptions.length;
      applySuggestionSelection(nextIndex);
    }

    document.getElementById('review-form').addEventListener('submit', (event) => {
      event.preventDefault();
      placeDirty = false;
      submitDecision(document.getElementById('place').value);
    });

    document.getElementById('place').addEventListener('input', (event) => {
      placeDirty = true;
      suggestionDraft = event.target.value;
      activeSuggestion = null;
      activeSuggestionIndex = 0;
      renderSuggestions();
    });

    document.getElementById('place').addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      const root = document.getElementById('suggestions');
      if (root.hidden) renderSuggestions();
      moveSuggestionSelection(event.key === 'ArrowDown' ? 1 : -1);
    });

    window.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (expandedView) {
          event.preventDefault();
          closeExpandedImage(true);
          return;
        }
        const suggestions = document.getElementById('suggestions');
        if (suggestions) suggestions.hidden = true;
        activeSuggestion = null;
        activeSuggestionIndex = -1;
      }
      if (isSpaceKey(event) && expandedView && !isTextInputTarget(event.target)) {
        event.preventDefault();
        closeExpandedImage(true);
        return;
      }
      if (isSpaceKey(event) && !isTextInputTarget(event.target) && selectedImageIndex >= 0 && !expandedView) {
        event.preventDefault();
        openSelectedImage();
        return;
      }
      if (event.key === 'Enter' && expandedView) {
        event.preventDefault();
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        const form = document.getElementById('review-form');
        if (form) form.requestSubmit();
      }
    });

    loadState();
  </script>
</body>
</html>
"""
