"""Capture sanitized account fixtures without changing postcards by default.

Credentials come from BB_EMAIL/BB_PASSWORD (or a local .env). Authentication
may exchange tokens; the default data requests only read account information.
Identification changes server inference state and requires
``--identify-postcard-id ID``. Collection requires the separate
``--collect-postcard-id ID`` flag and does not implicitly identify or share.
No postcard is automatically selected from the feed.

Only allowlisted, sanitized data is written by default. Use ``--raw-output``
only for a deliberately private raw capture; never commit that file. Review
sanitized captures before adding them as fixtures. Unknown fields are omitted,
so a new response field requires an explicit sanitizer decision.
"""

import argparse
import asyncio
from collections.abc import Awaitable, Mapping
from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Any
import uuid

from dotenv import load_dotenv

from birdbuddy import queries
from birdbuddy.client import BirdBuddy

_ROOT = Path(__file__).resolve().parent.parent
_SANITIZED = _ROOT / "birdbuddy_payload.sanitized.json"
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_REDACTED = "REDACTED"

# These fields preserve response structure. Unknown keys are omitted, including
# server error messages and arbitrary keys that might themselves identify users.
_CONTAINER_KEYS = {
    "me",
    "feeders",
    "collections",
    "new_postcards",
    "reanalyze",
    "postcard_collect",
    "feed",
    "edges",
    "node",
    "pageInfo",
    "user",
    "settings",
    "medias",
    "media",
    "videoMedia",
    "species",
    "sightingReport",
    "sightingReportPreview",
    "sightings",
    "suggestions",
    "battery",
    "signal",
    "food",
    "temperature",
    "members",
    "invitations",
    "coverCollectionMedia",
    "previewMedia",
    "updatedFeedItem",
    "collectedPostcard",
    "postcardCollectedDetails",
    "inferenceExternalPostcardReanalyze",
    "postcardCollect",
    "mediaSpeciesAssignedName",
}
_ID_KEYS = {
    "id",
    "feederId",
    "feedItemId",
    "postcardId",
    "mediaId",
    "speciesId",
    "cursor",
    "startCursor",
    "endCursor",
}
_TOKEN_KEYS = {"accessToken", "refreshToken", "reportToken", "token"}
_EMAIL_KEYS = {"email", "memberEmail"}
_NAME_KEYS = {"name", "feederName", "memberName", "ownerName", "sharedByName"}
_URL_KEYS = {
    "avatarUrl",
    "contentUrl",
    "thumbnailUrl",
    "iconUrl",
    "mediaUrl",
    "mapUrl",
}
_DATE_KEYS = {
    "createdAt",
    "visitLastTime",
    "presenceUpdatedAt",
    "accessDate",
    "expiresAt",
}
_ENUM_KEYS = {
    "state",
    "powerProfile",
    "frequency",
    "housingType",
    "version",
    "origin",
    "signInType",
    "kind",
    "failedReason",
    "inferenceConfidenceLevel",
    "inferenceExecutionMode",
    "inferenceType",
    "reanalyzeAvailability",
    "mediaSpeciesNameIdentificationConfidenceLevel",
}
_NUMBER_KEYS = {
    "percentage",
    "value",
    "progress",
    "visitsAllTime",
    "confidence",
    "likes",
    "numberOfLikes",
    "mediaImageCount",
    "invitationsAvailable",
}
_BOOL_KEYS = {
    "charging",
    "offGrid",
    "audioEnabled",
    "confirmed",
    "hasNextPage",
    "hasPreviousPage",
    "markedAsNew",
    "hasNewSpecies",
    "hasMysteryVisitor",
    "isFirstCollectedPostcard",
    "liked",
    "isShared",
    "owning",
    "success",
    "isUnofficialName",
    "haveCollections",
}
_SPECIES_TYPES = {
    "Species",
    "SpeciesBird",
    "SpeciesBirdFamily",
    "SpeciesBirdGenus",
    "SpeciesBirdOrder",
}
_ERROR_TYPES = {
    "AuthenticationFailedError",
    "AuthTokenExpiredError",
    "CompositeException",
    "GraphqlError",
    "NoResponseError",
    "UnexpectedResponseError",
}
_ERROR_CODES = {
    "AUTH_TOKEN_EXPIRED_ERROR",
    "INTERNAL_SERVER_ERROR",
    "PAYMENTS_SUBSCRIPTION_IS_NOT_ACTIVE",
    "FEEDER_FIRMWARE_UPGRADE_ALREADY_IN_PROGRESS",
}
_FIRMWARE_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?")


@lru_cache(maxsize=1)
def _schema_vocabulary() -> tuple[set[str], set[str]]:
    """Load known type/enum strings; a missing schema fails closed."""
    schema = json.loads((_ROOT / "tests/fixtures/schema.json").read_text())
    types = schema["__schema"]["types"]
    return (
        {item["name"] for item in types},
        {value["name"] for item in types for value in item.get("enumValues") or []},
    )


def _identifier(value: str) -> str:
    """Replace an opaque identifier with a stable, non-usable fixture UUID."""
    return str(uuid.uuid5(_NAMESPACE, value))


def _safe_scalar(value: Any, key: str, species: bool) -> Any:
    """Preserve only explicitly approved scalar categories."""
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        return _REDACTED
    types, enum_values = _schema_vocabulary()
    if key in _ID_KEYS and isinstance(value, (str, int)):
        return _identifier(str(value))
    if key in _TOKEN_KEYS:
        return "REDACTED_TOKEN"
    if key in _EMAIL_KEYS:
        return "owner@example.invalid"
    if key in _URL_KEYS:
        return "https://example.invalid/asset"
    if key in _NAME_KEYS:
        return value if key == "name" and species else "Test Bird Buddy"
    if key == "serialNumber":
        return "SN-TEST-0001"
    if key in _DATE_KEYS:
        return "2026-01-01T00:00:00.000Z"
    if key == "__typename":
        return value if isinstance(value, str) and value in types else "UnknownType"
    if key in _ENUM_KEYS:
        return value if isinstance(value, str) and value in enum_values else _REDACTED
    if key in {"firmwareVersion", "availableFirmwareVersion"}:
        return (
            value
            if isinstance(value, str) and _FIRMWARE_RE.fullmatch(value)
            else _REDACTED
        )
    if (
        key in _NUMBER_KEYS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return value
    if key in _BOOL_KEYS and isinstance(value, bool):
        return value
    if key == "error_type":
        return value if value in _ERROR_TYPES else "UnhandledError"
    if key == "error_code":
        return value if value in _ERROR_CODES else "UNRECOGNIZED_ERROR"
    return _REDACTED


_SCALAR_KEYS = (
    _ID_KEYS
    | _TOKEN_KEYS
    | _EMAIL_KEYS
    | _NAME_KEYS
    | _URL_KEYS
    | _DATE_KEYS
    | _ENUM_KEYS
    | _NUMBER_KEYS
    | _BOOL_KEYS
    | {
        "__typename",
        "serialNumber",
        "firmwareVersion",
        "availableFirmwareVersion",
        "error_type",
        "error_code",
    }
)


def _sanitize(obj: Any, key: str | None = None) -> Any:
    """Allowlist structured data; omit locations, raw errors and unknown keys.

    Names are public only on an immediate Species object, never merely inside
    a species descendant. Auth/report tokens are replaced wholesale; neither
    token headers nor embedded JSON is copied into a public fixture.
    """
    if isinstance(obj, Mapping):
        if key in {"feeders", "reanalyze"}:
            return {_identifier(str(k)): _sanitize(v) for k, v in obj.items()}
        typename = obj.get("__typename")
        species = (
            isinstance(typename, str) and typename in _SPECIES_TYPES
        ) or key == "species"
        result: dict[str, Any] = {}
        for child_key, value in obj.items():
            if child_key in _CONTAINER_KEYS:
                result[child_key] = _sanitize(value, child_key)
            elif child_key in _SCALAR_KEYS:
                result[child_key] = _safe_scalar(value, child_key, species)
        return result
    if isinstance(obj, list):
        return [_sanitize(value, key) for value in obj]
    return None


async def _capture(label: str, coro: Awaitable[Any], out: dict[str, Any]) -> Any:
    """Store a response or a failure category without raw exception text."""
    try:
        result = await coro
    except Exception as err:  # noqa: BLE001
        error_type = type(err).__name__
        out[label] = {
            "error_type": error_type if error_type in _ERROR_TYPES else "UnhandledError"
        }
        return None
    out[label] = result
    return result


async def _collect(
    bb: BirdBuddy,
    *,
    identify_postcard_id: str | None = None,
    collect_postcard_id: str | None = None,
) -> dict[str, Any]:
    """Capture reads; each optional postcard mutation requires its own ID."""
    for postcard_id in (identify_postcard_id, collect_postcard_id):
        if postcard_id is not None:
            _postcard_id(postcard_id)
    out: dict[str, Any] = {}
    profile = await _capture("me", bb._make_request(query=queries.me.ME), out)
    if isinstance(profile, dict) and isinstance(profile.get("me"), dict):
        out["me"] = profile["me"]
    collections = await _capture(
        "collections", bb._make_request(query=queries.me.COLLECTIONS), out
    )
    if isinstance(collections, dict) and isinstance(collections.get("me"), dict):
        out["collections"] = collections["me"].get("collections", [])
    postcards = await _capture("new_postcards", bb.new_postcards(), out)
    if postcards is not None:
        out["new_postcards"] = [postcard.data for postcard in postcards]

    if identify_postcard_id is not None:
        await _capture(
            "reanalyze",
            bb._make_request(
                query=queries.birds.POSTCARD_REANALYZE,
                variables={"feedItemId": identify_postcard_id},
            ),
            out,
        )
        out["reanalyze"] = {identify_postcard_id: out["reanalyze"]}
    if collect_postcard_id is not None:
        # Send only the explicitly requested collect mutation. Calling the
        # client's collect_postcard helper would implicitly identify first.
        await _capture(
            "postcard_collect",
            bb._make_request(
                query=queries.birds.POSTCARD_COLLECT,
                variables={
                    "feedItemId": collect_postcard_id,
                    "postcardCollectInput": {"share": False},
                },
            ),
            out,
        )
    return out


def _postcard_id(value: str) -> str:
    """Require an exact non-empty identifier instead of choosing a card."""
    if not value or any(char.isspace() for char in value):
        message = "Provide an exact, non-empty feed-item ID"
        raise argparse.ArgumentTypeError(message)
    return value


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse independent, explicit mutation and raw-output opt-ins."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identify-postcard-id", type=_postcard_id)
    parser.add_argument("--collect-postcard-id", type=_postcard_id)
    parser.add_argument("--output", type=Path, default=_SANITIZED)
    parser.add_argument("--raw-output", type=Path)
    args = parser.parse_args(argv)
    if args.raw_output and args.output.resolve() == args.raw_output.resolve():
        parser.error("Raw and sanitized outputs must use different paths")
    if args.raw_output and os.path.lexists(args.raw_output):
        parser.error("Raw output already exists; choose a new private output path")
    return args


def main(argv: list[str] | None = None) -> None:
    """Write allowlisted fixtures; write private raw data only when requested."""
    args = _arguments(argv)
    load_dotenv()
    bb = BirdBuddy(os.environ["BB_EMAIL"], os.environ["BB_PASSWORD"])
    data = asyncio.run(
        _collect(
            bb,
            identify_postcard_id=args.identify_postcard_id,
            collect_postcard_id=args.collect_postcard_id,
        )
    )
    sanitized = _sanitize(data)
    args.output.write_text(json.dumps(sanitized, indent=2))
    if args.raw_output:
        # Create privately and refuse to overwrite an existing file or symlink.
        descriptor = os.open(
            args.raw_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as raw:
            json.dump(data, raw, indent=2, default=str)
        print(f"Wrote {args.raw_output} (private account data; do not commit)")
    print(f"Wrote {args.output} (allowlisted; review before committing)")


if __name__ == "__main__":
    main()
