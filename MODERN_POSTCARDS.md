# Modern postcard workflow

Display data comes from `get_feed_item(id)` or the paginated feed. Both are
queries. They select all image/video assets, recognized species and available
feeder identity. `normalize_visit()` retains the raw selected payload separately
from its compact fields. Missing dates, sparse media and unknown variants remain
explicit; an image preview does not replace the full media list.

`identify_postcard(id)` runs inference and changes server state. It is an
explicit action and is not needed to refresh an image. The returned analysis
must identify the requested new postcard. The client does not promise that
repeating inference is harmless or idempotent.

`collect_postcard(id, share=False, identify=False)` uses the server's current
selection. Identification and sharing are separate opt-ins. The modern helper
does not interpret legacy confidence thresholds or species strategies. The
Home Assistant adapter requires an explicit selection policy and rejects
unsupported legacy inputs before invoking these methods.

Successful collection returns a validated `CollectedPostcard`, with all selected
media and a `CollectionReceipt`. The receipt retains the requested ID, the
server's first-collection flag when present, and whether collection was confirmed
by a subsequent read. The replacement `postcardCollectedDetails.collectedPostcard`
is preferred over the deprecated field. A historical response that selected only
the first-collection boolean can still use its valid deprecated postcard field.

`share_requested` records the request. `sharing_confirmed` remains unknown
because the selected result does not establish per-media sharing state. A
collection result must not be presented as proof that sharing succeeded.

A transport failure or unusable collection result triggers one read of the
original feed item, never another collect mutation. If that read explicitly
returns a collected postcard, the receipt marks it as reconciled. Otherwise the
client raises `PostcardOutcomeUnknownError`. Collection can replace the feed item
ID, so disappearance of the original ID is inconclusive. Inspect the Bird Buddy
app before manually retrying an unknown outcome. An explicit HTTP rejection,
such as a rate limit, keeps its typed error and retry hint.

All of these behaviors are verified with offline selected-response fixtures and
failure tests. The November schema establishes field availability, not account
entitlement, confidence scale, empty-selection semantics or hardware behavior.
No authenticated account operation was used to validate this candidate.
