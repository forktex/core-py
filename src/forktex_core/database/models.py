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

"""SQLAlchemy base models and mixins.

Shared base classes used across ForkTex Python services.

- ``BaseDBModel``: DeclarativeBase with StrEnum auto-mapping and repr.
- ``TimestampMixin``: created_at / updated_at with server defaults.
- ``AuditMixin``: Extends TimestampMixin with created_by_id, updated_by_id,
  soft delete (archived_at / is_active), and archive consistency constraints.
  Set ``__actor_fk_target__ = "user.id"`` on a subclass to add FK→user table.
- ``NamespacedMixin``: Adds a plain ``namespace`` string column for
  tenant isolation. No FK to any consumer table — the library stays
  agnostic about which table holds the tenant identity.
- ``JsonModelColumn``: Helper for storing Pydantic models in JSON columns.
"""

import enum
import uuid
from datetime import datetime
from typing import Any, ClassVar

import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.decl_api import declared_attr

from forktex_core.iso import to_iso


class ReprMixin:
    """Readable __repr__ showing all non-private attributes."""

    def __repr__(self) -> str:
        cls = self.__class__.__name__
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items() if not k.startswith("_"))
        return f"{cls}({attrs})"


#: The canonical temporal column type for the whole library.
#:
#: **Always** timezone-aware, to stay in sync with ``forktex_core.iso``, which
#: guarantees every datetime it produces is UTC-aware. That is not a stylistic
#: preference — asyncpg *rejects* writing an aware datetime into a naive
#: ``timestamp`` column outright (``DataError: can't subtract offset-naive and
#: offset-aware datetimes``), so a naive column is a latent crash for any code
#: that assigns ``iso.now()`` rather than relying on a server-side default.
#: A ``timestamptz`` column also normalises the offset on write, so
#: ``14:00+02:00`` is stored and read back as ``12:00+00:00`` — exactly the
#: invariant ``iso.to_iso``/``from_iso`` maintain in Python.
UtcDateTime = sa.TIMESTAMP(timezone=True)

#: Shared by ``BaseDBModel`` and every :func:`substrate_base`, so a library
#: schema and a consumer schema cannot drift on how Python types map to columns.
_TYPE_ANNOTATION_MAP: dict[Any, Any] = {
    enum.StrEnum: sa.Enum(enum.StrEnum, native_enum=False, length=64),
    datetime: UtcDateTime,
}


class BaseDBModel(DeclarativeBase, ReprMixin):
    """Base for all SQLAlchemy ORM models.

    Maps ``StrEnum`` to non-native string columns, and ``datetime`` to a
    timezone-aware ``timestamptz`` (see :data:`UtcDateTime`) so a bare
    ``Mapped[datetime]`` cannot accidentally declare a naive column.
    """

    type_annotation_map: ClassVar[dict[Any, Any]] = _TYPE_ANNOTATION_MAP


def substrate_base(schema: str) -> type[DeclarativeBase]:
    """A declarative base for a **library-owned** schema, on its own ``MetaData``.

    ``BaseDBModel.metadata`` belongs to the consumer: ``create_all`` on it is
    the documented way to bring up your own tables. Library substrates
    (``forktex_flow``, ``forktex_grid``) must therefore not register into it —
    otherwise a consumer's ``create_all`` also tries to create forktex's
    internal tables, in schemas it never asked for and which may not exist.
    Those substrates own migration runners; they are never created that way.

    The returned base keeps everything ``BaseDBModel`` establishes — the
    ``StrEnum``/``datetime`` type map and :class:`ReprMixin` — and defaults
    every table to ``schema``, so models need no per-table ``__table_args__``
    for it. Because the schema lives on ``Table.schema`` either way, the
    engine's ``schema_translate_map`` still rewrites it per instance.
    """

    class _SubstrateBase(DeclarativeBase, ReprMixin):
        metadata = sa.MetaData(schema=schema)
        type_annotation_map: ClassVar[dict[Any, Any]] = _TYPE_ANNOTATION_MAP

    return _SubstrateBase


class TimestampMixin:
    """Adds created_at and updated_at with server-side defaults."""

    created_at: Mapped[datetime] = mapped_column(server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False)


class AuditMixin(TimestampMixin):
    """Full audit trail with soft delete.

    Provides:
    - created_by_id / updated_by_id as UUID columns. By default no FK is added.
      Set ``__actor_fk_target__ = "user.id"`` on the subclass to add an FK
      with ON DELETE SET NULL pointing at the consuming service's user table.
    - archived_at / is_active for soft delete.
    - A check constraint enforcing (is_active ⟺ archived_at IS NULL).
    - An optional partial unique index on ``unique_fields`` (active records only).

    Usage::

        class MyModel(BaseDBModel, AuditMixin):
            __tablename__ = "my_model"
            unique_fields = ("org_id", "name")  # optional: unique on active rows
            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
            ...

        # With FK to a user table (e.g. in a service that owns a "user" table):
        class ServiceAuditMixin(AuditMixin):
            __actor_fk_target__ = "user.id"

        class Invoice(BaseDBModel, ServiceAuditMixin):
            __tablename__ = "invoice"
            ...
    """

    __actor_fk_target__: ClassVar[str | None] = None

    @declared_attr
    def created_by_id(cls) -> Mapped[uuid.UUID | None]:
        fk_target = getattr(cls, "__actor_fk_target__", None)
        fk_args = [sa.ForeignKey(fk_target, ondelete="SET NULL")] if fk_target else []
        return mapped_column(sa.UUID(as_uuid=True), *fk_args, nullable=True, index=True)

    @declared_attr
    def updated_by_id(cls) -> Mapped[uuid.UUID | None]:
        fk_target = getattr(cls, "__actor_fk_target__", None)
        fk_args = [sa.ForeignKey(fk_target, ondelete="SET NULL")] if fk_target else []
        return mapped_column(sa.UUID(as_uuid=True), *fk_args, nullable=True, index=True)

    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True, index=True)

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        tablename = getattr(cls, "__tablename__", None)
        if not tablename:
            return  # intermediate mixin subclass — defer validation to concrete model
        # Any declarative base, not `BaseDBModel` specifically: the mixin needs
        # a mapped class, and library substrates map onto their own base (see
        # `substrate_base`) precisely so they stay out of the consumer registry.
        if not issubclass(cls, DeclarativeBase):
            raise TypeError("AuditMixin can only be used with declarative model classes.")
        if not isinstance(tablename, str):
            raise TypeError("Classes using AuditMixin must define __tablename__ as a string")

    @declared_attr  # type: ignore[arg-type]
    def __table_args__(cls) -> tuple[sa.Index | sa.CheckConstraint, ...]:
        table_args: list[sa.Index | sa.CheckConstraint] = []
        # ``__tablename__`` and ``unique_fields`` are defined by concrete
        # subclasses; pyright doesn't see them on the mixin. Access via
        # ``getattr`` so the type-checker is satisfied without losing the
        # runtime contract enforced by ``__init_subclass__`` above.
        tablename = getattr(cls, "__tablename__")  # noqa: B009 -- pyright can't see it on the mixin
        unique_fields = getattr(cls, "unique_fields", None)

        # Partial unique index for "active only" uniqueness.
        if unique_fields:
            table_args.append(
                sa.Index(
                    f"uq_{tablename}_active",
                    *unique_fields,
                    unique=True,
                    postgresql_where=sa.text("archived_at IS NULL"),
                )
            )

        # Validity constraint: active ⟺ not archived.
        table_args.append(
            sa.CheckConstraint(
                "(is_active AND archived_at IS NULL) OR (NOT is_active AND archived_at IS NOT NULL)",
                name=f"ck_{tablename}_active_archive_consistency",
            )
        )

        return tuple(table_args)


class NamespacedMixin:
    """Tenant isolation via a plain string ``namespace`` column — no FK.

    forktex-core is a generic library and intentionally takes no
    position on which table in a consuming service holds the tenant
    identity. ``namespace`` is a plain string key — consumers typically
    set it to ``str(org_id)``, but any tenant-discriminating string is
    valid (e.g. a slug, a UUID, an external account id).

    Consumers that want a DB-level FK to their own tenant table should
    declare that mixin locally in their own ``shared/database/models.py``.

    Usage::

        class KnowledgeEntry(BaseDBModel, NamespacedMixin, TimestampMixin):
            __tablename__ = "knowledge_entry"
            id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7)
    """

    namespace: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)


class JsonModelColumn[T: BaseModel]:
    """Helper for SQLAlchemy JSON columns storing lists of Pydantic models.

    Usage::

        # Serialize before writing to DB
        row.tags = JsonModelColumn.serialize(tag_models)

        # Deserialize after reading from DB
        tags = JsonModelColumn.deserialize(row.tags, TagModel)
    """

    @staticmethod
    def serialize(models: list[T] | list[dict]) -> list[dict]:
        result = []
        for v in models:
            if isinstance(v, BaseModel):
                result.append(v.model_dump(mode="json"))
            else:
                cleaned = {}
                for key, val in v.items():
                    if isinstance(val, enum.Enum):
                        cleaned[key] = val.value
                    elif isinstance(val, datetime):
                        cleaned[key] = to_iso(val)
                    else:
                        cleaned[key] = val
                result.append(cleaned)
        return result

    @staticmethod
    def deserialize(data: list[dict], model: type[T]) -> list[T]:
        return [model(**item) for item in (data or [])]


__all__ = [
    "AuditMixin",
    "BaseDBModel",
    "JsonModelColumn",
    "NamespacedMixin",
    "ReprMixin",
    "TimestampMixin",
]
