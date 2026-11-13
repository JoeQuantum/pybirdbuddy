"""Bird Buddy Feed models."""

from __future__ import annotations

from collections import UserDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from typing import Any
import uuid

from propcache import cached_property

from birdbuddy import LOGGER
from birdbuddy.exceptions import UnexpectedResponseError

FEED_OVERLAP = timedelta(minutes=5)
MAX_CHECKPOINT_IDS = 10_000


class PaginationError(UnexpectedResponseError):
    """A bounded traversal could not establish a complete requested result."""

    def __init__(self, reason: str) -> None:
        """Describe a local validation failure without copying server payloads."""
        super().__init__()
        self.reason = reason
        self.args = (reason,)


def _checkpoint_datetime(value: object) -> datetime | None:
    """Validate persisted UTC timestamps without accepting ambiguous local time."""
    if value is None:
        return None
    if not isinstance(value, str):
        msg = "Invalid checkpoint timestamp"
        raise ValueError(msg)  # noqa: TRY004 - persisted-state validation
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        msg = "Checkpoint timestamps must have a timezone"
        raise ValueError(msg)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class FeedCheckpoint:
    """Accepted intake state, independent of in-flight pagination cursors.

    IDs retain a five-minute timestamp overlap, plus undated items. Items
    arriving later with older timestamps require explicit backfill. Capacity
    exhaustion raises rather than silently evicting deduplication records.
    """

    watermark: datetime | None = None
    seen: tuple[tuple[str, datetime | None], ...] = ()

    @property
    def cutoff(self) -> datetime | None:
        """Oldest dated item considered by the next normal scan."""
        return self.watermark - FEED_OVERLAP if self.watermark else None

    def to_dict(self) -> dict[str, Any]:
        """Export versioned JSON-safe state for atomic intake persistence."""
        return {
            "version": 1,
            "watermark": self.watermark.isoformat() if self.watermark else None,
            "seen": {
                node_id: created.isoformat() if created else None
                for node_id, created in self.seen
            },
        }

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> FeedCheckpoint:
        """Reject corrupt, oversized or unknown-version persisted state."""
        if not isinstance(state, dict) or set(state) != {
            "version",
            "watermark",
            "seen",
        }:
            msg = "Invalid feed checkpoint structure"
            raise ValueError(msg)
        if type(state["version"]) is not int or state["version"] != 1:
            msg = "Unsupported feed checkpoint version"
            raise ValueError(msg)
        watermark = _checkpoint_datetime(state["watermark"])
        records = state["seen"]
        if not isinstance(records, dict) or len(records) > MAX_CHECKPOINT_IDS:
            msg = "Invalid feed checkpoint ID collection"
            raise ValueError(msg)
        seen = []
        for node_id, timestamp in records.items():
            if not isinstance(node_id, str) or not node_id:
                msg = "Invalid feed checkpoint ID"
                raise ValueError(msg)
            created = _checkpoint_datetime(timestamp)
            if created and (watermark is None or created > watermark):
                msg = "Feed checkpoint item exceeds watermark"
                raise ValueError(msg)
            seen.append((node_id, created))
        return cls(watermark, tuple(seen))

    def advance(self, nodes: list[FeedNode]) -> FeedCheckpoint:
        """Propose state after intake; this does not acknowledge the batch."""
        seen = dict(self.seen)
        for node in nodes:
            seen[node.node_id] = node.created_at
        dates = [created for created in seen.values() if created is not None]
        if self.watermark is not None:
            dates.append(self.watermark)
        watermark = max(dates, default=None)
        cutoff = watermark - FEED_OVERLAP if watermark else None
        retained = tuple(
            (node_id, created)
            for node_id, created in seen.items()
            if created is None or cutoff is None or created >= cutoff
        )
        if len(retained) > MAX_CHECKPOINT_IDS:
            msg = "Feed checkpoint capacity exceeded"
            raise PaginationError(msg)
        return FeedCheckpoint(watermark, retained)


@dataclass(frozen=True)
class FeedBatch:
    """Unacknowledged intake with immutable state and defensive node copies."""

    batch_id: str
    checkpoint: FeedCheckpoint
    _payload: str = field(repr=False)

    @classmethod
    def create(cls, nodes: list[FeedNode], checkpoint: FeedCheckpoint) -> FeedBatch:
        """Snapshot payloads so consumer edits cannot alter a pending replay."""
        return cls(
            str(uuid.uuid4()), checkpoint, json.dumps([node.data for node in nodes])
        )

    @property
    def nodes(self) -> tuple[FeedNode, ...]:
        """Return fresh node copies on each access."""
        return tuple(FeedNode(data) for data in json.loads(self._payload))


class FeedNodeType(Enum):
    """Known Feed node types."""

    CollectedPostcard = "FeedItemCollectedPostcard"
    EditorsChoice = "FeedItemEditorsChoice"
    InvitationAccepted = "FeedItemFeederInvitationAccepted"
    InvitationConfirmed = "FeedItemFeederInvitationConfirmed"
    InvitationDeclined = "FeedItemFeederInvitationDeclined"
    LivestreamAccessRequest = "FeedItemFeederLivestreamAccessRequest"
    MemberDeleted = "FeedItemFeederMemberDeleted"
    MemberJoined = "FeedItemFeederMemberJoined"
    MediaLiked = "FeedItemMediaLiked"
    MysteryVisitorNotRecognized = "FeedItemMysteryVisitorNotRecognized"
    MysteryVisitorResolved = "FeedItemMysteryVisitorResolved"
    NewPostcard = "FeedItemNewPostcard"
    ReferralCodeUsed = "FeedItemReferralCodeUsed"
    RemoteFeederConnectionExpired = "FeedItemRemoteFeederConnectionExpired"
    RemoteFeederUnlocked = "FeedItemRemoteFeederUnlocked"
    SpeciesSighting = "FeedItemSpeciesSighting"
    SpeciesUnlocked = "FeedItemSpeciesUnlocked"

    Unknown = "Unknown"
    """Sentinel value for an unexpected feed type."""

    @classmethod
    def _missing_(cls, value: object) -> FeedNodeType:
        """Fall back to ``Unknown`` for an unrecognized feed type.

        Args:
            value: The raw value that matched no enum member.

        Returns:
            ``FeedNodeType.Unknown``.
        """
        LOGGER.warning("Unexpected Feed type: %s", value)
        return FeedNodeType.Unknown


class FeedNode(UserDict[str, Any]):
    """A single Feed edge node."""

    _DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f%z"
    """The format string of GraphQL timestamps. Not guaranteed to conform to
    :func:`datetime.fromisoformat()`, so it has to be parsed manually."""

    @staticmethod
    def parse_datetime(timestr: str | None) -> datetime | None:
        """Parse a GraphQL timestamp string into a datetime.

        Args:
            timestr: The timestamp string, or ``None``.

        Returns:
            The parsed ``datetime``, or ``None`` when ``timestr`` is ``None``.
        """
        if not isinstance(timestr, str):
            return None
        try:
            value = datetime.fromisoformat(timestr.replace("Z", "+00:00"))
        except ValueError:
            return None
        # A malformed timestamp remains an undated item rather than losing
        # its ID or comparing naive account-local time with UTC checkpoints.
        return value.astimezone(timezone.utc) if value.tzinfo else None

    @property
    def node_id(self) -> str:
        """The Feed node id."""
        return self["id"]

    @property
    def node_type(self) -> FeedNodeType:
        """The feed node type."""
        return FeedNodeType(self.get("__typename"))

    @property
    def created_at(self) -> datetime | None:
        """The `datetime` when the item was created, or None if absent.

        Feed items are ``AnyFeedItem`` union members; a member the query does
        not select ``createdAt`` for carries no timestamp, hence ``None``.
        """
        return FeedNode.parse_datetime(self.get("createdAt"))


class FeedEdge(UserDict[str, Any]):
    """A single Feed edge."""

    @property
    def cursor(self) -> str | None:
        """Feed edge cursor."""
        return self.get("cursor")

    @property
    def node(self) -> FeedNode:
        """Feed edge node."""
        return FeedNode(self.get("node"))


class Feed(UserDict[str, Any]):
    """Representation of the Bird Buddy Feed items."""

    def validated_nodes(self) -> list[FeedNode]:
        """Require IDs for intake; malformed pages cannot advance checkpoints."""
        edges = self.get("edges")
        if not isinstance(edges, list):
            msg = "Feed edges are missing or malformed"
            raise PaginationError(msg)
        nodes = []
        for edge in edges:
            data = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("id"), str):
                msg = "Feed node has no usable ID"
                raise PaginationError(msg)
            if not data["id"]:
                msg = "Feed node has no usable ID"
                raise PaginationError(msg)
            nodes.append(FeedNode(data))
        return nodes

    @property
    def edges(self) -> Iterator[FeedEdge]:
        """Returns all edges of the Feed."""
        return (FeedEdge(edge) for edge in self.get("edges", []))

    @property
    def nodes(self) -> Iterator[FeedNode]:
        """Returns all nodes of the Feed edges."""
        return (edge.node for edge in self.edges)

    @property
    def page_end_cursor(self) -> str | None:
        """The cursor used to access the next (older) page of feed items."""
        return self.get("pageInfo", {}).get("endCursor", None)

    @cached_property
    def newest_edge(self) -> FeedEdge | None:
        """Return the newest `FeedEdge` by time, or None when all undated."""
        dated = [
            (created, edge)
            for edge in self.edges
            if (created := edge.node.created_at) is not None
        ]
        return max(dated, key=lambda pair: pair[0])[1] if dated else None

    def filter(
        self,
        of_type: FeedNodeType | list[FeedNodeType] | None = None,
        newer_than: datetime | None = None,
    ) -> list[FeedNode]:
        """Filter the feed by node type and/or recency.

        Args:
            of_type: Only include nodes of this type (or types); ``None``
                includes all types.
            newer_than: Only include nodes created after this time; ``None``
                includes all times.

        Returns:
            The matching feed nodes.
        """
        if isinstance(of_type, FeedNodeType):
            of_type = [of_type]
        return [
            node
            for node in self.nodes
            if (of_type is None or node.node_type in of_type)
            and (
                newer_than is None or (node.created_at and node.created_at > newer_than)
            )
        ]
