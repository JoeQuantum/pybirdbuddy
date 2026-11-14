# Optional account insights

`AccountInsights` adds explicit read-only queries alongside ordinary feeder polling.
Create one adapter for each authenticated client and retain it for that client’s
lifetime. Constructing it makes no request. Calling a method performs a read only
when its cached result has expired. It creates no entities, periodic jobs,
connections, identification requests, collection actions, or preference changes.
Call `await insights.async_close()` when unloading its owner. Close rejects new
reads, cancels and awaits shared in-flight reads, and clears cached data. It is
idempotent and prevents a late network response from publishing into the cache.

```python
from birdbuddy.insights import AccountInsights

# client is already authenticated and, in Home Assistant, bound to its account.
insights = AccountInsights(client)
hardware = await insights.hardware()
named_birds = await insights.named_birds(limit=25)
weekly = await insights.weekly_recap()
```

The current account UUID comes from `client.user`; no method accepts a user UUID.
Every uncached request selects `me.user.id` and checks both the current and returned
identity before publishing a result. Identity changes invalidate cached data. A
report also verifies its returned `userId`. A selected feeder must already belong
to the authenticated client, and the returned report cannot include another
feeder when one was selected. These checks complement the client’s authentication
and account-binding protections; they do not grant additional API permissions.

| Method | Selected data | Cache TTL |
| --- | --- | --- |
| `hardware(limit=50)` | Owner/member/pending role, hardware/housing/mount/solar, explicit capability flags, account feature locks, premium role/type/active flag, owner video quality/camera field of view, configured power profile and Smart Energy state/running profile | 5 minutes |
| `preferences(limit=50)` | Ignored species IDs and mystery-visitor preference | 5 minutes |
| `species(species_id)` | Stable ID, localized and scientific names, taxonomy variant, icon, exact-species alternate/unofficial name flags | 24 hours |
| `named_birds(limit=50)` | Individual ID/nickname, recognition state, personality/relationship, species, first/last seen, vendor visit count, usual time of day, thumbnail previews | 15 minutes |
| `weekly_recap(limit=50)` | Vendor-selected ISO week/year, visit and observation totals, daily/feeder/species counts, mystery count, available sample media, streaks | 1 hour |
| `yearly_review(limit=50)` | Vendor-selected year, visit/diversity percentiles, top days, monthly/species counts, named-individual identification counts and available sample media | 1 hour |
| `feeder_report(feeder_id=None, start=None, end=None, limit=50)` | Returned account/feeder scope, authoritative report period and label, visit/species totals, busiest hour and species summaries | 1 hour |
| `remote_connections(limit=50)` | Connection and remote-feeder IDs, names, expiry, slot, tier, type, connected/unlocked flags | 5 minutes |

A hardware `featureLocks` entry reports the vendor’s explicit feature name. It does
not explain why that feature is restricted. Premium role/type flags do not imply
an undocumented entitlement rule. A member or pending feeder has no fabricated
owner settings. Unknown enum strings remain visible. Smart Energy `NOT_SUPPORTED`
and its reason remain distinct from `DISABLED`; `runningPowerProfile` is separate
from the configured `powerProfile`, and the adapter never substitutes one for the
other when a value is unavailable. Selected owner video quality and camera field
of view are reported settings only; this does not enable their controls.

`supportsLivestreamSnapshot` is a `FeederCapabilitySupport` enum, not a Boolean.
Its known values are `SUPPORTED`, `UNAVAILABLE`, and `REQUIRES_FIRMWARE_UPDATE`.
Do not use string truthiness to enable a capability. The adapter preserves these
strings and unknown future values; only an explicit supported value establishes
the reported capability. Reading it does not start a snapshot or camera session.

Named birds are individuals, not species totals. `STOPPED` stays stopped, even
when historical statistics exist. Null/missing statistics remain null; no zero
visit count, recent recognition, or new assignment is inferred. Names need not be
unique. Stable species IDs remain suitable for automation filters across language
changes; a returned taxonomy may identify an order, family, or genus rather than
an exact species. Recognition accuracy and support for any particular animal are
not established by these fields.

Weekly and yearly reports have no week/year argument in this schema. The adapter
does not invent historical pagination. The separate feeder-report query does
accept `start` and `end`: locally, both must be aware datetimes with a positive
range of at most 366 days. This is a client resource limit, not a vendor retention
promise. The returned `reportPeriod`, `isoWeek`, or `year` is authoritative. Null
media lists or empty sample-media lists are not evidence of zero visits.

With no explicit range, the vendor selects the reporting period; callers must
not assume it is rolling or lifetime analytics. An `INTERNAL_SERVER_ERROR` for
one feeder or range does not identify an empty period, battery condition, or
retention limit. It remains a `GraphqlError`, without fabricated counts or an
automatic fallback to another range. The failure is not cached and does not
invalidate successful reports for other ranges. A later explicit read can
recover if the vendor returns data; a report error alone does not refresh tokens.

The adapter preserves explicit instants, UTC offsets and fractional seconds; it
does not round requests to whole days or widen a failing range. A short interval
can fail while a wider report for the same day remains usable in its own cache
entry. Equivalent offset encodings describe the same instants. Vendor boundary
inclusion, minimum supported duration and exact filtering precision remain
unverified; an echoed period alone does not establish them. Empty aggregation
is a possible explanation for some errors, not a documented zero-count contract.

`visitAggregateMetrics.busiestHourOfDay` is preserved as vendor summary metadata.
Its `DateTime` date can fall outside `reportPeriod`; it is not an actual visit or
last-seen timestamp. Do not replace its date, derive freshness from it, or treat
it as evidence of a future visit. Keep it separate from the vendor's period and
the adapter's `fetched_at`. Date anchoring and timezone semantics are not
guaranteed, and the related per-species `peakHour` field has not been independently
verified. Projection completeness does not validate these temporal semantics.

Remote connections always carry `entity_scope: remote_connection`. They are not
owned physical devices and do not enter the client’s feeder registry. The
`expired` value is recomputed when a cached result is consumed; null or malformed
expiry is `null`, not a promise of permanent access. Remote metadata works without
physical feeders when the account/API permits it. Community media browsing,
connection changes, sharing, naming actions, preference changes, and Smart Energy
toggles are outside this adapter.

## Result and resource contract

Every method returns `schema_version: 1`, `account_id`, `resource`, `status`, `data`,
`complete`, `limit`, `pagination`, `truncated_fields`, `missing_fields`,
`unknown_variant_fields`, `fetched_at`, and `cache_ttl_seconds`.
Recap results also preserve their selected `variant`; other results use `null`.
`status` is `available`, `unavailable`, `missing`, or `unknown`. Known insufficient
visits/not-available/not-found union variants are ordinary unavailable results.
Unknown union variants remain unknown. Vendor `errorMessage` text is not selected
or returned. Authentication, transport, and GraphQL exceptions propagate to the
caller; optional failures never modify core feeder data.

`complete` describes this selected projection, not every field offered by the
API: it is false after local list truncation, absent selected keys, invalid object
shapes, unknown selected object variants, or unavailable results. Explicit null
fields are preserved. In particular, callers must inspect `stats` and `state`
rather than treating projection completeness as successful bird recognition.
Nested selected lists use the caller’s limit of 1–100, with an explicit path for
each truncation. The selected list fields have no pagination arguments, so
`pagination` is always `{"supported": false, "next_cursor": null}`. Local limits
bound returned data; they do not limit the server’s original list response.

The adapter limits each result to 500,000 encoded bytes and bounds projection
construction before building a large nested tree. A scalar over 8,192 characters
or a result over the local budget raises `InsightLimitError` instead of silently
cutting an ID, name, or URL. The default cache retains at most 32 results and
2,000,000 encoded bytes. Its configurable ceilings are 128 entries and 10,000,000
bytes; each result is also limited by the total cache budget. Up to 16 distinct
reads may be in flight. Concurrent identical requests share one read, including
one failure. Failures are not retained in the cache. Cancelling one waiter does
not cancel the read for other waiters. Returned dictionaries are defensive copies.
Language, account, selected resource, variables, and limit are part of cache keys.
If the client language changes during a read, `InsightLanguageError` prevents
mismatched data from being cached; retry after choosing a stable language.
No force-refresh parameter bypasses the documented TTL.

Billing details, subscription management links, account email/location, feeder
coordinates, serials, invitation/authentication/report tokens, and named-by user
metadata are not selected. Thumbnail addresses are selected only where useful;
they are temporary media references, not durable archive URLs.

## Evidence and limitations

All eight query documents validate offline against the committed schema captured
on 2026-11-13. Tests use explicitly synthetic fixtures with provenance in
`tests/fixtures/insights/provenance.json`; deliberately malformed/null cases test
safe partial-data handling. These tests do not prove current account access,
hardware behavior, premium eligibility, data retention, or availability on an
actual feeder. Live account evidence is tracked separately from this offline
suite and must retain its account, operation and time limitations. Standalone
SDK reads do not validate Home Assistant installation, service wiring, lifecycle
or physical device controls; those require separate integration checks.
