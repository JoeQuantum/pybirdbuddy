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
