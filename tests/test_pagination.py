"""Feed intake advances only after durable consumer acknowledgement."""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from birdbuddy import feed as feed_models
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import NoResponseError
from birdbuddy.feed import FeedCheckpoint, FeedNodeType, PaginationError


def _timestamp(minute: int) -> str:
    return f"2026-11-13T10:{minute:02d}:00+00:00"


def _node(node_id: str, minute: int | None = None, **fields: object) -> dict:
    return {
        "id": node_id,
        "__typename": "FeedItemNewPostcard",
        "createdAt": _timestamp(minute) if minute is not None else None,
        **fields,
    }


def _page(*nodes: dict, cursor: str | None = None, more: bool = False) -> dict:
    return {
        "me": {
            "feed": {
                "edges": [{"node": node} for node in nodes],
                "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            }
        }
    }


def _client(*responses: object) -> tuple[BirdBuddy, AsyncMock]:
    client = BirdBuddy(refresh_token="fake-refresh", access_token="fake-access")
    request = AsyncMock(side_effect=responses)
    client._make_request = request
    return client, request


def _accepted(minute: int = 10) -> dict:
    return {
        "version": 1,
        "watermark": _timestamp(minute),
        "seen": {"known": _timestamp(minute)},
    }


@pytest.mark.asyncio
async def test_later_page_failure_retries_entire_unaccepted_intake():
    """A first page never becomes an accepted or pending partial result."""
    first = _page(_node("new", 12), _node("middle", 7), cursor="next", more=True)
    second = _page(_node("older-new", 6), _node("out-of-window", 4))
    client, request = _client(first, NoResponseError(), first, second)
    client.import_feed_checkpoint(_accepted())
    with pytest.raises(NoResponseError):
        await client.retrieve_feed_batch()
    assert client.export_feed_checkpoint() == _accepted()
    batch = await client.retrieve_feed_batch()
    assert {node.node_id for node in batch.nodes} == {"new", "middle", "older-new"}
    assert request.await_count == 4
    assert "after" not in request.await_args_list[2].kwargs["variables"]
    assert client.export_feed_checkpoint() == _accepted()


@pytest.mark.asyncio
async def test_equal_timestamp_boundary_pages_and_overlap_deduplicate_ids():
    """Equal cutoff timestamps on later pages and bounded late arrivals survive."""
    client, request = _client(
        _page(
            _node("known", 10),
            _node("late", 9),
            _node("boundary-a", 5),
            cursor="c1",
            more=True,
        ),
        _page(_node("late", 9), _node("boundary-b", 5), _node("too-old", 4)),
    )
    client.import_feed_checkpoint(_accepted())
    batch = await client.retrieve_feed_batch()
    assert [node.node_id for node in batch.nodes] == [
        "late",
        "boundary-a",
        "boundary-b",
    ]
    assert request.await_count == 2
    assert batch.checkpoint.watermark == FeedCheckpoint.from_dict(_accepted()).watermark


@pytest.mark.asyncio
async def test_pending_replay_and_defensive_node_copies():
    """Failed persistence leaves the same batch available, without another read."""
    client, request = _client(_page(_node("new", 12, media={"id": "original"})))
    initial = client.export_feed_checkpoint()
    batch = await client.retrieve_feed_batch()
    batch.nodes[0]["media"]["id"] = "caller-edit"
    replay = await client.retrieve_feed_batch(backfill=True)
    assert replay.batch_id == batch.batch_id
    assert replay.nodes[0]["media"]["id"] == "original"
    assert request.await_count == 1
    assert client.export_feed_checkpoint() == initial
    with pytest.raises(ValueError, match="acknowledgement"):
        client.ack_feed_batch("wrong-batch")
    with pytest.raises(ValueError, match="pending"):
        client.import_feed_checkpoint(initial)
    # This represents a single transaction persisting the queue and checkpoint.
    stored = {
        "queue": [node.data for node in batch.nodes],
        "checkpoint": batch.checkpoint.to_dict(),
    }
    assert client.ack_feed_batch(batch.batch_id) == stored["checkpoint"]
    assert client.export_feed_checkpoint() == stored["checkpoint"]


@pytest.mark.asyncio
async def test_restart_before_persistence_retrieves_work_again():
    """Only an atomically persisted proposed checkpoint can suppress replay."""
    page = _page(_node("new", 12), _node("known", 10))
    client, _ = _client(page)
    client.import_feed_checkpoint(_accepted())
    batch = await client.retrieve_feed_batch()
    restarted, _ = _client(page)
    restarted.import_feed_checkpoint(client.export_feed_checkpoint())
    retry = await restarted.retrieve_feed_batch()
    assert [node.node_id for node in retry.nodes] == ["new"]
    persisted, _ = _client(page)
    persisted.import_feed_checkpoint(batch.checkpoint.to_dict())
    assert (await persisted.retrieve_feed_batch()).nodes == ()


@pytest.mark.asyncio
async def test_initial_window_is_explicit_and_backfill_reads_history():
    """Latest 20 is deliberate bootstrap scope; backfill traverses every page."""
    first = _page(_node("new", 12), cursor="c1", more=True)
    second = _page(_node("historical", 1))
    initial, initial_request = _client(first)
    assert len((await initial.retrieve_feed_batch()).nodes) == 1
    assert initial_request.await_args.kwargs["variables"] == {"first": 20}
    backfill, backfill_request = _client(first, second)
    assert len((await backfill.retrieve_feed_batch(backfill=True)).nodes) == 2
    assert backfill_request.await_count == 2


@pytest.mark.asyncio
async def test_unknown_and_null_timestamps_keep_ids_and_do_not_stop_pages():
    """New union variants and malformed timestamps are undated, never discarded."""
    client, request = _client(
        _page(_node("unknown", __typename="FutureFeedItem"), cursor="c1", more=True),
        _page(_node("malformed", createdAt="invalid"), _node("new", 11)),
        _page(
            _node("unknown", __typename="FutureFeedItem"),
            _node("new", 11),
            _node("malformed", createdAt="invalid"),
        ),
    )
    client.import_feed_checkpoint(_accepted())
    batch = await client.retrieve_feed_batch()
    assert {node.node_id for node in batch.nodes} == {"unknown", "malformed", "new"}
    assert batch.nodes[0].node_type is FeedNodeType.Unknown
    assert dict(batch.checkpoint.seen)["malformed"] is None
    assert request.await_count == 2
    client.ack_feed_batch(batch.batch_id)
    assert (await client.retrieve_feed_batch()).nodes == ()


@pytest.mark.parametrize("cursor", [None, "", 42])
@pytest.mark.asyncio
async def test_missing_or_invalid_cursor_is_incomplete(cursor):
    """Malformed completeness metadata never produces a committed partial page."""
    client, request = _client(_page(_node("new", 12), cursor=cursor, more=True))
    with pytest.raises(PaginationError, match="cursor"):
        await client.retrieve_feed_batch(backfill=True)
    assert client.export_feed_checkpoint() == FeedCheckpoint().to_dict()
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_repeated_cursor_and_page_budget_are_explicit_failures():
    """Both server loops and a bounded incomplete history require retry/recovery."""
    first = _page(_node("one", 12), cursor="c1", more=True)
    client, request = _client(first, _page(_node("two", 11), cursor="c1", more=True))
    with pytest.raises(PaginationError, match="repeated"):
        await client.retrieve_feed_batch(backfill=True)
    assert request.await_count == 2
    bounded, bounded_request = _client(first)
    with pytest.raises(PaginationError, match="page limit"):
        await bounded.retrieve_feed_batch(backfill=True, max_pages=1)
    assert bounded_request.await_count == 1
    assert bounded.export_feed_checkpoint() == FeedCheckpoint().to_dict()


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"version": 2, "watermark": None, "seen": {}},
        {"version": True, "watermark": None, "seen": {}},
        {"version": 1, "watermark": "not-a-date", "seen": {}},
        {"version": 1, "watermark": "2026-11-13T10:00:00", "seen": {}},
        {"version": 1, "watermark": None, "seen": []},
        {"version": 1, "watermark": None, "seen": {"future": _timestamp(1)}},
        {"version": 1, "watermark": _timestamp(1), "seen": {"future": _timestamp(2)}},
        {"version": 1, "watermark": None, "seen": {"": None}},
    ],
)
def test_checkpoint_rejects_malformed_or_future_state(state):
    """Corrupt persistent state cannot silently become a skipping checkpoint."""
    client, request = _client()
    initial = client.export_feed_checkpoint()
    with pytest.raises(ValueError, match=r"[Cc]heckpoint|isoformat"):
        client.import_feed_checkpoint(state)
    assert client.export_feed_checkpoint() == initial
    request.assert_not_called()


@pytest.mark.asyncio
async def test_checkpoint_capacity_fails_without_dropping_ids(monkeypatch):
    """A full undated-ID budget requires explicit recovery, never silent eviction."""
    monkeypatch.setattr(feed_models, "MAX_CHECKPOINT_IDS", 2)
    client, _ = _client(_page(_node("a"), _node("b"), _node("c")))
    with pytest.raises(PaginationError, match="capacity"):
        await client.retrieve_feed_batch()
    assert client.export_feed_checkpoint() == FeedCheckpoint().to_dict()


@pytest.mark.asyncio
async def test_concurrent_retrieval_replays_one_batch():
    """Concurrent pollers cannot independently propose conflicting checkpoints."""
    client, request = _client(_page(_node("new", 12)))
    first, second = await asyncio.gather(
        client.retrieve_feed_batch(), client.retrieve_feed_batch()
    )
    assert first.batch_id == second.batch_id
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_preserves_accepted_state():
    """Cancellation midway through traversal leaves the old checkpoint usable."""
    client, _ = _client(
        _page(_node("one", 12), cursor="c1", more=True), asyncio.CancelledError()
    )
    client.import_feed_checkpoint(_accepted())
    with pytest.raises(asyncio.CancelledError):
        await client.retrieve_feed_batch()
    assert client.export_feed_checkpoint() == _accepted()


@pytest.mark.parametrize(
    "options", [{"last": 5}, {"before": "cursor"}, {"last": 5, "before": "cursor"}]
)
@pytest.mark.asyncio
async def test_backward_pagination_is_not_silently_ignored(options):
    """Unverified reverse pagination fails before any account request."""
    client, request = _client()
    with pytest.raises(ValueError, match="Backward"):
        await client.feed(**options)
    request.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_refresh_does_not_accept_checkpoint_implicitly():
    """The list adapter retains replay safety until the caller explicitly acks."""
    client, request = _client(_page(_node("new", 12)))
    initial = deepcopy(client.export_feed_checkpoint())
    assert [node.node_id for node in await client.refresh_feed()] == ["new"]
    assert [node.node_id for node in await client.refresh_feed()] == ["new"]
    assert client.export_feed_checkpoint() == initial
    assert request.await_count == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"me": {}},
        {"me": {"feed": []}},
        {"me": {"feed": {"edges": [], "pageInfo": {}}}},
        {"me": {"feed": {"edges": [], "pageInfo": {"hasNextPage": "false"}}}},
        {"me": {"feed": {"pageInfo": {"hasNextPage": False}}}},
        _page({"createdAt": _timestamp(12)}),
        _page({"id": "", "createdAt": _timestamp(12)}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_pages_cannot_become_accepted_results(payload):
    """A malformed connection, completion flag or ID fails the entire intake."""
    client, _ = _client(payload)
    with pytest.raises(PaginationError):
        await client.retrieve_feed_batch(backfill=True)
    assert client.export_feed_checkpoint() == FeedCheckpoint().to_dict()


@pytest.mark.parametrize(
    "options",
    [
        {"since": "not-a-timestamp"},
        {"since": "2026-11-13T10:00:00"},
        {"initial_limit": True},
        {"max_pages": 0},
        {"max_pages": True},
    ],
)
@pytest.mark.asyncio
async def test_invalid_scan_options_fail_before_requests(options):
    """Unsupported retrieval bounds never silently become an initial scan."""
    client, request = _client()
    with pytest.raises(ValueError, match=r"since|page"):
        await client.retrieve_feed_batch(**options)
    request.assert_not_called()
