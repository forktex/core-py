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

"""Rich VECTOR field handler — multi-mode embedding storage with cell back-ref.

Cell shape is a descriptor::

    {
        "vector":     [...],       # present in INLINE / BOTH modes
        "collection": "...",       # present in REMOTE / BOTH modes
        "point_id":   "<uuid>",    # present in REMOTE / BOTH modes
        "model":      "...",       # informational; carried through
        "dimensions": 384,         # informational; carried through
    }

Storage modes:

  - **none**:   handler accepts the descriptor but neither stores nor
                returns the vector. Useful as a placeholder when a
                future migration will populate the embedding.
  - **inline**: vector lives in the row's JSONB. No external dep at
                write time. Read-time near-search not supported (would
                require sequential scan). Good for small dimensions
                (< ~256) or low-cardinality use.
  - **remote**: vector lives in Qdrant via [vector]. Cell carries only
                the back-reference. Default mode; what most consumers
                want.
  - **both**:   write to both places. Useful during a vector-store
                migration; also enables degraded-mode reads when
                Qdrant is unavailable.

Lifecycle hooks:

  - ``on_row_write``:  upsert the vector to Qdrant (modes remote / both).
  - ``on_row_archive``: delete the point from Qdrant (modes remote /
                        both); soft-fails on missing collection or
                        offline service.

The point id used in Qdrant is always ``ctx.row_id`` so back-references
are deterministic from row identity — re-running a write is idempotent
and an archive can target the right point without consulting the
descriptor."""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from forktex_core.error import BadRequestError
from forktex_core.grid.domain.fieldtypes import is_registered, register_field_type
from forktex_core.grid.domain.fieldtypes.base import (
    Capabilities,
    CellValue,
    FieldTypeHandler,
    WriteContext,
)
from forktex_core.grid.persist import GridRow
from forktex_core.log import get_logger
from forktex_core.space.config import VectorStorageMode
from forktex_core.types import JsonValue

logger = get_logger(__name__)

#: ``grid_column.type_id`` under which the rich VECTOR handler registers.
VECTOR_TYPE_ID = "vector"


class VectorConfig(BaseModel):
    """Per-field config for a rich VECTOR field."""

    model_config = ConfigDict(extra="ignore")

    storage_mode: VectorStorageMode = "remote"
    dimensions: int | None = None
    """Expected vector dimensions. ``None`` skips the size check."""
    model: str | None = None
    """Free-form embedding-model tag carried through to the descriptor."""
    client_name: str = "default"
    """Registered ``[vector]`` client to talk to (see ``forktex_core.vector.register``)."""
    collection: str | None = None
    """Explicit Qdrant collection name. Wins over ``collection_prefix``.
    If both unset, falls back to ``"{namespace}--{entity_slug}--{field_key}"``."""
    collection_prefix: str | None = None
    """Optional prefix prepended to the auto-derived collection name.
    Useful when the consumer wants to group a Bundle's collections under
    one Qdrant prefix without overriding each individual collection
    (e.g. ``"intelligence-kb"`` → collections become
    ``"intelligence-kb--{namespace}--{entity_slug}--{field_key}"``).
    Ignored when ``collection`` is set. Mirrors
    ``BundleConfig.vector.collection_prefix`` — consumers can stamp the
    Bundle-level default onto each field's config_json at declare time."""
    delete_on_archive: bool = True


def _coerce_descriptor(value: object, *, field_key: str, expected_dim: int | None) -> dict[str, JsonValue]:
    """Normalise the cell value to a descriptor dict.

    Accepts:
      - ``list[float]``                 → ``{"vector": [...]}``
      - ``dict``                        → must carry at minimum either ``vector`` or
                                          ``point_id`` (already-uploaded reference).
      - ``None``                        → empty descriptor (clears the cell).
    """
    if value is None:
        return {}
    if isinstance(value, list):
        if expected_dim is not None and len(value) != expected_dim:
            raise BadRequestError(
                f"Field '{field_key}' VECTOR dimension mismatch: expected {expected_dim}, got {len(value)}",
                details={"field": field_key},
            )
        return {"vector": list(value)}
    if isinstance(value, dict):
        descriptor = dict(value)
        vec = descriptor.get("vector")
        pid = descriptor.get("point_id")
        if vec is None and pid is None:
            raise BadRequestError(
                f"Field '{field_key}' VECTOR descriptor needs 'vector' or 'point_id'",
                details={"field": field_key},
            )
        if vec is not None:
            if not isinstance(vec, list):
                raise BadRequestError(
                    f"Field '{field_key}' VECTOR descriptor 'vector' must be a list",
                    details={"field": field_key},
                )
            if expected_dim is not None and len(vec) != expected_dim:
                raise BadRequestError(
                    f"Field '{field_key}' VECTOR dimension mismatch: expected {expected_dim}, got {len(vec)}",
                    details={"field": field_key},
                )
        return descriptor
    raise BadRequestError(
        f"Field '{field_key}' VECTOR expects list[float] or descriptor dict",
        details={"field": field_key, "received_type": type(value).__name__},
    )


def _as_embedding(value: JsonValue) -> list[float] | None:
    """``value`` as a float vector, or ``None`` if it is not one.

    The descriptor comes out of JSONB, so its ``vector`` field is only *conventionally* a
    list of numbers. This was annotated ``Any``, which meant a string reached
    ``create(dim=len(...))`` and sized a collection by character count.
    """
    if not isinstance(value, list):
        return None
    out: list[float] = []
    for item in value:
        # `bool` is an `int` subclass and never a coordinate — reject it.
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        out.append(float(item))
    return out


def _resolve_collection(config: VectorConfig, ctx: WriteContext) -> str:
    if config.collection:
        return config.collection
    base = f"{ctx.namespace}--{ctx.table_slug}--{ctx.column_key}"
    if config.collection_prefix:
        return f"{config.collection_prefix}--{base}"
    return base


class RichVectorType(FieldTypeHandler):
    """VECTOR field type with multi-mode storage + Qdrant lifecycle hooks."""

    type_id = VECTOR_TYPE_ID
    config_model = VectorConfig
    # Embeddings are opaque to the typed filter/sort path — near-search runs
    # through the remote vector store, not the SQL query engine.
    capabilities = Capabilities(filterable=False, sortable=False, fuzzy=False)

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        assert isinstance(config, VectorConfig)
        descriptor = _coerce_descriptor(value, field_key=VECTOR_TYPE_ID, expected_dim=config.dimensions)
        if not descriptor:
            return None
        # Carry model/dimensions through for downstream consumers.
        if config.model is not None and "model" not in descriptor:
            descriptor["model"] = config.model
        if config.dimensions is not None and "dimensions" not in descriptor:
            descriptor["dimensions"] = config.dimensions
        # The inline vector is kept here regardless of storage mode so
        # ``on_rows_written`` can read it; mode-dependent stripping happens
        # in the hook AFTER the remote upsert succeeds.
        return descriptor

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        """Render the descriptor as a compact JSON string for tabular export."""
        return None if value is None else json.dumps(value, separators=(",", ":"))

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        """Parse a tabular cell (JSON string, list, or descriptor) back."""
        if cell is None:
            return None
        if isinstance(cell, str):
            try:
                parsed = json.loads(cell)
            except json.JSONDecodeError:
                raise ValueError("vector cell is not valid JSON") from None
            return self.normalize(parsed, config=config)
        return self.normalize(cell, config=config)

    def sql_cast(self, text_expr: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
        return text_expr

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()

    async def on_rows_written(
        self,
        contexts: list[WriteContext],
        *,
        config: BaseModel,
    ) -> None:
        """Apply the storage-mode policy across a batch of rows.

        v4 batched lifecycle: one ``await handle.upsert([N points])``
        for ``remote`` / ``both`` modes instead of N separate calls.
        Big throughput win when ``service.bulk_insert_rows(fire_hooks=True)``
        feeds in a 10k-row CSV import.

        Storage modes:
        - ``none``:   strip the inline vector from the row payload.
        - ``inline``: leave the vector inline; no Qdrant work.
        - ``remote``: upsert to Qdrant + strip the inline vector +
                      write back ``collection`` / ``point_id`` references.
        - ``both``:   upsert to Qdrant + leave the inline vector + write
                      back the references.
        """
        assert isinstance(config, VectorConfig)
        if not contexts:
            return

        if config.storage_mode == "inline":
            # Nothing to do; vector already in row payload.
            return

        if config.storage_mode == "none":
            for ctx in contexts:
                descriptor = ctx.after_value or {}
                if isinstance(descriptor, dict):
                    await self._patch_row_payload(ctx, strip_vector=True)
            return

        # remote / both: collect points for one batched Qdrant upsert.
        try:
            from forktex_core.vector import VectorPoint, get_client
        except ImportError:
            logger.debug("space.vector: [vector] extra not installed; skipping remote upsert")
            return
        try:
            client = get_client(config.client_name)
        except Exception:
            logger.warning(
                "space.vector: vector client %r not registered; remote upsert skipped",
                config.client_name,
                exc_info=True,
            )
            return

        # Group by resolved collection name — each collection may have
        # a different dimension. In practice rows in one batch usually
        # land in the same collection, so this is a 1-entry dict.
        by_collection: dict[str, list[tuple[WriteContext, list[float]]]] = {}
        for ctx in contexts:
            descriptor = ctx.after_value or {}
            if not isinstance(descriptor, dict):
                continue
            vector = descriptor.get("vector")
            if vector is None:
                continue
            embedding = _as_embedding(vector)
            if embedding is None:
                logger.warning(
                    "space.vector: skipping row %s, 'vector' is not a list of numbers: %r",
                    getattr(ctx, "row_id", "?"),
                    vector,
                )
                continue
            collection_name = _resolve_collection(config, ctx)
            by_collection.setdefault(collection_name, []).append((ctx, embedding))

        for collection_name, entries in by_collection.items():
            if not entries:
                continue
            handle = client.collection(collection_name)
            await handle.create(dim=len(entries[0][1]))
            await handle.upsert(
                [
                    VectorPoint(
                        id=str(ctx.row_id),
                        vector=vector,
                        payload={
                            "namespace": ctx.namespace,
                            "entity": ctx.table_slug,
                            "field": ctx.column_key,
                            "row_id": str(ctx.row_id),
                        },
                    )
                    for ctx, vector in entries
                ]
            )
            for ctx, _vec in entries:
                await self._patch_row_payload(
                    ctx,
                    strip_vector=(config.storage_mode == "remote"),
                    set_back_ref={"collection": collection_name, "point_id": str(ctx.row_id)},
                )

    async def _patch_row_payload(
        self,
        ctx: WriteContext,
        *,
        strip_vector: bool = False,
        set_back_ref: dict[str, Any] | None = None,
    ) -> None:
        """Mutate the row payload inside the same transaction.

        Called after a successful Qdrant upsert to either strip the
        inline embedding (mode ``remote``) or stamp ``collection`` /
        ``point_id`` references onto the cell."""

        row = await ctx.session.get(GridRow, ctx.row_id)
        if row is None:
            return
        cell = dict(row.payload.get(ctx.column_key) or {})
        if strip_vector:
            cell.pop("vector", None)
        if set_back_ref:
            cell.update(set_back_ref)
        new_payload = dict(row.payload or {})
        new_payload[ctx.column_key] = cell
        row.payload = new_payload
        ctx.session.add(row)
        await ctx.session.flush()

    async def on_rows_archived(
        self,
        contexts: list[WriteContext],
        *,
        config: BaseModel,
    ) -> None:
        """v4 batched archive: one ``delete_points([N ids])`` per
        collection instead of N separate calls.
        """
        assert isinstance(config, VectorConfig)
        if not contexts or not config.delete_on_archive:
            return
        if config.storage_mode in ("none", "inline"):
            return
        try:
            from forktex_core.vector import get_client
        except ImportError:
            logger.debug("space.vector: [vector] extra not installed; skipping point cleanup")
            return
        try:
            client = get_client(config.client_name)
        except Exception:
            logger.warning(
                "space.vector: vector client %r not registered; orphaning %d point(s) on archive",
                config.client_name,
                len(contexts),
                exc_info=True,
            )
            return

        # Group by collection so we batch the delete per Qdrant
        # collection. Usually a 1-entry dict in practice. Type-wise
        # the upstream client expects ``list[str | int]``; we always
        # write strings but annotate to match.
        by_collection: dict[str, list[str | int]] = {}
        for ctx in contexts:
            collection_name = _resolve_collection(config, ctx)
            by_collection.setdefault(collection_name, []).append(str(ctx.row_id))

        for collection_name, ids in by_collection.items():
            handle = client.collection(collection_name)
            try:
                await handle.delete_points(ids)
            except Exception:
                # Tolerate missing collection / offline service. The
                # archive succeeded; points are now orphaned in Qdrant.
                logger.warning(
                    "space.vector: failed to delete %d point(s) from collection %r; orphans retained",
                    len(ids),
                    collection_name,
                    exc_info=True,
                )


# Side-effect registration: rich VECTOR is the first handler for this
# type_id (no bare [grid] handler exists). Guard so re-import in tests
# doesn't trip the duplicate-registration check.
if not is_registered(VECTOR_TYPE_ID):
    register_field_type(RichVectorType())


__all__ = ["VECTOR_TYPE_ID", "RichVectorType", "VectorConfig"]
