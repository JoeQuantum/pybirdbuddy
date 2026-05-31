import json
from unittest.mock import AsyncMock

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import NoResponseError


def _content_type_error() -> aiohttp.ContentTypeError:
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
    graphql_mock.side_effect = error
    with pytest.raises(NoResponseError):
        await bbclient._make_request(query="{ me { id } }", auth=False)
