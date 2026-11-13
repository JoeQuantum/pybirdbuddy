"""Capture helpers require deliberate mutations and redact private payloads."""

import argparse
import json
from pathlib import Path
import stat
from unittest.mock import AsyncMock, Mock

from graphql import OperationType, parse
from graphql.language.ast import OperationDefinitionNode
import pytest

from birdbuddy import queries
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import GraphqlError
from scripts import dump_payloads as capture


def _client() -> tuple[BirdBuddy, AsyncMock]:
    """Create a real client whose entire network boundary is mocked."""
    bb = BirdBuddy(refresh_token="fake-refresh", access_token="fake-access")
    request = AsyncMock(
        side_effect=[
            {"data": {"me": {"user": {"id": "account"}, "feeders": []}}},
            {"data": {"me": {"collections": []}}},
            {
                "data": {
                    "me": {
                        "feed": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "first-card",
                                        "__typename": "FeedItemNewPostcard",
                                    }
                                }
                            ],
                            "pageInfo": {"hasNextPage": False},
                        }
                    }
                }
            },
            {"data": {"mutationResult": None}},
            {"data": {"mutationResult": None}},
        ]
    )
    bb.graphql.execute_async = request
    return bb, request


@pytest.mark.asyncio
async def test_default_capture_never_identifies_or_collects(monkeypatch):
    """The actual default request documents are reads, despite legacy env vars."""
    monkeypatch.setenv("BB_COLLECT_POSTCARD_ID", "hidden-target")
    monkeypatch.setenv("BB_IDENTIFY_POSTCARD_ID", "hidden-target")
    bb, request = _client()
    result = await capture._collect(bb)
    assert request.await_count == 3
    for call in request.await_args_list:
        operations = [
            item
            for item in parse(call.kwargs["query"]).definitions
            if isinstance(item, OperationDefinitionNode)
        ]
        assert [item.operation for item in operations] == [OperationType.QUERY]
    assert "reanalyze" not in result
    assert "postcard_collect" not in result
    args = capture._arguments([])
    assert args.identify_postcard_id is None
    assert args.collect_postcard_id is None
    assert args.raw_output is None


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (
            {"identify_postcard_id": "chosen-identify"},
            [(queries.birds.POSTCARD_REANALYZE, {"feedItemId": "chosen-identify"})],
        ),
        (
            {"collect_postcard_id": "chosen-collect"},
            [
                (
                    queries.birds.POSTCARD_COLLECT,
                    {
                        "feedItemId": "chosen-collect",
                        "postcardCollectInput": {"share": False},
                    },
                )
            ],
        ),
        (
            {
                "identify_postcard_id": "chosen-identify",
                "collect_postcard_id": "chosen-collect",
            },
            [
                (queries.birds.POSTCARD_REANALYZE, {"feedItemId": "chosen-identify"}),
                (
                    queries.birds.POSTCARD_COLLECT,
                    {
                        "feedItemId": "chosen-collect",
                        "postcardCollectInput": {"share": False},
                    },
                ),
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_mutations_have_separate_exact_targets(options, expected):
    """Neither opt-in implies the other or chooses the first available card."""
    bb, request = _client()
    await capture._collect(bb, **options)
    mutation_calls = request.await_args_list[3:]
    assert [
        (c.kwargs["query"], c.kwargs["variables"]) for c in mutation_calls
    ] == expected


@pytest.mark.parametrize("value", ["", " ", "ambiguous target"])
@pytest.mark.asyncio
async def test_invalid_mutation_id_fails_before_any_request(value):
    """An invalid target cannot produce an automatic fallback or side effect."""
    bb, request = _client()
    with pytest.raises(argparse.ArgumentTypeError):
        await capture._collect(bb, collect_postcard_id=value)
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_does_not_store_raw_exception_text():
    """Server errors can contain credentials, locations and signed URLs."""
    secret = "private-token https://private.invalid/?Signature=secret user@example.com"
    operation = AsyncMock(side_effect=GraphqlError({"message": secret}))
    out = {}
    assert await capture._capture("me", operation(), out) is None
    assert out == {"me": {"error_type": "GraphqlError"}}
    assert secret not in json.dumps(out)


def test_sanitizer_omits_unknown_private_data_and_nested_names():
    """Only approved fields survive, and species context cannot leak names."""
    private = "PRIVATE_ACCOUNT_VALUE"
    signed = "https://private.invalid/asset?Expires=123&Signature=PRIVATE_SIGNATURE"
    payload = {
        "me": {
            "user": {
                "id": private,
                "name": private,
                "email": private,
                "avatarUrl": signed,
            },
            "location": {"latitude": 40.123, "longitude": -74.987, "address": private},
            "unexpected": {private: signed},
            "errors": [{"message": private, "extensions": {"accessToken": private}}],
        },
        "new_postcards": [
            {
                "id": private,
                "medias": [
                    {"id": private, "contentUrl": signed, "thumbnailUrl": signed}
                ],
                "sightingReport": {"reportToken": f"{private}.{private}.{private}"},
                "species": {
                    "__typename": "SpeciesBird",
                    "name": "Blue Jay",
                    "members": [{"name": private}],
                },
                "mediaSpeciesAssignedName": {
                    "__typename": "MediaSpeciesName",
                    "name": private,
                },
            }
        ],
        private: private,
    }
    sanitized = capture._sanitize(payload)
    encoded = json.dumps(sanitized)
    assert private not in encoded
    assert "PRIVATE_SIGNATURE" not in encoded
    assert "40.123" not in encoded
    assert "74.987" not in encoded
    assert "location" not in sanitized["me"]
    assert "errors" not in sanitized["me"]
    assert "unexpected" not in sanitized["me"]
    card = sanitized["new_postcards"][0]
    assert card["species"]["name"] == "Blue Jay"
    assert card["species"]["members"][0]["name"] == "Test Bird Buddy"
    assert card["mediaSpeciesAssignedName"]["name"] == "Test Bird Buddy"
    assert card["sightingReport"]["reportToken"] == "REDACTED_TOKEN"
    assert sanitized["me"]["user"]["id"] == card["id"] == card["medias"][0]["id"]


def test_unexpected_nested_scalar_is_redacted():
    """A malformed scalar cannot bypass the allowlist with nested private data."""
    sanitized = capture._sanitize({"species": {"name": {"secret": "PRIVATE"}}})
    assert sanitized == {"species": {"name": "REDACTED"}}


def _mock_main(monkeypatch, tmp_path: Path) -> Mock:
    """Prevent credential loading and every network call made by the CLI."""
    monkeypatch.setenv("BB_EMAIL", "fake@example.invalid")
    monkeypatch.setenv("BB_PASSWORD", "fake-password")
    monkeypatch.setattr(capture, "load_dotenv", lambda: None)
    monkeypatch.setattr(capture, "_SANITIZED", tmp_path / "capture.sanitized.json")
    client = Mock()
    monkeypatch.setattr(capture, "BirdBuddy", client)
    monkeypatch.setattr(
        capture,
        "_collect",
        AsyncMock(return_value={"me": {"user": {"name": "PRIVATE"}}}),
    )
    return client


def test_main_writes_only_sanitized_output_by_default(monkeypatch, tmp_path):
    """No raw account dump is created without the independent output opt-in."""
    _mock_main(monkeypatch, tmp_path)
    capture.main([])
    assert [p.name for p in tmp_path.iterdir()] == ["capture.sanitized.json"]
    assert "PRIVATE" not in (tmp_path / "capture.sanitized.json").read_text()


def test_raw_output_is_explicit_private_and_never_overwritten(monkeypatch, tmp_path):
    """Raw output is mode 0600 and an existing target fails before account use."""
    client = _mock_main(monkeypatch, tmp_path)
    raw = tmp_path / "private.dump.json"
    capture.main(["--raw-output", str(raw)])
    assert "PRIVATE" in raw.read_text()
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    client.reset_mock()
    with pytest.raises(SystemExit):
        capture.main(["--raw-output", str(raw)])
    client.assert_not_called()


def test_sanitizer_failure_does_not_write_any_capture(monkeypatch, tmp_path):
    """A broken allowlist/schema cannot fall back to publishing raw data."""
    _mock_main(monkeypatch, tmp_path)
    monkeypatch.setattr(
        capture, "_schema_vocabulary", Mock(side_effect=ValueError("bad schema"))
    )
    with pytest.raises(ValueError, match="bad schema"):
        capture.main(["--raw-output", str(tmp_path / "private.dump.json")])
    assert not list(tmp_path.iterdir())
