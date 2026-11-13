"""Collection snapshots reconcile safely and retain wrapper attribution."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.media import CollectionMedia, Media, is_media_expired
from birdbuddy.user import BirdBuddyUser

URL = "https://cdn.example.invalid/image.jpg?Expires=99999999999"


def _row(key="c1", *, name="Robin", visits=1, url=URL):
    """Build one complete collection row."""
    return {
        "id": key,
        "__typename": "CollectionBird",
        "species": {"id": "s1", "name": name},
        "visitsAllTime": visits,
        "visitLastTime": "2026-11-13T12:00:00Z",
        "coverCollectionMedia": {
            "id": "wrapper1",
            "feederName": "Backyard",
            "feeder": {"id": "explicit-feeder"},
            "origin": "SHARED",
            "owning": False,
            "media": {
                "id": "m1",
                "__typename": "MediaImage",
                "createdAt": "2026-11-13T12:00:00Z",
                "thumbnailUrl": url,
                "contentUrl": url,
            },
        },
    }


def _response(*rows: dict):
    """Wrap collection rows in the GraphQL result shape."""
    return {"me": {"collections": list(rows)}}


async def test_reconcile_preserves_references_and_prunes(bbclient):
    """Replace metadata and prune removed or empty rows after a full read."""
    bbclient._make_request = AsyncMock(
        side_effect=[
            _response(_row(), _row("deleted"), _row("emptied")),
            _response(_row(name="Renamed"), _row("emptied", visits=0)),
        ]
    )
    first = await bbclient.refresh_collections()
    original = first["c1"]
    original["obsolete"] = "removed"
    second = await bbclient.refresh_collections(force=True)
    assert first is second
    assert list(second) == ["c1"]
    assert second["c1"] is original
    assert original.bird_name == "Renamed"
    assert "obsolete" not in original


@pytest.mark.parametrize(
    "bad",
    [None, {}, {"me": None}, {"me": {"collections": None}}, _response({"id": "bad"})],
)
async def test_bad_snapshot_does_not_evict(bbclient, bad):
    """Missing or malformed snapshots cannot erase known collections."""
    bbclient._make_request = AsyncMock(side_effect=[_response(_row()), bad])
    cached = await bbclient.refresh_collections()
    before = deepcopy(cached)
    with pytest.raises(UnexpectedResponseError):
        await bbclient.refresh_collections(force=True)
    assert cached == before
    assert bbclient.collections_loaded


async def test_remote_failure_preserves_snapshot(bbclient):
    """Transport failures preserve the last successful data without marking fresh."""
    bbclient._make_request = AsyncMock(side_effect=[_response(_row()), TimeoutError()])
    cached = await bbclient.refresh_collections()
    updated_at = bbclient._collections_updated_at
    with pytest.raises(TimeoutError):
        await bbclient.refresh_collections(force=True)
    assert bbclient.collections is cached
    assert bbclient._collections_updated_at == updated_at


async def test_empty_is_loaded_until_ttl(bbclient, monkeypatch):
    """An empty account snapshot is cached, then revalidated after the TTL."""
    clock = [10.0]
    monkeypatch.setattr("birdbuddy.client.monotonic", lambda: clock[0])
    bbclient._make_request = AsyncMock(side_effect=[_response(), _response(_row())])
    assert not bbclient.collections_loaded
    assert await bbclient.refresh_collections() == {}
    assert bbclient.collections_loaded
    assert not bbclient.collections_stale
    assert await bbclient.refresh_collections() == {}
    bbclient._make_request.assert_awaited_once()
    clock[0] = 311
    assert bbclient.collections_stale
    assert "c1" in await bbclient.refresh_collections()
    assert bbclient._make_request.await_count == 2


async def test_expired_cover_marks_stale_without_deleting(bbclient):
    """Expiration changes freshness, not the known collection identity."""
    bbclient._make_request = AsyncMock(
        side_effect=[_response(_row()), _response(_row(name="Refreshed"))]
    )
    cached = await bbclient.refresh_collections()
    item = cached["c1"]
    item["coverCollectionMedia"]["media"]["thumbnailUrl"] = URL + "&Expires=1"
    assert bbclient.collections_stale
    assert bbclient.collections["c1"] is item
    assert (await bbclient.refresh_collections())["c1"] is item
    assert item.bird_name == "Refreshed"


async def test_concurrent_forced_calls_share_snapshot(bbclient):
    """Concurrent callers share one successful refresh, even when forced."""
    started, release = asyncio.Event(), asyncio.Event()

    async def fetch(**_kwargs: object):
        started.set()
        await release.wait()
        return _response(_row())

    bbclient._make_request = AsyncMock(side_effect=fetch)
    first = asyncio.create_task(bbclient.refresh_collections(force=True))
    await started.wait()
    second = asyncio.create_task(bbclient.refresh_collections(force=True))
    await asyncio.sleep(0)
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one is two
    bbclient._make_request.assert_awaited_once()


async def test_account_namespace_never_reuses_another_identity(bbclient):
    """An account change hides the prior identity's cache until a successful read."""
    bbclient._me = BirdBuddyUser({"id": "account1"})
    bbclient._make_request = AsyncMock(
        side_effect=[_response(_row()), _response(_row(name="Other account"))]
    )
    original = (await bbclient.refresh_collections())["c1"]
    other = BirdBuddy(refresh_token="other")
    assert other.collections == {}
    bbclient._me = BirdBuddyUser({"id": "account2"})
    assert bbclient.collections == {}
    assert not bbclient.collections_loaded
    changed = (await bbclient.refresh_collections())["c1"]
    assert changed is not original
    assert changed.bird_name == "Other account"


async def test_identity_change_during_fetch_does_not_publish(bbclient):
    """A response for the previous identity cannot populate the new identity."""
    bbclient._me = BirdBuddyUser({"id": "account1"})

    async def fetch(**_kwargs: object):
        bbclient._me = BirdBuddyUser({"id": "account2"})
        return _response(_row())

    bbclient._make_request = AsyncMock(side_effect=fetch)
    with pytest.raises(UnexpectedResponseError):
        await bbclient.refresh_collections()
    assert not bbclient.collections_loaded
    assert bbclient.collections == {}


async def test_collection_media_keeps_wrapper_identity(bbclient):
    """Nested media stays Media-compatible while explicit attribution survives."""
    wrapper = _row()["coverCollectionMedia"]
    bbclient._make_request = AsyncMock(
        return_value={
            "collection": {
                "media": {
                    "edges": [{"node": wrapper}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    )
    result = await bbclient.collection("c1")
    item = result["m1"]
    assert isinstance(item, CollectionMedia)
    assert isinstance(item, Media)
    assert item.collection_id == "c1"
    assert item.collection_media_id == "wrapper1"
    assert item.feeder_id == "explicit-feeder"
    assert item.feeder_name == "Backyard"
    assert item.collection_metadata["origin"] == "SHARED"
    assert item.collection_metadata["owning"] is False
    assert item.data == wrapper["media"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, None),
        ("", None),
        ("https://x/image", None),
        ("https://x/image?Expires=invalid", None),
        ("https://x/image?Expires=0", True),
        ("https://x/image?X-Amz-Date=20200101T000000Z&X-Amz-Expires=60", True),
        ("https://x/image?X-Amz-Date=20990101T000000Z&X-Amz-Expires=60", False),
        ("https://x/image?X-Amz-Date=bad&X-Amz-Expires=60", None),
        ("https://x/image?X-Amz-Date=20990101T000000Z&X-Amz-Expires=bad", None),
    ],
)
def test_signed_url_metadata_is_optional(url, expected):
    """Unknown expiry is safe, but explicit past expiry is never fresh."""
    assert is_media_expired(url) is expected


async def test_concurrent_failed_calls_share_failure(bbclient):
    """A failed request does not fan out into one request per waiting browser."""
    started, release = asyncio.Event(), asyncio.Event()

    async def fail(**_kwargs: object):
        started.set()
        await release.wait()
        raise TimeoutError

    bbclient._make_request = AsyncMock(side_effect=fail)
    first = asyncio.create_task(bbclient.refresh_collections())
    await started.wait()
    second = asyncio.create_task(bbclient.refresh_collections())
    await asyncio.sleep(0)
    release.set()
    result = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(error, TimeoutError) for error in result)
    bbclient._make_request.assert_awaited_once()
    assert not bbclient.collections_loaded


@pytest.mark.parametrize(
    "change", [{"visitsAllTime": None}, {"coverCollectionMedia": None}]
)
async def test_partial_row_does_not_remove_other_collections(bbclient, change):
    """Even a partially valid response is rejected before cache reconciliation."""
    row = _row()
    row.update(change)
    bbclient._make_request = AsyncMock(
        side_effect=[_response(_row(), _row("keep")), _response(row)]
    )
    original = await bbclient.refresh_collections()
    with pytest.raises(UnexpectedResponseError):
        await bbclient.refresh_collections(force=True)
    assert set(original) == {"c1", "keep"}
