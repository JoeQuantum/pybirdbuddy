"""Device controls use explicit roles, bounded progress and truthful final state."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from birdbuddy import queries
from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import NoResponseError
from birdbuddy.feeder import (
    Feeder,
    FeederControlError,
    FeederControlTimeoutError,
    FeederControlUnavailableError,
    FeederHousingType,
    FeederMountingType,
    FeederSolarRoof,
    PowerProfile,
)

_FIELDS = {
    "off_grid": ("offGrid", "FeederToggleOffGrid", True),
    "audio": ("audioEnabled", "FeederToggleAudio", True),
    "power": ("powerProfile", "FeederUpdatePowerProfile", "POWER_SAVER_MODE"),
}


def _client(
    *responses: object, role: str = "FeederForOwner"
) -> tuple[BirdBuddy, AsyncMock, AsyncMock]:
    client = BirdBuddy(refresh_token="fake-refresh", access_token="fake-access")
    client._feeders["feeder"] = Feeder(
        {
            "id": "feeder",
            "__typename": role,
            "offGrid": False,
            "audioEnabled": False,
            "powerProfile": "STANDARD_MODE",
        }
    )
    request = AsyncMock(side_effect=responses)
    refresh = AsyncMock(return_value=True)
    client._make_request = request
    client.refresh = refresh
    return client, request, refresh


def _result(control: str, suffix: str, value: bool | str | None = None) -> dict:
    field, prefix, target = _FIELDS[control]
    return {
        "__typename": prefix + suffix,
        "feeder": {
            "id": "feeder",
            "__typename": "FeederForOwner",
            field: target if value is None else value,
        },
    }


async def _invoke(client: BirdBuddy, control: str, feeder: Feeder | str = "feeder"):
    if control == "options":
        return await client.set_feeder_options(feeder, name="Renamed")
    if control == "off_grid":
        return await client.toggle_off_grid(feeder, True)
    if control == "audio":
        return await client.toggle_audio_enabled(feeder, True)
    return await client.set_power_profile(feeder, PowerProfile.POWER_SAVE)


@pytest.mark.parametrize("control", _FIELDS)
@pytest.mark.asyncio
async def test_finished_control_updates_cache_and_refreshes(control):
    """Even immediate completion updates the existing object and confirms state."""
    client, request, refresh = _client(_result(control, "FinishedResult"))
    existing = client._feeders["feeder"]
    await _invoke(client, control)
    field, _, target = _FIELDS[control]
    assert existing[field] == target
    assert client._feeders["feeder"] is existing
    assert request.await_count == 1
    refresh.assert_awaited_once()


@pytest.mark.parametrize("control", _FIELDS)
@pytest.mark.asyncio
async def test_in_progress_uses_action_specific_checks(control, monkeypatch):
    """Several progress checks never replay the initiating mutation."""
    sleeps = AsyncMock()
    monkeypatch.setattr("birdbuddy.client.asyncio.sleep", sleeps)
    client, request, refresh = _client(
        _result(
            control,
            "InProgressResult",
            "STANDARD_MODE" if control == "power" else False,
        ),
        _result(control, "InProgressResult"),
        _result(control, "FinishedResult"),
    )
    await _invoke(client, control)
    check_queries = {
        "off_grid": queries.feeder.TOGGLE_OFF_GRID_CHECK,
        "audio": queries.feeder.TOGGLE_AUDIO_ENABLED_CHECK,
        "power": queries.feeder.UPDATE_POWER_PROFILE_CHECK,
    }
    assert request.await_count == 3
    for call in request.await_args_list[1:]:
        assert call.kwargs["query"] == check_queries[control]
        assert call.kwargs["variables"] == {"feederId": "feeder"}
    assert sleeps.await_count == 2
    refresh.assert_awaited_once()


@pytest.mark.parametrize("control", _FIELDS)
@pytest.mark.asyncio
async def test_failed_check_preserves_observed_state(control, monkeypatch):
    """A failure union is explicit and leaves the server-reported state cached."""
    monkeypatch.setattr("birdbuddy.client.asyncio.sleep", AsyncMock())
    old = "STANDARD_MODE" if control == "power" else False
    client, request, refresh = _client(
        _result(control, "InProgressResult", old), _result(control, "FailedResult", old)
    )
    with pytest.raises(FeederControlError, match="reported control failure") as caught:
        await _invoke(client, control)
    assert caught.value.action_completed is False
    assert client._feeders["feeder"][_FIELDS[control][0]] == old
    assert request.await_count == 2
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "role", ["FeederForMember", "FeederForMemberPending", "FeederForPublic"]
)
@pytest.mark.parametrize("as_object", [False, True])
@pytest.mark.parametrize("control", ["audio", "off_grid", "power", "options"])
@pytest.mark.asyncio
async def test_nonowners_never_send_mutations(role, control, as_object):
    """Passing an ID cannot bypass the same known role restriction as an object."""
    client, request, refresh = _client(role=role)
    feeder = client._feeders["feeder"] if as_object else "feeder"
    with pytest.raises(FeederControlUnavailableError, match="owner"):
        await _invoke(client, control, feeder)
    request.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_id_reads_role_before_mutation():
    """An uncached string ID must first resolve to an explicit owner."""
    client, request, refresh = _client()
    client._feeders.clear()
    with pytest.raises(FeederControlUnavailableError, match="owner"):
        await client.toggle_off_grid("unknown", True)
    refresh.assert_awaited_once()
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_audio_capability_blocks_only_false():
    """Missing capability is unknown; a returned false is an explicit restriction."""
    client, request, _ = _client(_result("audio", "FinishedResult"))
    client._feeders["feeder"]["supportsAudio"] = False
    with pytest.raises(FeederControlUnavailableError, match="Audio"):
        await client.toggle_audio_enabled("feeder", True)
    request.assert_not_awaited()
    client._feeders["feeder"].pop("supportsAudio")
    assert client._feeders["feeder"].supports_audio is None
    await client.toggle_audio_enabled("feeder", True)
    assert request.await_count == 1


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
@pytest.mark.asyncio
async def test_invalid_deadline_fails_before_role_lookup(timeout):
    """The total budget must be positive finite seconds, not a bool."""
    client, request, refresh = _client()
    client._feeders.clear()
    with pytest.raises(ValueError, match="timeout"):
        await client.toggle_off_grid("unknown", True, timeout=timeout)
    request.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_total_deadline_covers_a_stalled_request():
    """A stalled mutation is cancelled by the outer deadline and never replayed."""
    client, request, refresh = _client()
    cancelled = asyncio.Event()

    async def stalled(**kwargs: object):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    request.side_effect = stalled
    with pytest.raises(FeederControlTimeoutError) as caught:
        await client.toggle_off_grid("feeder", True, timeout=0.01)
    assert caught.value.action_completed is None
    assert cancelled.is_set()
    assert request.await_count == 1
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_after_finished_result_reports_completion():
    """A timed-out final refresh distinguishes completion from unknown outcome."""
    client, request, refresh = _client(_result("off_grid", "FinishedResult"))

    async def stalled_refresh():
        await asyncio.Event().wait()

    refresh.side_effect = stalled_refresh
    with pytest.raises(FeederControlTimeoutError) as caught:
        await client.toggle_off_grid("feeder", True, timeout=0.01)
    assert caught.value.action_completed is True
    assert request.await_count == 1
    assert client._feeders["feeder"].is_off_grid is True


@pytest.mark.asyncio
async def test_final_refresh_failure_is_not_reported_as_mutation_failure():
    """A completed operation remains completed when subsequent account reads fail."""
    client, request, refresh = _client(_result("off_grid", "FinishedResult"))
    refresh.side_effect = NoResponseError()
    with pytest.raises(FeederControlError, match="final state refresh") as caught:
        await client.toggle_off_grid("feeder", True)
    assert caught.value.action_completed is True
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_truthful_final_state_does_not_claim_requested_value():
    """A refreshed conflicting value is retained and success is not fabricated."""
    client, _, refresh = _client(_result("audio", "FinishedResult"))

    async def changed_back():
        client._feeders["feeder"]["audioEnabled"] = False
        return True

    refresh.side_effect = changed_back
    with pytest.raises(FeederControlError, match="requested state") as caught:
        await client.toggle_audio_enabled("feeder", True)
    assert caught.value.action_completed is True
    assert client._feeders["feeder"].is_audio_enabled is False


@pytest.mark.asyncio
async def test_external_cancellation_propagates_and_cancels_poll(monkeypatch):
    """Cancellation releases a polling operation without another start request."""
    client, request, refresh = _client(_result("off_grid", "InProgressResult", False))
    sleeping = asyncio.Event()

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("birdbuddy.client.asyncio.sleep", sleep)
    task = asyncio.create_task(client.toggle_off_grid("feeder", True))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert request.await_count == 1
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"bogus": "value"},
        {"name": "Valid", "housingType": "FUTURE"},
        {"name": ""},
        {"name": 123},
        {"offGrid": "true"},
        {"solarRoof": True},
        {"mountingType": "INVALID"},
        {"housingType": FeederHousingType.UNKNOWN},
        {"offlineMode": True, "offGrid": False},
    ],
)
@pytest.mark.asyncio
async def test_options_validate_all_inputs_before_any_request(options):
    """Unsupported keys or values cannot be silently dropped or partially applied."""
    client, request, refresh = _client()
    with pytest.raises(ValueError, match=r"options|option|name|conflicts"):
        await client.set_feeder_options("feeder", **options)
    request.assert_not_awaited()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_schema_metadata_options_are_preserved():
    """Housing, mounting and solar values join the existing notification/name keys."""
    updated = {"id": "feeder", "__typename": "FeederForOwner", "name": "New name"}
    client, request, refresh = _client(updated)
    await client.set_feeder_options(
        "feeder",
        name="New name",
        lowBatteryNotification=True,
        lowFoodNotification=False,
        housingType=FeederHousingType.BIRD_BATH,
        mountingType=FeederMountingType.POLE_MOUNT,
        solarRoof=FeederSolarRoof.YES,
    )
    assert request.await_args.kwargs["variables"]["feederUpdateInput"] == {
        "name": "New name",
        "lowBatteryNotification": True,
        "lowFoodNotification": False,
        "housingType": "BIRD_BATH",
        "mountingType": "POLE_MOUNT",
        "solarRoof": "YES",
    }
    refresh.assert_awaited_once()


@pytest.mark.parametrize(
    "options", [{"offlineMode": True}, {"offGrid": True, "offlineMode": True}]
)
@pytest.mark.asyncio
async def test_legacy_offline_alias_uses_bounded_off_grid_workflow(options):
    """The accepted alias normalizes once and uses the verified device workflow."""
    client, request, _ = _client(_result("off_grid", "FinishedResult"))
    result = await client.set_feeder_options("feeder", **options)
    assert result["offGrid"] is True
    assert request.await_args.kwargs["query"] == queries.feeder.TOGGLE_OFF_GRID
    assert request.await_args.kwargs["variables"]["feederToggleOffGridInput"] == {
        "offGrid": True
    }


@pytest.mark.asyncio
async def test_unknown_profile_rejected_without_logging_or_request(caplog):
    """The read fallback remains usable but UNKNOWN is not a writable profile."""
    client, request, refresh = _client()
    with pytest.raises(ValueError, match="PowerProfile"):
        await client.set_power_profile("feeder", PowerProfile.UNKNOWN)
    request.assert_not_awaited()
    refresh.assert_not_awaited()
    assert not caplog.records
    feeder = Feeder({"__typename": "FeederForMember"})
    assert feeder.selected_power_profile is PowerProfile.UNKNOWN


def test_minimal_control_capability_is_selected_with_role():
    """Normal refresh, sign-in and options results provide the gating field."""
    for document in (queries.me.ME, queries.auth.SIGN_IN, queries.feeder.SET_OPTIONS):
        assert "supportsAudio" in document
        assert "__typename" in document


@pytest.mark.asyncio
async def test_in_progress_cannot_wait_forever():
    """An endlessly pending device is bounded even when requests return quickly."""
    client, request, refresh = _client(_result("off_grid", "InProgressResult", False))
    with pytest.raises(FeederControlTimeoutError):
        await client.toggle_off_grid("feeder", True, timeout=0.01)
    assert request.await_count == 1
    assert client._feeders["feeder"].is_off_grid is False
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "result",
    [
        {"__typename": "FutureResult", "feeder": {}},
        {"__typename": [], "feeder": {}},
        {"__typename": "FeederToggleAudioFinishedResult", "feeder": {"id": "wrong"}},
        {
            "__typename": "FeederToggleAudioFinishedResult",
            "feeder": {"audioEnabled": 1},
        },
        {"__typename": "FeederToggleAudioFinishedResult", "feeder": None},
    ],
)
@pytest.mark.asyncio
async def test_malformed_control_result_preserves_known_state(result):
    """Unknown result variants and malformed values cannot pollute device state."""
    client, _, refresh = _client(result)
    with pytest.raises(FeederControlError, match=r"control|invalid"):
        await client.toggle_audio_enabled("feeder", True)
    assert client._feeders["feeder"].is_audio_enabled is False
    refresh.assert_not_awaited()


@pytest.mark.parametrize("control", ["audio", "off_grid"])
@pytest.mark.asyncio
async def test_boolean_controls_reject_truthy_nonboolean_values(control):
    """A string or integer is not silently coerced into a hardware command."""
    client, request, refresh = _client()
    method = (
        client.toggle_audio_enabled if control == "audio" else client.toggle_off_grid
    )
    with pytest.raises(ValueError, match="bool"):
        await method("feeder", "false")  # type: ignore[arg-type]
    request.assert_not_awaited()
    refresh.assert_not_awaited()
