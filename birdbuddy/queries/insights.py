"""Optional, explicit read-only account queries; no background or mutation calls."""

_SPECIES = """
fragment InsightSpecies on Species {
  __typename id name scientificName iconUrl
}
"""
_MEDIA = """
fragment InsightMedia on Media { __typename id createdAt thumbnailUrl }
"""
_NAMED = (
    """
fragment InsightNamedBird on MediaSpeciesName {
  id name createdAt state personalityBadge relationshipLevel
  species { ...InsightSpecies }
  stats { firstSeenAt lastSeenAt visitCount usualTimeOfDay }
}
"""
    + _SPECIES
)

HARDWARE = """
query AccountInsightHardware {
  me {
    user { id }
    featureLocks { feature }
    subscriptionPremium { __typename isActive type }
    feeders {
      __typename
      ... on FeederForPrivate {
        id name version housingType mountingType solarRoof
        supportsAudio supportsEnhancedLivestream supportsLivestreamSnapshot supportsWebRTC
      }
      ... on FeederForOwner {
        powerProfile videoQuality cameraFieldOfView
        smartEnergy {
          __typename status
          ... on FeederSmartEnergyEnabled { state runningPowerProfile }
          ... on FeederSmartEnergyNotSupported { reason }
        }
      }
      ... on FeederForMember { livestreamAccessStatus }
      ... on FeederForMemberPending { id name }
    }
  }
}
"""

PREFERENCES = (
    """
query AccountInsightPreferences {
  me {
    user { id }
    postcardSettings {
      ignoreMysteryVisitors
      ignoredSpecies { ...InsightSpecies }
    }
  }
}
"""
    + _SPECIES
)

SPECIES = (
    """
query AccountInsightSpecies($speciesId: ID!) {
  me { user { id } }
  species(speciesId: $speciesId) {
    ...InsightSpecies
    ... on SpeciesBird { alternativeName isUnofficialName }
  }
}
"""
    + _SPECIES
)

NAMED_BIRDS = (
    """
query AccountInsightNamedBirds {
  me {
    user { id }
    mediaSpeciesNames {
      ...InsightNamedBird
      previewMedia { ...InsightMedia }
    }
  }
}
"""
    + _NAMED
    + _MEDIA
)

WEEKLY = (
    """
query AccountInsightWeekly($userId: ID!) {
  me { user { id } }
  weeklyRecapForUser(userId: $userId) {
    __typename
    ... on WeeklyRecapForUserSuccess {
      data {
        isoWeek year
        generalVisitStats {
          totalVisits previousWeekTotalVisits totalObservationTimeMinutes
          dailyVisits { date visitCount }
        }
        feederVisitStats { feeders { feeder { id name } visitCount } }
        speciesVisitStats {
          distinctSpeciesCount mysteryVisitorVisitCount
          mysteryVisitor { medias { ...InsightMedia } }
          distinctSpecies { species { ...InsightSpecies } visitCount medias { ...InsightMedia } }
        }
        milestoneStats { currentStreakWeeks lifetimeTotalVisits streakActiveDays }
      }
    }
  }
}
"""
    + _SPECIES
    + _MEDIA
)

YEARLY = (
    """
query AccountInsightYearly($userId: ID!) {
  me { user { id } }
  yearInReviewForUser(userId: $userId) {
    __typename
    ... on YearInReviewForUserSuccess {
      data {
        year
        generalVisitStats { totalVisits totalVisitsPercentile hasFeederHousingTypeClassicVisits }
        dailyVisitStats { topDays { date visitCount } }
        monthlyVisitStats { monthlyVisits { month visitCount } }
        speciesVisitStats {
          distinctSpeciesPercentile
          distinctSpecies { species { ...InsightSpecies } visitCount medias { ...InsightMedia } }
        }
        namedBirdVisitStats { namedBirds { identificationCount namedBird { ...InsightNamedBird } } }
      }
    }
  }
}
"""
    + _NAMED
    + _MEDIA
)

FEEDER_REPORT = """
query AccountInsightFeederReport($userId: ID!, $feederId: ID, $start: DateTime, $end: DateTime) {
  me { user { id } }
  userFeederReport(userId: $userId, feederId: $feederId, start: $start, end: $end) {
    userId feederIds reportLabel
    reportPeriod { startDate endDate }
    visitAggregateMetrics {
      busiestHourOfDay numberOfUniqueSpecies totalNumberOfVisits
      speciesVisitSummaries { speciesId speciesLabel numberOfVisits peakHour }
    }
  }
}
"""

REMOTE_CONNECTIONS = """
query AccountInsightRemoteConnections {
  me {
    user { id }
    remoteFeederConnections {
      id createdAt expiresAt slotNumber feederName
      remoteFeeder { id feederName title tier type isUnlocked isUserConnected }
    }
  }
}
"""
