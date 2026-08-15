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

"""The never-break guardrail — freeze the curated 4.0 public surface.

4.0 deliberately reduced the surface (the procedural 3.0 façade — ``create_table`` /
``create_column`` / ``create_row`` / ``apply_schema`` / ``list_tables`` … — is gone).
This test fails on any silent addition *or* removal, and asserts the removed 3.0
names stay removed.
"""

from __future__ import annotations

import forktex_core.grid as grid

# The whole public interface, curated to a small, physical-DB vocabulary. Changing this set is
# a deliberate act. The engine mechanisms (reconciler, change-set, reports, the Batch machinery)
# are intentionally NOT here — consumers drive everything through the ``Namespace`` facade.
FROZEN_PUBLIC_API: frozenset[str] = frozenset(
    {
        # entry point — one facade, isolation-scoped
        "Namespace",
        "Grid",
        "declare_relation",
        # the schema (whole namespace) + declarative building blocks
        "Schema",
        "TableSpec",
        "ColumnSpec",
        "RelationSpec",
        "IndexSpec",
        "Overlay",
        "Extension",
        # data-mutation op (for Namespace.batch / row batches)
        "RowOp",
        # vocabulary
        "Cardinality",
        "Materialization",
        "RelationShape",
        "OnDelete",
        "BrowseMode",
        "FieldType",
        # field-type extension seam — everything needed to implement a handler from
        # outside grid. `CellValue` is `to_cell`'s return type and `WriteContext` is what
        # the write hooks receive, so a half-public seam cannot actually be extended.
        "FieldTypeHandler",
        "Capabilities",
        "CellValue",
        "WriteContext",
        "FilterOp",
        "register_field_type",
        "is_registered",
        # results
        "Page",
        "Row",
        # infra
        "apply_migrations",
        "ReadOnlyStorage",
    }
)

# Names that MUST NOT reappear on the public surface: the removed 3.0 procedural façade, plus
# the 4.x mechanism nouns that were internalized behind the ``Namespace`` facade + verbs.
REMOVED_NAMES: frozenset[str] = frozenset(
    {
        # 3.0 procedural façade
        "create_table",
        "create_column",
        "create_row",
        "apply_schema",
        "SchemaChange",
        "list_tables",
        "describe_table",
        "bind_extension",
        "set_extension",
        "GridRow",
        "GridTable",
        "Ownership",
        "RelationType",
        # 4.x mechanism nouns now internal (reached via Namespace)
        "Catalog",
        "SchemaReconciler",
        "ReconcileOptions",
        "ReconcileReport",
        "Envelope",
        "EnvelopeReport",
        "RowMutation",
        "MutationResult",
        "RowView",
        "OverlayBinding",
        "ExtensionBinding",
        "SpaceSpec",
        "describe_catalog",
        "apply_catalog",
        "apply_envelope",
        "apply_envelope_json",
    }
)


def test_public_api_is_frozen() -> None:
    assert set(grid.__all__) == FROZEN_PUBLIC_API


def test_all_names_are_importable() -> None:
    for name in grid.__all__:
        assert hasattr(grid, name), f"{name} is in __all__ but not importable"


def test_removed_surface_stays_removed() -> None:
    leaked = REMOVED_NAMES & set(dir(grid))
    assert not leaked, f"removed names leaked back onto the public surface: {sorted(leaked)}"


def test_frontdoor_submodules_exist_but_stay_out_of_root() -> None:
    # the agentic (ops) and decorator (declare) front-doors are opt-in submodules, deliberately
    # kept OUT of the lean root namespace so `grid.__all__` stays minimal.
    from forktex_core.grid import declare, ops

    assert hasattr(ops, "run") and hasattr(ops, "tool_schemas") and hasattr(ops, "TOOLS")
    assert hasattr(declare, "Registry") and hasattr(declare, "Column") and hasattr(declare, "field_type")
    assert "run" not in grid.__all__ and "Registry" not in grid.__all__
