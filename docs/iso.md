# forktex_core.iso

The one module that decides how a datetime becomes text and back: always UTC, always `datetime.isoformat()`'s default shape. `log`, `types`, `grid`, `flow`, and `database` all delegate here rather than each hand-rolling the same normalisation.

## Install

Always bundled — `iso` is a level-0 extra with no packages behind it. `pip install forktex-core` is enough; `forktex-core[iso]` resolves but installs nothing extra. Stdlib only, no imports from any other `forktex_core` module. Nothing here ever raises `ImportError` for a missing extra.

## Wiring

Shape C — five plain functions. No state, no client, no setup, no teardown.

```python
from datetime import date, datetime, timedelta, timezone

from forktex_core.iso import from_date_iso, from_iso, now, to_date_iso, to_iso

now()                                          # datetime.now(UTC)

to_iso(now())                                  # "2026-08-12T10:30:00.123456+00:00"
to_iso(datetime(2026, 1, 1, 12, 0))            # naive assumed UTC: "2026-01-01T12:00:00+00:00"
to_iso(datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=5))))
                                               # "2026-01-01T07:00:00+00:00"

from_iso("2026-01-01T12:00:00+00:00")          # UTC-aware datetime
from_iso("2026-01-01T12:00:00Z")               # same result

to_date_iso(date(2026, 1, 1))                  # "2026-01-01"
from_date_iso("2026-01-01")                    # date(2026, 1, 1)
```

## Public surface

`__all__`:

| Name | Behaviour |
| --- | --- |
| `now() -> datetime` | `datetime.now(UTC)` — the canonical "current time" call. |
| `to_iso(value, *, strict=False) -> str` | Naive input is assumed UTC, aware input is converted to UTC, then `isoformat()`. `strict=True` raises `ValueError` on naive input instead. |
| `from_iso(value, *, strict=False) -> datetime` | Parses ISO-8601 and always returns a UTC-aware datetime. `strict=True` raises `ValueError` when the text carries no offset. |
| `to_date_iso(value) -> str` | `YYYY-MM-DD`. A `datetime` is accepted (it subclasses `date`) and its date component used. |
| `from_date_iso(value) -> date` | `date.fromisoformat(value)`. |

## Errors

No custom exception type. Three stdlib ones:

| Raised | When |
| --- | --- |
| `TypeError` | `to_iso()` given something that is not a `datetime`. The message points at `to_date_iso()`. |
| `ValueError` | `to_iso(naive, strict=True)`, or `from_iso(no_offset, strict=True)`. |
| `ValueError` | Malformed text in `from_iso()` / `from_date_iso()`, propagated from stdlib `fromisoformat` — not swallowed, not rewrapped. |

Catch `ValueError` at a parse boundary; there is nothing else to catch.

## Gotchas

- **Naive means UTC by default.** `to_iso`/`from_iso` assume it rather than raising, because every existing caller relied on that before this module existed. Pass `strict=True` if you have no such assumption to preserve — the default stays `False` so `grid`'s stored text format is untouched.
- **`strict` is keyword-only and exists only on `to_iso`/`from_iso`.** The date functions have no strict mode; there is no offset to be ambiguous about.
- **Precision follows `isoformat()`.** Microseconds appear only when nonzero — do not assume a fixed-width string.
- **`to_iso(a_date)` raises.** `date` is not a `datetime`, and passing one is a `TypeError`, not a silent coercion. It is the `to_date_iso()` direction that is lenient: it accepts a full `datetime` and truncates.
- **UTC only.** There is no helper to convert into a non-UTC display timezone, no ISO validity predicate, and no `timedelta` formatting. The surface is deliberately these five functions.
