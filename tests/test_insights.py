"""Synthetic schema-based optional reads; no live account entitlement claims."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from graphql import OperationDefinitionNode, build_client_schema, parse, validate
import pytest

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import (
    AuthenticationFailedError,
    GraphqlError,
    NoResponseError,
    UnexpectedResponseError,
)
from birdbuddy.insights import (
    AccountInsights,
    InsightAccountError,
    InsightClosedError,
    InsightLanguageError,
    InsightLimitError,
)
from birdbuddy.queries import insights as queries

DOCUMENTS = {
    name: value
    for name, value in vars(queries).items()
    if name.isupper() and not name.startswith("_")
}
STAMP = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def payloads():
    """Load explicitly synthetic selected-field responses."""
    return json.loads(
        (
            Path(__file__).parent / "fixtures/insights/synthetic_selected.json"
        ).read_text()
    )


@pytest.fixture
def account(payloads):
    """Mock the authenticated request boundary without any network access."""
    client = MagicMock(spec=BirdBuddy)
    client.user = {"id": "account-a"}
    client.language_code = "en"
    client.feeders = {"feeder-owner": {"id": "feeder-owner"}}

    async def read(*, query, variables):
        name = next(name for name, document in DOCUMENTS.items() if query == document)
        return deepcopy(payloads[name])

    client._make_request = AsyncMock(side_effect=read)
    return client


@pytest.mark.parametrize("name", DOCUMENTS)
def test_each_document_validates_against_committed_live_schema_and_is_read_only(name):
    """Each document validates against committed live schema and is read only."""
    schema = build_client_schema(
        json.loads((Path(__file__).parent / "fixtures/schema.json").read_text())
    )
    document = parse(DOCUMENTS[name])
    assert not validate(schema, document)
    assert all(
        definition.operation.value == "query"
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    )
    for excluded in (
        "errorMessage",
        "latitude",
        "longitude",
        "email",
        "accessToken",
        "managementUrl",
        "customerPortalUrl",
        "namedByUser",
    ):
        assert excluded not in DOCUMENTS[name]


async def test_constructing_the_adapter_is_idle(account):
    """Constructing the adapter is idle."""
    AccountInsights(account)
    account._make_request.assert_not_called()


async def test_hardware_and_locks_preserve_role_and_running_profile(account):
    """Hardware and locks preserve role and running profile."""
    result = await AccountInsights(account).hardware()
    owner, member, pending = result["data"]["feeders"]
    assert result["schema_version"] == 1
    assert result["complete"] is True
    assert owner["version"] == "V2_NOOK"
    assert owner["powerProfile"] == "FRENZY_MODE"
    assert owner["smartEnergy"]["runningPowerProfile"] == "POWER_SAVER_MODE"
    assert "smartEnergy" not in member
    assert "version" not in pending
    assert result["data"]["featureLocks"] == [{"feature": "LIVESTREAM"}]
    assert result["data"]["subscriptionPremium"] is None


@pytest.mark.parametrize(
    "energy",
    [
        {"__typename": "FeederSmartEnergyDisabled", "status": "DISABLED"},
        {
            "__typename": "FeederSmartEnergyNotSupported",
            "status": "NOT_SUPPORTED",
            "reason": "FEEDER_KIND",
        },
        None,
        {
            "__typename": "FeederSmartEnergyFuture",
            "status": "FUTURE",
            "privateNewField": "not-selected",
        },
    ],
)
async def test_smart_energy_disabled_unsupported_null_and_unknown_are_distinct(
    account, payloads, energy
):
    """Smart energy disabled unsupported null and unknown are distinct."""
    payloads["HARDWARE"]["me"]["feeders"][0]["smartEnergy"] = energy
    result = await AccountInsights(account).hardware()
    actual = result["data"]["feeders"][0]["smartEnergy"]
    assert actual == energy if energy is None else actual["status"] == energy["status"]
    assert "privateNewField" not in str(result)
    if energy and energy["status"] == "FUTURE":
        assert result["unknown_variant_fields"] == ["data.feeders[0].smartEnergy"]
        assert not result["complete"]


async def test_future_hardware_values_are_kept_without_inventing_capabilities(
    account, payloads
):
    """Future hardware values are kept without inventing capabilities."""
    payloads["HARDWARE"]["me"]["feeders"][0].update(
        version="V9", mountingType="UNKNOWN_MOUNT"
    )
    result = await AccountInsights(account).hardware()
    assert result["data"]["feeders"][0]["version"] == "V9"
    assert result["data"]["feeders"][0]["mountingType"] == "UNKNOWN_MOUNT"


async def test_named_individuals_keep_stopped_state_missing_stats_and_duplicate_names(
    account,
):
    """Named individuals keep stopped state missing stats and duplicate names."""
    result = await AccountInsights(account).named_birds()
    active, stopped = result["data"]
    assert active["id"] != stopped["id"]
    assert active["name"] == stopped["name"]
    assert active["stats"]["visitCount"] == 12
    assert stopped["state"] == "STOPPED"
    assert stopped["stats"] is None
    assert stopped["previewMedia"] == []
    assert result["account_id"] == "account-a"


@pytest.mark.parametrize(
    ("kind", "level"),
    [
        ("SpeciesBird", "species"),
        ("SpeciesBirdFamily", "family"),
        ("SpeciesBirdGenus", "genus"),
        ("SpeciesBirdOrder", "order"),
        ("SpeciesFuture", None),
    ],
)
async def test_species_taxonomy_and_localized_names_keep_stable_identity(
    account, payloads, kind, level
):
    """Species taxonomy and localized names keep stable identity."""
    payloads["SPECIES"]["species"].update(__typename=kind, name="Cardenal rojo")
    result = await AccountInsights(account).species("species-cardinal")
    assert result["data"]["id"] == "species-cardinal"
    assert result["data"]["name"] == "Cardenal rojo"
    assert result["data"]["scientificName"] == "Cardinalis cardinalis"
    assert result["data"]["taxonomic_level"] == level
    if level is None:
        assert not result["complete"]


async def test_preferences_are_read_only_ids_and_unknown_mystery_values_stay_null(
    account, payloads
):
    """Preferences are read only ids and unknown mystery values stay null."""
    payloads["PREFERENCES"]["me"]["postcardSettings"]["ignoreMysteryVisitors"] = None
    result = await AccountInsights(account).preferences()
    assert result["data"]["ignoreMysteryVisitors"] is None
    assert result["data"]["ignoredSpecies"][0]["id"] == "species-cardinal"
    account._make_request.assert_awaited_once_with(
        query=queries.PREFERENCES, variables={}
    )


@pytest.mark.parametrize(
    ("method", "key", "variant"),
    [
        (
            "weekly_recap",
            "weeklyRecapForUser",
            "WeeklyRecapForUserInsufficientVisitsError",
        ),
        ("weekly_recap", "weeklyRecapForUser", "WeeklyRecapForUserNotAvailableError"),
        ("weekly_recap", "weeklyRecapForUser", "WeeklyRecapForUserNotFoundError"),
        (
            "yearly_review",
            "yearInReviewForUser",
            "YearInReviewForUserInsufficientVisitsError",
        ),
        (
            "yearly_review",
            "yearInReviewForUser",
            "YearInReviewForUserNotAvailableError",
        ),
    ],
)
async def test_typed_unavailable_reports_are_data_not_coordinator_failures(
    account, payloads, method, key, variant
):
    """Typed unavailable reports are data not coordinator failures."""
    document = "WEEKLY" if method == "weekly_recap" else "YEARLY"
    payloads[document][key] = {
        "__typename": variant,
        "errorMessage": "unsafe vendor text",
    }
    result = await getattr(AccountInsights(account), method)()
    assert result["status"] == "unavailable"
    assert result["variant"] == variant
    assert result["data"] is None
    assert not result["complete"]
    assert "unsafe vendor text" not in str(result)
    assert account._make_request.call_args.kwargs["variables"] == {
        "userId": "account-a"
    }


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (None, "missing"),
        ({"__typename": "FutureResult", "private": "secret"}, "unknown"),
        ({"__typename": "WeeklyRecapForUserSuccess", "data": None}, "missing"),
        ({"__typename": "YearInReviewForUserSuccess", "data": {}}, "unknown"),
    ],
)
async def test_unknown_or_missing_weekly_results_never_invent_zero_counts(
    account, payloads, value, status
):
    """Unknown or missing weekly results never invent zero counts."""
    payloads["WEEKLY"]["weeklyRecapForUser"] = value
    result = await AccountInsights(account).weekly_recap()
    assert result["status"] == status
    assert result["data"] is None
    assert not result["complete"]
    assert "secret" not in str(result)


async def test_weekly_yearly_periods_counts_and_empty_media_are_vendor_scoped(account):
    """Weekly yearly periods counts and empty media are vendor scoped."""
    adapter = AccountInsights(account)
    weekly = await adapter.weekly_recap()
    yearly = await adapter.yearly_review()
    assert weekly["data"]["isoWeek"] == "2026-W44"
    assert weekly["data"]["speciesVisitStats"]["distinctSpecies"][0]["medias"] == []
    assert weekly["data"]["generalVisitStats"]["totalVisits"] == 24
    assert yearly["data"]["year"] == 2026
    assert (
        yearly["data"]["namedBirdVisitStats"]["namedBirds"][0]["identificationCount"]
        == 12
    )
    assert weekly["complete"]
    assert yearly["complete"]


@pytest.mark.parametrize(
    "support", ["SUPPORTED", "UNAVAILABLE", "REQUIRES_FIRMWARE_UPDATE", "FUTURE_VALUE"]
)
async def test_snapshot_support_is_preserved_as_an_enum(account, payloads, support):
    """A nonempty unsupported capability string must not become Boolean true."""
    for feeder in payloads["HARDWARE"]["me"]["feeders"][:2]:
        feeder["supportsLivestreamSnapshot"] = support
    result = await AccountInsights(account).hardware()
    owner, member, pending = result["data"]["feeders"]
    assert owner["supportsLivestreamSnapshot"] == support
    assert member["supportsLivestreamSnapshot"] == support
    assert "supportsLivestreamSnapshot" not in pending
    account._make_request.assert_awaited_once_with(query=queries.HARDWARE, variables={})


async def test_optional_report_range_only_uses_schema_arguments_and_current_account(
    account,
):
    """Optional report range only uses schema arguments and current account."""
    result = await AccountInsights(account).feeder_report(
        feeder_id="feeder-owner", start=STAMP - timedelta(days=30), end=STAMP
    )
    variables = account._make_request.call_args.kwargs["variables"]
    assert variables == {
        "userId": "account-a",
        "feederId": "feeder-owner",
        "start": (STAMP - timedelta(days=30)).isoformat(),
        "end": STAMP.isoformat(),
    }
    assert result["data"]["reportPeriod"] == {
        "startDate": "2026-10-01T00:00:00Z",
        "endDate": "2026-11-01T00:00:00Z",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"feeder_id": "foreign"},
        {"start": STAMP},
        {"start": STAMP.replace(tzinfo=None), "end": STAMP},
        {"start": STAMP, "end": STAMP},
        {"start": STAMP, "end": STAMP + timedelta(days=367)},
    ],
)
async def test_report_target_and_date_validation_precedes_io(account, kwargs):
    """Report target and date validation precedes io."""
    with pytest.raises((ValueError, InsightAccountError)):
        await AccountInsights(account).feeder_report(**kwargs)
    account._make_request.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"), [("userId", "other-account"), ("feederIds", ["foreign"])]
)
async def test_report_result_cannot_cross_account_or_selected_feeder(
    account, payloads, field, value
):
    """Report result cannot cross account or selected feeder."""
    payloads["FEEDER_REPORT"]["userFeederReport"][field] = value
    with pytest.raises(InsightAccountError):
        await AccountInsights(account).feeder_report(feeder_id="feeder-owner")


@pytest.mark.parametrize("intraday", [False, True])
async def test_report_range_error_is_not_cached_or_replaced_by_another_period(
    bbclient, graphql_mock, payloads, intraday
):
    """Real transport errors leave other ranges usable and permit later recovery."""
    bbclient._save_me(
        {
            "user": {"id": "account-a"},
            "feeders": [{"id": "feeder-owner", "__typename": "FeederForOwner"}],
        }
    )
    bbclient.bind_account("account-a")
    good = {"start": STAMP - timedelta(days=31), "end": STAMP}
    bad = {"start": STAMP - timedelta(days=62), "end": good["start"]}
    if intraday:
        good = {
            "start": datetime.fromisoformat("2034-06-02T05:15:00.123456+02:00"),
            "end": datetime.fromisoformat("2034-06-02T06:45:59.999000+02:00"),
        }
        bad = {
            "start": good["end"] - timedelta(minutes=2),
            "end": good["end"] - timedelta(minutes=1),
        }
    failing = True

    async def execute(*, query, variables, headers):
        assert query == queries.FEEDER_REPORT  # No fallback, mutation or token refresh.
        assert variables["userId"] == "account-a"
        assert variables["feederId"] == "feeder-owner"
        if failing and variables.get("start") == bad["start"].isoformat():
            return {
                "errors": [
                    {
                        "message": "synthetic-private-server-detail",
                        "path": ["userFeederReport"],
                        "extensions": {"code": "INTERNAL_SERVER_ERROR"},
                    }
                ]
            }
        response = deepcopy(payloads["FEEDER_REPORT"])
        response["userFeederReport"]["reportPeriod"] = {
            "startDate": variables["start"],
            "endDate": variables["end"],
        }
        return {"data": response}

    graphql_mock.side_effect = execute
    adapter = AccountInsights(bbclient)
    try:
        cached = await adapter.feeder_report(
            feeder_id="feeder-owner", start=good["start"], end=good["end"]
        )
        for attempt in range(2):
            with pytest.raises(GraphqlError) as error:
                await adapter.feeder_report(
                    feeder_id="feeder-owner", start=bad["start"], end=bad["end"]
                )
            assert error.value.error_code == "INTERNAL_SERVER_ERROR"
            assert "synthetic-private-server-detail" not in str(error.value)
            assert graphql_mock.await_count == attempt + 2
            assert (
                await adapter.feeder_report(
                    feeder_id="feeder-owner", start=good["start"], end=good["end"]
                )
                == cached
            )
            assert graphql_mock.await_count == attempt + 2
        failing = False
        recovered = await adapter.feeder_report(
            feeder_id="feeder-owner", start=bad["start"], end=bad["end"]
        )
        assert recovered["status"] == "available"
        assert (
            recovered["data"]["reportPeriod"]["startDate"] == bad["start"].isoformat()
        )
        assert (
            await adapter.feeder_report(
                feeder_id="feeder-owner", start=bad["start"], end=bad["end"]
            )
            == recovered
        )
        assert graphql_mock.await_count == 4
    finally:
        await adapter.async_close()


@pytest.mark.parametrize("offset", [timezone.utc, timezone(timedelta(hours=-7))])
async def test_report_preserves_exact_range_and_summary_hour_without_redating(
    account, payloads, offset
):
    """A summary-hour date is independent of the report and fetch timestamps."""
    start = datetime(2034, 6, 2, 3, 15, 0, 123456, timezone.utc).astimezone(offset)
    end = start + timedelta(minutes=90, microseconds=875544)
    fetched = datetime(2034, 7, 10, tzinfo=timezone.utc)
    summary_hour = "2034-07-10T09:00:00Z"
    report = payloads["FEEDER_REPORT"]["userFeederReport"]
    report["reportPeriod"] = {
        "startDate": start.astimezone(timezone.utc).isoformat(),
        "endDate": end.astimezone(timezone.utc).isoformat(),
    }
    report["visitAggregateMetrics"]["busiestHourOfDay"] = summary_hour
    adapter = AccountInsights(account, now=lambda: fetched)
    try:
        result = await adapter.feeder_report(
            feeder_id="feeder-owner", start=start, end=end
        )
        account._make_request.assert_awaited_once_with(
            query=queries.FEEDER_REPORT,
            variables={
                "userId": "account-a",
                "feederId": "feeder-owner",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        assert result["status"] == "available"
        assert result["complete"] is True
        assert result["data"]["reportPeriod"] == report["reportPeriod"]
        assert (
            result["data"]["visitAggregateMetrics"]["busiestHourOfDay"] == summary_hour
        )
        assert result["fetched_at"] == fetched.isoformat()
    finally:
        await adapter.async_close()


async def test_zero_feeder_account_reads_remote_metadata_with_current_expiry(
    account,
):
    """Zero feeder account can read remote metadata and expiry updates from cache."""
    account.feeders = {}
    now = [STAMP]
    adapter = AccountInsights(account, now=lambda: now[0])
    result = await adapter.remote_connections()
    assert result["data"][0]["entity_scope"] == "remote_connection"
    assert result["data"][0]["expired"] is False
    now[0] += timedelta(minutes=3)
    assert (await adapter.remote_connections())["data"][0]["expired"] is True
    account._make_request.assert_awaited_once()
    assert account.feeders == {}


@pytest.mark.parametrize("expiry", [None, "invalid", "2026-11-01T00:00:00"])
async def test_unavailable_or_unparseable_remote_expiry_stays_unknown(
    account, payloads, expiry
):
    """Unavailable or unparseable remote expiry stays unknown."""
    payloads["REMOTE_CONNECTIONS"]["me"]["remoteFeederConnections"][0]["expiresAt"] = (
        expiry
    )
    result = await AccountInsights(account).remote_connections()
    assert result["data"][0]["expired"] is None


async def test_nested_limits_declare_omissions_without_claiming_pagination(
    account, payloads
):
    """Nested limits declare omissions without claiming pagination."""
    result = await AccountInsights(account).hardware(limit=1)
    assert len(result["data"]["feeders"]) == 1
    assert result["truncated_fields"] == ["data.feeders"]
    assert not result["complete"]
    assert result["pagination"] == {"supported": False, "next_cursor": None}
    assert result["limit"] == 1


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_invalid_limit_is_rejected_before_io(account, limit):
    """Invalid limit is rejected before io."""
    with pytest.raises(InsightLimitError):
        await AccountInsights(account).named_birds(limit=limit)
    account._make_request.assert_not_called()


async def test_allowlisting_never_passes_through_private_extra_data(account, payloads):
    """Allowlisting never passes through private extra data."""
    raw = payloads["HARDWARE"]["me"]
    raw.update(accessToken="secret-a", location={"latitude": 99})
    raw["feeders"][0].update(
        serialNumber="private-serial", invitations=[{"token": "secret-b"}]
    )
    result = await AccountInsights(account).hardware()
    for secret in ("secret-a", "secret-b", "latitude", "private-serial"):
        assert secret not in str(result)


async def test_returned_data_is_a_defensive_copy(account):
    """Returned data is a defensive copy."""
    adapter = AccountInsights(account)
    first = await adapter.hardware()
    first["data"]["feeders"][0]["name"] = "mutated"
    assert (await adapter.hardware())["data"]["feeders"][0]["name"] == "Home"
    account._make_request.assert_awaited_once()


@pytest.mark.parametrize("method", ["weekly_recap", "yearly_review", "feeder_report"])
async def test_analytics_cache_lasts_at_least_one_hour(account, method):
    """Analytics cache lasts at least one hour."""
    now = [0.0]
    adapter = AccountInsights(account, clock=lambda: now[0])
    await getattr(adapter, method)()
    now[0] = 3599
    await getattr(adapter, method)()
    account._make_request.assert_awaited_once()
    now[0] = 3600
    await getattr(adapter, method)()
    assert account._make_request.await_count == 2


async def test_cache_is_bounded_and_language_is_part_of_its_identity(account):
    """Cache is bounded and language is part of its identity."""
    adapter = AccountInsights(account, max_cached_results=1)
    await adapter.hardware()
    await adapter.preferences()
    assert len(adapter._cache) == 1
    await adapter.hardware()
    account.language_code = "es"
    await adapter.hardware()
    assert account._make_request.await_count == 4


async def test_no_identity_prevents_even_a_cached_account_read(account):
    """No identity prevents even a cached account read."""
    adapter = AccountInsights(account)
    await adapter.hardware()
    account.user = None
    with pytest.raises(InsightAccountError):
        await adapter.hardware()
    assert not adapter._cache
    account._make_request.assert_awaited_once()


async def test_changed_identity_cannot_reuse_cached_or_wrong_response_data(
    account, payloads
):
    """Changed identity cannot reuse cached or wrong response data."""
    adapter = AccountInsights(account)
    await adapter.hardware()
    account.user = {"id": "account-b"}
    with pytest.raises(InsightAccountError):
        await adapter.hardware()
    assert not adapter._cache
    payloads["HARDWARE"]["me"]["user"]["id"] = "account-b"
    assert (await adapter.hardware())["account_id"] == "account-b"


async def test_account_change_while_a_read_is_in_flight_discards_the_response(
    account, payloads
):
    """Account change while a read is in flight discards the response."""

    async def read(**kwargs: object):
        account.user = {"id": "account-b"}
        return payloads["HARDWARE"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    with pytest.raises(InsightAccountError):
        await adapter.hardware()
    assert not adapter._cache


@pytest.mark.parametrize(
    "error",
    [
        None,
        NoResponseError(),
        AuthenticationFailedError(),
        GraphqlError({"message": "synthetic failure"}),
    ],
)
async def test_concurrent_success_or_failure_is_one_explicit_read(
    account, payloads, error
):
    """Concurrent success or failure is one explicit read."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        await release.wait()
        if error is not None:
            raise error
        return payloads["HARDWARE"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    tasks = [asyncio.create_task(adapter.hardware()) for _ in range(10)]
    await entered.wait()
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    account._make_request.assert_awaited_once()
    if error is not None:
        assert all(result is error for result in results)
        assert not adapter._cache
        with pytest.raises(type(error)):
            await adapter.hardware()
        assert account._make_request.await_count == 2
    else:
        assert all(result == results[0] for result in results)


async def test_cancelling_one_waiter_does_not_cancel_the_shared_read(account, payloads):
    """Cancelling one waiter does not cancel the shared read."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        await release.wait()
        return payloads["HARDWARE"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    first = asyncio.create_task(adapter.hardware())
    second = asyncio.create_task(adapter.hardware())
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)["status"] == "available"
    account._make_request.assert_awaited_once()


async def test_oversized_data_is_not_cached_or_silently_truncated(account, payloads):
    """Oversized data is not cached or silently truncated."""
    payloads["HARDWARE"]["me"]["feeders"][0]["name"] = "x" * 8193
    adapter = AccountInsights(account)
    with pytest.raises(InsightLimitError):
        await adapter.hardware()
    assert not adapter._cache


async def test_wrong_species_identity_and_wrong_account_response_are_rejected(
    account, payloads
):
    """Wrong species identity and wrong account response are rejected."""
    with pytest.raises(UnexpectedResponseError):
        await AccountInsights(account).species("other-species")
    payloads["HARDWARE"]["me"]["user"] = {"id": "other-account"}
    with pytest.raises(InsightAccountError):
        await AccountInsights(account).hardware()


async def test_nested_payload_construction_is_bounded_before_caching(account, payloads):
    """Large individually valid scalars cannot build an unlimited projection tree."""
    original = payloads["NAMED_BIRDS"]["me"]["mediaSpeciesNames"][0]
    payloads["NAMED_BIRDS"]["me"]["mediaSpeciesNames"] = [
        original | {"id": f"named-{i}", "name": "x" * 8000} for i in range(100)
    ]
    adapter = AccountInsights(account)
    with pytest.raises(InsightLimitError, match="budget"):
        await adapter.named_birds(limit=100)
    assert not adapter._cache


async def test_total_cache_bytes_are_bounded_not_just_entry_count(account):
    """A second result evicts old data when the shared byte allowance fills."""
    adapter = AccountInsights(account, max_cache_bytes=1800)
    await adapter.species("species-cardinal")
    await adapter.preferences()
    await adapter.remote_connections()
    assert sum(entry[1] for entry in adapter._cache.values()) <= 1800
    assert len(adapter._cache) < 3


@pytest.mark.parametrize(
    "value", [{"__typename": {"private": "secret"}}, [], "invalid"]
)
async def test_malformed_union_does_not_leak_unknown_objects(account, payloads, value):
    """Unexpected union shapes remain unavailable and incomplete."""
    payloads["WEEKLY"]["weeklyRecapForUser"] = value
    result = await AccountInsights(account).weekly_recap()
    assert result["status"] in {"unknown", "missing"}
    assert not result["complete"]
    assert "secret" not in str(result)


async def test_malformed_selected_object_never_becomes_a_scalar_success(
    account, payloads
):
    """A wrong shape is explicit missing data, not a fabricated empty success."""
    payloads["PREFERENCES"]["me"]["postcardSettings"] = []
    result = await AccountInsights(account).preferences()
    assert result["status"] == "missing"
    assert result["data"] is None
    assert result["missing_fields"] == ["data"]


async def test_distinct_inflight_reads_have_a_hard_limit(account, payloads):
    """Limit unique work while keeping duplicate requests coalesced."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        await release.wait()
        return payloads["NAMED_BIRDS"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    tasks = [asyncio.create_task(adapter.named_birds(limit=i)) for i in range(1, 17)]
    await entered.wait()
    with pytest.raises(InsightLimitError, match="16"):
        await adapter.named_birds(limit=17)
    release.set()
    await asyncio.gather(*tasks)
    assert account._make_request.await_count == 16


async def test_close_cancels_and_awaits_shared_work_then_refuses_new_reads(account):
    """Unload leaves neither a background read nor cached account data."""
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    waiter = asyncio.create_task(adapter.hardware())
    await entered.wait()
    await adapter.async_close()
    assert cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not adapter._inflight
    assert not adapter._cache
    with pytest.raises(InsightClosedError, match="closed"):
        await adapter.hardware()
    await adapter.async_close()
    account._make_request.assert_awaited_once()


async def test_a_cancel_suppressing_request_cannot_publish_after_close(
    account, payloads
):
    """A response finishing during cancellation cannot repopulate the cache."""
    entered = asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return payloads["HARDWARE"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    waiter = asyncio.create_task(adapter.hardware())
    await entered.wait()
    await adapter.async_close()
    with pytest.raises(InsightClosedError):
        await waiter
    assert not adapter._cache
    assert not adapter._inflight


async def test_closing_an_idle_populated_adapter_discards_cached_account_data(account):
    """Cached metadata does not outlive integration unload."""
    adapter = AccountInsights(account)
    result = await adapter.hardware()
    assert result["data"]["feeders"][0]["videoQuality"] == "K_2"
    assert result["data"]["feeders"][0]["cameraFieldOfView"] == "WIDE"
    await adapter.async_close()
    assert not adapter._cache
    with pytest.raises(InsightClosedError):
        await adapter.hardware()
    account._make_request.assert_awaited_once()


async def test_language_change_before_load_cannot_cache_wrong_language(
    account,
):
    """The cache key's language must still hold when the shared task begins."""
    adapter = AccountInsights(account)
    pending = asyncio.create_task(adapter.species("species-cardinal"))
    # The caller captures the key and schedules _load; this task runs before
    # that newly scheduled loader, reproducing the original wrong-key cache.
    await asyncio.sleep(0)
    account.language_code = "es"
    with pytest.raises(InsightLanguageError, match="Language changed"):
        await pending
    account._make_request.assert_not_called()
    assert not adapter._cache
    account.language_code = "en"
    assert (await adapter.species("species-cardinal"))["status"] == "available"
    account._make_request.assert_awaited_once()


async def test_language_change_during_shared_read_discards_result_for_every_waiter(
    account, payloads
):
    """A changed language cannot be published or retained under the old key."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(**kwargs: object):
        entered.set()
        await release.wait()
        return payloads["SPECIES"]

    account._make_request.side_effect = read
    adapter = AccountInsights(account)
    waiters = [
        asyncio.create_task(adapter.species("species-cardinal")) for _ in range(2)
    ]
    await entered.wait()
    account.language_code = "es"
    release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)
    assert all(isinstance(result, InsightLanguageError) for result in results)
    assert not adapter._cache
    account._make_request.assert_awaited_once()
    account.language_code = "en"
    assert (await adapter.species("species-cardinal"))["status"] == "available"
    assert account._make_request.await_count == 2


async def test_language_change_after_load_before_waiter_return_is_rejected(account):
    """A valid cached result is not returned into a newly selected language."""
    adapter = AccountInsights(account)
    finish = adapter._finished

    def changed_before_waiter(key, task):
        finish(key, task)
        account.language_code = "es"

    adapter._finished = changed_before_waiter
    with pytest.raises(InsightLanguageError):
        await adapter.species("species-cardinal")
    account.language_code = "en"
    assert (await adapter.species("species-cardinal"))["status"] == "available"
    account._make_request.assert_awaited_once()
