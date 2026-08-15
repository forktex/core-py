# forktex_core.types

Base Pydantic models and JSON type aliases: the wire-shape opinion (`BaseAppModel`), the domain-shape opinion (`BaseValueObject`), their combination, and the `UtcDateTime`/`UtcDate` field types that keep timestamps consistent with the rest of the library.

## Install

Always bundled — `types` is a level-0 extra with no packages behind it. `pip install forktex-core` is enough; `forktex-core[types]` resolves but installs nothing extra. Depends on `pydantic` (a core dependency) and on `forktex_core.iso`, a level-0 sibling. Nothing here ever raises `ImportError` for a missing extra.

## Wiring

Shape C — plain classes and type aliases. Nothing to initialise, nothing global, no teardown. Import a base and subclass it.

```python
from forktex_core.types import BaseAppModel, BaseValueObject, BaseWireValueObject, UtcDate, UtcDateTime


class CreateUserRequest(BaseAppModel):
    first_name: str
    created_at: UtcDateTime
    birth_date: UtcDate


# Accepts either input shape
CreateUserRequest.model_validate({"firstName": "Ada", "createdAt": "2026-01-01T00:00:00Z", "birthDate": "1990-01-01"})
CreateUserRequest.model_validate({"first_name": "Ada", "created_at": "2026-01-01T00:00:00Z", "birth_date": "1990-01-01"})

# Emits camelCase without anyone passing by_alias=True
request.model_dump()   # {"firstName": "Ada", "createdAt": ..., "birthDate": ...}


class Percentage(BaseValueObject):     # frozen + hashable, plain field names
    basis_points: int


class Money(BaseWireValueObject):      # frozen + hashable + camel aliases
    amount_cents: int
    currency_code: str
```

## Public surface

`__all__`:

| Name | What it is |
| --- | --- |
| `BaseAppModel` | `BaseModel` with `alias_generator=to_camel`, `validate_by_name=True`, `validate_by_alias=True`, `serialize_by_alias=True`. For anything crossing a boundary: HTTP body, JSON storage, queue payload. |
| `BaseValueObject` | `BaseModel` with `frozen=True` — immutable after construction, auto `__hash__` on field values, structural equality. Internal domain primitives. |
| `BaseWireValueObject` | `BaseValueObject` plus `BaseAppModel`'s alias config. A value object that also crosses a boundary. |
| `UtcDateTime` | `Annotated[datetime, PlainSerializer(to_iso, return_type=str)]` — a field type, not a base class. |
| `UtcDate` | `Annotated[date, PlainSerializer(to_date_iso, return_type=str)]`. |
| `JsonScalar` | `str \| int \| float \| bool \| None`. |
| `JsonValue` | `JsonScalar \| list[JsonValue] \| dict[str, JsonValue]` — the honest annotation for data crossing a JSON/JSONB boundary. |
| `JsonObject` | `dict[str, JsonValue]`. |

Use `object`, not `JsonValue`, where a value is genuinely arbitrary and only passed through (e.g. what a caller hands a normaliser *before* it becomes JSON).

## Errors

This module defines no exceptions. Everything that can fail is Pydantic's: `pydantic.ValidationError` on `model_validate()` with a missing/ill-typed field, and on assignment to a frozen model. `UtcDateTime`'s serializer delegates to `forktex_core.iso.to_iso`, which raises `TypeError` if handed a non-`datetime`.

## Gotchas

- **`UtcDateTime` normalises on the way out, not in.** Validating `"2026-01-01T12:00:00+05:00"` keeps that offset on the in-memory value; UTC conversion happens when the model is serialized. `PlainSerializer` runs in both dump modes, so `model_dump()` and `model_dump(mode="json")` both yield the canonical string.
- **A plain `datetime` field drifts.** Pydantic's own default is `Z`-suffixed, offset-preserving and not UTC-forced — it disagrees with the text `log`, `grid`, `flow`, and `database` all produce through `iso.to_iso()`. Annotate wire-facing datetimes as `UtcDateTime`.
- **`frozen=True` does not deep-freeze.** A `list`/`dict` field on a `BaseValueObject` subclass can still be mutated in place, changing the instance's hash or leaking a shared reference between instances. Prefer `tuple` / `frozenset` / nested `BaseValueObject`.
- **`BaseValueObject` has no aliases.** camelCase input raises `ValidationError`; that is the point of the split. Reach for `BaseWireValueObject` when the value crosses a boundary.
- **`UtcDate` exists for symmetry.** A plain `date` field already serializes as `YYYY-MM-DD`, and Pydantic rejects a `datetime` with a nonzero time where a `date` is expected. `UtcDate` fixes no live bug.
- **Some type checkers do not see the generated `__hash__`.** Using a frozen model as a dict key is correct at runtime even when pyright complains.
- **Internal adoption is thin.** Outside `error.ErrorEnvelope` and `space.config`, most `forktex_core` modules subclass `pydantic.BaseModel` directly — do not expect these bases everywhere in the repo.
