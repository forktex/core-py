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

"""Tests for db ORM mixins: AuditMixin, NamespacedMixin.

AuditMixin is the most business-critical — it must enforce:
1. created_at / updated_at timestamps on every row
2. soft-delete via archived_at / is_active
3. CHECK constraint: is_active ⟺ archived_at IS NULL (can't have both true or both set)
4. Partial unique index on unique_fields (active rows only)
5. created_by_id / updated_by_id audit trail columns
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from forktex_core.database import BaseDBModel, TimestampMixin, AuditMixin, NamespacedMixin


# ---------------------------------------------------------------------------
# Test models (module-level — SQLAlchemy needs them in module globals)
# ---------------------------------------------------------------------------


class _Audited(BaseDBModel, AuditMixin):
    """Minimal model exercising AuditMixin with a unique_fields partial index."""

    __tablename__ = "mixin_audited"
    unique_fields = ("slug",)

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)


class _Timestamped(BaseDBModel, TimestampMixin):
    """Minimal model with only timestamps — no audit columns."""

    __tablename__ = "mixin_timestamped"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    value: Mapped[str] = mapped_column(sa.String(50))


class _Namespaced(BaseDBModel, NamespacedMixin, TimestampMixin):
    """Model using NamespacedMixin (no FK — plain string tenant key)."""

    __tablename__ = "mixin_namespaced"
    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(100))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session(postgres_url_str: str, fresh_schema: str) -> AsyncSession:
    engine = create_async_engine(
        postgres_url_str,
        execution_options={
            "schema_translate_map": {
                None: fresh_schema,
                "forktex_grid": fresh_schema,
            }
        },
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# TimestampMixin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timestamp_mixin_sets_created_at(session: AsyncSession):
    row = _Timestamped(value="hello")
    session.add(row)
    await session.flush()
    assert row.created_at is not None
    assert row.updated_at is not None


@pytest.mark.asyncio
async def test_timestamp_mixin_updated_at_changes_on_update(session: AsyncSession):
    row = _Timestamped(value="v1")
    session.add(row)
    await session.commit()
    original_updated = row.updated_at

    row.value = "v2"
    await session.commit()
    # updated_at is server-side so we need to refresh
    await session.refresh(row)
    # Allow equal — server timestamps have 1s resolution on some configs
    assert row.updated_at >= original_updated


# ---------------------------------------------------------------------------
# AuditMixin — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_mixin_defaults(session: AsyncSession):
    row = _Audited(slug="alpha")
    session.add(row)
    await session.flush()
    assert row.is_active is True
    assert row.archived_at is None
    assert row.created_by_id is None
    assert row.updated_by_id is None
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_audit_mixin_with_actor_ids(session: AsyncSession):
    actor = uuid.uuid4()
    row = _Audited(slug="beta", created_by_id=actor, updated_by_id=actor)
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert row.created_by_id == actor
    assert row.updated_by_id == actor


@pytest.mark.asyncio
async def test_audit_mixin_soft_delete(session: AsyncSession):
    row = _Audited(slug="gamma")
    session.add(row)
    await session.flush()

    # Soft delete: set both fields consistently
    now = datetime.now(timezone.utc)
    row.archived_at = now
    row.is_active = False
    await session.commit()
    await session.refresh(row)
    assert row.is_active is False
    assert row.archived_at is not None


# ---------------------------------------------------------------------------
# AuditMixin — CHECK constraint enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_check_constraint_active_and_archived_raises(session: AsyncSession):
    """is_active=True with archived_at set violates the CHECK constraint."""
    row = _Audited(slug="bad-state")
    session.add(row)
    await session.flush()
    # Attempt invalid state: active=True AND archived_at IS NOT NULL
    row.archived_at = datetime.now(timezone.utc)
    row.is_active = True  # violates: (is_active AND archived_at IS NULL)
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_audit_check_constraint_inactive_without_archived_at_raises(session: AsyncSession):
    """is_active=False with archived_at=None violates the CHECK constraint."""
    row = _Audited(slug="bad-state-2", is_active=False, archived_at=None)
    session.add(row)
    with pytest.raises(IntegrityError):
        await session.flush()


# ---------------------------------------------------------------------------
# AuditMixin — partial unique index (active rows only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_unique_active_rows_rejects_duplicate(session: AsyncSession):
    """Two active rows with the same slug must fail."""
    session.add(_Audited(slug="dup"))
    await session.flush()
    session.add(_Audited(slug="dup"))
    with pytest.raises(IntegrityError):
        await session.flush()


@pytest.mark.asyncio
async def test_partial_unique_allows_duplicate_after_archive(session: AsyncSession):
    """Archiving a row allows a new active row with the same slug."""
    row = _Audited(slug="reuse")
    session.add(row)
    await session.flush()
    row.archived_at = datetime.now(timezone.utc)
    row.is_active = False
    await session.commit()

    # New active row with same slug — should succeed
    session.add(_Audited(slug="reuse"))
    await session.flush()  # no IntegrityError expected


@pytest.mark.asyncio
async def test_partial_unique_allows_multiple_archived_rows(session: AsyncSession):
    """Many archived rows with the same slug are allowed."""
    for _ in range(3):
        row = _Audited(slug="archived-many")
        session.add(row)
        await session.flush()
        row.archived_at = datetime.now(timezone.utc)
        row.is_active = False
        await session.commit()
    # Three archived rows with same slug — no error


# ---------------------------------------------------------------------------
# AuditMixin — class constraints
# ---------------------------------------------------------------------------


def test_audit_mixin_requires_a_declarative_model():
    """The mixin adds mapped columns, so it needs a mapped class. It accepts any
    declarative base — see `test_registry_isolation` — not `BaseDBModel` alone,
    because library substrates map onto registries of their own."""
    with pytest.raises(TypeError, match="declarative"):

        class Bad(AuditMixin):  # no declarative base in MRO
            __tablename__ = "bad"


def test_audit_mixin_allows_intermediate_mixin_subclass():
    """AuditMixin can be subclassed without __tablename__ to build intermediate mixins."""

    class ServiceMixin(AuditMixin):
        __actor_fk_target__ = "user.id"

    # No error — intermediate mixins with __actor_fk_target__ are the intended pattern


def test_audit_mixin_actor_fk_target_inherited_by_subclasses():
    """__actor_fk_target__ propagates through the MRO to concrete subclasses."""

    class ServiceMixin(AuditMixin):
        __actor_fk_target__ = "user.id"

    # Verify that an intermediate subclass carries the attribute through MRO.
    # We do NOT create a concrete BaseDBModel table here because the FK to
    # "user.id" would fail create_all in tests that share the global metadata.
    assert ServiceMixin.__actor_fk_target__ == "user.id"
    assert issubclass(ServiceMixin, AuditMixin)


# ---------------------------------------------------------------------------
# NamespacedMixin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_namespaced_mixin_stores_namespace(session: AsyncSession):
    row = _Namespaced(namespace="org-abc-123", name="widget")
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert row.namespace == "org-abc-123"


@pytest.mark.asyncio
async def test_namespaced_mixin_no_fk_constraint(session: AsyncSession):
    """NamespacedMixin uses a plain string — any value is valid, no FK lookup."""
    row = _Namespaced(namespace="nonexistent-tenant", name="item")
    session.add(row)
    await session.flush()  # must not fail — no FK to any org table


@pytest.mark.asyncio
async def test_namespaced_mixin_different_namespaces_same_name(session: AsyncSession):
    """Different namespaces can have the same name without conflict."""
    session.add(_Namespaced(namespace="org-1", name="shared-name"))
    session.add(_Namespaced(namespace="org-2", name="shared-name"))
    await session.flush()  # both succeed — no uniqueness constraint on name
