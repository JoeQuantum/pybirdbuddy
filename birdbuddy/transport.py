"""Bounded GraphQL HTTP transport with status-aware, payload-free errors."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math

import aiohttp
from python_graphql_client import GraphqlClient

from birdbuddy.exceptions import NoResponseError


class HttpResponseError(NoResponseError):
    """An HTTP failure; headers may advise a retry but bodies are not logged."""

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        """Store a status and optional delay, without a URL or response body."""
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"Bird Buddy HTTP status {status}")


def retry_after_seconds(value: str | None) -> float | None:
    """Parse Retry-After seconds or an HTTP date; reject invalid/nonfinite values."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class BirdBuddyGraphqlClient(GraphqlClient):
    """Keep the existing client interface while enforcing HTTP boundaries.

    The upstream transport does not inspect status codes and creates one
    session per request. Retain that lifecycle to avoid introducing a required
    client close method, but bound each request and reject error responses
    before parsing them. Retry decisions belong to the operation caller.
    """

    async def execute_async(
        self,
        query: str,
        variables: dict | None = None,
        operation_name: str | None = None,
        headers: dict | None = None,
    ) -> dict:
        """Execute once; never replay a possibly completed mutation."""
        body = {"query": query, "variables": variables or {}}
        if operation_name:
            body["operationName"] = operation_name
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(
                self.endpoint,
                json=body,
                headers={**self.headers, **(headers or {})},
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                raise HttpResponseError(
                    response.status,
                    retry_after_seconds(response.headers.get("Retry-After")),
                )
            data = await response.json()
            if not isinstance(data, dict):
                raise NoResponseError
            return data
