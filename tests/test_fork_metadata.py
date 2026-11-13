"""Preserve hardware metadata through both API queries and model accessors."""

from graphql import parse
from graphql.language.ast import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    SelectionSetNode,
)
import pytest

from birdbuddy import queries
from birdbuddy.feeder import (
    Feeder,
    FeederDeviceVersion,
    FeederHousingType,
    PowerProfile,
)


def _selected_fields(
    selections: SelectionSetNode, fragments: dict[str, FragmentDefinitionNode]
) -> set[str]:
    """Expand fragment spreads to inspect fields selected on one role."""
    names = set()
    for selection in selections.selections:
        if isinstance(selection, FieldNode):
            names.add(selection.name.value)
        elif isinstance(selection, FragmentSpreadNode):
            names.update(
                _selected_fields(
                    fragments[selection.name.value].selection_set, fragments
                )
            )
    return names


@pytest.mark.parametrize(
    ("query", "owner_fragment"),
    [
        (queries.auth.SIGN_IN, "MeFeederForOwnerFields"),
        (queries.me.ME, "ListOwnerFeederFields"),
    ],
    ids=["sign_in", "refresh"],
)
def test_hardware_fields_selected_for_accessible_roles(query, owner_fragment):
    """Models alone are insufficient: both requests must fetch the metadata."""
    fragments = {
        d.name.value: d
        for d in parse(query).definitions
        if isinstance(d, FragmentDefinitionNode)
    }
    for name in (owner_fragment, "ListMemberFeederFields"):
        assert {"housingType", "version"} <= _selected_fields(
            fragments[name].selection_set, fragments
        )
    pending = _selected_fields(
        fragments["FeederForMemberPendingFields"].selection_set, fragments
    )
    assert pending == {"id", "name", "__typename"}


def test_hardware_and_firmware_versions_are_distinct():
    """Preserve established accessors despite GraphQL's naming collision."""
    feeder = Feeder(
        {
            "id": "feeder",
            "version": "V2_NOOK",
            "firmwareVersion": "1.8.1",
            "housingType": "HUMMINGBIRD",
        }
    )
    assert feeder.version == "1.8.1"
    assert feeder.device_version is FeederDeviceVersion.V2_NOOK
    assert feeder.housing_type is FeederHousingType.HUMMINGBIRD


@pytest.mark.parametrize("raw", [{}, {"version": None, "housingType": None}])
def test_sparse_hardware_metadata(raw):
    """Missing or null metadata on sparse role payloads remains unknown."""
    feeder = Feeder(raw)
    assert feeder.device_version is FeederDeviceVersion.UNKNOWN
    assert feeder.housing_type is FeederHousingType.UNKNOWN


@pytest.mark.parametrize("value", [None, "FUTURE_PROFILE"])
def test_power_profile_unknown_or_null(value):
    """New or nullable profiles remain safe for enum-driven consumers."""
    assert Feeder({"powerProfile": value}).power_profile is PowerProfile.UNKNOWN
