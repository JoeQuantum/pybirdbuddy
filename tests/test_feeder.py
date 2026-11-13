"""Basic tests for the birdbuddy.feeder models."""

import pytest

from birdbuddy.feeder import (
    Battery,
    Feeder,
    FeederDeviceVersion,
    FeederHousingType,
    FeederState,
    FeederUpdateStatus,
    MetricState,
    PowerProfile,
    Signal,
)


@pytest.mark.parametrize(
    ("enum_cls", "value", "member"),
    [
        (MetricState, "HIGH", MetricState.HIGH),
        (PowerProfile, "FRENZY_MODE", PowerProfile.FRENZY),
        (PowerProfile, "ULTRA_FRENZY_MODE", PowerProfile.ULTRA_FRENZY),
        (FeederState, "READY_TO_STREAM", FeederState.READY_TO_STREAM),
        (FeederDeviceVersion, "V1", FeederDeviceVersion.V1),
        (FeederDeviceVersion, "V1_PRO", FeederDeviceVersion.V1_PRO),
        (FeederDeviceVersion, "V2", FeederDeviceVersion.V2),
        (FeederDeviceVersion, "V2_NOOK", FeederDeviceVersion.V2_NOOK),
        (FeederHousingType, "CLASSIC", FeederHousingType.CLASSIC),
        (FeederHousingType, "HUMMINGBIRD", FeederHousingType.HUMMINGBIRD),
        (FeederHousingType, "BIRD_BATH", FeederHousingType.BIRD_BATH),
    ],
)
def test_feeder_enum_known_value(enum_cls, value, member):
    """A known raw value resolves to its enum member."""
    assert enum_cls(value) is member


@pytest.mark.parametrize(
    ("enum_cls", "unknown"),
    [
        (MetricState, MetricState.UNKNOWN),
        (PowerProfile, PowerProfile.UNKNOWN),
        (FeederState, FeederState.UNKNOWN),
        (FeederDeviceVersion, FeederDeviceVersion.UNKNOWN),
        (FeederHousingType, FeederHousingType.UNKNOWN),
    ],
)
def test_feeder_enum_unknown_falls_back(enum_cls, unknown):
    """An unrecognized value falls back to the UNKNOWN member."""
    assert enum_cls("NOT_A_REAL_VALUE") is unknown


def test_signal_and_battery():
    """Signal and Battery read their metrics and state."""
    signal = Signal({"value": -41, "state": "HIGH"})
    assert signal.rssi == -41
    assert signal.state is MetricState.HIGH

    battery = Battery({"percentage": 93, "charging": True, "state": "HIGH"})
    assert battery.percentage == 93
    assert battery.is_charging is True
    assert battery.state is MetricState.HIGH


def test_signal_and_battery_defaults():
    """Absent metrics remain unknown instead of becoming invented readings."""
    assert Signal({}).rssi is None
    assert Signal({}).state is MetricState.UNKNOWN
    assert Battery({}).percentage is None
    assert Battery({}).is_charging is None


@pytest.mark.parametrize("metric", [None, {}])
def test_null_telemetry_remains_unknown(metric):
    """Owner/member payloads can legitimately lack an entire metric object."""
    feeder = Feeder(dict.fromkeys(("battery", "signal", "food", "temperature"), metric))
    assert feeder.battery.percentage is None
    assert feeder.battery.is_charging is None
    assert feeder.signal.rssi is None
    assert feeder.food is MetricState.UNKNOWN
    assert feeder.temperature is None


def test_explicit_zero_telemetry_is_preserved():
    """Measured zero and charging false are distinct from missing fields."""
    feeder = Feeder(
        {
            "battery": {"percentage": 0, "charging": False},
            "signal": {"value": 0},
            "temperature": {"value": 0.0},
            "food": {"state": "LOW"},
        }
    )
    assert feeder.battery.percentage == 0
    assert feeder.battery.is_charging is False
    assert feeder.signal.rssi == 0
    assert feeder.temperature == 0.0
    assert feeder.food is MetricState.LOW


def test_feeder_properties():
    """Feeder exposes identity, ownership, state, and nested metrics."""
    feeder = Feeder(
        {
            "id": "f1",
            "serialNumber": "SN123",
            "name": "Backyard",
            "__typename": "FeederForOwner",
            "state": "READY_TO_STREAM",
            "battery": {"percentage": 80, "state": "HIGH"},
            "signal": {"value": -50, "state": "MEDIUM"},
            "powerProfile": "POWER_SAVER_MODE",
            "locationCity": "Portland",
            "locationCountry": "US",
        }
    )
    assert feeder.id == "f1"
    assert feeder.serial == "SN123"
    assert feeder.name == "Backyard"
    assert feeder.is_owner is True
    assert feeder.is_public is False
    assert feeder.state is FeederState.READY_TO_STREAM
    assert feeder.battery.percentage == 80
    assert feeder.signal.rssi == -50
    assert feeder.power_profile is PowerProfile.POWER_SAVE
    assert feeder.location == ("Portland", "US")


def test_feeder_location():
    """Owner feeders nest location{city,country}; others use flat keys."""
    owner = Feeder(
        {
            "__typename": "FeederForOwner",
            "location": {"city": "Portland", "country": "US"},
        }
    )
    assert owner.location == ("Portland", "US")
    member = Feeder(
        {
            "__typename": "FeederForMember",
            "locationCity": "Austin",
            "locationCountry": "US",
        }
    )
    assert member.location == ("Austin", "US")


def test_feeder_defaults():
    """A sparse Feeder payload falls back for name and state.

    A Feeder that does not report a powerProfile resolves to UNKNOWN
    (non-owner feeders never carry the field).
    """
    feeder = Feeder({"id": "f2"})
    assert feeder.name == "Bird Buddy"
    assert feeder.is_owner is False
    assert feeder.state is FeederState.UNKNOWN
    assert feeder.power_profile is PowerProfile.UNKNOWN
    assert str(feeder).startswith("<Feeder:")


def test_feeder_update_status():
    """FeederUpdateStatus classifies an in-progress firmware update."""
    status = FeederUpdateStatus(
        {
            "__typename": "FeederFirmwareUpdateProgressResult",
            "progress": 42,
            "feeder": {"id": "f1"},
        }
    )
    assert status.is_in_progress is True
    assert status.is_complete is False
    assert status.is_failed is False
    assert status.progress == 42
    assert status.feeder.id == "f1"
