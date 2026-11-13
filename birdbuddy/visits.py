"""Canonical, tolerant views of visitor-bearing feed items.

These read models neither identify nor collect postcards. The copied raw
payload remains available separately from the compact normalized fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlparse


def _text(value: object) -> str | None:
    """Accept a nonempty scalar string without inventing missing values."""
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime | None:
    """Tolerate sparse historical selections and malformed backend dates."""
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class VisitMedia:
    """One selected asset, including sparse or future media variants."""

    id: str | None
    kind: str
    created_at: datetime | None
    content_url: str | None
    thumbnail_url: str | None

    @property
    def is_image(self) -> bool:
        """Whether the API explicitly identifies an image."""
        return self.kind == "MediaImage"

    @property
    def is_video(self) -> bool:
        """Whether the API explicitly identifies a video."""
        return self.kind == "MediaVideo"

    def to_dict(self) -> dict[str, Any]:
        """Return a fresh selected-shape media object for existing consumers."""
        return {
            "id": self.id,
            "__typename": self.kind,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "contentUrl": self.content_url,
            "thumbnailUrl": self.thumbnail_url,
        }


@dataclass(frozen=True)
class VisitSpecies:
    """A recognized species, distinct from suggestions and named individuals."""

    id: str | None
    name: str | None
    icon_url: str | None

    def to_dict(self) -> dict[str, str | None]:
        """Return the compact fields useful to automations and visitor views."""
        return {"id": self.id, "name": self.name, "iconUrl": self.icon_url}


@dataclass(frozen=True)
class Visit:
    """An account-scoped item; missing attribution remains unknown."""

    item_id: str | None
    kind: str
    created_at: datetime | None
    feeder_id: str | None
    attribution: str | None
    medias: tuple[VisitMedia, ...]
    species: tuple[VisitSpecies, ...]
    _raw: dict = field(repr=False, compare=False)

    @property
    def raw(self) -> dict:
        """Return a copy so event compaction cannot destroy canonical data."""
        return deepcopy(self._raw)

    @property
    def representative_image(self) -> VisitMedia | None:
        """Pick a usable image; never pass a video URL to an image entity."""
        return next(
            (
                m
                for m in self.medias
                if m.is_image and (m.content_url or m.thumbnail_url)
            ),
            None,
        )

    @property
    def preview_url(self) -> str | None:
        """Prefer an image, then a video's thumbnail, without discarding that video."""
        if image := self.representative_image:
            return image.content_url or image.thumbnail_url
        return next((m.thumbnail_url for m in self.medias if m.thumbnail_url), None)


def _media_values(data: Mapping) -> list[Mapping]:
    """Merge singular/plural/legacy-video selections without losing distinct IDs."""
    values = data.get("medias") or []
    candidates = list(values) if isinstance(values, list) else []
    candidates.extend([data.get("media"), data.get("videoMedia")])
    seen: set[str] = set()
    result = []
    for value in candidates:
        if not isinstance(value, Mapping):
            continue
        media_id = _text(value.get("id"))
        if media_id and media_id in seen:
            continue
        if media_id:
            seen.add(media_id)
        result.append(value)
    return result


def _species_values(data: Mapping) -> list[Mapping]:
    """Read direct, nested collection and recognized preview species only."""
    direct = data.get("species")
    values = list(direct) if isinstance(direct, list) else [direct]
    collection = data.get("collection")
    if isinstance(collection, Mapping):
        values.append(collection.get("species"))
    preview = data.get("sightingReportPreview")
    if isinstance(preview, Mapping):
        sightings = preview.get("sightings")
        for sighting in sightings if isinstance(sightings, list) else []:
            if isinstance(sighting, Mapping) and sighting.get("__typename") in {
                "SightingRecognizedBird",
                "SightingRecognizedBirdUnlocked",
            }:
                values.append(sighting.get("species"))
    return [value for value in values if isinstance(value, Mapping)]


def _attribution(
    data: Mapping, media: list[Mapping], known_feeder_ids: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """Prefer explicit identity; a collection cover is relevant only to that asset."""
    feeder = data.get("feeder")
    if isinstance(feeder, Mapping) and (feeder_id := _text(feeder.get("id"))):
        return feeder_id, "feeder"
    collection = data.get("collection")
    cover = (
        collection.get("coverCollectionMedia")
        if isinstance(collection, Mapping)
        else None
    )
    if isinstance(cover, Mapping):
        cover_media = cover.get("media")
        cover_feeder = cover.get("feeder")
        if isinstance(cover_media, Mapping) and isinstance(cover_feeder, Mapping):
            media_id = _text(cover_media.get("id"))
            if (
                media_id
                and any(m.get("id") == media_id for m in media)
                and (feeder_id := _text(cover_feeder.get("id")))
            ):
                return feeder_id, "matching_collection_media"
    # Compatibility hint for older feed variants without an explicit feeder.
    # Compare complete path segments, never substrings or signed query values.
    matches = set()
    for asset in media:
        for key in ("contentUrl", "thumbnailUrl"):
            url = _text(asset.get(key))
            if url:
                try:
                    segments = set(unquote(urlparse(url).path).split("/"))
                except ValueError:
                    continue
                matches.update(
                    feeder_id for feeder_id in known_feeder_ids if feeder_id in segments
                )
    return (next(iter(matches)), "url_path") if len(matches) == 1 else (None, None)


def normalize_visit(
    payload: Mapping, *, known_feeder_ids: tuple[str, ...] = ()
) -> Visit:
    """Normalize selected read data without modifying it or making API calls."""
    data = deepcopy(dict(payload))
    media_values = _media_values(data)
    medias = tuple(
        VisitMedia(
            _text(m.get("id")),
            _text(m.get("__typename")) or "UnknownMedia",
            _timestamp(m.get("createdAt")),
            _text(m.get("contentUrl")),
            _text(m.get("thumbnailUrl")),
        )
        for m in media_values
    )
    species = tuple(
        dict.fromkeys(
            VisitSpecies(
                _text(s.get("id")), _text(s.get("name")), _text(s.get("iconUrl"))
            )
            for s in _species_values(data)
        )
    )
    feeder_id, attribution = _attribution(data, media_values, known_feeder_ids)
    return Visit(
        _text(data.get("id")),
        _text(data.get("__typename")) or "UnknownFeedItem",
        _timestamp(data.get("createdAt")),
        feeder_id,
        attribution,
        medias,
        species,
        data,
    )
