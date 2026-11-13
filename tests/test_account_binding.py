"""An explicitly bound client cannot authenticate or mutate as another account."""

import asyncio
from copy import deepcopy

import pytest

from birdbuddy.client import BirdBuddy
from birdbuddy.exceptions import AuthenticationFailedError
from birdbuddy.media import Collection
from birdbuddy.queries import auth, birds


def profile(account_id="account-a"):
    """Keep safe synthetic identity and role metadata for cache assertions."""
    return {
        "user": {"id": account_id, "email": "same@example.invalid"},
        "feeders": [{"id": "shared-feeder", "__typename": "FeederForOwner"}],
    }


def authentication(field, account_id="account-b", *, full=True):
    """The refresh response selects only account identity, not feeder snapshots."""
    return {
        "data": {
            field: {
                "__typename": "Auth",
                "accessToken": "candidate-access",
                "refreshToken": "candidate-refresh",
                "me": profile(account_id) if full else {"user": {"id": account_id}},
            }
        }
    }


def bound_client():
    """Create a previously verified account with tokens and populated caches."""
    client = BirdBuddy(
        "same@example.invalid",
        "synthetic-password",
        refresh_token="original-refresh",
        access_token="original-access",
    )
    client._save_me(profile())
    client.bind_account("account-a")
    client._collections["collection-a"] = Collection({"id": "collection-a"})
    return client


@pytest.mark.parametrize("invalid", [None, 1, "", " ", " account-a", "account-a "])
def test_binding_rejects_invalid_identifiers(invalid):
    """Invalid IDs must not establish or change a binding."""
    client = BirdBuddy()
    with pytest.raises(ValueError, match="Account ID"):
        client.bind_account(invalid)
    assert client._bound_account_id is None


def test_binding_is_idempotent_but_cannot_be_changed_or_cleared():
    """Clearing credentials for recovery must never clear account ownership."""
    client = bound_client()
    client.bind_account("account-a")
    client._clear()
    assert client._bound_account_id == "account-a"
    with pytest.raises(AuthenticationFailedError, match="bound account"):
        client.bind_account("account-b")
    assert client._bound_account_id == "account-a"
    client.bind_account("account-a")
    assert client._auth_failure is not None


def test_binding_existing_wrong_profile_rejects_without_changing_caches():
    """A new binding cannot retroactively legitimize an unrelated profile."""
    client = BirdBuddy()
    client._save_me(profile("account-b"))
    before = deepcopy(client._me)
    with pytest.raises(AuthenticationFailedError):
        client.bind_account("account-a")
    assert client._me == before
    assert client._bound_account_id is None


async def test_wrong_snapshot_rejected_before_cache_changes_and_latches_failure(
    graphql_mock,
):
    """A routine ME read fails outside _check_auth, but future writes still stop."""
    client = bound_client()
    feeder = client._feeders["shared-feeder"]
    before = (
        deepcopy(client._me),
        deepcopy(client._feeders),
        deepcopy(client._collections),
    )
    wrong = profile("account-b")
    wrong["feeders"] = []
    graphql_mock.return_value = {"data": {"me": wrong}}
    with pytest.raises(AuthenticationFailedError):
        await client.refresh()
    assert (client._me, client._feeders, client._collections) == before
    assert client._feeders["shared-feeder"] is feeder
    assert (client._access_token, client._refresh_token) == (
        "original-access",
        "original-refresh",
    )
    with pytest.raises(AuthenticationFailedError):
        await client.share_medias(["media-a"])
    assert graphql_mock.await_count == 1


@pytest.mark.parametrize("flow", ["login", "refresh"])
async def test_mutation_authentication_cannot_publish_wrong_account_tokens_or_execute(
    graphql_mock, flow
):
    """The public sharing mutation never reaches transport after wrong UUID auth."""
    client = bound_client()
    if flow == "login":
        client._refresh_token = None
        field, query = "authEmailSignIn", auth.SIGN_IN
    else:
        client._access_token = None
        field, query = "authRefreshToken", auth.REFRESH_AUTH_TOKEN
    previous_tokens = (client._access_token, client._refresh_token)
    previous_user = deepcopy(client._me)
    previous_feeders = deepcopy(client._feeders)
    graphql_mock.return_value = authentication(field, full=flow == "login")
    failures = await asyncio.gather(
        *(client.share_medias(["media-a"]) for _ in range(12)), return_exceptions=True
    )
    assert all(isinstance(failure, AuthenticationFailedError) for failure in failures)
    assert graphql_mock.await_count == 1
    assert graphql_mock.call_args.kwargs["query"] == query
    assert graphql_mock.call_args.kwargs["query"] != birds.SHARE_MEDIAS
    assert (client._access_token, client._refresh_token) == previous_tokens
    assert client._me == previous_user
    assert client._feeders == previous_feeders
    assert client._auth_failure is failures[0]
    assert "account-b" not in str(failures[0])
    assert "candidate-access" not in str(failures[0])


@pytest.mark.parametrize(
    "missing", [None, {}, {"user": None}, {"user": {}}, {"user": {"id": None}}]
)
async def test_bound_refresh_requires_explicit_account_identity(graphql_mock, missing):
    """Missing proof is not accepted as a matching account."""
    client = bound_client()
    data = authentication("authRefreshToken")
    data["data"]["authRefreshToken"]["me"] = missing
    graphql_mock.return_value = data
    with pytest.raises(AuthenticationFailedError):
        await client._refresh_access_token()
    assert (client._access_token, client._refresh_token) == (
        "original-access",
        "original-refresh",
    )


async def test_expired_token_identity_mismatch_retains_original_pair(graphql_mock):
    """Do not publish changed tokens or lose the previous pair on identity failure."""
    client = bound_client()
    graphql_mock.return_value = authentication("authRefreshToken", full=False)
    with pytest.raises(AuthenticationFailedError):
        await client._check_auth(expired_token="original-access")
    assert (client._access_token, client._refresh_token) == (
        "original-access",
        "original-refresh",
    )
    assert graphql_mock.await_count == 1


async def test_bound_token_only_client_proves_identity_before_first_mutation(
    graphql_mock,
):
    """Preexisting tokens alone do not establish a bound account's UUID."""
    client = BirdBuddy(refresh_token="existing-refresh", access_token="existing-access")
    client.bind_account("account-a")
    graphql_mock.side_effect = [
        authentication("authRefreshToken", "account-a", full=False),
        {"data": {"mediaShareToggle": {"success": True}}},
        {"data": {"mediaShareToggle": {"success": True}}},
    ]
    assert await client.share_medias(["media-a"])
    assert await client.share_medias(["media-b"])
    assert [call.kwargs["query"] for call in graphql_mock.await_args_list] == [
        auth.REFRESH_AUTH_TOKEN,
        birds.SHARE_MEDIAS,
        birds.SHARE_MEDIAS,
    ]
    assert client._me is None  # A partial identity response is not a feeder snapshot.


async def test_matching_refresh_preserves_full_profile_and_executes_once(graphql_mock):
    """Minimal auth identity proof must not erase full owner and feeder caches."""
    client = bound_client()
    client._access_token = None
    original_user, original_feeder = client._me, client._feeders["shared-feeder"]
    graphql_mock.side_effect = [
        authentication("authRefreshToken", "account-a", full=False),
        {"data": {"mediaShareToggle": {"success": True}}},
    ]
    assert await client.share_medias(["media-a"])
    assert client._me is original_user
    assert client._feeders["shared-feeder"] is original_feeder
    assert graphql_mock.await_count == 2
    assert client._access_token == "candidate-access"


async def test_matching_bound_login_executes_only_after_profile_validation(
    graphql_mock,
):
    """A valid initial bound login still supports the normal mutation workflow."""
    client = BirdBuddy("same@example.invalid", "synthetic-password")
    client.bind_account("account-a")
    graphql_mock.side_effect = [
        authentication("authEmailSignIn", "account-a"),
        {"data": {"mediaShareToggle": {"success": True}}},
    ]
    assert await client.share_medias(["media-a"])
    assert client.user is not None
    assert client.user.id == "account-a"
    assert graphql_mock.await_count == 2


def test_unbound_generic_clients_can_still_reconcile_account_switches():
    """Persistent account binding is opt-in; generic callers keep compatibility."""
    client = BirdBuddy()
    client._save_me(profile())
    client._save_me(profile("account-b"))
    assert client.user is not None
    assert client.user.id == "account-b"
    assert client._auth_failure is None
