"""Exercise HTTP status handling through an actual local aiohttp server."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from aiohttp import web
import pytest

from birdbuddy.transport import (
    BirdBuddyGraphqlClient,
    HttpResponseError,
    retry_after_seconds,
)


@pytest.mark.parametrize("status", [302, 401, 429, 502, 503])
async def test_http_error_body_cannot_be_mistaken_for_graphql_data(
    aiohttp_server, status
):
    """Even JSON error bodies preserve status and do not leak body content."""

    async def handler(request):
        return web.json_response(
            {"data": {"private": "secret"}},
            status=status,
            headers={"Retry-After": "12"},
        )

    app = web.Application()
    app.router.add_post("/graphql", handler)
    server = await aiohttp_server(app)
    client = BirdBuddyGraphqlClient(str(server.make_url("/graphql")))
    with pytest.raises(HttpResponseError) as raised:
        await client.execute_async("query { me { id } }")
    assert raised.value.status == status
    assert raised.value.retry_after == 12
    assert "secret" not in str(raised.value)


async def test_success_preserves_graphql_envelope_and_headers(aiohttp_server):
    """The caller retains control of auth and GraphQL error interpretation."""

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer dummy"
        body = await request.json()
        assert body["variables"] == {"id": "example"}
        assert body["operationName"] == "Account"
        return web.json_response({"data": {"me": {"id": "example"}}})

    app = web.Application()
    app.router.add_post("/graphql", handler)
    server = await aiohttp_server(app)
    client = BirdBuddyGraphqlClient(str(server.make_url("/graphql")))
    result = await client.execute_async(
        "query Account { me { id } }",
        variables={"id": "example"},
        operation_name="Account",
        headers={"Authorization": "Bearer dummy"},
    )
    assert result == {"data": {"me": {"id": "example"}}}


@pytest.mark.parametrize("value", [None, "", "invalid", "NaN", "inf"])
def test_invalid_retry_after(value):
    """Bad server hints cannot create unbounded sleeps."""
    assert retry_after_seconds(value) is None


def test_retry_after_supports_http_dates_and_nonnegative_seconds():
    """Both wire formats are accepted; a stale date is immediately eligible."""
    assert retry_after_seconds("-2") == 0
    assert retry_after_seconds("2.5") == 2.5
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60))
    seconds = retry_after_seconds(future)
    assert seconds is not None
    assert 58 <= seconds <= 60
