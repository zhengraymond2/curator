"""Curator photo and video file-management toolkit."""

from .place_identification import (
    OpenRouterPlaceIdentifier,
    PhotoCandidate,
    PlaceIdentification,
    identify_places_for_groups,
    select_place_identification_samples,
)

__all__ = [
    "__version__",
    "OpenRouterPlaceIdentifier",
    "PhotoCandidate",
    "PlaceIdentification",
    "identify_places_for_groups",
    "select_place_identification_samples",
]

__version__ = "0.1.0"
