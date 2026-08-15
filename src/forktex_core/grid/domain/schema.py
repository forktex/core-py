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

"""The ``Schema`` — a namespace-wide, in-memory snapshot of the whole grid schema.

This is the load-bearing data structure of the JSON management surface: the *state* the
declarative engine reconciles toward. It is a frozen Pydantic model, so it serialises to a
JSON document (``to_document``) and rebuilds from one (``from_document``) with no bespoke
codec — and ``describe`` out == ``apply`` in is a straight round-trip.

It holds the serialisable specs (``TableSpec`` with its nested ``ColumnSpec``s, plus
``RelationSpec``/``IndexSpec``), *not* the ``Table`` aggregates. ``check()``
constructs the aggregates transiently to run every existing invariant in one place, then adds
the cross-object checks (a ref projects a known relation, a relation's endpoints exist, …).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forktex_core.grid.domain.spec import IndexSpec, RelationSpec, TableSpec
from forktex_core.grid.domain.table import Table
from forktex_core.grid.errors import BadRequestError


class Schema(BaseModel):
    """Every schema object in one namespace, keyed for lookup and diffing."""

    model_config = ConfigDict(frozen=True)

    namespace: str = ""
    tables: dict[str, TableSpec] = Field(default_factory=dict)  # slug -> table
    relations: dict[str, RelationSpec] = Field(default_factory=dict)  # key -> relation
    indexes: dict[str, tuple[IndexSpec, ...]] = Field(default_factory=dict)  # table slug -> indexes

    def to_document(self) -> dict[str, Any]:
        """The canonical JSON document for this schema (inverse of :meth:`from_document`)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_document(cls, doc: Mapping[str, Any]) -> Schema:
        """Rebuild a schema from its JSON document; validates every nested spec."""
        return cls.model_validate(dict(doc))

    def merged_with(self, patch: Schema, *, prune: bool) -> Schema:
        """The desired end-state ``self`` should converge to, given ``patch``.

        ``prune=True`` — ``patch`` is authoritative: it *is* the end-state (entities absent
        from it will be dropped by the diff). ``prune=False`` — ``patch`` is a partial overlay:
        its entities are added/replaced onto ``self`` and everything else is left in place.
        Merge is entity-level (a present entity replaces its namesake wholesale).
        """
        if prune:
            return patch.model_copy(update={"namespace": self.namespace})
        return Schema(
            namespace=self.namespace,
            tables={**self.tables, **patch.tables},
            relations={**self.relations, **patch.relations},
            indexes={**self.indexes, **patch.indexes},
        )

    def check(self) -> Schema:
        """Assert cross-object integrity; raises ``BadRequestError`` on the first breach.

        Per-object invariants already ran when each spec was constructed; this adds the
        whole-schema checks a single spec cannot see. Returns ``self`` for chaining.
        """
        for slug, table in self.tables.items():
            if slug != table.slug:
                raise BadRequestError(f"schema table key '{slug}' != spec slug '{table.slug}'")
            Table.declare(table)  # storage + column aggregate invariants, in one place
            for col in table.columns:
                if col.relation_ref is not None and col.relation_ref not in self.relations:
                    raise BadRequestError(f"column '{slug}.{col.key}' projects unknown relation '{col.relation_ref}'")

        for key, rel in self.relations.items():
            if key != rel.key:
                raise BadRequestError(f"schema relation key '{key}' != spec key '{rel.key}'")
            for role, endpoint in (("source", rel.source), ("target", rel.target), ("through", rel.through)):
                if endpoint is not None and endpoint not in self.tables:
                    raise BadRequestError(f"relation '{key}' {role} table '{endpoint}' is not in the schema")

        for slug, specs in self.indexes.items():
            if slug not in self.tables:
                raise BadRequestError(f"indexes reference unknown table '{slug}'")
            known = {c.key for c in self.tables[slug].columns}
            for idx in specs:
                missing = [k for k in idx.column_keys if k not in known]
                if missing:
                    raise BadRequestError(f"index on '{slug}' references unknown column(s) {missing}")
        return self


__all__ = ["Schema"]
