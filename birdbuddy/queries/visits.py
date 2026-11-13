"""Read-only feed details shared by polling, image refresh and media lookup."""

_VISIT_FRAGMENTS = """
fragment VisitMediaFields on Media {
  __typename
  id
  createdAt
  thumbnailUrl
  ... on MediaImage { contentUrl(size: ORIGINAL) }
  ... on MediaVideo { contentUrl(size: ORIGINAL) }
}
fragment VisitSpeciesFields on Species {
  __typename
  id
  name
  iconUrl
}
fragment VisitCollectionFields on AnyCollection {
  __typename
  ... on Collection {
    id
    coverCollectionMedia {
      id
      feeder { __typename id name }
      media { ...VisitMediaFields }
    }
  }
  ... on CollectionBird { species { ... on Species { ...VisitSpeciesFields } } }
}
fragment VisitReadFields on FeedItem {
  __typename
  id
  createdAt
  ... on FeedItemNewPostcard {
    expiresAt
    canBeShared
    mediaImageCount
    hasVideoMedia
    inferenceConfidenceLevel
    inferenceExecutionMode
    inferenceType
    reanalyzeAvailability
    feeder { __typename ... on Feeder { id name } }
    medias { ...VisitMediaFields }
    sightingReportPreview {
      sightings {
        __typename
        ... on SightingRecognizedBird {
          species { ... on Species { ...VisitSpeciesFields } }
        }
        ... on SightingRecognizedBirdUnlocked {
          species { ... on Species { ...VisitSpeciesFields } }
        }
      }
    }
  }
  ... on FeedItemCollectedPostcard {
    expiresAt
    hasMysteryVisitor
    hasNewSpecies
    inferenceExecutionMode
    inferenceType
    mediaSpeciesNameIdentificationConfidenceLevel
    feeder { __typename ... on Feeder { id name } }
    medias { ...VisitMediaFields }
    species { ...VisitSpeciesFields }
  }
  ... on FeedItemSpeciesSighting {
    media { ...VisitMediaFields }
    collection { ...VisitCollectionFields }
  }
  ... on FeedItemSpeciesUnlocked {
    media { ...VisitMediaFields }
    collection { ...VisitCollectionFields }
  }
  ... on FeedItemMysteryVisitorNotRecognized { media { ...VisitMediaFields } }
  ... on FeedItemMysteryVisitorResolved { media { ...VisitMediaFields } }
}
"""

DETAIL = (
    """
query VisitDetail($feedItemId: ID!) {
  feedItem(feedItemId: $feedItemId) { ...VisitReadFields }
}
"""
    + _VISIT_FRAGMENTS
)
