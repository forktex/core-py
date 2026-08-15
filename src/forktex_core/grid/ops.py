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

"""Agentic tool surface — the ``Namespace`` facade's operations, reified as JSON-schema'd tools.

This is NOT a second interface: each op is a frozen Pydantic input model (so ``model_json_schema``
gives an agent the tool contract) plus a thin handler that validates the args and calls the *same*
``Namespace`` / ``Grid`` methods a system consumer would. ``tool_schemas()`` feeds a tool registry;
``run(space, op, args)`` dispatches one call and returns a JSON dict. One implementation underneath.

Kept out of the lean top-level ``grid`` namespace on purpose — an agent runtime imports it
explicitly (``from forktex_core.grid.ops import run, tool_schemas, TOOLS``).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from forktex_core.grid.domain.enums import BrowseMode
from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.namespace import Namespace
from forktex_core.grid.write.batch import RowOp


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True)


class DescribeSchema(_Op):
    """Return the whole namespace's schema as a JSON document."""


class ApplySchema(_Op):
    """Converge the schema toward a desired document (create/alter/drop)."""

    schema_doc: dict[str, Any] = Field(alias="schema")
    prune: bool = False
    allow_destructive: bool = False
    dry_run: bool = False


class ApplyBatch(_Op):
    """Apply an optional schema + an ordered row batch in one transaction."""

    schema_doc: dict[str, Any] | None = Field(default=None, alias="schema")
    rows: list[RowOp] = Field(default_factory=list)
    prune: bool = False
    allow_destructive: bool = False


class Query(_Op):
    table: str
    filter: dict[str, Any] | None = None
    sort: list[dict[str, str]] | None = None
    mode: BrowseMode = BrowseMode.page
    limit: int = 50
    offset: int = 0
    cursor: str | None = None
    include_total: bool = False


class Insert(_Op):
    table: str
    values: dict[str, Any] | None = None  # single
    rows: list[dict[str, Any]] | None = None  # bulk
    external_ref: uuid.UUID | None = None


class Patch(_Op):
    table: str
    row_id: uuid.UUID
    values: dict[str, Any]


class Archive(_Op):
    table: str
    row_id: uuid.UUID


class Get(_Op):
    table: str
    row_id: uuid.UUID


class Relate(_Op):
    table: str
    rel_key: str
    source_id: uuid.UUID
    target_id: uuid.UUID


class Unrelate(_Op):
    table: str
    rel_key: str
    source_id: uuid.UUID
    target_id: uuid.UUID


TOOLS: dict[str, type[_Op]] = {
    "describe_schema": DescribeSchema,
    "apply_schema": ApplySchema,
    "apply_batch": ApplyBatch,
    "query": Query,
    "insert": Insert,
    "patch": Patch,
    "archive": Archive,
    "get": Get,
    "relate": Relate,
    "unrelate": Unrelate,
}


def tool_schemas() -> dict[str, dict[str, Any]]:
    """``{op_name: JSON-Schema}`` for every tool — hand this to an agent runtime."""
    return {name: model.model_json_schema(by_alias=True) for name, model in TOOLS.items()}


async def run(space: Namespace, op: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate ``args`` for ``op`` and drive the ``space`` — the single agentic entry point."""
    if op not in TOOLS:
        raise BadRequestError(f"unknown grid op '{op}' (known: {', '.join(sorted(TOOLS))})")
    return await _DISPATCH[op](space, TOOLS[op].model_validate(args))


async def _describe_schema(space: Namespace, _: DescribeSchema) -> dict[str, Any]:
    return (await space.describe()).to_document()


async def _apply_schema(space: Namespace, a: ApplySchema) -> dict[str, Any]:
    return await space.apply(a.schema_doc, prune=a.prune, allow_destructive=a.allow_destructive, dry_run=a.dry_run)


async def _apply_batch(space: Namespace, a: ApplyBatch) -> dict[str, Any]:
    return await space.batch(a.schema_doc, a.rows, prune=a.prune, allow_destructive=a.allow_destructive)


async def _query(space: Namespace, a: Query) -> dict[str, Any]:
    grid = await space.table(a.table)
    page = await grid.query(
        filter=a.filter,
        sort=a.sort,
        mode=a.mode,
        limit=a.limit,
        offset=a.offset,
        cursor=a.cursor,
        include_total=a.include_total,
    )
    return page.model_dump(mode="json")


async def _insert(space: Namespace, a: Insert) -> dict[str, Any]:
    grid = await space.table(a.table)
    if a.rows is not None:
        created = await grid.create_many(a.rows)
    else:
        if a.values is None:
            raise BadRequestError("insert requires 'values' or 'rows'")
        created = [await grid.create(a.values, external_ref=a.external_ref)]
    return {"rows": [r.model_dump(mode="json") for r in created]}


async def _patch(space: Namespace, a: Patch) -> dict[str, Any]:
    grid = await space.table(a.table)
    return (await grid.patch(a.row_id, a.values)).model_dump(mode="json")


async def _archive(space: Namespace, a: Archive) -> dict[str, Any]:
    grid = await space.table(a.table)
    await grid.archive(a.row_id)
    return {"archived": str(a.row_id)}


async def _get(space: Namespace, a: Get) -> dict[str, Any]:
    grid = await space.table(a.table)
    return (await grid.get(a.row_id)).model_dump(mode="json")


async def _relate(space: Namespace, a: Relate) -> dict[str, Any]:
    grid = await space.table(a.table)
    await grid.relate(a.rel_key, a.source_id, a.target_id)
    return {"related": [str(a.source_id), str(a.target_id)]}


async def _unrelate(space: Namespace, a: Unrelate) -> dict[str, Any]:
    grid = await space.table(a.table)
    await grid.unrelate(a.rel_key, a.source_id, a.target_id)
    return {"unrelated": [str(a.source_id), str(a.target_id)]}


_DISPATCH: dict[str, Any] = {
    "describe_schema": _describe_schema,
    "apply_schema": _apply_schema,
    "apply_batch": _apply_batch,
    "query": _query,
    "insert": _insert,
    "patch": _patch,
    "archive": _archive,
    "get": _get,
    "relate": _relate,
    "unrelate": _unrelate,
}


__all__ = ["TOOLS", "run", "tool_schemas"]
