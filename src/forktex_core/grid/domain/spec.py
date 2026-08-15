# Copyright (C) 2026 FORKTEX S.R.L.
#
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
#
# This file is part of forktex-core.
#
# For commercial licensing -- including use in proprietary products, SaaS
# deployments, or any context where AGPL obligations cannot be met -- you
# MUST obtain a commercial license from FORKTEX S.R.L. (info@forktex.com).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Declarative, immutable specs — the validated input to every use-case.

These replace the loose ``**kwargs`` a procedural ``create_table`` / ``create_column`` would take.
Single-object invariants (ref⇔relation, derived⇔source, m2m⇔through, key/slug shape)
are enforced here at construction; cross-object and registry-aware invariants
(promotability, bound-projection-only) live on the ``Table``/``Column`` aggregates.

The specs are **frozen Pydantic models**: construction validates, ``model_dump(mode="json")``
serialises to the canonical JSON form, ``model_validate`` is the exact inverse, and Pydantic
emits a JSON Schema for network/agentic consumers. This is the single representation — there is
no parallel dict/DTO layer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forktex_core.grid.domain.binding import BoundBinding
from forktex_core.grid.domain.enums import Cardinality, Materialization, OnDelete, RelationShape
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.identifiers import validate_key, validate_slug

_REF_TYPE = "ref"


class ColumnSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    type_id: str
    cardinality: Cardinality = Cardinality.one
    materialization: Materialization = Materialization.payload
    is_required: bool = False
    is_unique: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    relation_ref: str | None = None  # ref columns: the RelationSpec.key they project
    promoted_column: str | None = None  # native sidecar column name (defaults to key)
    derived_source: str | None = None  # derived columns: "ref_key.target_field"
    default_value: Any = None
    display_order: int = 0

    @model_validator(mode="before")
    @classmethod
    def _default_promoted_column(cls, data: object) -> object:
        # A promoted column defaults its native sidecar name to its key. Done here (before
        # freeze) rather than in the after-validator, which cannot mutate a frozen model.
        if isinstance(data, Mapping):
            mat = data.get("materialization", Materialization.payload)
            if mat == Materialization.promoted and not data.get("promoted_column"):
                return {**data, "promoted_column": data.get("key")}
        return data

    @model_validator(mode="after")
    def _validate(self) -> ColumnSpec:
        validate_key(self.key)

        is_ref = self.type_id == _REF_TYPE
        if is_ref != (self.relation_ref is not None):
            raise BadRequestError("a 'ref' column must have a relation_ref (and vice versa)")

        is_derived = self.materialization is Materialization.derived
        if is_derived != (self.derived_source is not None):
            raise BadRequestError("a derived column must have a derived_source (and vice versa)")
        if is_derived and (is_ref or "." not in (self.derived_source or "")):
            raise BadRequestError("derived_source must be 'ref_key.target_field' and the column not a ref")

        if self.materialization is Materialization.promoted:
            validate_key(self.promoted_column or self.key)
        elif self.promoted_column is not None:
            raise BadRequestError("promoted_column is only valid for promoted columns")
        return self

    @property
    def derived_parts(self) -> tuple[str, str] | None:
        if self.derived_source is None:
            return None
        ref_key, target_field = self.derived_source.split(".", 1)
        return ref_key, target_field

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ColumnSpec:
        """Ergonomic alias for :meth:`model_validate` (unknown keys ignored, enums coerced)."""
        return cls.model_validate(dict(data))


class RelationSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    source: str  # source table slug
    target: str  # target table slug
    shape: RelationShape
    through: str | None = None  # through table slug (many_to_many only)
    on_delete: OnDelete = OnDelete.restrict

    @model_validator(mode="after")
    def _validate(self) -> RelationSpec:
        validate_key(self.key)
        if self.shape.needs_through != (self.through is not None):
            raise BadRequestError("a many_to_many relation requires a through table (and vice versa)")
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RelationSpec:
        return cls.model_validate(dict(data))


class IndexSpec(BaseModel):
    """Declarative index intent (reconciled to a physical Postgres index). ``physical_name``
    and reconciliation ``state`` are outputs, not intent, so they are not part of the spec."""

    model_config = ConfigDict(frozen=True)

    column_keys: tuple[str, ...]
    index_kind: str = "btree"
    is_unique: bool = False

    @model_validator(mode="after")
    def _validate(self) -> IndexSpec:
        if not self.column_keys:
            raise BadRequestError("an index must cover at least one column")
        for key in self.column_keys:
            validate_key(key)
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IndexSpec:
        return cls.model_validate(dict(data))


class TableSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    slug: str
    label: str
    namespace: str = ""
    binding: BoundBinding | None = None
    scope_predicate: dict[str, Any] | None = None  # always-AND-ed filter AST
    natural_key: tuple[str, ...] = ()
    columns: tuple[ColumnSpec, ...] = ()
    is_system: bool = False

    @model_validator(mode="after")
    def _validate(self) -> TableSpec:
        validate_slug(self.slug)
        keys = [c.key for c in self.columns]
        if len(keys) != len(set(keys)):
            raise BadRequestError(f"duplicate column keys in table '{self.slug}'")
        return self

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TableSpec:
        """Ergonomic alias for :meth:`model_validate` (columns/binding coerced natively)."""
        return cls.model_validate(dict(data))

    @classmethod
    def from_dicts(
        cls,
        *,
        slug: str,
        label: str,
        columns: Sequence[Mapping[str, Any]],
        namespace: str = "",
        binding: BoundBinding | Mapping[str, Any] | None = None,
        scope_predicate: Mapping[str, Any] | None = None,
        natural_key: Sequence[str] = (),
        is_system: bool = False,
    ) -> TableSpec:
        """Build a table spec whose columns are supplied as plain dicts.

        Covers every field of the spec, not just the columns: ``binding`` (accepted
        as an ``Overlay``/``Extension`` or as its dict form),
        ``scope_predicate`` and ``natural_key`` used to be silently dropped, so a
        consumer declaring from JSON could not express a bound table at all.
        """
        return cls(
            slug=slug,
            label=label,
            namespace=namespace,
            binding=binding,  # pyright: ignore[reportArgumentType] — pydantic coerces the mapping
            scope_predicate=dict(scope_predicate) if scope_predicate is not None else None,
            natural_key=tuple(natural_key),
            is_system=is_system,
            columns=tuple(ColumnSpec.model_validate(dict(c)) for c in columns),
        )


__all__ = ["ColumnSpec", "IndexSpec", "RelationSpec", "TableSpec"]
