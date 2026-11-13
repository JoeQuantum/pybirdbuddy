"""Bird Buddy collections and media."""

from __future__ import annotations

from collections import UserDict
from datetime import datetime, timedelta, timezone
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from birdbuddy.birds import Species
from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.feed import FeedNode


class Media(UserDict[str, Any]):
    """Represents one ``MediaImage`` or ``MediaVideo`` type."""

    @property
    def id(self) -> str:
        """The media id."""
        return self["id"]

    @property
    def is_video(self) -> bool:
        """`True` if this Media is a Video item, `False` if Image."""
        return self["__typename"] == "MediaVideo"

    @property
    def created_at(self) -> datetime:
        """Creation timestamp.

        Returns:
            The creation ``datetime``. ``createdAt`` is non-null in the
            schema, so it is always present.

        Raises:
            UnexpectedResponseError: If ``createdAt`` is missing or null,
                which the schema does not permit.
        """
        if (created := FeedNode.parse_datetime(self.get("createdAt"))) is None:
            raise UnexpectedResponseError(self.data)
        return created

    @property
    def thumbnail_url(self) -> str:
        """Thumbnail URL."""
        return self["thumbnailUrl"]

    @property
    def content_url(self) -> str | None:
        """Large content URL."""
        return self.get("contentUrl", None)

    @property
    def is_expired(self) -> bool | None:
        """`True` if the media URL is expired."""
        return is_media_expired(self.thumbnail_url)


def is_media_expired(media_url: str | None) -> bool | None:
    """Report whether a signed media URL has expired.

    Args:
        media_url: A CloudFront or S3 signed media URL.

    Returns:
        ``True`` if expired, ``False`` if still valid, or ``None`` when the
        URL is empty or carries no usable expiry metadata.
    """
    if not isinstance(media_url, str) or not media_url:
        return None
    try:
        query = parse_qs(urlparse(media_url).query)
        if expires := query.get("Expires"):
            return int(expires[-1]) <= time.time()
        if (dates := query.get("X-Amz-Date")) and (
            durations := query.get("X-Amz-Expires")
        ):
            issued = datetime.strptime(dates[-1], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
            return (
                issued + timedelta(seconds=int(durations[-1]))
            ).timestamp() <= time.time()
    except (TypeError, ValueError, OverflowError):
        return None
    return None


class CollectionMedia(Media):
    """A media item with its collection wrapper and explicit feeder attribution.

    The ordinary Media mapping remains unchanged for existing consumers. The
    wrapper is retained separately; feeder identity never comes from its URL.
    """

    def __init__(
        self, wrapper: dict[str, Any], collection_id: str | None = None
    ) -> None:
        """Retain the media and the metadata returned by the collection query."""
        super().__init__(wrapper["media"])
        self.collection_id = collection_id
        self.collection_metadata = {
            key: value for key, value in wrapper.items() if key != "media"
        }

    @property
    def collection_media_id(self) -> str | None:
        """The wrapper ID, distinct from the nested media ID."""
        value = self.collection_metadata.get("id")
        return value if isinstance(value, str) else None

    @property
    def feeder_id(self) -> str | None:
        """The explicitly returned feeder ID, when present."""
        feeder = self.collection_metadata.get("feeder")
        value = feeder.get("id") if isinstance(feeder, dict) else None
        return value if isinstance(value, str) else None

    @property
    def feeder_name(self) -> str | None:
        """The display name reported on this collection-media wrapper."""
        value = self.collection_metadata.get("feederName")
        return value if isinstance(value, str) else None


class Collection(UserDict[str, Any]):
    """Collection of media for a particular bird species."""

    @property
    def bird_name(self) -> str | None:
        """The bird species in this collection."""
        return self.get("species", {}).get("name", None)

    @property
    def species(self) -> Species | None:
        """The bird species of this collection."""
        if s := self.get("species", None):
            return Species(s)
        return None

    @property
    def collection_id(self) -> str:
        """The collection ``UUID``."""
        return self["id"]

    @property
    def total_visits(self) -> int:
        """Total number of visits."""
        return int(self.get("visitsAllTime", 0))

    @property
    def last_visit(self) -> datetime:
        """Most recent visit time.

        Returns:
            The last-visit ``datetime``. ``visitLastTime`` is non-null in the
            schema, so it is always present.

        Raises:
            UnexpectedResponseError: If ``visitLastTime`` is missing or null,
                which the schema does not permit.
        """
        if (visited := FeedNode.parse_datetime(self.get("visitLastTime"))) is None:
            raise UnexpectedResponseError(self.data)
        return visited

    @property
    def feeder_name(self) -> str | None:
        """The feeder that captured this cover."""
        return self["coverCollectionMedia"].get("feederName")

    @property
    def cover_media(self) -> CollectionMedia:
        """The cover media."""
        return CollectionMedia(self["coverCollectionMedia"], self.collection_id)
