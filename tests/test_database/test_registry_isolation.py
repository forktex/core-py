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

"""`BaseDBModel.metadata` belongs to the consumer, not to forktex's substrates.

`create_all` on the shared base is the documented way for a consumer to bring up
its own tables. When `flow` and `grid` mapped onto `BaseDBModel` directly they
joined that registry, so a consumer's `create_all` also tried to build
`forktex_flow.*` / `forktex_grid.*` — in schemas it never created, which fails
outright. They now map onto `substrate_base()` registries of their own.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from forktex_core.database.models import AuditMixin, BaseDBModel, substrate_base


def test_library_substrates_are_absent_from_the_consumer_registry():
    # Importing them is what used to register them, so import explicitly rather
    # than relying on whatever an earlier test happened to pull in.
    import forktex_core.flow.persist.models  # noqa: F401
    import forktex_core.grid.persist.models  # noqa: F401

    leaked = [t.key for t in BaseDBModel.metadata.sorted_tables if t.schema in {"forktex_flow", "forktex_grid"}]
    assert leaked == [], f"library substrate tables leaked into the consumer registry: {leaked}"


def test_each_substrate_keeps_its_own_tables():
    from forktex_core.flow.persist.models import Run
    from forktex_core.grid.persist.models import GridRow

    assert Run.__table__.schema == "forktex_flow"
    assert GridRow.__table__.schema == "forktex_grid"
    # Separate registries, so neither can see the other's tables.
    assert Run.metadata is not GridRow.metadata
    assert {t.schema for t in Run.metadata.sorted_tables} == {"forktex_flow"}
    assert {t.schema for t in GridRow.metadata.sorted_tables} == {"forktex_grid"}


def test_substrate_base_defaults_the_schema_without_per_table_args():
    Base = substrate_base("some_substrate")

    class Thing(Base):  # type: ignore[misc, valid-type]
        __tablename__ = "thing"
        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)

    assert Thing.__table__.schema == "some_substrate"


def test_substrate_base_inherits_the_shared_type_conventions():
    """A substrate must not be able to drift from `BaseDBModel` on how Python
    types map to columns — notably `datetime` → timezone-aware `timestamptz`."""
    Base = substrate_base("some_substrate")
    assert Base.type_annotation_map == BaseDBModel.type_annotation_map


def test_audit_mixin_accepts_a_substrate_base():
    """The mixin's guard is "is a mapped class", not "is BaseDBModel" — grid's
    audited tables live on a substrate base and still need it."""
    Base = substrate_base("some_substrate")

    class Audited(Base, AuditMixin):  # type: ignore[misc, valid-type]
        __tablename__ = "audited"
        id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)

    assert issubclass(Audited, DeclarativeBase)
    assert "archived_at" in Audited.__table__.c
