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

"""The ``text`` field type — strings, optionally length-bounded / subtyped."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from forktex_core.grid.domain.enums import FieldType
from forktex_core.grid.domain.fieldtypes.base import Capabilities, CellValue, FieldTypeHandler, FilterOp
from forktex_core.types import JsonValue


class TextConfig(BaseModel):
    """Per-column config for ``text``.

    ``subtype`` is a render/semantics hint (``email`` / ``url`` / ``multiline``
    / ``richtext`` / ``color`` / ``phone`` / ``secret``) — not a separate type.
    Only ``email`` carries a light validation rule here; the rest are advisory.
    """

    model_config = ConfigDict(extra="ignore")
    max_length: int | None = None
    subtype: str | None = None


class TextType(FieldTypeHandler):
    type_id = FieldType.text.value
    config_model = TextConfig
    capabilities = Capabilities(
        filterable=True,
        sortable=True,
        fuzzy=True,
        filter_ops=frozenset(
            {
                FilterOp.eq,
                FilterOp.ne,
                FilterOp.in_,
                FilterOp.not_in,
                FilterOp.contains,
                FilterOp.icontains,
                FilterOp.starts_with,
                FilterOp.ends_with,
                FilterOp.is_null,
                FilterOp.fuzzy,
            }
        ),
        index_kinds=frozenset({"btree", "trgm"}),
        default_index_kind="btree",
    )

    def normalize(self, value: object, *, config: BaseModel) -> JsonValue:
        if value is None:
            return None
        assert isinstance(config, TextConfig)
        text = str(value)
        if "\x00" in text:
            raise ValueError("text may not contain a NUL character")
        if config.max_length is not None and len(text) > config.max_length:
            raise ValueError(f"value exceeds max_length {config.max_length}")
        if config.subtype == "email" and "@" not in text:
            raise ValueError("invalid email address")
        return text

    def to_cell(self, value: JsonValue, *, config: BaseModel) -> CellValue:
        return None if value is None else str(value)

    def from_cell(self, cell: CellValue, *, config: BaseModel) -> JsonValue:
        return None if cell is None else self.normalize(str(cell), config=config)

    def promoted_type(self, *, config: BaseModel) -> sa.types.TypeEngine[Any]:
        assert isinstance(config, TextConfig)
        return sa.String(config.max_length) if config.max_length else sa.Text()


__all__ = ["TextConfig", "TextType"]
