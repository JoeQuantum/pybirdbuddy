"""Firmware initiation is explicit, preflighted and never blindly replayed."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from birdbuddy import queries
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import GraphqlError, NoFirmwareUpdateAvailableError
from birdbuddy.feeder import (
    Feeder,
    FeederControlUnavailableError,
    FeederUpdateStatus,
    FirmwareUpdateBusyError,
    FirmwareUpdatePreconditionError,
    FirmwareUpdateUnknownStateError,
)


def _owner(*, available=True, ready=True):
    availability = {
        "__typename": "FeederFirmwareUpdateAvailable"
        if available
        else "FeederFirmwareUpdateNotAvailable",
        "isAvailable": available,
    }
    if available:
        availability["preconditionsFulfillment"] = {
            "__typename": "FeederFirmwareUpdatePreconditionsFulfilled"
            if ready
            else "FeederFirmwareUpdatePreconditionsNotFulfilled",
            "arePreconditionsFulfilled": ready,
            "reason": "BATTERY_LOW" if not ready else None,
        }
    return {
        "id": "feeder",
        "__typename": "FeederForOwner",
        "state": "ONLINE",
        "firmwareVersion": "1.0",
        "availableFirmwareVersion": "2.0" if available else None,
        "firmwareUpdateAvailability": availability,
    }


def _status(kind="Succeeded", progress=None):
    data: dict = {"__typename": f"FeederFirmwareUpdate{kind}Result"}
    if kind != "Failed":
        data["feeder"] = {
            "id": "feeder",
            "firmwareVersion": "1.0",
            "availableFirmwareVersion": "2.0",
        }
    if kind == "Progress":
        data["progress"] = progress
    if kind == "Failed":
        data["failedReason"] = "BATTERY_LOW"
    return data


def _check(status=None):
    return {"feederFirmwareUpdateCheckProgress": status or _status()}


def _client(*responses: object):
    client = BirdBuddy(refresh_token="fake-refresh", access_token="fake-access")
    client._feeders["feeder"] = Feeder(_owner())
    client._make_request = AsyncMock(side_effect=responses)
    return client, client._make_request


@pytest.mark.parametrize("immediate", [False, True])
@pytest.mark.asyncio
async def test_start_semantics_are_explicit(immediate):
    """Immediate and scheduled starts use distinct schema operations."""
    operation = (
        "feederFirmwareUpdateRegularStart" if immediate else "feederFirmwareUpdateStart"
    )
    client, request = _client(
        {"me": {"feeder": _owner()}}, _check(), {operation: _status("Progress", 0)}
    )
    status = await client.update_firmware_start("feeder", immediate=immediate)
    assert status.is_in_progress
    assert request.await_args_list[0].kwargs["query"] == queries.feeder.FIRMWARE_STATE
    expected = (
        queries.feeder.UPDATE_FIRMWARE_REGULAR
        if immediate
        else queries.feeder.UPDATE_FIRMWARE
    )
    assert request.await_args_list[2].kwargs["query"] == expected


@pytest.mark.asyncio
async def test_default_keeps_scheduling_semantics():
    """The legacy default schedules even when immediate preconditions fail."""
    client, request = _client(
        {"me": {"feeder": _owner(ready=False)}},
        _check(),
        {"feederFirmwareUpdateStart": _status("Progress", 0)},
    )
    await client.update_firmware_start("feeder")
    assert request.await_args.kwargs["query"] == queries.feeder.UPDATE_FIRMWARE


@pytest.mark.asyncio
async def test_immediate_requires_server_preconditions():
    """A RegularStart cannot run before verified preconditions are fulfilled."""
    client, request = _client({"me": {"feeder": _owner(ready=False)}}, _check())
    with pytest.raises(FirmwareUpdatePreconditionError, match="preconditions"):
        await client.update_firmware_start("feeder", immediate=True)
    assert request.await_count == 2


@pytest.mark.parametrize("progress", [None, 0, 42.5])
@pytest.mark.asyncio
async def test_existing_backend_progress_attaches_without_start(progress):
    """Existing work, including unknown numeric progress, is not restarted."""
    client, request = _client(
        {"me": {"feeder": _owner(ready=False)}}, _check(_status("Progress", progress))
    )
    result = await client.update_firmware_start("feeder", immediate=True)
    assert result.is_in_progress
    assert result.progress == progress
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_no_update_and_unknown_availability_are_distinct():
    """Absent availability cannot reuse previously cached permission to start."""
    client, request = _client({"me": {"feeder": _owner(available=False)}}, _check())
    with pytest.raises(NoFirmwareUpdateAvailableError):
        await client.update_firmware_start("feeder")
    assert request.await_count == 2
    unknown = _owner()
    unknown.pop("firmwareUpdateAvailability")
    client, request = _client({"me": {"feeder": unknown}}, _check())
    # Remove previously cached availability too: missing preflight cannot reuse it.
    with pytest.raises(FirmwareUpdatePreconditionError, match="availability"):
        await client.update_firmware_start("feeder")
    assert request.await_count == 2


@pytest.mark.parametrize("role", ["FeederForMember", "FeederForMemberPending"])
@pytest.mark.asyncio
async def test_roles_fail_before_firmware_mutations(role):
    """Known nonowners cannot reach either progress or start mutations."""
    client, request = _client()
    client._feeders["feeder"]["__typename"] = role
    with pytest.raises(FeederControlUnavailableError):
        await client.update_firmware_start("feeder")
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_preflight_can_revoke_cached_owner():
    """A fresh account response overrides a stale owner assumption."""
    client, request = _client(
        {"me": {"feeder": {"id": "feeder", "__typename": "FeederForMember"}}}
    )
    with pytest.raises(FeederControlUnavailableError):
        await client.update_firmware_start("feeder")
    assert request.await_count == 1
    assert request.await_args.kwargs["query"].startswith("query")


@pytest.mark.asyncio
async def test_already_updating_race_rechecks_instead_of_returning_stale_status():
    """A concurrent backend start requires new progress evidence."""
    error = GraphqlError(
        {"extensions": {"code": "FEEDER_FIRMWARE_UPGRADE_ALREADY_IN_PROGRESS"}}
    )
    client, request = _client(
        {"me": {"feeder": _owner()}}, _check(), error, _check(_status("Progress", 42))
    )
    result = await client.update_firmware_start("feeder")
    assert result.progress == 42
    assert request.await_count == 4
    assert request.await_args.kwargs["query"] == queries.feeder.UPDATE_FIRMWARE_PROGRESS


@pytest.mark.asyncio
async def test_unknown_backend_variant_never_starts_an_update():
    """An unknown status cannot be interpreted as idle or completed."""
    client, request = _client(
        {"me": {"feeder": _owner()}}, _check({"__typename": "FutureFirmwareResult"})
    )
    with pytest.raises(FirmwareUpdateUnknownStateError, match="Unknown"):
        await client.update_firmware_start("feeder")
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_start_is_rejected_and_cancellation_releases_lock():
    """Only one start can run locally and cancellation releases its lock."""
    client, request = _client()
    entered = asyncio.Event()

    async def pending(**kwargs: object):
        entered.set()
        await asyncio.Event().wait()

    request.side_effect = pending
    first = asyncio.create_task(client.update_firmware_start("feeder"))
    await entered.wait()
    with pytest.raises(FirmwareUpdateBusyError, match="already active"):
        await client.update_firmware_start("feeder")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    request.side_effect = [
        {"me": {"feeder": _owner()}},
        _check(),
        {"feederFirmwareUpdateStart": _status("Progress", 0)},
    ]
    assert (await client.update_firmware_start("feeder")).is_in_progress


@pytest.mark.parametrize("progress", [None, True, -1, 101, float("nan"), "42"])
def test_progress_variant_survives_unknown_numeric_progress(progress):
    """An invalid percentage does not change an in-progress union to idle."""
    status = FeederUpdateStatus(_status("Progress", progress))
    assert status.is_in_progress
    assert status.progress is None
    assert not status.is_unknown


def test_failed_and_unknown_statuses_are_distinct_and_safe():
    """Failure payloads may omit the feeder and still expose a safe reason."""
    failed = FeederUpdateStatus(_status("Failed"))
    assert failed.is_failed
    assert failed.failure_reason == "BATTERY_LOW"
    assert failed.feeder == {}
    unknown = FeederUpdateStatus({"__typename": "FutureFirmwareResult"})
    assert unknown.is_unknown
    assert not unknown.is_complete


@pytest.mark.asyncio
async def test_requested_version_is_rechecked_before_start():
    """Stale caller metadata cannot start an unexpected newly available version."""
    client, request = _client({"me": {"feeder": _owner()}}, _check())
    with pytest.raises(FirmwareUpdatePreconditionError, match="requested firmware"):
        await client.update_firmware_start(
            "feeder", immediate=True, expected_version="stale-version"
        )
    assert request.await_count == 2
