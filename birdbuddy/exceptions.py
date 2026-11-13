"""Bird Buddy errors."""

import re

AUTH_TOKEN_EXPIRED_ERROR = "AUTH_TOKEN_EXPIRED_ERROR"


class NoResponseError(Exception):
    """The GraphQL request had no response."""


class MutationOutcomeUnknownError(NoResponseError):
    """A mutation may have completed; read its state before considering a replay."""


class PostcardOutcomeUnknownError(MutationOutcomeUnknownError):
    """Collection could not be confirmed; the caller must not automatically replay."""

    def __init__(self, postcard_id: str, *, share_requested: bool) -> None:
        """Retain lookup context without asserting the action failed or succeeded."""
        self.postcard_id = postcard_id
        self.share_requested = share_requested
        super().__init__(
            "Postcard collection outcome is unknown; "
            "check the Bird Buddy app before retrying"
        )


class GraphqlError(Exception):
    """The GraphQL response contained one or more errors."""

    response: dict

    def __init__(self, error: dict) -> None:
        """Store the GraphQL error payload.

        Args:
            error: A single GraphQL error dict from the response.
        """
        self.response = error
        # Resolver messages can echo credentials, media URLs or private input.
        # Keep the payload accessible explicitly, but safe in ordinary logs.
        code = self.error_code or "UNKNOWN"
        if not re.fullmatch(r"[A-Z0-9_]{1,80}", code):
            code = "UNKNOWN"
        super().__init__(f"Bird Buddy GraphQL error: {code}")

    @property
    def error_code(self) -> str | None:
        """The Bird Buddy GraphQL error code."""
        extensions = self.response.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        return code if isinstance(code, str) else None

    @staticmethod
    def raise_errors(errors: list[dict]) -> None:
        """Raise the appropriate exception for a list of GraphQL errors.

        Args:
            errors: The ``errors`` list from a GraphQL response.

        Raises:
            GraphqlError: If exactly one error is present (as the most
                specific subclass, e.g. ``AuthTokenExpiredError``).
            CompositeException: If more than one error is present.
        """
        converted = [GraphqlError._convert_error(err) for err in errors]
        if (n := len(converted)) == 0:
            return
        if n > 1:
            raise CompositeException(converted)
        raise converted[0]

    @staticmethod
    def _convert_error(err: dict) -> Exception:
        """Convert a GraphQL error dict to the most specific exception.

        Args:
            err: A single GraphQL error dict.

        Returns:
            An ``AuthTokenExpiredError`` for expired-token codes, otherwise a
            ``GraphqlError``.
        """
        gqlerr = GraphqlError(err)
        if gqlerr.error_code == AUTH_TOKEN_EXPIRED_ERROR:
            return AuthTokenExpiredError(err)
        return gqlerr


class AuthTokenExpiredError(GraphqlError):
    """The auth token has expired."""


class AuthenticationFailedError(Exception):
    """The login attempt failed."""


class SocialAuthenticationRequiredError(AuthenticationFailedError):
    """The account requires a social sign-in provider unsupported by this client."""

    def __init__(self, providers: list[str]) -> None:
        """Retain only recognized public provider names for a useful UI error."""
        self.providers = tuple(
            provider
            for provider in providers
            if provider in {"APPLE", "FACEBOOK", "GOOGLE"}
        )
        super().__init__("This Bird Buddy account requires social sign-in")


class NoFirmwareUpdateAvailableError(Exception):
    """A firmware update was requested but the feeder is already current."""


class UnexpectedResponseError(Exception):
    """The response did not contain the expected fields."""

    def __init__(self, response: dict | None = None) -> None:
        """Store the unexpected response.

        Args:
            response: The raw GraphQL response, if available.
        """
        Exception.__init__(self)
        self.response = response


class CompositeException(Exception):  # noqa: N818
    """Represents multiple errors.

    Named without an ``Error`` suffix for backwards compatibility; it is part
    of the public API consumed by downstream packages.
    """
