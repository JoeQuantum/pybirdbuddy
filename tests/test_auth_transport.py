"""Authentication recovery and replay safety at the client request boundary."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import (
    AuthenticationFailedError,
    GraphqlError,
    MutationOutcomeUnknownError,
    NoResponseError,
    SocialAuthenticationRequiredError,
    UnexpectedResponseError,
)
from birdbuddy.transport import HttpResponseError

READ = "query Read { me { id } }"
WRITE = "mutation Write { postcardCollect { __typename } }"
EXPIRED = {"extensions": {"code": "AUTH_TOKEN_EXPIRED_ERROR"}}
REFRESHED = {
    "data": {"authRefreshToken": {"accessToken": "new", "refreshToken": "new-refresh"}}
}


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (
            {"__typename": "Problem", "items": [{"kind": "NO_MATCH"}]},
            AuthenticationFailedError,
        ),
        (
            {"__typename": "AuthUseSocialProblem", "socialProviders": ["GOOGLE"]},
            SocialAuthenticationRequiredError,
        ),
        ({"__typename": "FutureAuthResult"}, UnexpectedResponseError),
        (None, UnexpectedResponseError),
    ],
)
async def test_sign_in_union_failures_are_typed(graphql_mock, result, error):
    """Valid non-Auth union members never become a missing-token KeyError."""
    graphql_mock.return_value = {"data": {"authEmailSignIn": result}}
    client = BirdBuddy("example@example.invalid", "dummy")
    with pytest.raises(error):
        await client.refresh()
    assert client._access_token is None
    assert client._refresh_token is None


async def test_rejected_login_is_shared_by_concurrent_requests(graphql_mock):
    """A persistent failure causes one login, including later requests."""
    graphql_mock.return_value = {"data": {"authEmailSignIn": {"__typename": "Problem"}}}
    client = BirdBuddy("example@example.invalid", "dummy")
    failures = await asyncio.gather(
        *(client.refresh() for _ in range(20)), return_exceptions=True
    )
    assert all(isinstance(f, AuthenticationFailedError) for f in failures)
    assert graphql_mock.await_count == 1


@pytest.mark.parametrize("multiple", [False, True])
async def test_expired_retry_preserves_subscript(bbclient, graphql_mock, multiple):
    """Token expiry inside multiple errors still recovers a safe read."""
    errors = [EXPIRED]
    if multiple:
        errors.append({"extensions": {"code": "INTERNAL_SERVER_ERROR"}})
    graphql_mock.side_effect = [
        {"errors": errors},
        REFRESHED,
        {"data": {"me": {"id": "same-account"}}},
    ]
    result = await bbclient._make_request(READ, subscript="me")
    assert result == {"id": "same-account"}
    assert graphql_mock.await_count == 3


async def test_expired_concurrent_reads_refresh_only_once(bbclient, graphql_mock):
    """Late failures from an old token do not invalidate a newly refreshed token."""
    refreshes = 0

    async def execute(*, query, variables, headers):
        nonlocal refreshes
        if "authRefreshToken" in query:
            refreshes += 1
            await asyncio.sleep(0.01)
            return REFRESHED
        if headers["Authorization"] == "Bearer access":
            await asyncio.sleep(0)
            return {"errors": [EXPIRED]}
        return {"data": {"me": {"id": "same-account"}}}

    graphql_mock.side_effect = execute
    results = await asyncio.gather(*(bbclient._make_request(READ) for _ in range(20)))
    assert refreshes == 1
    assert all(r == {"me": {"id": "same-account"}} for r in results)


async def test_persistent_expiry_is_auth_failure_without_loop(bbclient, graphql_mock):
    """A second rejection opens the consumer's reauth path, with no retry storm."""
    graphql_mock.side_effect = [{"errors": [EXPIRED]}, REFRESHED, {"errors": [EXPIRED]}]
    with pytest.raises(AuthenticationFailedError):
        await bbclient._make_request(READ)
    with pytest.raises(AuthenticationFailedError):
        await bbclient._make_request(READ)
    assert graphql_mock.await_count == 3


@pytest.mark.parametrize(
    "response",
    [
        {"errors": [EXPIRED], "data": {"postcardCollect": {"id": "accepted"}}},
        {"errors": [EXPIRED, {"extensions": {"code": "INTERNAL_SERVER_ERROR"}}]},
    ],
)
async def test_partial_or_mixed_mutation_error_is_not_replayed(
    bbclient, graphql_mock, response
):
    """A token error is not proof every mutation field failed before execution."""
    graphql_mock.return_value = response
    with pytest.raises(MutationOutcomeUnknownError):
        await bbclient._make_request(WRITE)
    assert graphql_mock.await_count == 1


async def test_wholly_rejected_mutation_can_refresh_once(bbclient, graphql_mock):
    """A token-only rejection without data is distinct from an uncertain outcome."""
    graphql_mock.side_effect = [
        {"errors": [EXPIRED], "data": {"postcardCollect": None}},
        REFRESHED,
        {"data": {"postcardCollect": {"id": "accepted"}}},
    ]
    result = await bbclient._make_request(WRITE, subscript="postcardCollect")
    assert result == {"id": "accepted"}
    assert graphql_mock.await_count == 3


@pytest.mark.parametrize(
    "failure",
    [asyncio.TimeoutError(), aiohttp.ClientError("lost"), HttpResponseError(502)],
)
async def test_uncertain_mutation_transport_failure_is_never_replayed(
    bbclient, graphql_mock, failure
):
    """Timeout or connection loss can happen after server acceptance."""
    graphql_mock.side_effect = failure
    with pytest.raises(MutationOutcomeUnknownError) as raised:
        await bbclient._make_request(WRITE)
    assert raised.value.__cause__ is failure
    assert graphql_mock.await_count == 1


async def test_read_retry_after_is_respected_and_bounded(bbclient, graphql_mock):
    """One retry obeys the hint; repeated rate limits are returned to the caller."""
    graphql_mock.side_effect = HttpResponseError(429, 3)
    with (
        patch("birdbuddy.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(HttpResponseError),
    ):
        await bbclient._make_request(READ)
    sleep.assert_awaited_once_with(3)
    assert graphql_mock.await_count == 2


@pytest.mark.parametrize(("query", "delay"), [(READ, 600), (WRITE, 1)])
async def test_long_read_backoff_and_mutation_rate_limit_are_not_retried(
    bbclient, graphql_mock, query, delay
):
    """The caller receives long delays; writes are never rate-limit replayed."""
    graphql_mock.side_effect = HttpResponseError(429, delay)
    with (
        patch("birdbuddy.client.asyncio.sleep", new_callable=AsyncMock) as sleep,
        pytest.raises(HttpResponseError),
    ):
        await bbclient._make_request(query)
    sleep.assert_not_awaited()
    assert graphql_mock.await_count == 1


async def test_cancellation_propagates(bbclient, graphql_mock):
    """A cancelled HA operation cannot silently restart itself."""
    graphql_mock.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await bbclient._make_request(READ)
    assert graphql_mock.await_count == 1


async def test_transient_refresh_failure_preserves_tokens(bbclient, graphql_mock):
    """A network outage does not turn an existing account into invalid credentials."""
    bbclient._access_token = None
    graphql_mock.side_effect = aiohttp.ClientError("lost")
    with pytest.raises(NoResponseError):
        await bbclient._make_request(READ)
    assert bbclient._refresh_token == "refresh"
    assert bbclient._auth_failure is None


async def test_payloads_and_resolver_messages_are_not_logged(
    bbclient, graphql_mock, caplog
):
    """Debug logging cannot expose request secrets or signed media URLs."""
    secret = "https://example.invalid/private?Signature=dummy-secret"
    graphql_mock.side_effect = [
        {"data": {"me": {"contentUrl": secret}}},
        {
            "errors": [
                {"message": secret, "extensions": {"code": "INTERNAL_SERVER_ERROR"}}
            ]
        },
    ]
    with caplog.at_level(logging.DEBUG, logger="birdbuddy"):
        await bbclient._make_request(READ, variables={"password": secret})
        with pytest.raises(GraphqlError) as raised:
            await bbclient._make_request(READ)
    assert secret not in caplog.text
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
