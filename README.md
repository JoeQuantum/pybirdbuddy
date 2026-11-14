# pybirdbuddy

This fork is preparing a reconciled client candidate. See
[the reconciliation notes](RECONCILIATION.md) for preserved behavior,
upstream changes, packaging and validation commands.

```python
import asyncio
import pprint

from birdbuddy.client import BirdBuddy

bb = BirdBuddy("user@email.com", "Pa$$w0rd")

# Using coroutines with async/await:
async def async_test():
    await bb.refresh()
    pprint.pprint(bb.feeders)

# Without async/await, including from a top-level module:
result = asyncio.run(bb.refresh())
pprint.pprint(result)
pprint.pprint(bb.feeders)
```

Note: only password login is supported currently. Google and other SSOs are not
supported. If you've already set up your Bird Buddy with SSO, one option could be
to register a new account with a password, and then redeem an invite code to your
Bird Buddy under the new account. Some fields will be missing (such as firmware
versions and off-grid status).

The `feeders` property is a dictionary keyed by feeder ID, with these fields:

```graphql
fragment ListFeederFields on FeederForPrivate {
  battery {
    charging    # Boolean
    percentage  # Int (93)
    state       # String (enum: "HIGH")
  }
  food {
    state       # String (enum: "LOW")
  }
  id            # String (UUID)
  name          # String
  signal {
    state       # String (enum: "HIGH")
    value       # Int (rssi: -41)
  }
  state         # String (enum: "READY_TO_STREAM")
  temperature {
    value       # Int
  }
}
```

## Optional account insights

The separate `AccountInsights` adapter provides eight explicit read-only queries:
hardware, preferences, species, named birds, weekly recap, yearly review, feeder
reports and remote connection metadata. Construction performs no request; reads
use bounded account/language-scoped caches and never add background polling or
mutations. See [the account-insights contract](docs/account-insights.md) for
examples, selected fields, resource limits and fixture-validation limitations.

## Translations

API responses can return translated strings by setting the client's
`language_code` property.
Language codes are parsed using [`langcodes`](https://pypi.org/project/langcodes/)

```python
from birdbuddy.client import BirdBuddy

async def main():
    bb = BirdBuddy("user@email.com", "Pa$$w0rd")
    bb.language_code = "de"

    collections = await bb.refresh_collections()
    birds = [c.species.name for c in collections.values()]
    print(birds)
```
