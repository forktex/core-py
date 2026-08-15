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

"""Rich FILE field handler — descriptor cells + auto-cleanup on archive.

The ``[space]`` extra registers the ``file`` field type (there is no bare
``[grid]`` FILE handler — core stays storage-agnostic). This handler:

  - Cell shape is a descriptor:
    ``{"storage_key": "...", "filename": ..., "content_type": ..., "size": ...}``.
  - Accepts a string for convenience (interpreted as
    ``{"storage_key": value}``).
  - On row archive, lazy-imports ``[storage]`` and deletes the blob
    so MinIO doesn't accumulate orphaned objects.

What this handler does NOT do:
  - Auto-upload from raw bytes (``"upload": True`` payload). Tracked as
    a follow-up — needs a pre-row-insert hook so the descriptor can be
    stored synchronously.
  - Presigned-URL minting on read. That's a consumer-track concern; the
    descriptor's ``storage_key`` is enough to mint URLs at the API
    boundary.
"""

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
from forktex_core.log import get_logger
from forktex_core.types import JsonValue

logger = get_logger(__name__)

#: ``grid_column.type_id`` under which the rich FILE handler registers.
FILE_TYPE_ID = "file"


class FileConfig(BaseModel):
    """Per-field config for a rich FILE field.

    ``client_name`` selects which registered ``[storage]`` client to
    use (``"default"`` if unset). ``delete_on_archive`` controls
    whether ``on_row_archive`` removes the blob (``True`` by default —
    consumers that need archival retention set it to ``False``).
    """

    model_config = ConfigDict(extra="ignore")

    client_name: str = "default"
    delete_on_archive: bool = True


def _coerce_descriptor(value: object, *, field_key: str) -> dict[str, JsonValue]:
    """Normalise the cell value to a descriptor dict.

    Accepts either a plain string (legacy storage key only) or a dict
    with at minimum ``storage_key``. Other dict keys flow through.
    """
    if value is None:
        return {}
    if isinstance(value, str):
        return {"storage_key": value}
    if isinstance(value, dict):
        if "storage_key" not in value or not isinstance(value["storage_key"], str):
            raise BadRequestError(
                f"Field '{field_key}' FILE descriptor missing 'storage_key'",
                details={"field": field_key},
            )
        return dict(value)
    raise BadRequestError(
        f"Field '{field_key}' expects a FILE descriptor dict or storage key string",
        details={"field": field_key, "received_type": type(value).__name__},
    )


class RichFileType(FieldTypeHandler):
    """Descriptor-shaped FILE handler with archive cleanup."""

    type_id = FILE_TYPE_ID
    config_model = FileConfig
    # The descriptor is a JSONB object — opaque to the typed filter/sort path.
    capabilities = Capabilities(filterable=False, sortable=False, fuzzy=False)

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        return _coerce_descriptor(value, field_key=FILE_TYPE_ID) or None

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else json.dumps(value, separators=(",", ":"))

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        if cell is None:
            return None
        if isinstance(cell, str):
            # A bare string is a legacy storage key; a JSON object is a descriptor.
            try:
                parsed = json.loads(cell)
            except json.JSONDecodeError:
                return self.normalize(cell, config=config)
            return self.normalize(parsed, config=config)
        return self.normalize(cell, config=config)

    def sql_cast(self, text_expr: sa.ColumnElement[Any]) -> sa.ColumnElement[Any]:
        return text_expr

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        from sqlalchemy.dialects.postgresql import JSONB

        return JSONB()

    async def on_rows_archived(
        self,
        contexts: list[WriteContext],
        *,
        config: BaseModel,
    ) -> None:
        """Delete the blobs behind the descriptors when the rows are
        archived.

        v4 batched lifecycle: collects all storage keys for the batch
        and issues per-row ``delete`` calls in a loop. Storage clients
        in core today don't expose a batched delete; if they ever do,
        this loop becomes a single call.

        Soft-fails on missing storage extra or missing client — the
        archive proceeds; orphans are tolerable. Cleanup-side failures
        are logged but never raised — the row archive must succeed.
        """
        assert isinstance(config, FileConfig)
        if not contexts or not config.delete_on_archive:
            return
        try:
            from forktex_core.storage import get_client
        except ImportError:
            logger.debug("space.file: [storage] extra not installed; skipping blob cleanup")
            return
        try:
            client = get_client(config.client_name)
        except Exception:
            logger.warning(
                "space.file: storage client %r not registered; orphaning %d blob(s)",
                config.client_name,
                len(contexts),
                exc_info=True,
            )
            return
        for ctx in contexts:
            descriptor = ctx.before_value or {}
            if not isinstance(descriptor, dict):
                continue
            storage_key = descriptor.get("storage_key")
            # A non-string key would reach `client.delete()` and fail there (or worse,
            # address the wrong object). The `Any` this field used to carry hid that.
            if not storage_key:
                continue
            if not isinstance(storage_key, str):
                logger.warning(
                    "space.file: skipping cleanup, storage_key is %s not str: %r",
                    type(storage_key).__name__,
                    storage_key,
                )
                continue
            try:
                await client.delete(storage_key)
            except Exception:
                # Tolerate cleanup failures — the row archive succeeded;
                # the blob is now an orphan in storage. Consumers can
                # run a GC pass; we log at WARNING so operators see
                # orphans accumulate in their log aggregator.
                logger.warning(
                    "space.file: failed to delete blob %r from client %r; orphan retained",
                    storage_key,
                    config.client_name,
                    exc_info=True,
                )


# Side-effect registration: the [space] extra owns the ``file`` type_id.
# Guard so re-import in tests doesn't trip the duplicate-registration check.
if not is_registered(FILE_TYPE_ID):
    register_field_type(RichFileType())


__all__ = ["FILE_TYPE_ID", "FileConfig", "RichFileType"]
