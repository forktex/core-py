# `forktex_core.iso` — Canonical ISO-8601 date/time

> One module deciding how a datetime becomes text and back: always UTC, always
> `datetime.isoformat()`'s default shape. `log`, `grid`, `flow`, and `database`
> delegate here instead of each hand-rolling the same UTC-normalization dance.

## Overview

`iso` is stdlib-only — no extra dependencies. A Level-0 primitive alongside
`log`/`error`/`types` — the surface is five small functions.

Before this module existed, the same "naive-or-aware, `.isoformat()`/
`.fromisoformat()`" logic was hand-rolled in five places across the codebase,
with real drift: some call sites forced UTC before formatting, some didn't;
none agreed on whether a naive datetime should be assumed UTC or rejected.
`grid`'s canonical stored/indexed temporal text depends on getting this
exactly right — that's the one place the algorithm here is copied from
verbatim, not invented.

## Quick start

```python
from datetime import date, datetime, timezone, timedelta
from forktex_core.iso import now, to_iso, from_iso, to_date_iso, from_date_iso

now()  # datetime.now(timezone.utc)

to_iso(now())  # "2026-08-12T10:30:00.123456+00:00"
to_iso(datetime(2026, 1, 1, 12, 0))  # naive → assumed UTC: "2026-01-01T12:00:00+00:00"
to_iso(datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=5))))
# aware, non-UTC → converted: "2026-01-01T07:00:00+00:00"

from_iso("2026-01-01T12:00:00+00:00")  # → datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
from_iso("2026-01-01T12:00:00Z")  # "Z" suffix also accepted, same result

to_date_iso(date(2026, 1, 1))  # "2026-01-01"
from_date_iso("2026-01-01")  # date(2026, 1, 1)
```

## API reference

```python
def now() -> datetime
    # datetime.now(timezone.utc) — the one canonical "current time" call.

def to_iso(value: datetime, *, strict: bool = False) -> str
    # Naive input is assumed UTC; aware input is converted to UTC;
    # rendered via datetime.isoformat() (its default precision/offset shape).
    # strict=True raises ValueError on naive input instead of assuming UTC.

def from_iso(value: str, *, strict: bool = False) -> datetime
    # Inverse of to_iso() — always returns a UTC-aware datetime, regardless
    # of whether the string had an offset.
    # strict=True raises ValueError if the string has no offset instead of
    # assuming UTC.

def to_date_iso(value: date) -> str
    # date.isoformat() — "YYYY-MM-DD". A full datetime is also accepted
    # (datetime is a subclass of date) — its date component is used, not
    # the full timestamp.

def from_date_iso(value: str) -> date
    # date.fromisoformat(value).
```

## Why "naive is assumed UTC" instead of raising

Every existing caller in this codebase already relied on that leniency before
this module existed (`grid`'s temporal field types, `flow`'s retry-timestamp
encoding) — centralizing the behavior isn't introducing a new leniency, it's
naming and testing the one that was already implicit and scattered. If a
future consumer needs a strict "reject naive input" mode, pass `strict=True`
to either function — it raises `ValueError` on naive input instead of
assuming UTC. The default stays `False`, so `grid`'s canonical stored text
format is untouched.

## Consumers

```
iso ──── used by ──── log       (JSON "timestamp" field)
iso ──── used by ──── types     (UtcDateTime/UtcDate field types on BaseAppModel/BaseWireValueObject)
iso ──── used by ──── grid      (canonical stored/indexed date/datetime text)
iso ──── used by ──── flow      (pagination cursors, retry "next_attempt_at")
iso ──── used by ──── database  (JSON-column datetime fields)
iso ──── (no deps on) ── any other forktex_core module
```

`forktex_core.types` (`BaseAppModel`/`BaseValueObject`) now depends on `iso`
too — `UtcDateTime` (`Annotated[datetime, PlainSerializer(to_iso)]`) and its
`UtcDate` counterpart (see [`types.md`](types.md)) are exactly the field
type this doc used to describe as "a natural future addition." It's a
Level-0-to-Level-0 dependency, the same relationship `log` already has with
`iso` — not a dependency on a facade or substrate module, so it doesn't
compromise either primitive's "zero-dep" positioning.

## Known limitations (not fixed here — documented as backlog items)

The 5-function surface is deliberately minimal. Plausible additions a future
Level-1/2 consumer might need, none of which exist yet:

- **No non-UTC timezone-conversion helper** — everything here normalizes to UTC by design; converting *to* a specific non-UTC zone for display (e.g. a user's local time) is out of scope today.
- **No ISO-8601 validity predicate** (`is_iso(s: str) -> bool`) — the only way to check if a string is valid ISO-8601 today is to try `from_iso()`/`from_date_iso()` and catch `ValueError`.
- **No duration/`timedelta` formatting helper** — only point-in-time values (`datetime`/`date`) are covered, not durations.

## Agent guide

### Edge cases

| Scenario | Behaviour |
|---|---|
| `to_iso(naive_datetime)` | Assumed UTC (`.replace(tzinfo=UTC)`), not rejected |
| `to_iso(naive_datetime, strict=True)` | Raises `ValueError` instead of assuming UTC |
| `to_iso(aware_datetime)` | Converted to UTC (`.astimezone(UTC)`) |
| `from_iso(naive_looking_text, strict=True)` | Raises `ValueError` instead of assuming UTC |
| `to_iso(a_date_not_a_datetime)` | Raises `TypeError` pointing at `to_date_iso()` — not a confusing `AttributeError` on `.tzinfo` |
| `from_iso("...+02:00")` | Parsed then converted to UTC — always returns a UTC-aware datetime |
| `from_iso("...Z")` | `Z` suffix accepted (Python 3.11+) and treated as UTC, same as `+00:00` |
| `to_iso(dt)` where `dt` has zero microseconds | No fractional-second suffix (matches `datetime.isoformat()`'s default) |
| `grid`'s `DateTimeType`/`DateType.normalize` | Delegate to `to_iso`/`to_date_iso` — output is byte-identical to before the relocation (regression-tested in `tests/test_iso/test_iso.py`) |
| `to_date_iso(a_full_datetime)` | Extracts the date component (`.date().isoformat()`) rather than emitting the full timestamp — `datetime` is a subclass of `date`, so this isn't rejected, just normalized |
| `from_iso`/`from_date_iso` given a malformed string | Raises `ValueError` (from stdlib's own `fromisoformat`) — not swallowed, not a different exception type |
| `types.UtcDateTime` vs a plain `datetime` field | `UtcDateTime` always UTC-normalizes; a plain field preserves Pydantic's own default (offset-preserving, `Z`-suffixed, not UTC-forced) — see `docs/types.md`'s Anti-patterns |

### Integration map

```
iso ──── replaces ──── hand-rolled datetime.isoformat()/.fromisoformat() calls
iso ──── (standalone) ── any Python process; stdlib only, no third-party dep
```

### Checklist

- [ ] Any "current UTC time" call uses `iso.now()`, not `datetime.now(timezone.utc)`
- [ ] Any datetime-to-string formatting uses `iso.to_iso()`, not `.isoformat()` directly
- [ ] Any datetime/date field on a `BaseAppModel`/`BaseWireValueObject` subclass uses `types.UtcDateTime`/`types.UtcDate`
- [ ] A `date` (not `datetime`) value uses `to_date_iso()`/`from_date_iso()`, not `to_iso()`/`from_iso()`
