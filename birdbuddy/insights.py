"""Bounded account insight reads, independent of ordinary feeder polling.

Schema validation proves field shapes, not entitlement or hardware support.
Reuse one adapter per client; no method mutates or starts a periodic task.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from time import monotonic
from typing import Any

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.queries import insights as queries

Shape = Any
_SPECIES = dict.fromkeys(("__typename", "id", "name", "scientificName", "iconUrl"))
_MEDIA = dict.fromkeys(("__typename", "id", "createdAt", "thumbnailUrl"))
_STATS = dict.fromkeys(("firstSeenAt", "lastSeenAt", "visitCount", "usualTimeOfDay"))
_NAMED = {
    **dict.fromkeys(
        ("id", "name", "createdAt", "state", "personalityBadge", "relationshipLevel")
    ),
    "species": _SPECIES,
    "stats": _STATS,
}
_PRIVATE = dict.fromkeys(
    (
        "version",
        "housingType",
        "mountingType",
        "solarRoof",
        "supportsAudio",
        "supportsEnhancedLivestream",
        "supportsLivestreamSnapshot",
        "supportsWebRTC",
    )
)
_ENERGY = (
    {"__typename": None, "status": None},
    {
        "FeederSmartEnergyEnabled": {"state": None, "runningPowerProfile": None},
        "FeederSmartEnergyDisabled": {},
        "FeederSmartEnergyNotSupported": {"reason": None},
    },
)
_HARDWARE = {
    "featureLocks": [{"feature": None}],
    "subscriptionPremium": dict.fromkeys(("__typename", "isActive", "type")),
    "feeders": [
        (
            {"__typename": None, "id": None, "name": None},
            {
                "FeederForOwner": {
                    **_PRIVATE,
                    "powerProfile": None,
                    "videoQuality": None,
                    "cameraFieldOfView": None,
                    "smartEnergy": _ENERGY,
                },
                "FeederForMember": {**_PRIVATE, "livestreamAccessStatus": None},
                "FeederForMemberPending": {},
            },
        )
    ],
}
_SPECIES_VISIT = {"species": _SPECIES, "visitCount": None, "medias": [_MEDIA]}
_WEEKLY = {
    "isoWeek": None,
    "year": None,
    "generalVisitStats": {
        **dict.fromkeys(
            ("totalVisits", "previousWeekTotalVisits", "totalObservationTimeMinutes")
        ),
        "dailyVisits": [{"date": None, "visitCount": None}],
    },
    "feederVisitStats": {
        "feeders": [{"feeder": {"id": None, "name": None}, "visitCount": None}]
    },
    "speciesVisitStats": {
        "distinctSpeciesCount": None,
        "mysteryVisitorVisitCount": None,
        "mysteryVisitor": {"medias": [_MEDIA]},
        "distinctSpecies": [_SPECIES_VISIT],
    },
    "milestoneStats": dict.fromkeys(
        ("currentStreakWeeks", "lifetimeTotalVisits", "streakActiveDays")
    ),
}
_YEARLY = {
    "year": None,
    "generalVisitStats": dict.fromkeys(
        ("totalVisits", "totalVisitsPercentile", "hasFeederHousingTypeClassicVisits")
    ),
    "dailyVisitStats": {"topDays": [{"date": None, "visitCount": None}]},
    "monthlyVisitStats": {"monthlyVisits": [{"month": None, "visitCount": None}]},
    "speciesVisitStats": {
        "distinctSpeciesPercentile": None,
        "distinctSpecies": [_SPECIES_VISIT],
    },
    "namedBirdVisitStats": {
        "namedBirds": [{"identificationCount": None, "namedBird": _NAMED}]
    },
}
_REPORT = {
    "userId": None,
    "feederIds": [None],
    "reportLabel": None,
    "reportPeriod": {"startDate": None, "endDate": None},
    "visitAggregateMetrics": {
        **dict.fromkeys(
            ("busiestHourOfDay", "numberOfUniqueSpecies", "totalNumberOfVisits")
        ),
        "speciesVisitSummaries": [
            dict.fromkeys(("speciesId", "speciesLabel", "numberOfVisits", "peakHour"))
        ],
    },
}
_REMOTE = [
    dict.fromkeys(("id", "createdAt", "expiresAt", "slotNumber", "feederName"))
    | {
        "remoteFeeder": dict.fromkeys(
            (
                "id",
                "feederName",
                "title",
                "tier",
                "type",
                "isUnlocked",
                "isUserConnected",
            )
        ),
    }
]
_UNAVAILABLE = {
    "WeeklyRecapForUserInsufficientVisitsError",
    "WeeklyRecapForUserNotAvailableError",
    "WeeklyRecapForUserNotFoundError",
    "YearInReviewForUserInsufficientVisitsError",
    "YearInReviewForUserNotAvailableError",
}


class InsightLimitError(ValueError):
    """The requested or selected data exceeds a declared local resource bound."""


class InsightClosedError(RuntimeError):
    """The owning integration has closed this adapter."""


class InsightLanguageError(RuntimeError):
    """The client language changed while selecting or loading a cached result."""


class InsightAccountError(UnexpectedResponseError):
    """The current/returned account differs from the scoped insight read."""

    def __init__(self, message: str) -> None:
        """Keep a fixed diagnostic without retaining an account response."""
        super().__init__()
        self.args = (message,)


@dataclass(frozen=True)
class _Read:
    resource: str
    query: str
    path: tuple[str, ...]
    shape: Shape
    ttl: int
    union: bool = False


_READS = {
    "hardware": _Read("hardware", queries.HARDWARE, ("me",), _HARDWARE, 300),
    "preferences": _Read(
        "preferences",
        queries.PREFERENCES,
        ("me", "postcardSettings"),
        {"ignoreMysteryVisitors": None, "ignoredSpecies": [_SPECIES]},
        300,
    ),
    "species": _Read(
        "species",
        queries.SPECIES,
        ("species",),
        (
            _SPECIES,
            {
                "SpeciesBird": {"alternativeName": None, "isUnofficialName": None},
                "SpeciesBirdFamily": {},
                "SpeciesBirdGenus": {},
                "SpeciesBirdOrder": {},
            },
        ),
        86400,
    ),
    "named_birds": _Read(
        "named_birds",
        queries.NAMED_BIRDS,
        ("me", "mediaSpeciesNames"),
        [_NAMED | {"previewMedia": [_MEDIA]}],
        900,
    ),
    "weekly_recap": _Read(
        "weekly_recap", queries.WEEKLY, ("weeklyRecapForUser",), _WEEKLY, 3600, True
    ),
    "yearly_review": _Read(
        "yearly_review", queries.YEARLY, ("yearInReviewForUser",), _YEARLY, 3600, True
    ),
    "feeder_report": _Read(
        "feeder_report", queries.FEEDER_REPORT, ("userFeederReport",), _REPORT, 3600
    ),
    "remote_connections": _Read(
        "remote_connections",
        queries.REMOTE_CONNECTIONS,
        (
            "me",
            "remoteFeederConnections",
        ),
        _REMOTE,
        300,
    ),
}


class _Projection:
    """Allowlist fields and declare local list and shape limitations."""

    def __init__(self, limit: int, byte_budget: int = 500_000) -> None:
        self.limit = limit
        self._remaining = byte_budget
        self.truncated: list[str] = []
        self.missing: list[str] = []
        self.unknown: list[str] = []

    def project(self, value: object, shape: Shape, path: str = "data") -> object:
        """Return only selected fields; unknown dictionaries never pass through."""
        # Bound construction itself, before assembling a potentially large tree.
        self._remaining -= len(path.encode("utf-8")) + 16
        if shape is None and isinstance(value, (str, int, float, bool)):
            self._remaining -= len(
                json.dumps(value, ensure_ascii=False).encode("utf-8")
            )
        if self._remaining < 0:
            message = "Selected insight data exceeds its construction budget"
            raise InsightLimitError(message)
        if value is None:
            return None
        if shape is None:
            if isinstance(value, str) and len(value) > 8192:
                msg = "An insight scalar exceeds 8192 characters"
                raise InsightLimitError(msg)
            if isinstance(value, (str, int, float, bool)):
                return value
            self.missing.append(path)
            return None
        if isinstance(shape, list):
            if not isinstance(value, list):
                self.missing.append(path)
                return None
            if len(value) > self.limit:
                self.truncated.append(path)
            return [
                self.project(item, shape[0], f"{path}[{i}]")
                for i, item in enumerate(value[: self.limit])
            ]
        if not isinstance(value, Mapping):
            self.missing.append(path)
            return None
        if isinstance(shape, tuple):
            base, variants = shape
            kind = value.get("__typename")
            variant = variants.get(kind) if isinstance(kind, str) else None
            if variant is None:
                self.unknown.append(path)
            shape = base | (variant or {})
        result = {}
        for key, nested in shape.items():
            if key not in value:
                self.missing.append(f"{path}.{key}")
            result[key] = self.project(value.get(key), nested, f"{path}.{key}")
        return result


def _identifier(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
    ):
        msg = "Expected an explicit ID of 1 to 512 characters"
        raise ValueError(msg)
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        msg = "Insight list limit must be an integer from 1 to 100"
        raise InsightLimitError(msg)
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AccountInsights:
    """Explicit insight reads with bounded LRU caching and in-flight coalescing.

    Limits apply to returned/cached data, not server-side list retrieval: these
    fields have no pagination arguments. Errors are uncached; concurrent callers
    share one read or exception. Cancelling one waiter preserves a shared read.
    """

    def __init__(
        self,
        client: BirdBuddy,
        *,
        clock: Callable[[], float] = monotonic,
        now: Callable[[], datetime] = _utc_now,
        max_cached_results: int = 32,
        max_cache_bytes: int = 2_000_000,
    ) -> None:
        """Create an idle adapter; construction performs no authentication or I/O."""
        if (
            not 1 <= max_cached_results <= 128
            or not 1024 <= max_cache_bytes <= 10_000_000
        ):
            msg = "Cache limits exceed supported bounds"
            raise InsightLimitError(msg)
        self._client = client
        self._clock = clock
        self._now = now
        self._capacity = max_cached_results
        self._byte_capacity = max_cache_bytes
        self._account: str | None = None
        self._closed = False
        self._cache: OrderedDict[str, tuple[float, int, dict[str, Any]]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}

    def _identity(self) -> str:
        if self._closed:
            message = "The account insight adapter is closed"
            raise InsightClosedError(message)
        user = self._client.user
        account = user.get("id") if isinstance(user, Mapping) else None
        if account != self._account:
            self._cache.clear()
            self._account = account if isinstance(account, str) else None
        if not isinstance(account, str) or not account:
            msg = "Authenticate the account before requesting insights"
            raise InsightAccountError(msg)
        return account

    async def async_close(self) -> None:
        """Reject new reads, cancel and await pending work, and release cached data."""
        self._closed = True
        self._cache.clear()
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

    async def hardware(self, *, limit: int = 50) -> dict[str, Any]:
        """Read roles, hardware, explicit locks and configured/running energy modes."""
        return await self._read("hardware", {}, limit)

    async def preferences(self, *, limit: int = 50) -> dict[str, Any]:
        """Read ignored species and mystery preference without changing either."""
        return await self._read("preferences", {}, limit)

    async def species(self, species_id: str) -> dict[str, Any]:
        """Read stable species identity and the precise vendor taxonomy variant."""
        return await self._read("species", {"speciesId": _identifier(species_id)}, 50)

    async def named_birds(self, *, limit: int = 50) -> dict[str, Any]:
        """Read individual-bird vendor statistics, including stopped recognition."""
        return await self._read("named_birds", {}, limit)

    async def weekly_recap(self, *, limit: int = 50) -> dict[str, Any]:
        """Read the vendor-selected ISO week or its typed unavailable result."""
        return await self._read("weekly_recap", {"userId": self._identity()}, limit)

    async def yearly_review(self, *, limit: int = 50) -> dict[str, Any]:
        """Read the vendor-selected year; no historical year parameter exists."""
        return await self._read("yearly_review", {"userId": self._identity()}, limit)

    async def feeder_report(
        self,
        *,
        feeder_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Read account/known-feeder metrics, with an optional range up to 366 days.

        Date arguments are supported only here. Both must be timezone-aware.
        Returned reportPeriod is authoritative; historical retention is unverified.
        """
        variables: dict[str, Any] = {"userId": self._identity()}
        if feeder_id is not None:
            feeder_id = _identifier(feeder_id)
            if feeder_id not in self._client.feeders:
                msg = "The feeder is not in the authenticated account"
                raise InsightAccountError(msg)
            variables["feederId"] = feeder_id
        if start is not None or end is not None:
            if (
                not isinstance(start, datetime)
                or not isinstance(end, datetime)
                or start.utcoffset() is None
                or end.utcoffset() is None
            ):
                msg = "Supply both start and end as timezone-aware datetimes"
                raise ValueError(msg)
            if not timedelta(0) < end - start <= timedelta(days=366):
                msg = "Report range must be positive and at most 366 days"
                raise InsightLimitError(msg)
            variables.update(start=start.isoformat(), end=end.isoformat())
        return await self._read("feeder_report", variables, limit)

    async def remote_connections(self, *, limit: int = 50) -> dict[str, Any]:
        """Read expiring remote connections as account metadata, never owned devices."""
        return await self._read("remote_connections", {}, limit)

    async def _read(
        self, resource: str, variables: dict[str, Any], limit: int
    ) -> dict[str, Any]:
        account = self._identity()
        language = self._client.language_code
        limit = _limit(limit)
        key = json.dumps(
            [account, language, resource, variables, limit],
            sort_keys=True,
        )
        cached = self._cache.get(key)
        if cached is not None and cached[0] > self._clock():
            self._verify_language(language)
            self._cache.move_to_end(key)
            return self._decorate(deepcopy(cached[2]))
        if key not in self._inflight:
            if len(self._inflight) >= 16:
                msg = "At most 16 distinct insight reads may run concurrently"
                raise InsightLimitError(msg)
            task = asyncio.create_task(
                self._load(_READS[resource], variables, limit, account, language, key)
            )
            self._inflight[key] = task
            task.add_done_callback(lambda done: self._finished(key, done))
        result = await asyncio.shield(self._inflight[key])
        if self._identity() != account:
            msg = "Account changed during the insight read"
            raise InsightAccountError(msg)
        self._verify_language(language)
        return self._decorate(deepcopy(result))

    def _verify_language(self, language: str) -> None:
        if self._client.language_code != language:
            msg = "Language changed during the insight read; request it again"
            raise InsightLanguageError(msg)

    def _finished(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled():
            task.exception()  # Retrieve failures if every waiter was cancelled.

    async def _load(
        self,
        read: _Read,
        variables: dict[str, Any],
        limit: int,
        account: str,
        language: str,
        key: str,
    ) -> dict[str, Any]:
        if self._identity() != account:
            msg = "Account changed before the insight read"
            raise InsightAccountError(msg)
        self._verify_language(language)
        response = await self._client._make_request(
            query=read.query, variables=variables
        )
        if self._identity() != account:
            msg = "Account changed during the insight read"
            raise InsightAccountError(msg)
        self._verify_language(language)
        if not isinstance(response, dict):
            raise UnexpectedResponseError
        me = response.get("me")
        returned = me.get("user") if isinstance(me, dict) else None
        if not isinstance(returned, dict) or returned.get("id") != account:
            msg = "Insight response does not identify this account"
            raise InsightAccountError(msg)
        value: object = response
        for part in read.path:
            value = value.get(part) if isinstance(value, dict) else None
        self._verify_resource(read.resource, value, variables, account)
        result = self._decorate(self._result(read, value, limit, account))
        size = len(
            json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        if size > min(500_000, self._byte_capacity):
            msg = "Insight result exceeds its declared byte budget"
            raise InsightLimitError(msg)
        self._cache[key] = (self._clock() + read.ttl, size, result)
        while (
            len(self._cache) > self._capacity
            or sum(entry[1] for entry in self._cache.values()) > self._byte_capacity
        ):
            self._cache.popitem(last=False)
        return result

    def _verify_resource(
        self, resource: str, value: object, variables: dict[str, Any], account: str
    ) -> None:
        if value is None:
            return
        if resource == "species" and (
            not isinstance(value, dict) or value.get("id") != variables["speciesId"]
        ):
            raise UnexpectedResponseError
        if resource == "feeder_report":
            if not isinstance(value, dict) or value.get("userId") != account:
                msg = "Report does not identify this account"
                raise InsightAccountError(msg)
            if "feederId" in variables and (
                not isinstance(value.get("feederIds"), list)
                or not all(isinstance(item, str) for item in value["feederIds"])
                or set(value["feederIds"]) - {variables["feederId"]}
            ):
                msg = "Report includes a different feeder"
                raise InsightAccountError(msg)

    def _result(
        self, read: _Read, value: object, limit: int, account: str
    ) -> dict[str, Any]:
        projection = _Projection(limit, min(500_000, self._byte_capacity))
        status = "available" if value is not None else "missing"
        variant = None
        if read.union and isinstance(value, dict):
            raw_variant = value.get("__typename")
            variant = (
                raw_variant
                if isinstance(raw_variant, str) and len(raw_variant) <= 128
                else None
            )
            prefix = (
                "WeeklyRecapForUser"
                if read.resource == "weekly_recap"
                else "YearInReviewForUser"
            )
            if variant in _UNAVAILABLE and variant.startswith(prefix):
                status, value = "unavailable", None
            elif variant == prefix + "Success":
                value = value.get("data")
                status = "available" if isinstance(value, dict) else "missing"
            else:
                status, value = "unknown", None
        data = projection.project(value, read.shape)
        if data is None and status == "available":
            status = "missing"
        return {
            "schema_version": 1,
            "account_id": account,
            "resource": read.resource,
            "status": status,
            "variant": variant,
            "data": data,
            "complete": status == "available"
            and not (projection.truncated or projection.missing or projection.unknown),
            "limit": limit,
            "pagination": {"supported": False, "next_cursor": None},
            "truncated_fields": projection.truncated,
            "missing_fields": projection.missing,
            "unknown_variant_fields": projection.unknown,
            "fetched_at": self._now().isoformat(),
            "cache_ttl_seconds": read.ttl,
        }

    def _decorate(self, result: dict[str, Any]) -> dict[str, Any]:
        """Compute expiry when consumed so caching cannot extend remote access."""
        if result["resource"] == "remote_connections" and isinstance(
            result["data"], list
        ):
            for connection in result["data"]:
                if not isinstance(connection, dict):
                    continue
                connection["entity_scope"] = "remote_connection"
                expires = connection.get("expiresAt")
                try:
                    parsed = (
                        datetime.fromisoformat(expires.replace("Z", "+00:00"))
                        if isinstance(expires, str)
                        else None
                    )
                    connection["expired"] = (
                        parsed <= self._now()
                        if parsed is not None and parsed.utcoffset() is not None
                        else None
                    )
                except ValueError:
                    connection["expired"] = None
        if result["resource"] == "species" and isinstance(result["data"], dict):
            result["data"]["taxonomic_level"] = {
                "SpeciesBird": "species",
                "SpeciesBirdGenus": "genus",
                "SpeciesBirdFamily": "family",
                "SpeciesBirdOrder": "order",
            }.get(result["data"].get("__typename") or "")
        return result
