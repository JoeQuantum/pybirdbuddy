"""Bird Buddy feeder models."""

from __future__ import annotations

from collections import UserDict
from enum import Enum
from typing import Any, Literal, TypedDict

from birdbuddy import LOGGER


class FeederControlError(Exception):
    """A device control failed or its final state could not be confirmed."""

    def __init__(self, reason: str, *, action_completed: bool | None = None) -> None:
        """Keep safe local detail and distinguish completion from uncertainty."""
        super().__init__(reason)
        self.action_completed = action_completed


class FeederControlTimeoutError(FeederControlError):
    """The overall control budget expired; do not blindly repeat the mutation."""


class FeederControlUnavailableError(FeederControlError):
    """The selected account role or explicit capability forbids a control."""


class FeederMountingType(Enum):
    """Schema-supported mounting values accepted by the options mutation."""

    HANGER = "HANGER"
    NOT_SET = "NOT_SET"
    OTHER = "OTHER"
    POLE_MOUNT = "POLE_MOUNT"


class FeederSolarRoof(Enum):
    """Schema-supported solar-roof configuration, not inferred hardware support."""

    NO = "NO"
    NOT_SET = "NOT_SET"
    YES = "YES"


class MetricState(Enum):
    """Feeder metric states."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"

    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> MetricState:
        """Fall back to ``UNKNOWN`` for an unrecognized metric state.

        Args:
            value: The raw value that matched no enum member.

        Returns:
            ``MetricState.UNKNOWN``.
        """
        LOGGER.warning("Unexpected metric state: %s", value)
        return MetricState.UNKNOWN


class PowerProfile(Enum):
    """Feeder power profiles."""

    FRENZY = "FRENZY_MODE"
    ULTRA_FRENZY = "ULTRA_FRENZY_MODE"
    POWER_SAVE = "POWER_SAVER_MODE"
    STANDARD = "STANDARD_MODE"

    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> PowerProfile:
        """Fall back to ``UNKNOWN`` for an unrecognized power profile.

        Args:
            value: The raw value that matched no enum member.

        Returns:
            ``PowerProfile.UNKNOWN``.
        """
        LOGGER.warning("Unexpected power profile: %s", value)
        return PowerProfile.UNKNOWN


class FeederState(Enum):
    """Feeder states."""

    DEEP_SLEEP = "DEEP_SLEEP"
    FACTORY_RESET = "FACTORY_RESET"
    FIRMWARE_UPDATE = "FIRMWARE_UPDATE"
    OFFLINE = "OFFLINE"
    OFF_GRID = "OFF_GRID"
    ONLINE = "ONLINE"
    OUT_OF_FEEDER = "OUT_OF_FEEDER"
    PENDING_FACTORY_RESET = "PENDING_FACTORY_RESET"
    PENDING_REMOVAL = "PENDING_REMOVAL"
    READY_TO_STREAM = "READY_TO_STREAM"
    STREAMING = "STREAMING"
    TAKING_POSTCARDS = "TAKING_POSTCARDS"

    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> FeederState:
        """Fall back to ``UNKNOWN`` for an unrecognized feeder state.

        Args:
            value: The raw value that matched no enum member.

        Returns:
            ``FeederState.UNKNOWN``.
        """
        LOGGER.warning("Unexpected feeder.state: %s", value)
        return FeederState.UNKNOWN


class FeederDeviceVersion(Enum):
    """Hardware generations, distinct from the installed firmware version."""

    V1 = "V1"
    V1_PRO = "V1_PRO"
    V2 = "V2"
    V2_NOOK = "V2_NOOK"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> FeederDeviceVersion:
        """Preserve compatibility with hardware generations not yet known."""
        LOGGER.warning("Unexpected device version: %s", value)
        return cls.UNKNOWN


class FeederHousingType(Enum):
    """The feeder housing, independent of its camera hardware generation."""

    CLASSIC = "CLASSIC"
    HUMMINGBIRD = "HUMMINGBIRD"
    BIRD_BATH = "BIRD_BATH"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value: object) -> FeederHousingType:
        """Preserve compatibility with housing types not yet known."""
        LOGGER.warning("Unexpected housing type: %s", value)
        return cls.UNKNOWN


class FeederOptions(TypedDict, total=False):
    """Supported update input; offlineMode is a deprecated offGrid alias."""

    lowBatteryNotification: bool
    lowFoodNotification: bool
    name: str
    offGrid: bool
    offlineMode: bool
    housingType: FeederHousingType | Literal["CLASSIC", "HUMMINGBIRD", "BIRD_BATH"]
    mountingType: (
        FeederMountingType | Literal["HANGER", "NOT_SET", "OTHER", "POLE_MOUNT"]
    )
    solarRoof: FeederSolarRoof | Literal["NO", "NOT_SET", "YES"]


def validate_feeder_options(options: dict[str, object]) -> dict[str, bool | str]:
    """Normalize all supported values before permitting any account request."""
    boolean_keys = {
        "lowBatteryNotification",
        "lowFoodNotification",
        "offGrid",
        "offlineMode",
    }
    enum_keys = {
        "housingType": FeederHousingType,
        "mountingType": FeederMountingType,
        "solarRoof": FeederSolarRoof,
    }
    supported = boolean_keys | set(enum_keys) | {"name"}
    if not options or set(options) - supported:
        msg = "Provide only supported, non-empty feeder options"
        raise ValueError(msg)
    result: dict[str, bool | str] = {}
    for key, value in options.items():
        if key in boolean_keys:
            if type(value) is not bool:
                msg = "Boolean feeder options require bool values"
                raise ValueError(msg)
        elif key == "name":
            if not isinstance(value, str) or not value.strip():
                msg = "Feeder name must be a non-empty string"
                raise ValueError(msg)
        else:
            expected = enum_keys[key]
            value = (
                value.value
                if isinstance(value, Enum) and isinstance(value, expected)
                else value
            )
            if not isinstance(value, str) or value not in {
                member.value for member in expected if member.value != "UNKNOWN"
            }:
                msg = "Feeder option has an unsupported enum value"
                raise ValueError(msg)
        result[key] = value  # type: ignore[assignment]
    if "offlineMode" in result:
        legacy = result.pop("offlineMode")
        if "offGrid" in result and result["offGrid"] != legacy:
            msg = "offlineMode conflicts with offGrid"
            raise ValueError(msg)
        result["offGrid"] = legacy
    return result


class Signal(UserDict[str, Any]):
    """Wifi signal metrics."""

    @property
    def rssi(self) -> int:
        """Signal strength."""
        return self.get("value", -1)

    @property
    def state(self) -> MetricState:
        """Signal strength."""
        return MetricState(self.get("state", "UNKNOWN"))


class Battery(UserDict[str, Any]):
    """Battery info."""

    @property
    def percentage(self) -> int:
        """Percentage of battery remaining."""
        return self.get("percentage", 0)

    @property
    def is_charging(self) -> bool:
        """Whether the battery is charging."""
        return self.get("charging", False)

    @property
    def state(self) -> MetricState:
        """The state (low, medium, high) of the battery."""
        return MetricState(self.get("state", "UNKNOWN"))


class Feeder(UserDict[str, Any]):
    """Represents one Bird Buddy device."""

    def __str__(self) -> str:
        """Return a string representation of the Feeder."""
        return f"<Feeder: {self.name}, {self.state}, {self.battery.percentage}%>"

    @property
    def id(self) -> str:
        """UUID."""
        return self["id"]

    @property
    def serial(self) -> str:
        """Feeder SN."""
        return self["serialNumber"]

    @property
    def name(self) -> str:
        """Feeder name, as set in the app."""
        return self.get("name", "Bird Buddy")

    @property
    def is_owner(self) -> bool:
        """Whether the logged in user is the owner of this feeder."""
        return self.get("__typename") == "FeederForOwner"

    @property
    def is_pending(self) -> bool:
        """``True`` if waiting for the owner account to approve access."""
        return self.get("__typename") == "FeederForMemberPending"

    @property
    def is_public(self) -> bool:
        """Whether this is a public feeder."""
        return self.get("__typename") == "FeederForPublic"

    @property
    def version(self) -> str | None:
        """Firmware version (owner only)."""
        return self.get("firmwareVersion")

    @property
    def version_update_available(self) -> str | None:
        """Firmware update version (owner only)."""
        return self.get("availableFirmwareVersion", None)

    @property
    def device_version(self) -> FeederDeviceVersion:
        """Hardware generation from GraphQL ``version``, not firmware.

        ``Feeder.version`` retains its existing meaning: the installed
        ``firmwareVersion`` string. ``V2_NOOK`` is recognized by the schema;
        recognizing its value does not establish tested hardware support.
        """
        return FeederDeviceVersion(self.get("version", "UNKNOWN"))

    @property
    def housing_type(self) -> FeederHousingType:
        """The reported housing, or UNKNOWN when the field is absent."""
        return FeederHousingType(self.get("housingType", "UNKNOWN"))

    @property
    def state(self) -> FeederState:
        """State of the Feeder."""
        return FeederState(self.get("state", "UNKNOWN"))

    @property
    def is_off_grid(self) -> bool | None:
        """Whether `state` is `FeederState.OFF_GRID`."""
        return self.get("offGrid", None)

    @property
    def is_audio_enabled(self) -> bool | None:
        """Whether videos will contain audio."""
        return self.get("audioEnabled", None)

    @property
    def supports_audio(self) -> bool | None:
        """Explicit device capability; absent data remains unknown, not false."""
        value = self.get("supportsAudio")
        return value if isinstance(value, bool) else None

    @property
    def owner(self) -> str | None:
        """The username who first paired the Feeder."""
        return self.get("ownerName")

    @property
    def battery(self) -> Battery:
        """Battery metrics."""
        return Battery(self.get("battery", {}))

    @property
    def signal(self) -> Signal:
        """(wifi) signal metrics."""
        return Signal(self.get("signal", {}))

    @property
    def location(self) -> tuple[str | None, str | None]:
        """Configured location of the Feeder.

        Owner feeders nest it as ``location{city,country}``; member and
        public feeders expose flat ``locationCity``/``locationCountry``.

        Returns:
            A ``(city, country)`` tuple; either element is ``None`` when the
            feeder does not report it.
        """
        if nested := self.get("location"):
            return (nested.get("city"), nested.get("country"))
        return (self.get("locationCity"), self.get("locationCountry"))

    @property
    def frequency(self) -> MetricState:
        """Configured frequency of the Feeder."""
        LOGGER.warning("Feeder.frequency is deprecated. Use power_profile instead")
        return MetricState(self.get("frequency", "UNKNOWN"))

    @property
    def power_profile(self) -> PowerProfile:
        """Configured power profile of the Feeder.

        Only owner responses carry the field; non-owner feeders omit it.

        Returns:
            The reported ``PowerProfile``, or ``PowerProfile.UNKNOWN`` when the
            feeder does not report one.
        """
        value = self.get("powerProfile")
        return PowerProfile(value) if value else PowerProfile.UNKNOWN

    @property
    def selected_power_profile(self) -> PowerProfile:
        """Configured selection from the owner's ``powerProfile`` field.

        ``power_profile`` remains the compatible accessor. Neither property
        selects or infers Smart Energy's separate ``runningPowerProfile``
        field on the ``FeederSmartEnergyEnabled`` variant.
        """
        return self.power_profile

    @property
    # @incubating
    def food(self) -> MetricState:
        """Level of bird seed in the feeder.

        @incubating This field appears not to work currently.
        """
        LOGGER.debug("birdbuddy.Feeder.food is incubating")
        return MetricState(self.get("food", {}).get("state", "UNKNOWN"))

    @property
    # @incubating
    def temperature(self) -> int:
        """Temperature at the feeder.

        @incubating This field appears not to work currently.
        """
        LOGGER.debug("birdbuddy.Feeder.temperature is incubating")
        return self.get("temperature", {}).get("value", 0)


class FeederUpdateStatus(UserDict[str, Any]):
    """Feeder update status."""

    @property
    def feeder(self) -> Feeder:
        """Returns a partial Feeder result."""
        return Feeder(self["feeder"])

    @property
    def is_complete(self) -> bool:
        """`True` if the firmware update was successfully completed."""
        return self["__typename"] == "FeederFirmwareUpdateSucceededResult"

    @property
    def is_in_progress(self) -> bool:
        """`True` if the firmware update is in progress."""
        return (
            self["__typename"] == "FeederFirmwareUpdateProgressResult"
            and self.get("progress", None) is not None
        )

    @property
    def is_failed(self) -> bool:
        """`True` if the firmware update has failed."""
        return self["__typename"] == "FeederFirmwareUpdateFailedResult"

    @property
    def failure_reason(self) -> str | None:
        """Failure reason, or `None` if no failure."""
        return self.get("failedReason", None)

    @property
    def progress(self) -> int | None:
        """Current firmware installation progress."""
        return self.get("progress", None)
