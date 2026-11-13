"""Bird Buddy client module."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
import json
from math import isfinite
import re
from time import monotonic
from typing import Any, TypeVar

import aiohttp
import langcodes
from typing_extensions import Unpack, deprecated

from birdbuddy import LOGGER, queries
from birdbuddy.const import BB_URL
from birdbuddy.exceptions import (
    AUTH_TOKEN_EXPIRED_ERROR,
    AuthenticationFailedError,
    GraphqlError,
    MutationOutcomeUnknownError,
    NoFirmwareUpdateAvailableError,
    NoResponseError,
    PostcardOutcomeUnknownError,
    SocialAuthenticationRequiredError,
    UnexpectedResponseError,
)
from birdbuddy.feed import (
    Feed,
    FeedBatch,
    FeedCheckpoint,
    FeedNode,
    FeedNodeType,
    PaginationError,
)
from birdbuddy.feeder import (
    Feeder,
    FeederControlError,
    FeederControlTimeoutError,
    FeederControlUnavailableError,
    FeederOptions,
    FeederUpdateStatus,
    PowerProfile,
    validate_feeder_options,
)
from birdbuddy.media import Collection, CollectionMedia, Media, is_media_expired
from birdbuddy.postcards import CollectedPostcard, CollectionReceipt, PostcardAnalysis
from birdbuddy.queries.debug import DUMP_SCHEMA
from birdbuddy.sightings import (
    PostcardSighting,
    Sighting,
    SightingCreateProgress,
    SightingFinishMod,
    SightingFinishStrategy,
    SightingReport,
)
from birdbuddy.transport import BirdBuddyGraphqlClient, HttpResponseError
from birdbuddy.user import BirdBuddyUser
from birdbuddy.visits import Visit, normalize_visit

_ControlResult = TypeVar("_ControlResult")
_CONTROL_POLL_INTERVAL = 0.25

_NO_VALUE = object()
"""Sentinel value to allow None to override a default value."""

_MAX_PAGE_SIZE = 100
"""The largest page size the API accepts; a larger ``first`` errors server
side (HTTP 200 with a GraphQL ``INTERNAL_SERVER_ERROR``)."""

_COLLECTIONS_TTL = 300
"""Maximum age of a successful account collection snapshot in seconds."""


def _is_read(query: str) -> bool:
    """Only ordinary query documents qualify for transient transport retries."""
    document = re.sub(r"#[^\n]*", "", query).lstrip()
    return document.startswith("{") or bool(re.match(r"query\b", document))


def _require_page_size(page_size: int) -> None:
    """Validate a pagination page size.

    Args:
        page_size: The requested per-page item count.

    Raises:
        ValueError: If ``page_size`` is not between 1 and the API's limit.
    """
    if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
        msg = f"page size must be between 1 and {_MAX_PAGE_SIZE}"
        raise ValueError(msg)


class BirdBuddy:
    """Bird Buddy api client."""

    graphql: BirdBuddyGraphqlClient
    _email: str | None
    _password: str | None
    _access_token: str | None
    _refresh_token: str | None
    _language_code: str
    _me: BirdBuddyUser | None
    _feeders: dict[str, Feeder]
    _collections: dict[str, Collection]
    _last_feed_date: datetime | None

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        /,
        refresh_token: str | None = None,
        access_token: str | None = None,
    ) -> None:
        """Initialize the Bird Buddy client.

        Args:
            email: Account email, for password login.
            password: Account password, for password login.
            refresh_token: An existing refresh token, to skip password login.
            access_token: An existing access token.
        """
        self._email = email
        self._password = password
        self._refresh_token = refresh_token
        self._access_token = access_token

        self.graphql = BirdBuddyGraphqlClient(BB_URL)
        self._auth_lock = asyncio.Lock()
        self._auth_failure: AuthenticationFailedError | None = None
        self._me = None
        self._last_feed_date = None
        self._feed_checkpoint = FeedCheckpoint()
        self._pending_feed_batch: FeedBatch | None = None
        self._feed_lock = asyncio.Lock()
        self._feeders = {}
        self._collections = {}
        self._collections_lock = asyncio.Lock()
        self._collections_loaded = False
        self._collections_updated_at = 0.0
        self._collections_account_id: str | None = None
        self._collections_type = "bird"
        self._collections_revision = 0
        self._collections_attempt = 0
        self._collections_error: tuple[str | None, str, Exception] | None = None
        self.language_code = "en"

    def _save_me(self, me_data: dict) -> bool:
        if not me_data:
            return False
        me_data["__last_updated"] = datetime.now()
        self._me = BirdBuddyUser(me_data["user"])
        # pylint: disable=invalid-name
        for f in me_data.get("feeders", []):
            if f["id"] in self._feeders:
                # Refresh Feeder data inline
                self._feeders[f["id"]].update(f)
            else:
                self._feeders[f["id"]] = Feeder(f)
        return True

    def _needs_login(self) -> bool:
        return self._refresh_token is None

    def _needs_refresh(self) -> bool:
        return self._access_token is None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept-Language": self._language_code,
        }

    def _clear(self) -> None:
        self._access_token = None
        self._refresh_token = None
        self._me = None

    async def dump_schema(self) -> dict:
        """For debugging purposes: dump the entire GraphQL schema."""
        return await self._make_request(query=DUMP_SCHEMA, auth=False)

    async def _check_auth(self, expired_token: str | None = None) -> bool:
        """Serialize token changes and reuse a refresh completed by another task."""
        async with self._auth_lock:
            if self._auth_failure is not None:
                raise self._auth_failure
            if expired_token is not None and self._access_token == expired_token:
                self._access_token = None
            try:
                if self._needs_login():
                    return await self._login()
                if self._needs_refresh():
                    await self._refresh_access_token()
            except AuthenticationFailedError as err:
                # Persistent rejection must not make every queued request log in.
                self._auth_failure = err
                raise
            return self._access_token is not None

    def _save_tokens(self, result: dict) -> None:
        """Replace a token pair only after validating both values."""
        access = result.get("accessToken")
        refresh = result.get("refreshToken")
        if not isinstance(access, str) or not access:
            raise UnexpectedResponseError
        if not isinstance(refresh, str) or not refresh:
            raise UnexpectedResponseError
        self._access_token = access
        self._refresh_token = refresh

    async def _login(self) -> bool:
        """Handle every current email sign-in union without leaking credentials."""
        if not (self._email and self._password):
            msg = "Email and password are required to sign in"
            raise AuthenticationFailedError(msg)
        data = await self._make_request(
            query=queries.auth.SIGN_IN,
            variables={
                "emailSignInInput": {"email": self._email, "password": self._password}
            },
            auth=False,
        )
        result = data.get("authEmailSignIn")
        if not isinstance(result, dict):
            raise UnexpectedResponseError
        kind = result.get("__typename")
        if kind == "AuthUseSocialProblem":
            raise SocialAuthenticationRequiredError(result.get("socialProviders") or [])
        if kind == "Problem":
            msg = "Bird Buddy rejected the email sign-in details"
            raise AuthenticationFailedError(msg)
        if kind not in (None, "Auth"):
            raise UnexpectedResponseError
        profile = result.get("me")
        if not isinstance(profile, dict) or not isinstance(profile.get("user"), dict):
            raise UnexpectedResponseError
        self._save_tokens(result)
        return self._save_me(profile)

    async def _refresh_access_token(self) -> bool:
        """Refresh once; transient failures preserve the last known token pair."""
        if not self._refresh_token:
            msg = "A refresh token is required"
            raise AuthenticationFailedError(msg)
        data = await self._make_request(
            query=queries.auth.REFRESH_AUTH_TOKEN,
            variables={"refreshTokenInput": {"token": self._refresh_token}},
            auth=False,
        )
        tokens = data.get("authRefreshToken")
        if not isinstance(tokens, dict):
            raise UnexpectedResponseError
        self._save_tokens(tokens)
        LOGGER.debug("Access token refreshed")
        return True

    async def _execute_request(
        self, query: str, variables: dict, headers: dict
    ) -> dict:
        """Bound transient read retries; never retry an uncertain mutation here."""
        read = _is_read(query)
        for attempt in range(2):
            try:
                return await asyncio.wait_for(
                    self.graphql.execute_async(
                        query=query, variables=variables, headers=headers
                    ),
                    timeout=30,
                )
            except HttpResponseError as err:
                # 429 is a rejection, but only reads qualify for automatic retry.
                delay = err.retry_after if err.retry_after is not None else 1.0
                retryable = err.status in {429, 502, 503, 504}
                if read and attempt == 0 and retryable and delay <= 30:
                    await asyncio.sleep(delay)
                    continue
                if not read and err.status >= 500:
                    raise MutationOutcomeUnknownError from err
                raise
            except (
                aiohttp.ClientError,
                json.JSONDecodeError,
                asyncio.TimeoutError,
                NoResponseError,
            ) as err:
                if read and attempt == 0:
                    await asyncio.sleep(1)
                    continue
                error_type = NoResponseError if read else MutationOutcomeUnknownError
                raise error_type from err
        raise NoResponseError  # The bounded loop always returns or raises.

    @staticmethod
    def _response_errors(response: dict) -> list[dict]:
        """Validate the error envelope before interpreting authentication failures."""
        errors = response.get("errors") or []
        if not isinstance(errors, list) or any(not isinstance(e, dict) for e in errors):
            raise UnexpectedResponseError
        return errors

    async def _make_request(
        self,
        query: str,
        variables: dict | None = None,
        auth: bool = True,
        reauth: bool = True,
        subscript: str | None = None,
    ) -> dict:
        """Execute with bounded auth recovery and preserve the requested shape.

        Raw request variables, responses and resolver messages are deliberately
        absent from logs. Partial mutation responses are never replayed merely
        because another field also reported an expired token.
        """
        if auth:
            await self._check_auth()
        headers = self._headers() if auth else {}
        sent_token = self._access_token
        LOGGER.debug("Sending GraphQL %s", "read" if _is_read(query) else "mutation")
        try:
            response = await self._execute_request(query, variables or {}, headers)
        except HttpResponseError as err:
            if err.status != 401:
                raise
            response = {"errors": [{"extensions": {"code": AUTH_TOKEN_EXPIRED_ERROR}}]}
        if not isinstance(response, dict) or not response:
            if _is_read(query):
                raise NoResponseError
            raise MutationOutcomeUnknownError
        errors = self._response_errors(response)
        expired = [
            e for e in errors if GraphqlError(e).error_code == AUTH_TOKEN_EXPIRED_ERROR
        ]
        if expired:
            partial_data = response.get("data")
            has_data = isinstance(partial_data, dict) and any(
                value is not None for value in partial_data.values()
            )
            safe_retry = _is_read(query) or (
                len(expired) == len(errors) and not has_data
            )
            if auth and reauth and safe_retry:
                await self._check_auth(expired_token=sent_token)
                return await self._make_request(
                    query=query,
                    variables=variables,
                    auth=auth,
                    reauth=False,
                    subscript=subscript,
                )
            if not safe_retry:
                raise MutationOutcomeUnknownError
            failure = AuthenticationFailedError(
                "Bird Buddy authentication was rejected"
            )
            self._auth_failure = failure
            raise failure
        GraphqlError.raise_errors(errors)
        result = response.get("data")
        if not isinstance(result, dict):
            raise UnexpectedResponseError
        if subscript:
            selected = result.get(subscript)
            if not isinstance(selected, dict):
                raise UnexpectedResponseError
            return selected
        return result

    async def _iter_pages(
        self,
        query: str,
        variables: dict[str, Any],
        connection: Callable[[dict], dict],
        *,
        max_pages: int = 100,
    ) -> AsyncIterator[dict]:
        """Yield validated forward pages or fail explicitly on incomplete reads.

        Cursor repetition, malformed page metadata and exhaustion of the page
        budget raise PaginationError. Consumers must not commit partial output.
        """
        if type(max_pages) is not int or max_pages < 1:
            message = "max_pages must be a positive integer"
            raise ValueError(message)
        after = variables.get("after")
        seen = {after} if after is not None else set()
        for _ in range(max_pages):
            page_vars = {
                key: value for key, value in variables.items() if key != "after"
            }
            if after is not None:
                page_vars["after"] = after
            data = await self._make_request(query=query, variables=page_vars)
            try:
                page = connection(data)
            except (KeyError, TypeError) as err:
                message = "Missing pagination connection"
                raise PaginationError(message) from err
            if not isinstance(page, dict):
                message = "Malformed pagination connection"
                raise PaginationError(message)
            info = page.get("pageInfo")
            if not isinstance(info, dict) or type(info.get("hasNextPage")) is not bool:
                message = "Missing pagination completeness metadata"
                raise PaginationError(message)
            cursor = info.get("endCursor")
            if info["hasNextPage"] and (
                not isinstance(cursor, str) or not cursor or cursor in seen
            ):
                message = "Pagination cursor is missing or repeated"
                raise PaginationError(message)
            yield page
            if not info["hasNextPage"]:
                return
            seen.add(cursor)
            after = cursor
        message = "Pagination page limit exceeded"
        raise PaginationError(message)

    @property
    def user(self) -> BirdBuddyUser | None:
        """The logged in user data."""
        return self._me

    @property
    def language_code(self) -> str:
        """The current language code."""
        return self._language_code

    @language_code.setter
    def language_code(self, language_code: str) -> None:
        """Set the language used in API requests.

        Useful for localized responses, including translated bird species
        names.

        Args:
            language_code: A BCP 47 language tag (e.g. ``"de"``), normalized
                via ``langcodes.standardize_tag``.
        """
        self._language_code = langcodes.standardize_tag(language_code)

    async def refresh(self) -> bool:
        """Refresh the Bird Buddy feeder data."""
        data = await self._make_request(query=queries.me.ME)
        LOGGER.debug("Feeder data refreshed successfully")
        return self._save_me(data["me"])

    async def _with_control_timeout(
        self,
        operation: Callable[[dict[str, bool]], Awaitable[_ControlResult]],
        timeout: float,
    ) -> _ControlResult:
        """Bound role lookup, the mutation, progress checks and final refresh."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not isfinite(timeout)
            or timeout <= 0
        ):
            msg = "Control timeout must be positive and finite"
            raise ValueError(msg)
        progress = {"completed": False}
        try:
            return await asyncio.wait_for(operation(progress), timeout=timeout)
        except asyncio.TimeoutError as err:
            msg = "Device control timed out; refresh state before retrying"
            raise FeederControlTimeoutError(
                msg,
                action_completed=True if progress["completed"] else None,
            ) from err

    async def _control_feeder(self, feeder: Feeder | str) -> Feeder:
        """Resolve an explicit owner role before any mutation."""
        if isinstance(feeder, Feeder):
            selected = self._feeders.get(feeder.id, feeder)
            feeder_id = feeder.id
        elif isinstance(feeder, str) and feeder:
            feeder_id = feeder
            selected = self._feeders.get(feeder_id)
        else:
            msg = "A feeder object or non-empty ID is required"
            raise ValueError(msg)
        if selected is None or not selected.get("__typename"):
            await self.refresh()
            selected = self._feeders.get(feeder_id)
        if selected is None or not selected.is_owner:
            msg = "Device control requires an owner account"
            raise FeederControlUnavailableError(msg, action_completed=False)
        self._feeders.setdefault(feeder_id, selected)
        return selected

    def _cache_control_result(
        self, feeder: Feeder, result: dict, root: str, field: str
    ) -> str:
        """Validate a union result, preserving only the returned observed state."""
        prefix = root[0].upper() + root[1:]
        kind = result.get("__typename")
        if not isinstance(kind, str) or kind not in {
            prefix + suffix
            for suffix in ("FinishedResult", "InProgressResult", "FailedResult")
        }:
            msg = "Unrecognized device control result"
            raise FeederControlError(msg)
        state = result.get("feeder")
        if not isinstance(state, dict) or state.get("id", feeder.id) != feeder.id:
            msg = "Device control returned an invalid feeder"
            raise FeederControlError(msg)
        expected_type = str if field == "powerProfile" else bool
        if (
            type(state.get(field)) is not expected_type
            or state.get("__typename", "FeederForOwner") != "FeederForOwner"
        ):
            msg = "Device control returned invalid state or role"
            raise FeederControlError(msg)
        feeder.update(state)
        if kind == prefix + "FailedResult":
            msg = "Device reported control failure"
            raise FeederControlError(msg, action_completed=False)
        return kind

    async def _confirm_control_state(self, progress: dict[str, bool]) -> None:
        """A completed mutation is not repeated if its final refresh fails."""
        progress["completed"] = True
        try:
            refreshed = await self.refresh()
        except Exception as err:
            msg = "Device action completed, but final state refresh failed"
            raise FeederControlError(
                msg,
                action_completed=True,
            ) from err
        if not refreshed:
            msg = "Device action completed, but final refresh returned no account state"
            raise FeederControlError(
                msg,
                action_completed=True,
            )

    async def _control_operation(
        self,
        feeder: Feeder | str,
        field: str,
        value: bool | str,
        progress: dict[str, bool],
    ) -> Feeder:
        """Issue a control once, then use its actual API progress operation."""
        selected = await self._control_feeder(feeder)
        if field == "audioEnabled" and selected.supports_audio is False:
            msg = "Audio is unavailable on this feeder"
            raise FeederControlUnavailableError(msg, action_completed=False)
        operations = {
            "offGrid": (
                queries.feeder.TOGGLE_OFF_GRID,
                queries.feeder.TOGGLE_OFF_GRID_CHECK,
                "feederToggleOffGrid",
            ),
            "audioEnabled": (
                queries.feeder.TOGGLE_AUDIO_ENABLED,
                queries.feeder.TOGGLE_AUDIO_ENABLED_CHECK,
                "feederToggleAudio",
            ),
            "powerProfile": (
                queries.feeder.UPDATE_POWER_PROFILE,
                queries.feeder.UPDATE_POWER_PROFILE_CHECK,
                "feederUpdatePowerProfile",
            ),
        }
        query, check_query, root = operations[field]
        result = await self._make_request(
            query=query,
            variables={"feederId": selected.id, root + "Input": {field: value}},
            subscript=root,
        )
        while True:
            kind = self._cache_control_result(selected, result, root, field)
            if kind.endswith("FinishedResult"):
                break
            await asyncio.sleep(_CONTROL_POLL_INTERVAL)
            result = await self._make_request(
                query=check_query,
                variables={"feederId": selected.id},
                subscript=root + "Check",
            )
        await self._confirm_control_state(progress)
        current = self._feeders.get(selected.id)
        if current is None or current.get(field) != value:
            msg = "Completed control did not confirm the requested state"
            raise FeederControlError(msg, action_completed=True)
        return current

    async def toggle_off_grid(
        self, feeder: Feeder | str, is_off_grid: bool, *, timeout: float = 30.0
    ) -> Feeder:
        """Set off-grid mode once and confirm completion within one time budget."""
        if type(is_off_grid) is not bool:
            msg = "off-grid requires a bool value"
            raise ValueError(msg)
        return await self._with_control_timeout(
            lambda progress: self._control_operation(
                feeder, "offGrid", is_off_grid, progress
            ),
            timeout,
        )

    async def toggle_audio_enabled(
        self, feeder: Feeder | str, is_audio_enabled: bool, *, timeout: float = 30.0
    ) -> Feeder:
        """Set audio once, respecting explicit owner/capability information."""
        if type(is_audio_enabled) is not bool:
            msg = "audio-enabled requires a bool value"
            raise ValueError(msg)
        return await self._with_control_timeout(
            lambda progress: self._control_operation(
                feeder, "audioEnabled", is_audio_enabled, progress
            ),
            timeout,
        )

    async def feed(
        self,
        first: int = 20,
        after: str | None = None,
        last: int | None = None,
        before: str | None = None,
    ) -> Feed:
        """Read one forward page; reject unsupported backward arguments.

        ``first`` is 1-100. The default initial window is explicitly 20 items.
        ``last`` and ``before`` are unsupported, rather than silently ignored.
        """
        _require_page_size(first)
        if last is not None or before is not None:
            message = "Backward feed pagination is unsupported"
            raise ValueError(message)
        variables: dict[str, Any] = {"first": first}
        if after is not None:
            variables["after"] = after
        data = await self._make_request(query=queries.me.FEED, variables=variables)
        try:
            page = data["me"]["feed"]
        except (KeyError, TypeError) as err:
            message = "Missing feed connection"
            raise PaginationError(message) from err
        if not isinstance(page, dict):
            message = "Malformed feed connection"
            raise PaginationError(message)
        return Feed(page)

    def _feed_pages(self, *, max_pages: int = 100) -> AsyncIterator[dict]:
        """Iterate feed connection pages, newest first, with a finite budget."""
        return self._iter_pages(
            query=queries.me.FEED,
            variables={"first": _MAX_PAGE_SIZE},
            connection=lambda data: data["me"]["feed"],
            max_pages=max_pages,
        )

    def export_feed_checkpoint(self) -> dict[str, Any]:
        """Export accepted state only; a pending batch never advances it."""
        return self._feed_checkpoint.to_dict()

    def import_feed_checkpoint(self, state: dict[str, Any]) -> None:
        """Restore persisted state before polling; reject active intake changes."""
        if self._pending_feed_batch is not None or self._feed_lock.locked():
            message = "Cannot replace a checkpoint during pending feed intake"
            raise ValueError(message)
        checkpoint = FeedCheckpoint.from_dict(state)
        self._feed_checkpoint = checkpoint
        self._last_feed_date = checkpoint.watermark

    def ack_feed_batch(self, batch_id: str) -> dict[str, Any]:
        """Accept a batch only after the caller has durably persisted intake.

        Persist the batch's proposed checkpoint together with its work queue,
        then acknowledge. A restart can restore that atomically stored state.
        No durable storage or automatic acknowledgement is provided here.
        """
        batch = self._pending_feed_batch
        if batch is None or batch.batch_id != batch_id:
            message = "No pending feed batch matches this acknowledgement"
            raise ValueError(message)
        self._feed_checkpoint = batch.checkpoint
        self._last_feed_date = batch.checkpoint.watermark
        self._pending_feed_batch = None
        return self.export_feed_checkpoint()

    async def _read_feed_intake(
        self,
        cutoff: datetime | None,
        *,
        backfill: bool,
        initial_limit: int,
        max_pages: int,
    ) -> list[FeedNode]:
        """Finish the requested traversal before proposing any checkpoint."""
        if cutoff is None and not backfill:
            return (await self.feed(first=initial_limit)).validated_nodes()
        nodes: dict[str, FeedNode] = {}
        async for page in self._feed_pages(max_pages=max_pages):
            page_nodes = Feed(page).validated_nodes()
            for node in page_nodes:
                created = node.created_at
                if cutoff is None or created is None or created >= cutoff:
                    nodes.setdefault(node.node_id, node)
            dates = [node.created_at for node in page_nodes if node.created_at]
            # Equal timestamps can span pages: stop only at a strictly older
            # item. Null/unknown timestamps never terminate traversal.
            if cutoff is not None and dates and min(dates) < cutoff:
                break
        return list(nodes.values())

    async def retrieve_feed_batch(
        self,
        *,
        since: datetime | str | None = _NO_VALUE,  # type: ignore[assignment]
        backfill: bool = False,
        initial_limit: int = 20,
        max_pages: int = 100,
    ) -> FeedBatch:
        """Retrieve replayable intake without accepting its proposed watermark.

        Calls replay the pending batch until explicit acknowledgement, even if
        different arguments are supplied. First use reads the latest 20 items
        unless ``backfill=True``. Later calls scan a five-minute overlap and
        deduplicate accepted IDs. Late arrivals older than that window require
        explicit backfill. ``since`` is an inclusive explicit lower bound;
        ``backfill=True`` with the default since scans all history.

        Incomplete pagination and capacity limits fail without changing either
        accepted state or pending intake. Consumers must persist nodes and
        ``batch.checkpoint.to_dict()`` atomically before ack_feed_batch().
        """
        _require_page_size(initial_limit)
        if type(max_pages) is not int or max_pages < 1:
            message = "max_pages must be a positive integer"
            raise ValueError(message)
        async with self._feed_lock:
            if self._pending_feed_batch is not None:
                return self._pending_feed_batch
            cutoff = None if backfill else self._feed_checkpoint.cutoff
            if since is not _NO_VALUE:
                cutoff = (
                    FeedNode.parse_datetime(since) if isinstance(since, str) else since
                )
                if cutoff is not None and (
                    not isinstance(cutoff, datetime) or cutoff.tzinfo is None
                ):
                    message = "since must be a timezone-aware datetime"
                    raise ValueError(message)
                if isinstance(since, str) and cutoff is None:
                    message = "since must contain a valid timestamp"
                    raise ValueError(message)
            nodes = await self._read_feed_intake(
                cutoff,
                backfill=backfill,
                initial_limit=initial_limit,
                max_pages=max_pages,
            )
            seen = dict(self._feed_checkpoint.seen)
            unique: dict[str, FeedNode] = {}
            for node in nodes:
                if node.node_id not in seen:
                    unique.setdefault(node.node_id, node)
            new_nodes = list(unique.values())
            checkpoint = self._feed_checkpoint.advance(new_nodes)
            batch = FeedBatch.create(new_nodes, checkpoint)
            self._pending_feed_batch = batch
            return batch

    async def refresh_feed(
        self,
        since: datetime | str | None = _NO_VALUE,  # type: ignore[assignment]
    ) -> list[FeedNode]:
        """Compatibility list wrapper; it never auto-acknowledges intake.

        Use retrieve_feed_batch() and ack_feed_batch() for ongoing polling.
        Explicit ``since`` retains this legacy wrapper's strict comparison;
        the batch interface includes equal timestamps for lossless boundaries.
        """
        batch = await self.retrieve_feed_batch(since=since)
        nodes = list(batch.nodes)
        if since is _NO_VALUE or since is None:
            return nodes
        cutoff = FeedNode.parse_datetime(since) if isinstance(since, str) else since
        if cutoff is None:
            return nodes
        return [node for node in nodes if node.created_at and node.created_at > cutoff]

    async def feed_nodes(
        self, node_type: FeedNodeType, *, max_pages: int = 100
    ) -> list[FeedNode]:
        """Read matching nodes across all pages, deduplicating overlapping IDs."""
        nodes: dict[str, FeedNode] = {}
        async for page in self._feed_pages(max_pages=max_pages):
            for node in Feed(page).validated_nodes():
                if node.node_type is node_type:
                    nodes.setdefault(node.node_id, node)
        return list(nodes.values())

    async def new_postcards(self) -> list[FeedNode]:
        """Return all new 'Postcard' feed items.

        These node types are converted into sightings using
        ``sighting_from_postcard``.
        """
        return await self.feed_nodes(FeedNodeType.NewPostcard)

    async def get_feed_item(self, feed_item_id: str) -> Visit:
        """Read complete selected media/species without identification or collection."""
        if not isinstance(feed_item_id, str) or not feed_item_id:
            msg = "A nonempty feed item ID is required"
            raise ValueError(msg)
        result = await self._make_request(
            query=queries.visits.DETAIL,
            variables={"feedItemId": feed_item_id},
            subscript="feedItem",
        )
        if result.get("id") != feed_item_id:
            raise UnexpectedResponseError
        return normalize_visit(result, known_feeder_ids=tuple(self._feeders))

    def _postcard_id(self, postcard: str | FeedNode) -> str:
        """Resolve a postcard argument to its feed-item id.

        Args:
            postcard: A postcard feed-item id, or a ``NewPostcard`` FeedNode.

        Returns:
            The feed-item id.

        Raises:
            ValueError: If ``postcard`` is a FeedNode that is not a
                NewPostcard.
            TypeError: If ``postcard`` is neither a str nor a FeedNode.
        """
        if isinstance(postcard, str):
            if not postcard:
                msg = "A nonempty postcard ID is required"
                raise ValueError(msg)
            return postcard
        if isinstance(postcard, FeedNode):
            if postcard.node_type != FeedNodeType.NewPostcard:
                msg = f"expected a NewPostcard, got {postcard.node_type}"
                raise ValueError(msg)
            return postcard.node_id
        msg = f"postcard must be a str or FeedNode, got {type(postcard)}"
        raise TypeError(msg)

    async def identify_postcard(
        self,
        postcard: str | FeedNode,
    ) -> PostcardAnalysis:
        """Explicitly run inference and return the resulting new-postcard analysis.

        This is a mutation even when no collection occurs. Display refreshes use
        get_feed_item instead. No claim of idempotence or automatic replay is
        made when the server outcome is uncertain.
        """
        postcard_id = self._postcard_id(postcard)
        result = await self._make_request(
            query=queries.birds.POSTCARD_REANALYZE,
            variables={"feedItemId": postcard_id},
        )
        try:
            updated = result["inferenceExternalPostcardReanalyze"]["updatedFeedItem"]
        except (KeyError, TypeError) as err:
            raise UnexpectedResponseError from err
        if (
            not isinstance(updated, dict)
            or updated.get("id") != postcard_id
            or updated.get("__typename") != "FeedItemNewPostcard"
        ):
            raise UnexpectedResponseError
        return PostcardAnalysis(updated)

    @deprecated("reanalyze_postcard is deprecated; use identify_postcard")
    async def reanalyze_postcard(
        self,
        postcard: str | FeedNode,
    ) -> dict:
        """Trigger the AI identification (reanalysis) for a postcard.

        Deprecated since 0.0.22: use :func:`identify_postcard`, which returns a
        ``PostcardAnalysis``. This returns the raw
        ``inferenceExternalPostcardReanalyze`` payload unchanged.

        Args:
            postcard: A postcard feed-item id, or a ``NewPostcard`` FeedNode.

        Returns:
            The ``inferenceExternalPostcardReanalyze`` result payload.

        Raises:
            ValueError: If ``postcard`` is a FeedNode that is not a
                NewPostcard.
        """
        variables = {"feedItemId": self._postcard_id(postcard)}
        result = await self._make_request(
            query=queries.birds.POSTCARD_REANALYZE,
            variables=variables,
        )
        return result["inferenceExternalPostcardReanalyze"]

    async def _reconcile_collection(
        self,
        postcard_id: str,
        *,
        share: bool,
    ) -> CollectedPostcard:
        """Confirm only an explicit collected read result; never guess from absence.

        Collection can replace the feed item ID. If the original ID no longer
        resolves, that is not proof of success or failure. Sharing is not
        established by a read of the collected postcard.
        """
        try:
            visit = await self.get_feed_item(postcard_id)
        except (NoResponseError, UnexpectedResponseError, GraphqlError) as err:
            raise PostcardOutcomeUnknownError(
                postcard_id, share_requested=share
            ) from err
        if visit.kind != "FeedItemCollectedPostcard":
            raise PostcardOutcomeUnknownError(postcard_id, share_requested=share)
        return CollectedPostcard(
            visit.raw,
            receipt=CollectionReceipt(
                postcard_id,
                reconciled_by_read=True,
                share_requested=share,
            ),
        )

    @staticmethod
    def _collected_result(
        result: dict,
        postcard_id: str,
        *,
        share: bool,
    ) -> CollectedPostcard:
        """Prefer the replacement envelope, retaining the first-collection flag."""
        envelope = result.get("postcardCollect")
        if not isinstance(envelope, dict):
            raise UnexpectedResponseError
        details = envelope.get("postcardCollectedDetails")
        first = None
        if isinstance(details, dict):
            # Older selected captures requested only the boolean in details.
            # An explicitly null replacement remains invalid, not a fallback.
            collected = details.get(
                "collectedPostcard", envelope.get("collectedPostcard")
            )
            first = details.get("isFirstCollectedPostcard")
            if type(first) is not bool:
                raise UnexpectedResponseError
        elif details is None:
            # Historical responses may still populate only the deprecated field.
            collected = envelope.get("collectedPostcard")
        else:
            raise UnexpectedResponseError
        if not isinstance(collected, dict):
            raise UnexpectedResponseError
        return CollectedPostcard(
            collected,
            receipt=CollectionReceipt(
                postcard_id,
                is_first_collected_postcard=first,
                share_requested=share,
            ),
        )

    async def collect_postcard(
        self,
        postcard: str | FeedNode,
        *,
        share: bool = False,
        identify: bool = False,
    ) -> CollectedPostcard:
        """Collect using the server's current selection; identify only if requested.

        ``share`` and ``identify`` are independent explicit booleans. Neither
        numeric confidence nor legacy species policy is inferred here. A
        transport or null-result uncertainty triggers one safe detail lookup,
        never another collect mutation. The receipt distinguishes a returned
        collection result from a result confirmed by reading current state.
        """
        if type(share) is not bool or type(identify) is not bool:
            msg = "share and identify must be booleans"
            raise TypeError(msg)
        postcard_id = self._postcard_id(postcard)
        if identify:
            await self.identify_postcard(postcard_id)
        try:
            result = await self._make_request(
                query=queries.birds.POSTCARD_COLLECT,
                variables={
                    "feedItemId": postcard_id,
                    "postcardCollectInput": {"share": share},
                },
            )
            return self._collected_result(result, postcard_id, share=share)
        except HttpResponseError:
            # An explicit HTTP rejection (e.g. 429) is not an unknown outcome.
            raise
        except (NoResponseError, UnexpectedResponseError):
            return await self._reconcile_collection(postcard_id, share=share)

    @deprecated("sighting_from_postcard is deprecated; use identify_postcard")
    async def sighting_from_postcard(
        self,
        postcard: str | FeedNode,
    ) -> PostcardSighting:
        """Convert a postcard into a sighting report.

        Next step is to choose or confirm species and then finish the
        sighting. If the sighting type is ``SightingRecognized``, it can be
        collected with ``finish_postcard``.

        Args:
            postcard: A postcard feed-item id, or a ``NewPostcard`` FeedNode.

        Returns:
            The ``PostcardSighting`` for the postcard.

        Raises:
            ValueError: If ``postcard`` is a FeedNode that is not a
                NewPostcard.
        """
        postcard_id: str
        if isinstance(postcard, str):
            postcard_id = postcard
        elif isinstance(postcard, FeedNode):
            if postcard.node_type != FeedNodeType.NewPostcard:
                msg = f"expected a NewPostcard, got {postcard.node_type}"
                raise ValueError(msg)
            postcard_id = postcard.node_id
        else:
            msg = f"postcard must be a str or FeedNode, got {type(postcard)}"
            raise TypeError(msg)
        variables = {
            "sightingCreateFromPostcardInput": {
                "feedItemId": postcard_id,
            }
        }
        result = await self._make_request(
            query=queries.birds.POSTCARD_TO_SIGHTING,
            variables=variables,
        )
        data = result["sightingCreateFromPostcard"]
        return PostcardSighting(data).with_postcard(postcard_id)

    @deprecated("finish_postcard is deprecated; use collect_postcard")
    async def finish_postcard(
        self,
        feed_item_id: str,
        sighting_result: PostcardSighting,
        strategy: SightingFinishStrategy = SightingFinishStrategy.RECOGNIZED,
        confidence_threshold: int | None = None,
        share_media: bool = False,
    ) -> bool:
        """Finish collecting the postcard into your collections.

        Args:
            feed_item_id: The id from ``new_postcards``.
            sighting_result: From ``sighting_from_postcard``; should contain
                sightings of type ``SightingRecognizedBird`` or
                ``SightingRecognizedBirdUnlocked``.
            strategy: One of ``RECOGNIZED``, ``BEST_GUESS``, or ``MYSTERY``.
            confidence_threshold: For ``BEST_GUESS``, accept the highest
                confidence suggestion above this threshold (defaults to 10%).
            share_media: ``True`` to share the finished media.

        Returns:
            ``True`` if the postcard was finished successfully.
        """
        if not isinstance(sighting_result, PostcardSighting):
            # See sighting_from_postcard()["sightingCreateFromPostcard"]
            LOGGER.warning("Unexpected sighting result")
            return False

        report = sighting_result.report

        sighting: Sighting | None = None
        mod: SightingFinishMod | None = None
        for sighting, mod in report.sighting_finishing_strategies(
            confidence_threshold
        ).values():
            # if we need extra work, do it now and update `report`
            if mod.strategy == SightingFinishStrategy.RECOGNIZED:
                # Nothing to do, this will pass through as-is
                pass
            elif mod.strategy < strategy:
                # Caller chose not to allow this.
                # We will try to finish the postcard anyway.
                # This may result in losing the sighting data.
                LOGGER.info(
                    "Requested %s, but recommended strategy is %s",
                    strategy,
                    mod,
                )
            elif mod.strategy == SightingFinishStrategy.BEST_GUESS:
                # Choose the highest recommended species
                match_data = mod.data or {}
                LOGGER.debug(
                    "selecting highest confidence: %s%% for species %s",
                    match_data["confidence"],
                    match_data["speciesCode"],
                )
                new_report = await self._choose_species(
                    sighting.id,
                    match_data["speciesCode"],
                    report,
                )
                LOGGER.debug("Updated report after choosing species")
                report = new_report
            elif mod.strategy == SightingFinishStrategy.MYSTERY:
                new_report = await self._choose_mystery(
                    sighting.id,
                    report,
                )
                LOGGER.debug("Updated report after choosing mystery")
                report = new_report

        variables = {
            "sightingReportPostcardFinishInput": {
                "feedItemId": feed_item_id,
                "defaultCoverMedia": [
                    s.cover_media for s in report.sightings if s.is_unlocked
                ],
                "notSelectedMediaIds": [],
                "reportToken": report.token,
            }
        }
        if video := next(iter(sighting_result.video_media), None):
            variables["sightingReportPostcardFinishInput"]["videoMediaId"] = video.id
        data = await self._make_request(
            query=queries.birds.FINISH_SIGHTING,
            variables=variables,
        )
        result = bool(data["sightingReportPostcardFinish"]["success"])
        if share_media:
            media_ids = [m.id for m in sighting_result.medias]
            try:
                share_result = await self.share_medias(media_ids, share=True)
                LOGGER.info(
                    "Media sharing result: success=%s, count=%d",
                    share_result,
                    len(media_ids),
                )
            except Exception as err:  # pylint: disable=broad-except  # noqa: BLE001
                LOGGER.error("Media sharing failed (%s)", type(err).__name__)
        return result

    async def share_medias(self, media_ids: list[str], share: bool = True) -> bool:
        """Toggle sharing for the given media.

        Args:
            media_ids: The media ids to update.
            share: ``True`` to share, ``False`` to unshare.

        Returns:
            ``True`` if the toggle succeeded.
        """
        variables = {
            "mediaShareToggleInput": {
                "mediaIds": media_ids,
                "share": share,
            }
        }
        result = await self._make_request(
            query=queries.birds.SHARE_MEDIAS,
            variables=variables,
        )
        return bool(result["mediaShareToggle"]["success"])

    @deprecated("sighting_create was removed from the Bird Buddy API")
    async def sighting_create(
        self,
        media_ids: list[str],
    ) -> SightingCreateProgress:
        """Identify birds in media via a background process.

        Deprecated since 0.0.22: the ``sightingCreate`` mutation was removed
        from the Bird Buddy API, so this raises instead of round-tripping into
        a server error. The postcard flow (``identify_postcard`` /
        ``collect_postcard``) replaces media-based identification.

        Raises:
            NotImplementedError: Always; the mutation no longer exists.
        """
        msg = "sightingCreate was removed from the Bird Buddy API"
        raise NotImplementedError(msg)

    @deprecated("sighting_create_check_progress was removed from the Bird Buddy API")
    async def sighting_create_check_progress(
        self,
        sighting_create_id: str,
        watching_id: str,
    ) -> SightingCreateProgress | SightingReport:
        """Check the progress of a background bird identification.

        Deprecated since 0.0.22: the ``sightingCreateCheckProgress`` query was
        removed from the Bird Buddy API, so this raises instead of
        round-tripping into a server error.

        Raises:
            NotImplementedError: Always; the query no longer exists.
        """
        msg = "sightingCreateCheckProgress was removed from the Bird Buddy API"
        raise NotImplementedError(msg)

    @deprecated("sighting_choose_species is deprecated; use collect_postcard")
    async def sighting_choose_species(
        self,
        sighting_id: str,
        species_id: str,
        sighting_data: SightingReport | str | None = None,
    ) -> SightingReport:
        """Manually assign a species to a sighting.

        Args:
            sighting_id: The sighting to update.
            species_id: The species to assign.
            sighting_data: The ``SightingReport`` or its report token.

        Returns:
            The updated ``SightingReport``.

        Raises:
            TypeError: If ``sighting_data`` is not a SightingReport or token.
        """
        return await self._choose_species(sighting_id, species_id, sighting_data)

    # TODO: Inline into finish_postcard and remove when the deprecated report
    # flow (sighting_choose_species, finish_postcard) is removed. It exists
    # only so finish_postcard can choose a species without triggering
    # sighting_choose_species's deprecation warning.
    async def _choose_species(
        self,
        sighting_id: str,
        species_id: str,
        sighting_data: SightingReport | str | None = None,
    ) -> SightingReport:
        """Assign a species to a sighting (shared by the deprecated flow)."""
        token: str | None
        if isinstance(sighting_data, SightingReport):
            token = sighting_data.token
        elif isinstance(sighting_data, str):
            token = sighting_data
        else:
            msg = "sighting_data should be a SightingReport or its token"
            raise TypeError(msg)
        variables = {
            "sightingChooseSpeciesInput": {
                "sightingId": sighting_id,
                "speciesId": species_id,
                "reportToken": token,
            }
        }
        data = await self._make_request(
            query=queries.birds.SIGHTING_CHOOSE_SPECIES,
            variables=variables,
        )
        return SightingReport(data["sightingChooseSpecies"])

    @deprecated("sighting_choose_mystery is deprecated; use collect_postcard")
    async def sighting_choose_mystery(
        self,
        sighting_id: str,
        sighting_data: SightingReport | str | None = None,
    ) -> SightingReport:
        """Convert the sighting into a mystery visitor.

        Args:
            sighting_id: The sighting to convert.
            sighting_data: The ``SightingReport`` or its report token.

        Returns:
            The updated ``SightingReport``.

        Raises:
            TypeError: If ``sighting_data`` is not a SightingReport or token.
        """
        return await self._choose_mystery(sighting_id, sighting_data)

    # TODO: Remove with _choose_species when the deprecated report flow is
    # removed; exists only to keep finish_postcard from triggering
    # sighting_choose_mystery's deprecation warning.
    async def _choose_mystery(
        self,
        sighting_id: str,
        sighting_data: SightingReport | str | None = None,
    ) -> SightingReport:
        """Convert a sighting to a mystery visitor (shared by the deprecated flow)."""
        token: str | None
        if isinstance(sighting_data, SightingReport):
            token = sighting_data.token
        elif isinstance(sighting_data, str):
            token = sighting_data
        else:
            msg = "sighting_data should be a SightingReport or its token"
            raise TypeError(msg)
        variables = {
            "sightingConvertToMysteryVisitorInput": {
                "sightingId": sighting_id,
                "reportToken": token,
            }
        }
        data = await self._make_request(
            query=queries.birds.SIGHTING_CHOOSE_MYSTERY,
            variables=variables,
        )
        return SightingReport(data["sightingConvertToMysteryVisitor"])

    async def refresh_collections(
        self, of_type: str = "bird", *, force: bool = False
    ) -> dict[str, Collection]:
        """Reconcile a complete snapshot, coalescing concurrent refresh calls.

        A successful snapshot, including an empty one, is reused for up to
        five minutes. Expired cover URLs also make it stale. ``collections``
        retains known metadata on refresh failure; it never implies freshness.

        Args:
            of_type: The collection type to keep (e.g. ``"bird"``).
            force: Fetch despite a fresh cached snapshot. Concurrent callers
                still share a refresh completed while they waited.

        Returns:
            The cached collections, keyed by collection id.
        """
        revision = self._collections_revision
        attempt = self._collections_attempt
        async with self._collections_lock:
            await self._check_auth()
            account_id = self._me.id if self._me else None
            if (
                attempt != self._collections_attempt
                and (error := self._collections_error) is not None
                and error[:2] == (account_id, of_type)
            ):
                raise error[2]
            same_type = self._collections_type == of_type
            if (
                same_type
                and self.collections_loaded
                and (
                    revision != self._collections_revision
                    or (not force and not self.collections_stale)
                )
            ):
                return self._collections
            try:
                snapshot = await self._fetch_collection_snapshot(of_type, account_id)
            except Exception as err:
                self._collections_attempt += 1
                self._collections_error = (account_id, of_type, err)
                raise
            self._apply_collection_snapshot(snapshot, account_id, of_type)
            return self._collections

    async def _fetch_collection_snapshot(
        self, of_type: str, account_id: str | None
    ) -> dict[str, dict]:
        """Read and validate one complete snapshot in its original account."""
        data = await self._make_request(query=queries.me.COLLECTIONS)
        snapshot = self._collection_snapshot(data, of_type)
        if account_id != (self._me.id if self._me else None):
            raise UnexpectedResponseError()
        return snapshot

    def _apply_collection_snapshot(
        self, snapshot: dict[str, dict], account_id: str | None, of_type: str
    ) -> None:
        """Publish a validated snapshot without replacing surviving objects."""
        if account_id != self._collections_account_id:
            self._collections.clear()
        for key in self._collections.keys() - snapshot.keys():
            del self._collections[key]
        for key, value in snapshot.items():
            if key in self._collections:
                self._collections[key].clear()
                self._collections[key].update(value)
            else:
                self._collections[key] = Collection(value)
        self._collections_account_id = account_id
        self._collections_type = of_type
        self._collections_loaded = True
        self._collections_updated_at = monotonic()
        self._collections_revision += 1
        self._collections_attempt += 1
        self._collections_error = None

    @staticmethod
    def _collection_snapshot(data: dict, of_type: str) -> dict[str, dict]:
        """Validate every entry before changing the last successful cache."""
        try:
            rows = data["me"]["collections"]
            if not isinstance(rows, list):
                raise UnexpectedResponseError(data)
            snapshot = {}
            seen = set()
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise UnexpectedResponseError(data)
                if not row["id"] or not isinstance(row.get("__typename"), str):
                    raise UnexpectedResponseError(data)
                if row["id"] in seen:
                    raise UnexpectedResponseError(data)
                seen.add(row["id"])
                if row["__typename"] != f"Collection{of_type.capitalize()}":
                    continue
                if (
                    type(row.get("visitsAllTime")) is not int
                    or row["visitsAllTime"] < 0
                ):
                    raise UnexpectedResponseError(data)
                if row.get("visitsAllTime") == 0:
                    continue
                cover = row.get("coverCollectionMedia")
                if not isinstance(cover, dict) or not isinstance(
                    cover.get("media"), dict
                ):
                    raise UnexpectedResponseError(data)
                snapshot[row["id"]] = row
        except (KeyError, TypeError) as err:
            raise UnexpectedResponseError(data) from err
        return snapshot

    async def set_feeder_options(
        self,
        feeder: Feeder | str,
        *,
        timeout: float = 30.0,
        **kwargs: Unpack[FeederOptions],
    ) -> dict:
        """Validate supported options, including the deprecated offlineMode alias.

        All keys and values are validated before any request. offGrid uses its
        dedicated bounded control workflow. Other options use feederUpdate.
        Mixed changes are sequential; the API provides no atomic transaction.
        """
        options = validate_feeder_options(dict(kwargs))
        return await self._with_control_timeout(
            lambda progress: self._set_options_operation(feeder, options, progress),
            timeout,
        )

    async def _set_options_operation(
        self,
        feeder: Feeder | str,
        options: dict[str, bool | str],
        progress: dict[str, bool],
    ) -> dict:
        """Apply validated metadata and optionally converge the off-grid state."""
        selected = await self._control_feeder(feeder)
        metadata = {key: value for key, value in options.items() if key != "offGrid"}
        if metadata:
            updated = await self._make_request(
                query=queries.feeder.SET_OPTIONS,
                variables={"feederId": selected.id, "feederUpdateInput": metadata},
                subscript="feederUpdate",
            )
            if (
                updated.get("__typename") != "FeederForOwner"
                or updated.get("id") != selected.id
            ):
                msg = "Feeder options returned an invalid owner result"
                raise FeederControlError(msg)
            selected.update(updated)
        if "offGrid" in options:
            selected = await self._control_operation(
                feeder, "offGrid", options["offGrid"], progress
            )
        else:
            await self._confirm_control_state(progress)
        current = self._feeders.get(selected.id)
        if current is None:
            msg = "Feeder options completed, but the feeder is no longer available"
            raise FeederControlError(msg, action_completed=True)
        return dict(current.data)

    async def set_power_profile(
        self, feeder: Feeder | str, profile: PowerProfile, *, timeout: float = 30.0
    ) -> dict:
        """Set a known configured profile; never infer actual running behavior."""
        if not isinstance(profile, PowerProfile) or profile is PowerProfile.UNKNOWN:
            msg = "A supported PowerProfile is required"
            raise ValueError(msg)
        selected = await self._with_control_timeout(
            lambda progress: self._control_operation(
                feeder, "powerProfile", profile.value, progress
            ),
            timeout,
        )
        return {"powerProfile": selected.get("powerProfile")}

    async def update_firmware_start(self, feeder: Feeder | str) -> FeederUpdateStatus:
        """Start a firmware update.

        Args:
            feeder: The Feeder or its id to update.

        Returns:
            The firmware update status.

        Raises:
            NoFirmwareUpdateAvailableError: If the feeder already runs the
                latest firmware (the API errors internally otherwise).
        """
        current_status = await self.update_firmware_check(feeder)

        if current_status.is_in_progress:
            # There's already an update in progress
            return current_status

        # The API returns an internal error when asked to start an update that
        # is not available, so guard on the versions the check reported.
        reported = Feeder(current_status.get("feeder") or {})
        if reported.version and reported.version == reported.version_update_available:
            msg = f"feeder already on the latest firmware ({reported.version})"
            raise NoFirmwareUpdateAvailableError(msg)

        feeder_id: str
        if isinstance(feeder, Feeder):
            if not feeder.is_owner:
                LOGGER.warning("Firmware update is available only to owner accounts")
                # The request will fail
            feeder_id = feeder.id
        else:
            feeder_id = feeder
        variables = {"feederId": feeder_id}
        try:
            data = await self._make_request(
                query=queries.feeder.UPDATE_FIRMWARE,
                variables=variables,
            )
        except GraphqlError as err:
            if err.error_code == "FEEDER_FIRMWARE_UPGRADE_ALREADY_IN_PROGRESS":
                # Should have been handled above.
                return current_status
            raise
        LOGGER.debug("Firmware update request completed")
        result = FeederUpdateStatus(data["feederFirmwareUpdateStart"])
        if result.is_complete:
            # After completion, update the Feeder state
            self.feeders[feeder_id].update(result.get("feeder", {}))
        return result

    async def update_firmware_check(self, feeder: Feeder | str) -> FeederUpdateStatus:
        """Check on a firmware update.

        Args:
            feeder: The Feeder or its id to check.

        Returns:
            The current firmware update status.
        """
        if isinstance(feeder, Feeder):
            if not feeder.is_owner:
                LOGGER.warning("Firmware update is available only to owner accounts")
                # The request will fail
            feeder_id = feeder.id
        else:
            feeder_id = feeder
        variables = {"feederId": feeder_id}
        data = await self._make_request(
            query=queries.feeder.UPDATE_FIRMWARE_PROGRESS,
            variables=variables,
        )
        result = FeederUpdateStatus(data["feederFirmwareUpdateCheckProgress"])
        if result.is_complete:
            self.feeders[feeder_id].update(result.get("feeder", {}))
        return result

    @property
    def collections(self) -> dict[str, Collection]:
        """Return the last seen cached Collections.

        Note that this may be outdated.

        See also :func:`BirdBuddy.refresh_collections()`
        """
        if self._needs_login():
            LOGGER.warning(
                "BirdBuddy is not logged in. Call refresh_collections() first"
            )
            return {}

        if self._collections_account_id != (self._me.id if self._me else None):
            return {}
        return self._collections

    @property
    def collections_loaded(self) -> bool:
        """Whether this account has a successful snapshot, including empty."""
        return self._collections_loaded and self._collections_account_id == (
            self._me.id if self._me else None
        )

    @property
    def collections_stale(self) -> bool:
        """Whether a refresh is needed without deleting last-known metadata."""
        if not self.collections_loaded:
            return True
        if monotonic() - self._collections_updated_at >= _COLLECTIONS_TTL:
            return True
        for collection in self._collections.values():
            wrapper = collection.get("coverCollectionMedia")
            media = wrapper.get("media") if isinstance(wrapper, dict) else None
            if isinstance(media, dict) and any(
                is_media_expired(media.get(key))
                for key in ("thumbnailUrl", "contentUrl")
            ):
                return True
        return False

    async def collection(
        self, collection_id: str, page_size: int = 50, *, max_pages: int = 100
    ) -> dict[str, Media]:
        """Return all media in the specified collection.

        Follows pagination so collections larger than one page are fully
        retrieved (not truncated to the first page).

        Args:
            collection_id: The collection ``UUID``.
            max_pages: Maximum page requests before incomplete traversal fails.
            page_size: How many media to request per page. Must be 1-100; the
                API returns an internal error for larger page sizes.

        Returns:
            A mapping of ``media_id`` to its ``Media``.

        Raises:
            ValueError: If ``page_size`` is not between 1 and 100.
            UnexpectedResponseError: If a page lacks the expected media edges.
        """
        _require_page_size(page_size)
        result: dict[str, Media] = {}
        variables = {"collectionId": collection_id, "first": page_size}
        pages = self._iter_pages(
            query=queries.me.COLLECTIONS_MEDIA,
            variables=variables,
            connection=lambda data: data["collection"]["media"],
            max_pages=max_pages,
        )
        async for media in pages:
            try:
                for edge in media["edges"]:
                    wrapper = edge["node"]
                    node = wrapper["media"]
                    result[node["id"]] = CollectionMedia(wrapper, collection_id)
            except (KeyError, TypeError) as err:
                raise UnexpectedResponseError(media) from err
        return result

    @deprecated("latest_collections is deprecated; use refresh_collections()")
    async def latest_collections(self) -> dict[str, Collection]:
        """Return the account's bird collections.

        Deprecated since 0.0.22; use :func:`refresh_collections` instead. The
        previous implementation referenced an undefined query and raised
        ``AttributeError`` on every call; this now delegates to
        ``refresh_collections``, which keeps only bird collections.
        """
        return await self.refresh_collections()

    @property
    def feeders(self) -> dict[str, Feeder]:
        """The Feeder devices associated with the account."""
        if self._needs_login():
            LOGGER.warning("BirdBuddy is not logged in. Call refresh() first")
            return {}
        return self._feeders
