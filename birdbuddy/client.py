"""Bird Buddy client module."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
import json
import re
from typing import Any

import aiohttp
import langcodes
from typing_extensions import deprecated

from birdbuddy import LOGGER, queries
from birdbuddy.const import BB_URL
from birdbuddy.exceptions import (
    AUTH_TOKEN_EXPIRED_ERROR,
    AuthenticationFailedError,
    GraphqlError,
    MutationOutcomeUnknownError,
    NoFirmwareUpdateAvailableError,
    NoResponseError,
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
from birdbuddy.feeder import Feeder, FeederUpdateStatus, PowerProfile
from birdbuddy.media import Collection, Media
from birdbuddy.postcards import CollectedPostcard, PostcardAnalysis
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

_NO_VALUE = object()
"""Sentinel value to allow None to override a default value."""

_MAX_PAGE_SIZE = 100
"""The largest page size the API accepts; a larger ``first`` errors server
side (HTTP 200 with a GraphQL ``INTERNAL_SERVER_ERROR``)."""


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

    async def toggle_off_grid(
        self,
        feeder: Feeder | str,
        is_off_grid: bool,
    ) -> Feeder:
        """Toggle the feeder's off-grid status.

        Available to the owner account only.

        Args:
            feeder: The Feeder or its id to update.
            is_off_grid: Whether to enable off-grid mode.

        Returns:
            The Feeder once the status has updated.
        """
        if isinstance(feeder, Feeder):
            feeder_id = feeder.id
            if not feeder.is_owner:
                LOGGER.warning("Off-grid is available only to owner accounts")
                # The request will fail
        else:
            # We cannot check the owner status if we only have an id
            feeder_id = feeder
        variables = {
            "feederId": feeder_id,
            "feederToggleOffGridInput": {
                "offGrid": is_off_grid,
            },
        }
        result = await self._make_request(
            query=queries.feeder.TOGGLE_OFF_GRID,
            variables=variables,
        )
        LOGGER.debug("Off-grid request completed")
        # The off-grid status doesn't get updated right away.

        new_off_grid = result["feederToggleOffGrid"]["feeder"]["offGrid"]

        while new_off_grid != is_off_grid:
            LOGGER.debug("waiting for off-grid to update")
            await asyncio.sleep(1)
            await self.refresh()
            new_off_grid = self.feeders[feeder_id].is_off_grid
        return self.feeders[feeder_id]

    async def toggle_audio_enabled(
        self,
        feeder: Feeder | str,
        is_audio_enabled: bool,
    ) -> Feeder:
        """Toggle the feeder's audio-enabled setting.

        Available to the owner account only.

        Args:
            feeder: The Feeder or its id to update.
            is_audio_enabled: Whether videos should include audio.

        Returns:
            The Feeder once the setting has updated.
        """
        if isinstance(feeder, Feeder):
            feeder_id = feeder.id
            if not feeder.is_owner:
                LOGGER.warning("Audio setting is available only to owner accounts")
                # The request will fail
        else:
            # We cannot check the owner status if we only have an id
            feeder_id = feeder
        variables = {
            "feederId": feeder_id,
            "feederToggleAudioInput": {
                "audioEnabled": is_audio_enabled,
            },
        }
        result = await self._make_request(
            query=queries.feeder.TOGGLE_AUDIO_ENABLED,
            variables=variables,
        )
        LOGGER.debug("Audio toggle request completed")
        # The status doesn't get updated right away.

        new_setting = result["feederToggleAudio"]["feeder"]["audioEnabled"]

        while new_setting != is_audio_enabled:
            LOGGER.debug("waiting for audio setting to update")
            await asyncio.sleep(1)
            await self.refresh()
            new_setting = self.feeders[feeder_id].is_audio_enabled
        return self.feeders[feeder_id]

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
        """Identify a postcard's visitor (the app's "Identify this visitor").

        Runs the AI inference (GraphQL ``inferenceExternalPostcardReanalyze``)
        and returns the recognized species and media without collecting. Free
        accounts analyze on demand (this call); premium accounts analyze
        automatically, so this is then a no-op. Idempotent: an already-analyzed
        postcard returns the same analysis, with reanalyzeAvailability
        ALREADY_REANALYZED.

        Args:
            postcard: A postcard feed-item id, or a ``NewPostcard`` FeedNode.

        Returns:
            The postcard's ``PostcardAnalysis``: recognized species and media,
            without collecting.

        Raises:
            ValueError: If ``postcard`` is a FeedNode that is not a
                NewPostcard.
            TypeError: If ``postcard`` is neither a str nor a FeedNode.
            UnexpectedResponseError: If the response lacks the expected
                reanalyze fields.
        """
        result = await self._make_request(
            query=queries.birds.POSTCARD_REANALYZE,
            variables={"feedItemId": self._postcard_id(postcard)},
        )
        try:
            updated = result["inferenceExternalPostcardReanalyze"]["updatedFeedItem"]
        except (KeyError, TypeError) as err:
            raise UnexpectedResponseError(result) from err
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

    async def collect_postcard(
        self,
        postcard: str | FeedNode,
        *,
        share: bool = False,
    ) -> CollectedPostcard:
        """Collect a postcard into your account.

        Identifies the postcard first (idempotent, so it is safe whether or
        not inference has already run), then collects it with the species the
        backend recognized.

        Args:
            postcard: A postcard feed-item id, or a ``NewPostcard`` FeedNode.
            share: Whether to share the collected media.

        Returns:
            The collected postcard.

        Raises:
            ValueError: If ``postcard`` is a FeedNode that is not a
                NewPostcard.
            TypeError: If ``postcard`` is neither a str nor a FeedNode.
            UnexpectedResponseError: If the response lacks the expected
                collect fields.
        """
        postcard_id = self._postcard_id(postcard)
        await self.identify_postcard(postcard_id)
        result = await self._make_request(
            query=queries.birds.POSTCARD_COLLECT,
            variables={
                "feedItemId": postcard_id,
                "postcardCollectInput": {"share": share},
            },
        )
        try:
            collected = result["postcardCollect"]["collectedPostcard"]
        except (KeyError, TypeError) as err:
            raise UnexpectedResponseError(result) from err
        return CollectedPostcard(collected)

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

    async def refresh_collections(self, of_type: str = "bird") -> dict[str, Collection]:
        """Fetch and cache the remote collections.

        Args:
            of_type: The collection type to keep (e.g. ``"bird"``).

        Returns:
            The cached collections, keyed by collection id.
        """
        data = await self._make_request(query=queries.me.COLLECTIONS)
        collections = {
            (c := Collection(d)).collection_id: c
            for d in data["me"]["collections"]
            # __typename: CollectionBird
            if d["__typename"] == f"Collection{of_type.capitalize()}"
        }
        self._collections.update(collections)
        return self._collections

    async def set_feeder_options(
        self, feeder: Feeder | str, **kwargs: bool | str
    ) -> dict:
        """Update Feeder options.

        Args:
            feeder: The Feeder or its id to update.
            **kwargs: Feeder options to set. Recognized keys:
                ``lowBatteryNotification`` (bool), ``lowFoodNotification``
                (bool), ``name`` (str), ``offGrid`` (bool), ``offlineMode``
                (bool).

        Returns:
            The updated feeder-options payload.
        """
        if isinstance(feeder, Feeder):
            feeder_id = feeder.id
            if not feeder.is_owner:
                LOGGER.warning("Setting Feeder options requires an owner account")
        else:
            feeder_id = feeder
        variables = {
            "feederId": feeder_id,
            "feederUpdateInput": {
                k: v
                for (k, v) in kwargs.items()
                if k
                in [
                    "lowBatteryNotification",
                    "lowFoodNotification",
                    "name",
                    "offGrid",
                    "offlineMode",
                ]
            },
        }
        updated = await self._make_request(
            query=queries.feeder.SET_OPTIONS,
            variables=variables,
            subscript="feederUpdate",
        )
        self.feeders[feeder_id].update(updated)
        return updated

    async def set_power_profile(
        self, feeder: Feeder | str, profile: PowerProfile
    ) -> dict:
        """Update the feeder's power profile.

        Args:
            feeder: The Feeder or its id to update.
            profile: The power profile to set.

        Returns:
            A dict with the resulting ``powerProfile``.
        """
        if isinstance(feeder, Feeder):
            feeder_id = feeder.id
            if not feeder.is_owner:
                LOGGER.warning("Frequency setting is available only to owner accounts")
        else:
            feeder_id = feeder
        variables = {
            "feederId": feeder_id,
            "feederUpdatePowerProfileInput": {
                "powerProfile": profile.value,
            },
        }
        result = await self._make_request(
            query=queries.feeder.UPDATE_POWER_PROFILE,
            variables=variables,
            subscript="feederUpdatePowerProfile",
        )
        # May raise GraphqlError `PAYMENTS_SUBSCRIPTION_IS_NOT_ACTIVE`,
        # implying that `FRENZY_MODE` is a paid feature.
        updated = {"powerProfile": result.get("feeder", {}).get("powerProfile", None)}

        if result["__typename"] == "FeederUpdatePowerProfileInProgressResult":
            # Refresh after a short delay
            # We could also poll `feederUpdatePowerProfileCheck`
            LOGGER.debug("PowerProfile update is in progress")
            await asyncio.sleep(0.25)
            await self.refresh()
            updated["powerProfile"] = self.feeders[feeder_id].power_profile.value
        else:
            self.feeders[feeder_id].update(updated)
        return updated

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

        for collection in list(self._collections.values()):
            if collection.cover_media.is_expired:
                del self._collections[collection.collection_id]
        return self._collections

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
                    node = edge["node"]["media"]
                    result[node["id"]] = Media(node)
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
