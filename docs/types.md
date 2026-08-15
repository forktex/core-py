# `forktex_core.types` — Base Pydantic models + frozen value objects

> Three base classes every model in the system builds on: the wire-shape opinion (`BaseAppModel`), the domain-shape opinion (`BaseValueObject`), and their combination (`BaseWireValueObject`) — plus `UtcDateTime`, the datetime field type that keeps timestamps consistent with the rest of `forktex_core`.

## Overview

`types` is a Level-0 primitive — always pulled, no extra to install. It depends only on `forktex_core.iso` (for `UtcDateTime`'s canonical UTC serialization) — a Level-0 sibling, not a facade or substrate module, the same relationship `forktex_core.log` already has with `iso`.

- **`BaseAppModel`** — wire-shape opinion: snake_case in Python, camelCase on the wire, both input shapes accepted. Use for any model that crosses a service boundary (HTTP body, JSON storage, queue payload). `serialize_by_alias` is part of the config, so a plain `model_dump()` emits camelCase — the alias generator alone applied only when a caller passed `by_alias=True`, so any call site that forgot contradicted the error envelope travelling on the same connection.
- **`BaseValueObject`** — domain-shape opinion: frozen + hashable, structural equality. Use for internal-only domain primitives (UUID wrappers, monetary amounts, percentages) where two values with the same fields *are* the same value.
- **`BaseWireValueObject`** — both combined. Use when a value object crosses a service boundary (e.g. a typed `Money` field in an HTTP response).
- **`UtcDateTime`** — an `Annotated[datetime, ...]` field type, not a base class. Use it on any datetime field in a `BaseAppModel`/`BaseWireValueObject` subclass.
- **`UtcDate`** — the `date`-only counterpart of `UtcDateTime`, for naming symmetry (a plain `date` field has no offset/timezone ambiguity to normalize, so this mostly exists for consistency).

## Quick start

```python
from forktex_core.types import BaseAppModel, BaseValueObject, BaseWireValueObject, UtcDate, UtcDateTime


class CreateUserRequest(BaseAppModel):
    first_name: str
    last_name: str
    created_at: UtcDateTime
    birth_date: UtcDate


# Same model accepts snake_case (Python clients) AND camelCase (TS clients):
CreateUserRequest.model_validate({"firstName": "Ada", "lastName": "L", "createdAt": "2026-01-01T00:00:00Z"})
CreateUserRequest.model_validate({"first_name": "Ada", "last_name": "L", "created_at": "2026-01-01T00:00:00Z"})

# ...and emits camelCase without anyone remembering `by_alias=True`:
request.model_dump()  # {"firstName": "Ada", "lastName": "L", ...}


class Currency(BaseValueObject):
    cents: int
    code: str


m = Currency(cents=100, code="EUR")
{m, Currency(cents=100, code="EUR")}  # length 1 — structural equality, not identity


class Money(BaseWireValueObject):  # frozen + hashable + camel alias, combined
    amount_cents: int
    currency_code: str
```

## API reference

```python
class BaseAppModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,  # accepts snake_case input too
        validate_by_alias=True,  # accepts camelCase input
        serialize_by_alias=True,  # EMITS camelCase from a plain model_dump()
    )


class BaseValueObject(BaseModel):
    model_config = ConfigDict(frozen=True)
    # frozen=True → immutable after construction + auto-generated __hash__
    # on the field values (structural equality/hashing, not identity)


class BaseWireValueObject(BaseValueObject):
    model_config = ConfigDict(
        frozen=True,
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


UtcDateTime = Annotated[datetime, PlainSerializer(to_iso, return_type=str)]
# Field type, not a base class — annotate any datetime field with it:
#   created_at: UtcDateTime
# Serializes via forktex_core.iso.to_iso(): naive assumed UTC, aware converted
# to UTC, rendered with datetime.isoformat()'s default precision/offset shape.
# NOTE: normalization happens on serialize-out only — validating a string IN
# does not eagerly convert the in-memory value to UTC, only re-dumping it does.

UtcDate = Annotated[date, PlainSerializer(to_date_iso, return_type=str)]
# The date-only counterpart — mostly for naming symmetry with UtcDateTime.
```

## Patterns

### Pattern 1 — `UtcDateTime` on every wire-facing datetime field

```python
from forktex_core.types import BaseAppModel, UtcDateTime


class OrderCreated(BaseAppModel):
    order_id: str
    placed_at: UtcDateTime  # not `placed_at: datetime`
```

Without `UtcDateTime`, a plain `datetime` field serializes with Pydantic's own default (`Z`-suffixed, no forced UTC, offset-preserving) — see Anti-patterns below for exactly what that looks like side by side.

### Pattern 2 — `BaseValueObject` for internal domain primitives

```python
from forktex_core.types import BaseValueObject


class Percentage(BaseValueObject):
    basis_points: int  # 10000 == 100%


# Frozen + hashable: safe as a dict key, safe to share across coroutines
# without worrying about mutation.
rates: dict[Percentage, str] = {}
```

### Pattern 3 — `BaseWireValueObject` when a value object crosses a boundary

```python
from forktex_core.types import BaseWireValueObject


class Money(BaseWireValueObject):
    amount_cents: int
    currency_code: str


# In an HTTP response body: {"amountCents": 999, "currencyCode": "USD"}
# Still frozen + hashable in-process.
```

### Pattern 4 — Nested models inherit the same wire-shape opinion

```python
class Address(BaseAppModel):
    street_name: str


class Person(BaseAppModel):
    first_name: str
    home_address: Address


# model_dump(by_alias=True) → {"firstName": ..., "homeAddress": {"streetName": ...}}
```

## Anti-patterns

```python
# ❌ A plain `datetime` field on a wire model — output drifts from every
# other timestamp forktex_core produces.
class Event(BaseAppModel):
    created_at: datetime


Event(created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=5)))).model_dump(mode="json")
# → {"createdAt": "2026-01-01T12:00:00+05:00"}   ❌ original offset preserved, not UTC


# ✅ UtcDateTime normalizes it.
class Event(BaseAppModel):
    created_at: UtcDateTime


# → {"createdAt": "2026-01-01T07:00:00+00:00"}   ✅ matches log/grid/flow/database

# ❌ Reaching for BaseValueObject when the model crosses a service boundary —
# it has no wire-shape opinion, so the client sees snake_case with no
# alias flexibility. Use BaseWireValueObject instead.

# ❌ Defining your own snake↔camel alias_generator logic instead of
# inheriting BaseAppModel — duplicates a decision this primitive already made.
```

## Interaction with other primitives

- **`types` → `iso`**: `UtcDateTime` delegates to `forktex_core.iso.to_iso()` for serialization — same canonical UTC text as `log`'s JSON output, `grid`'s stored/indexed temporal columns, `flow`'s pagination cursors, and `database`'s JSON-column datetime fields. This is the one place `types` reaches outside itself, and it's a Level-0-to-Level-0 dependency, not a dependency on a facade.
- **`types` ← `error`**: `ErrorEnvelope` (see [`error.md`](error.md)) is itself a `BaseAppModel` — it gets snake↔camel wire aliasing for free, no duplicated logic.
- **Internal adoption is currently thin**: outside `error.ErrorEnvelope` and `space.config`'s config value objects, most other `forktex_core` modules (`grid`, `flow`, `database`, `api`, `catalog`, `graph`) define plain `pydantic.BaseModel` subclasses directly rather than inheriting these bases — worth knowing if you're looking for more usage examples in the repo than the two above.

## Agent guide

### Edge cases

| Scenario | Behaviour |
|---|---|
| `UtcDateTime` with a naive `datetime` | Assumed UTC (not rejected) — offset is still forced (`+00:00`) in the output |
| `UtcDateTime` with an aware, non-UTC `datetime` | Converted to UTC before serializing |
| Plain `datetime` field (no `UtcDateTime`) | Pydantic's default: `Z`-suffixed, offset-preserving, not UTC-forced — a real drift from every other timestamp in the system |
| `model_dump(by_alias=True)` vs `model_dump(mode="json", by_alias=True)` | Same output shape for plain scalar fields; only matters when a field type (like `UtcDateTime`) has a custom serializer, since `mode="json"` is what actually engages `PlainSerializer` for wire output |
| `BaseValueObject` subclass used as a `set`/`dict` key | Works — frozen models get an auto-generated `__hash__`; note some type checkers (pyright) don't statically recognize this, it's still correct at runtime |
| Missing required field on `model_validate()` | Raises `pydantic.ValidationError`, same as any Pydantic model |
| Nested `BaseAppModel` fields | Alias generation applies recursively — nested field names also convert to camelCase on the wire |
| `UtcDateTime` validated from a string (`model_validate(...)`) | The in-memory value keeps whatever offset the input string had — normalization to UTC happens only when the model is serialized back out, not on the way in |
| `UtcDateTime`/`UtcDate` on `BaseValueObject` (not just `BaseAppModel`) | Works the same way — the field type isn't tied to a specific base class |
| `Optional[UtcDateTime]` / `UtcDateTime \| None` | Works normally — `None` passes through, a real value gets UTC-normalized as usual |
| `UtcDate` given a `datetime` with a nonzero time component | Pydantic's own `date`-field validation rejects it with a `ValidationError` before `UtcDate`'s serializer is ever consulted — a zero-time `datetime` coerces fine |
| `BaseValueObject.model_validate()` given camelCase input | Raises `ValidationError` — unlike `BaseAppModel`/`BaseWireValueObject`, it has no alias generator |

### Integration map

```
types ──── used by ──── error (ErrorEnvelope), space.config
types ──── depends on ── iso (UtcDateTime/UtcDate's canonical UTC serialization)
types ──── (no deps on) ── any facade/substrate module (database, grid, flow, api, …)
```

### Checklist

- [ ] Any datetime field on a `BaseAppModel`/`BaseWireValueObject` subclass uses `UtcDateTime`, not a plain `datetime` annotation
- [ ] Any date-only field uses `UtcDate`, for consistency with `UtcDateTime`
- [ ] Cross-boundary payloads (HTTP body, JSON storage, queue payload) use `BaseAppModel`, not a plain `pydantic.BaseModel`
- [ ] Internal-only immutable domain values use `BaseValueObject` — reach for `BaseWireValueObject` only if that same value also crosses a boundary
