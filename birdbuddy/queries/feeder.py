"""Feeder queries."""

TOGGLE_OFF_GRID = """
mutation feederToggleOffGrid($feederId: ID!, $feederToggleOffGridInput: FeederToggleOffGridInput!) {
  feederToggleOffGrid(feederId: $feederId, feederToggleOffGridInput: $feederToggleOffGridInput) {
    __typename
    ... on FeederToggleOffGridFinishedResult {
      feeder {
        id
        offGrid
        __typename
      }
    }
    ... on FeederToggleOffGridInProgressResult {
      feeder {
        id
        offGrid
        __typename
      }
    }
  }
}
""".strip()

TOGGLE_OFF_GRID_CHECK = """
mutation feederToggleOffGridCheck($feederId: ID!) {
  feederToggleOffGridCheck(feederId: $feederId) {
    __typename
    ... on FeederToggleOffGridFinishedResult {
      feeder {
        id
        offGrid
        __typename
      }
    }
    ... on FeederToggleOffGridInProgressResult {
      feeder {
        id
        offGrid
        __typename
      }
    }
    ... on FeederToggleOffGridFailedResult {
      failedReason
      feeder {
        id
        offGrid
        __typename
      }
    }
  }
}
""".strip()

TOGGLE_AUDIO_ENABLED = """
mutation feederToggleAudio($feederId: ID!, $feederToggleAudioInput: FeederToggleAudioInput!) {
  feederToggleAudio(feederId: $feederId, feederToggleAudioInput: $feederToggleAudioInput) {
    __typename
    ... on FeederToggleAudioFinishedResult {
      feeder {
        id
        audioEnabled
        __typename
      }
    }
    ... on FeederToggleAudioInProgressResult {
      feeder {
        id
        audioEnabled
        __typename
      }
    }
  }
}
""".strip()

TOGGLE_AUDIO_ENABLED_CHECK = """
query feederToggleAudioCheck($feederId: ID!) {
  feederToggleAudioCheck(feederId: $feederId) {
    __typename
    ... on FeederToggleAudioFinishedResult {
      feeder {
        id
        audioEnabled
        __typename
      }
    }
    ... on FeederToggleAudioInProgressResult {
      feeder {
        id
        audioEnabled
        __typename
      }
    }
    ... on FeederToggleAudioFailedResult {
      failedReason
      feeder {
        id
        audioEnabled
        __typename
      }
    }
  }
}
""".strip()

UPDATE_POWER_PROFILE = """
mutation feederUpdatePowerProfile($feederId: ID!, $feederUpdatePowerProfileInput: FeederUpdatePowerProfileInput!) {
  feederUpdatePowerProfile(feederId: $feederId, feederUpdatePowerProfileInput: $feederUpdatePowerProfileInput) {
    __typename
    ... on FeederUpdatePowerProfileFinishedResult {
      feeder {
        id
        powerProfile
        __typename
      }
    }
    ... on FeederUpdatePowerProfileInProgressResult {
      feeder {
        id
        powerProfile
        __typename
      }
    }
  }
}
""".strip()

UPDATE_POWER_PROFILE_CHECK = """
query feederUpdatePowerProfileCheck($feederId: ID!) {
  feederUpdatePowerProfileCheck(feederId: $feederId) {
    __typename
    ... on FeederUpdatePowerProfileFinishedResult {
      feeder {
        id
        powerProfile
        __typename
      }
    }
    ... on FeederUpdatePowerProfileInProgressResult {
      feeder {
        id
        powerProfile
        __typename
      }
    }
    ... on FeederUpdatePowerProfileFailedResult {
      failedReason
      feeder {
        id
        powerProfile
        __typename
      }
    }
  }
}
""".strip()

SET_OPTIONS = """
mutation feederUpdate($feederId: ID!, $feederUpdateInput: FeederUpdateInput!) {
  feederUpdate(feederId: $feederId, feederUpdateInput: $feederUpdateInput) {
    ... on FeederForOwner {
      ...ListOwnerFeederFields
      ...SingleOwnerFeederAdditionalFields
    }
  }
}
fragment ListOwnerFeederFields on FeederForOwner {
  ...ListFeederFields
  audioEnabled
  supportsAudio
  availableFirmwareVersion
  firmwareVersion
  offGrid
  powerProfile
  presenceUpdatedAt
  serialNumber
}
fragment ListFeederFields on FeederForPrivate {
  housingType
  mountingType
  solarRoof
  version
  battery {
    charging
    percentage
    state
    __typename
  }
  food {
    state
    __typename
  }
  id
  name
  signal {
    state
    value
    __typename
  }
  state
  temperature {
    value
    __typename
  }
  __typename
}
fragment SingleOwnerFeederAdditionalFields on FeederForOwner {
  frequency
  lowBatteryNotification
  lowFoodNotification
  offGrid
  powerProfile
  serialNumber
  temperature {
    value
    __typename
  }
  __typename
}
""".strip()

UPDATE_FIRMWARE = """
mutation feederFirmwareUpdateStart($feederId: ID!) {
  feederFirmwareUpdateStart(feederId: $feederId) {
    ... on FeederFirmwareUpdateFailedResult {
      failedReason
      __typename
    }
    ... on FeederFirmwareUpdateProgressResult {
      progress
      __typename
    }
    ... on FeederFirmwareUpdateSucceededResult {
      __typename
      feeder {
        availableFirmwareVersion
        firmwareVersion
      }
    }
  }
}
""".strip()

UPDATE_FIRMWARE_PROGRESS = """
mutation feederFirmwareUpdateCheckProgress($feederId: ID!) {
  feederFirmwareUpdateCheckProgress(feederId: $feederId) {
    ... on FeederFirmwareUpdateFailedResult {
      failedReason
      __typename
    }
    ... on FeederFirmwareUpdateProgressResult {
      feeder {
        state
      }
      progress
      __typename
    }
    ... on FeederFirmwareUpdateSucceededResult {
      __typename
      feeder {
        availableFirmwareVersion
        firmwareVersion
      }
    }
  }
}
""".strip()
