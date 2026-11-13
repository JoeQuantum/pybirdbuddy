"""Authoritative feeder snapshots retain identity without stale membership."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from birdbuddy.exceptions import UnexpectedResponseError
from birdbuddy.feeder import Feeder, FeederControlUnavailableError


def _feeder(key="f1", role="FeederForOwner", **extra: object):
    """Build a selected feeder row, including ownership-sensitive fields."""
    return {"id": key, "name": "Fixture feeder", "__typename": role, **extra}


def _snapshot(*feeders: object, account="account"):
    """Build one complete account snapshot."""
    return {"user": {"id": account}, "feeders": list(feeders)}


def test_empty_added_removed_snapshots_are_valid(bbclient):
    """An authenticated account can move from zero to one to zero feeders."""
    assert bbclient._save_me(_snapshot())
    assert bbclient._feeders_loaded
    assert bbclient.feeders == {}
    assert bbclient._save_me(_snapshot(_feeder()))
    assert list(bbclient.feeders) == ["f1"]
    assert bbclient._save_me(_snapshot())
    assert bbclient.feeders == {}


def test_surviving_object_is_reconciled_without_old_owner_fields(bbclient):
    """Owner-to-member/pending changes replace data while preserving objects."""
    original = _snapshot(
        _feeder(audioEnabled=True, offGrid=True, firmwareVersion="1.0")
    )
    untouched = deepcopy(original)
    bbclient._save_me(original)
    entity_reference = bbclient.feeders["f1"]
    bbclient._save_me(_snapshot(_feeder(role="FeederForMember")))
    assert bbclient.feeders["f1"] is entity_reference
    assert not entity_reference.is_owner
    assert "audioEnabled" not in entity_reference
    assert "firmwareVersion" not in entity_reference
    bbclient._save_me(_snapshot(_feeder(role="FeederForMemberPending")))
    assert bbclient.feeders["f1"] is entity_reference
    assert entity_reference.is_pending
    assert original == untouched


@pytest.mark.parametrize(
    "malformed",
    [
        {"user": {"id": "account"}},
        {"user": {"id": "account"}, "feeders": None},
        {"user": {}, "feeders": []},
        _snapshot(_feeder("new"), {"id": "malformed"}),
        _snapshot(_feeder(), _feeder()),
        _snapshot(None),
    ],
)
def test_malformed_snapshot_cannot_partially_mutate_cache(bbclient, malformed):
    """Validate every row before changing any cached account or feeder data."""
    bbclient._save_me(_snapshot(_feeder(audioEnabled=True)))
    old_user = bbclient.user
    original = bbclient.feeders["f1"]
    data = deepcopy(original.data)
    with pytest.raises(UnexpectedResponseError):
        bbclient._save_me(malformed)
    assert bbclient.user is old_user
    assert bbclient.feeders == {"f1": original}
    assert original.data == data


def test_account_change_does_not_reuse_other_account_objects(bbclient):
    """The same physical ID in another login has its own role data."""
    bbclient._save_me(_snapshot(_feeder(), account="one"))
    first = bbclient.feeders["f1"]
    bbclient._save_me(_snapshot(_feeder(role="FeederForMember"), account="two"))
    assert bbclient.feeders["f1"] is not first
    assert not bbclient.feeders["f1"].is_owner


async def test_removed_passed_owner_object_cannot_resurrect_membership(bbclient):
    """A stale caller object cannot bypass an authoritative removal."""
    bbclient._save_me(_snapshot(_feeder()))
    old = bbclient.feeders["f1"]
    bbclient._save_me(_snapshot())
    bbclient._make_request = AsyncMock()
    with pytest.raises(FeederControlUnavailableError):
        await bbclient.toggle_off_grid(old, True)
    bbclient._make_request.assert_not_awaited()
    assert bbclient.feeders == {}


async def test_passed_owner_object_uses_current_member_role(bbclient):
    """An old object cannot override the current cache's reduced permission."""
    stale = Feeder(_feeder())
    bbclient._save_me(_snapshot(_feeder(role="FeederForMember")))
    bbclient._make_request = AsyncMock()
    with pytest.raises(FeederControlUnavailableError):
        await bbclient.toggle_audio_enabled(stale, True)
    bbclient._make_request.assert_not_awaited()


async def test_unknown_passed_object_requires_account_membership_read(bbclient):
    """Without a snapshot, an arbitrary owner-shaped object is not authority."""
    bbclient.refresh = AsyncMock(side_effect=lambda: bbclient._save_me(_snapshot()))
    with pytest.raises(FeederControlUnavailableError):
        await bbclient._control_feeder(Feeder(_feeder()))
    bbclient.refresh.assert_awaited_once()
