# Client reconciliation candidate

This unreleased `0.1.0.dev1` candidate combines the runtime from
[`jhansche/pybirdbuddy` at `207ed47`](https://github.com/jhansche/pybirdbuddy/tree/207ed47578016e7223708e94d36a6b5be8c3c1e4)
with the behavior retained from the fork's `3e92c3f` release. The upstream
`35e701a` main revision changes only its package version.

The candidate retains:

- `FeederDeviceVersion`, `FeederHousingType`, their accessors and the hardware
  fields requested for both owner and member accounts.
- `NoResponseError` for transport and non-JSON response failures, preserving
  the original exception as its cause.
- The legacy `SightingFinishStrategy` fallback to `MYSTERY` for unknown values.
- The current feed types, Ultra Frenzy profile and unknown-enum handling.

`V2_NOOK` is newly recognized from the November 2026 schema. Its enum value is
not a claim of tested Nook hardware support. `Feeder.version` remains the
firmware string; `Feeder.device_version` is the hardware generation.

## Legacy selection behavior

Upstream now uses an already recognized species to correct an ambiguous or
mystery sighting when the legacy best-guess strategy is allowed. The fork's
historical issue 40 test previously asserted the known wrong Fish Crow guess
and skipped the desired Carolina Wren correction. That original capture is
retained as JSON and now tests the corrected result. `RECOGNIZED` still does
not make an extra species-selection request.

Modern identification and collection helpers are available, while the old
report-based methods remain with upstream's deprecation warnings. This
candidate does not change Home Assistant's dependency pin or automation
contracts. Confidence-policy migration, nullable collection outcomes,
authentication retries and feed checkpoints remain separate follow-up work.

## Schema provenance

`tests/fixtures/schema.json` contains the full production introspection snapshot
retrieved on 2026-11-13 at 19:09:10 UTC through one unauthenticated, read-only
request. It contains 568 types, including eight introspection types. Its source,
timestamp and file checksum are recorded in `tests/fixtures/schema-provenance.json`.
The snapshot is imported from the previously reviewed API catalog; this work did
not make a live API request. Query validation proves structural compatibility,
not account authorization, resolver success or tested hardware support.

The optional `scripts/dump_payloads.py` helper captures account reads by default.
Authentication exchanges tokens, but it does not identify or collect a postcard
unless the corresponding `--identify-postcard-id ID` or
`--collect-postcard-id ID` flag is supplied. Each mutation uses the exact supplied
ID; collection does not implicitly identify or share. Old mutation environment
variables are not used. This reconciliation did not run the helper on an account.

Only allowlisted sanitized data is written by default. Unknown fields, locations
and raw server errors are omitted; report/auth tokens are replaced wholesale.
Identifiers are remapped, names are private except on immediate species objects,
URLs become nonfunctional examples and dates become fixed synthetic timestamps.
Review captures before turning them into fixtures. `--raw-output PATH` explicitly
creates a separate private file with mode 0600 and refuses existing files.

## Feed intake and checkpoints

`retrieve_feed_batch()` returns a `FeedBatch` without advancing accepted state.
Persist `batch.nodes` and `batch.checkpoint.to_dict()` in the same durable
transaction, then call `ack_feed_batch(batch.batch_id)`. Pending batches replay
without another request, and node copies protect the pending payload from caller
edits. Restore the saved checkpoint with `import_feed_checkpoint()` before the
next poll. `export_feed_checkpoint()` always exports accepted state only.

The first scan deliberately reads the latest 20 items; use `initial_limit` to
change that window or `backfill=True` to traverse history. Subsequent scans use
a five-minute overlap and ID deduplication. Equal-timestamp boundaries continue
across pages; null or malformed timestamps retain their IDs without ending a
scan. Arrivals older than the overlap require deliberate backfill.
Checkpoints retain at most 10,000 IDs; exhaustion raises instead of dropping
records.

Versioned checkpoint imports reject malformed or unknown formats. Repeated or
missing pagination cursors, malformed completion metadata and the default
100-page budget raise `PaginationError`, leaving accepted state unchanged.
Collection pagination uses the same incomplete-traversal checks. Unsupported
backward feed arguments now raise instead of being silently ignored.

The legacy `refresh_feed()` list wrapper no longer automatically advances a
timestamp. Ongoing consumers must migrate to retrieval plus explicit
acknowledgement; otherwise they keep receiving the pending batch. Its explicit
`since` comparison remains strict; the batch interface uses an inclusive bound.

## Device controls

Audio, off-grid and power-profile controls accept a positive finite `timeout`
in seconds (default 30). One deadline covers owner lookup, the initial mutation,
progress checks and final refresh. The mutation is issued once. Audio and power
use their query-based progress checks; off-grid uses the schema's mutation-based
check. Finished and failed responses update the observed cache immediately.

`FeederControlError` and its timeout/unavailable subclasses live in
`birdbuddy.feeder`. Their `action_completed` attribute is `True` when the action
finished but final refresh failed, `False` for a verified rejection/failure, and
`None` when completion is uncertain. Refresh uncertain state before retrying a
mutation. Cancellation propagates, and timeout preserves the last observed state.

Controls require an explicit owner role, including when passed only a feeder ID.
Audio additionally respects an explicitly false `supportsAudio`; missing data
remains unknown. The normal account queries select that capability. The current
schema's only feature lock is `LIVESTREAM`, so it cannot establish eligibility
for these controls or paid power profiles. The server retains that decision.
`selected_power_profile` and the existing `power_profile` both report the
configured selection; neither claims to report actual Smart Energy behavior.
The schema separately exposes `FeederSmartEnergyEnabled.runningPowerProfile`;
these core accessors do not select that field.

`FeederOptions` describes all eight supported update inputs, including housing,
mounting and solar-roof enums. Unknown keys, invalid values and conflicting
aliases fail before any request. `offlineMode` remains accepted as an `offGrid`
alias. Off-grid changes use the bounded dedicated workflow; other options use
`feederUpdate`. Mixed metadata and off-grid changes are sequential, so a later
failure can leave earlier changes applied. Results report the observed state.

## Packaging and checks

Package metadata and dependencies live in `pyproject.toml`; the old
requirements files and `setup.py` delegate to it. The runtime still targets
Python 3.10 and later. `aiohttp` and `propcache` are explicit dependencies
because runtime modules import them directly. The wheel includes `py.typed`.

```sh
python -m pip install -e '.[dev]'
python -m pytest
ruff check .
ruff format --check .
pyright
python -m build
```

The imported upstream license text is preserved in `LICENSE.upstream` as
provenance. Overall fork license metadata is intentionally not changed by
this reconciliation. No release tag is created or retargeted by these changes.
