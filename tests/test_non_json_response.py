"""Preserve the fork's transport-error compatibility contract."""

import json
from unittest.mock import AsyncMock

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
import pytest
from yarl import URL

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import NoResponseError


def _content_type_error() -> aiohttp.ContentTypeError:
    """Model the HTML error pages returned by an unhealthy API gateway."""
    request_info = aiohttp.RequestInfo(
        url=URL("https://bird.buddy/api"),
        method="POST",
        headers=CIMultiDictProxy(CIMultiDict()),
        real_url=URL("https://bird.buddy/api"),
    )
    return aiohttp.ContentTypeError(
        request_info=request_info,
        history=(),
        message="Attempt to decode JSON with unexpected mimetype: text/html",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _content_type_error(),
        json.JSONDecodeError("Expecting value", "<html>not json</html>", 0),
        aiohttp.ClientError("connection lost"),
    ],
    ids=["content_type_error", "json_decode_error", "client_error"],
)
async def test_non_json_response_raises_no_response_error(
    bbclient: BirdBuddy,
    graphql_mock: AsyncMock,
    error: Exception,
):
    """Network and non-JSON failures retain a common public error and cause."""
    graphql_mock.side_effect = error
    with pytest.raises(NoResponseError) as exc_info:
        await bbclient._make_request(query="{ me { id } }", auth=False)
    assert exc_info.value.__cause__ is error
    assert graphql_mock.await_count == 2
