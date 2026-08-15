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

"""``Namespace`` — the isolation-scoped facade, the single entry point to a grid.

A ``Namespace`` is a handle bound to one namespace (the tenant/isolation key; ``""`` is the
never-null root space). It is the one interface every consumer uses — network/api, workers,
scripts, and agents — and everything hangs off it, à la ``flow.Flow``:

- **DDL, fully dynamic:** ``describe()`` returns the whole schema; ``apply(schema | dict, …)``
  converges the live DDL toward a desired :class:`Schema` (create a table, alter/drop, all at
  runtime — no prior static setup). ``apply`` optionally seeds ``rows`` in the same transaction.
- **Handles + DML:** ``table(slug)`` / ``declare(spec)`` vend a :class:`Grid` for row work.

There is one IR (:class:`Schema` / ``TableSpec``) and one engine underneath; the decorator
front-door and the agentic dispatch are just other ways to author/drive this same facade.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.schema import Schema
from forktex_core.grid.domain.spec import RelationSpec, TableSpec
from forktex_core.grid.grid import Grid
from forktex_core.grid.grid import declare_relation as _declare_relation
from forktex_core.grid.persist import GridRelation
from forktex_core.grid.persist.schema_repo import hydrate
from forktex_core.grid.write.batch import Batch, RowOp, apply_batch
from forktex_core.grid.write.schema import ReconcileOptions, SchemaReconciler


def _as_schema(schema: Schema | Mapping[str, Any], namespace: str) -> Schema:
    """Coerce a ``Schema`` or a plain JSON document into a namespace-stamped ``Schema``."""
    model = schema if isinstance(schema, Schema) else Schema.from_document(schema)
    return model.model_copy(update={"namespace": namespace})


class Namespace:
    """A grid scoped to one namespace — the single facade for schema and data."""

    def __init__(self, session: AsyncSession, namespace: str = "") -> None:
        self.session = session
        self.namespace = namespace

    async def describe(self) -> Schema:
        """The whole namespace as a round-trippable :class:`Schema` (a valid ``apply`` input)."""
        return await hydrate(self.session, self.namespace)

    async def apply(
        self,
        schema: Schema | Mapping[str, Any],
        *,
        prune: bool = False,
        allow_destructive: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Converge the namespace toward ``schema`` (create/alter/drop tables, relations, …).

        Fully dynamic: build ``schema`` at runtime (typed or a plain JSON dict). ``prune`` makes
        it authoritative; ``allow_destructive`` is required for drops/tightening; ``dry_run`` plans
        without writing. Returns the reconcile report as a JSON dict. For schema-then-data in one
        transaction, use :meth:`batch`.
        """
        report = await SchemaReconciler().reconcile(
            self.session,
            _as_schema(schema, self.namespace),
            options=ReconcileOptions(prune=prune, allow_destructive=allow_destructive, dry_run=dry_run),
        )
        return report.model_dump(mode="json")

    async def batch(
        self,
        schema: Schema | Mapping[str, Any] | None = None,
        rows: Sequence[RowOp] = (),
        *,
        prune: bool = False,
        allow_destructive: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Schema (optional) + an ordered cross-table DML batch, applied in one transaction.

        Returns the batch report as a JSON dict (``{"reconcile": …, "results": [...]}``).
        """
        desired = None if schema is None else _as_schema(schema, self.namespace)
        report = await apply_batch(
            self.session,
            Batch(
                namespace=self.namespace,
                desired=desired,
                mutations=tuple(rows),
                options=ReconcileOptions(prune=prune, allow_destructive=allow_destructive, dry_run=dry_run),
            ),
        )
        return report.model_dump(mode="json")

    async def table(self, slug: str) -> Grid:
        """Open a :class:`Grid` handle for one table in this space."""
        return await Grid.open(self.session, slug=slug, namespace=self.namespace)

    async def declare(self, spec: TableSpec) -> Grid:
        """Declare one table in this space (namespace forced to the space's); returns its handle."""
        return await Grid.declare(self.session, spec.model_copy(update={"namespace": self.namespace}))

    async def declare_relation(self, spec: RelationSpec) -> GridRelation:
        """Declare a relation between two owned tables in this space."""
        return await _declare_relation(self.session, spec, self.namespace)


__all__ = ["Namespace"]
