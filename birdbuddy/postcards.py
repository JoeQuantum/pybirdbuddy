"""Postcard data models: analysis preview and the postcardCollect flow."""

from __future__ import annotations

from collections import UserDict
from dataclasses import dataclass
from typing import Any

from birdbuddy.birds import Species
from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.feeder import Feeder
from birdbuddy.media import Media

# Sighting-report-preview members that carry a recognized species.
_RECOGNIZED_SIGHTINGS = (
    "SightingRecognizedBird",
    "SightingRecognizedBirdUnlocked",
)


@dataclass(frozen=True)
class CollectionReceipt:
    """Separate the observed collection result from unverified sharing state."""

    requested_postcard_id: str
    is_first_collected_postcard: bool | None = None
    reconciled_by_read: bool = False
    share_requested: bool = False
    # The current response selection provides no per-media sharing confirmation.
    sharing_confirmed: bool | None = None


class CollectedPostcard(UserDict[str, Any]):
    """A postcard collected into your account.

    Wraps the ``FeedItemCollectedPostcard`` returned by
    ``BirdBuddy.collect_postcard``.
    """

    def __init__(self, data: dict, *, receipt: CollectionReceipt | None = None) -> None:
        """Require an identified collected item; null cannot become empty success."""
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not data["id"]
            or data.get("__typename") != "FeedItemCollectedPostcard"
        ):
            raise UnexpectedResponseError
        super().__init__(data)
        self.receipt = receipt

    @property
    def id(self) -> str:
        """The collected feed-item id."""
        return self["id"]

    @property
    def species(self) -> list[Species]:
        """The recognized species (a postcard may hold more than one)."""
        return [Species(s) for s in self.get("species") or []]

    @property
    def medias(self) -> list[Media]:
        """The postcard media."""
        return [Media(m) for m in self.get("medias") or []]

    @property
    def has_mystery_visitor(self) -> bool:
        """Whether the postcard contains an unrecognized visitor."""
        return bool(self.get("hasMysteryVisitor"))

    @property
    def has_new_species(self) -> bool:
        """Whether the postcard unlocked a new species."""
        return bool(self.get("hasNewSpecies"))

    @property
    def inference_execution_mode(self) -> str | None:
        """The inference execution mode (e.g. ``MANUAL_COMPLETED``)."""
        return self.get("inferenceExecutionMode")

    @property
    def inference_type(self) -> str | None:
        """The inference type (e.g. ``ADVANCED``)."""
        return self.get("inferenceType")

    @property
    def species_confidence(self) -> str | None:
        """Species-name identification confidence.

        One of ``CANNOT_DECIDE`` or ``VERY_CONFIDENT``, or ``None`` when the
        backend did not report it.
        """
        return self.get("mediaSpeciesNameIdentificationConfidenceLevel")


class PostcardAnalysis(UserDict[str, Any]):
    """A postcard's AI analysis (the app's "Reanalyze with AI").

    Wraps the ``FeedItemNewPostcard`` returned by
    ``BirdBuddy.identify_postcard``: the recognized species and media for a
    postcard, available without collecting it.
    """

    @property
    def id(self) -> str:
        """The postcard feed-item id."""
        return self["id"]

    @property
    def feeder(self) -> Feeder | None:
        """The feeder that captured the postcard, when reported."""
        feeder = self.get("feeder")
        return Feeder(feeder) if feeder else None

    @property
    def medias(self) -> list[Media]:
        """The postcard media."""
        return [Media(m) for m in self.get("medias") or []]

    @property
    def species(self) -> list[Species]:
        """The recognized species, from the sighting-report preview."""
        preview = self.get("sightingReportPreview") or {}
        return [
            Species(sighting["species"])
            for sighting in preview.get("sightings") or []
            if sighting.get("__typename") in _RECOGNIZED_SIGHTINGS
            and sighting.get("species")
        ]

    @property
    def confidence(self) -> str | None:
        """Overall inference confidence (e.g. ``HIGH_CONFIDENCE``)."""
        return self.get("inferenceConfidenceLevel")

    @property
    def inference_execution_mode(self) -> str | None:
        """The inference execution mode (e.g. ``MANUAL_COMPLETED``)."""
        return self.get("inferenceExecutionMode")
