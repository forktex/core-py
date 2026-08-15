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

"""SQLAlchemy ORM mappings for the ``forktex_flow.*`` tables.

The migration runner (:mod:`forktex_core.flow.persist.migrations._runner`)
owns the DDL; these mappings exist so read + write paths are typed,
testable, and clean. The shapes here MUST stay in sync with
``migrations/v0001__schema.sql``.

Built on :func:`forktex_core.database.models.substrate_base`, which gives flow
its own ``MetaData`` while inheriting ``BaseDBModel``'s conventions (the
``StrEnum``/``datetime`` type map and ``ReprMixin``). The separate registry is
deliberate and load-bearing: ``BaseDBModel.metadata`` belongs to the consumer,
whose ``create_all`` must not try to build forktex's internal substrate in
schemas it never asked for. ``grid`` uses the same helper, so the two substrate
packages stay structurally identical.

Schema name is hardcoded as ``forktex_flow``; SQLAlchemy's
``schema_translate_map`` (configured on the ``Database`` handle in
:class:`forktex_core.flow.flow.Flow.__init__`) rewrites it
per-Flow-instance at runtime so tests can spin up isolated schemas
without forking the model definitions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from forktex_core.database.models import substrate_base

_SCHEMA = "forktex_flow"

#: Flow's own declarative registry — every table defaults to ``_SCHEMA``.
_FlowBase = substrate_base(_SCHEMA)


class Workflow(_FlowBase):
    __tablename__ = "workflow"

    name: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    version: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    ast_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


class Run(_FlowBase):
    __tablename__ = "run"

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True)
    workflow_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, index=True)
    workflow_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    triggered_by: Mapped[str] = mapped_column(sa.String(32), nullable=False, server_default=sa.text("'manual'"))
    started_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
        index=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    steps: Mapped[list[StepRun]] = relationship(
        "StepRun",
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="StepRun.step_index, StepRun.started_at",
    )


class StepRun(_FlowBase):
    __tablename__ = "step_run"

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey(f"{_SCHEMA}.run.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    step_qualname: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    step_index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    args_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    output: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)

    run: Mapped[Run] = relationship("Run", back_populates="steps")

    __table_args__ = (sa.UniqueConstraint("run_id", "step_qualname", "args_hash", name="uq_step_run_identity"),)


class RunEvent(_FlowBase):
    __tablename__ = "run_event"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey(f"{_SCHEMA}.run.id", ondelete="CASCADE"),
        nullable=False,
    )
    ts: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


class ScheduledRun(_FlowBase):
    __tablename__ = "scheduled_run"

    workflow_name: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    workflow_version: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    cron: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    last_fired_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, index=True)

    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["workflow_name", "workflow_version"],
            [f"{_SCHEMA}.workflow.name", f"{_SCHEMA}.workflow.version"],
            ondelete="CASCADE",
        ),
    )


class Signal(_FlowBase):
    """Append-only inbox row used by ``ctx.wait_signal()`` /
    ``flow.send_signal()`` for manual graph advancement and general
    out-of-band coordination."""

    __tablename__ = "signal"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.UUID(as_uuid=True),
        sa.ForeignKey(f"{_SCHEMA}.run.id", ondelete="CASCADE"),
        nullable=False,
    )
    signal_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    consumed_at: Mapped[datetime | None] = mapped_column(sa.TIMESTAMP(timezone=True), nullable=True)


class WorkflowDefinitionRow(_FlowBase):
    """Persisted namespace-track workflow definition. Platform-track
    definitions live only in code; only namespace-track ones are stored here."""

    __tablename__ = "workflow_definition"

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid7)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    namespace: Mapped[str] = mapped_column(sa.Text, nullable=False)
    type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("name", "version", "namespace", name="uq_workflow_definition_identity"),
        sa.CheckConstraint("type IN ('pipeline', 'graph', 'scheduled')", name="ck_workflow_definition_type"),
    )


__all__ = [
    "Run",
    "RunEvent",
    "ScheduledRun",
    "Signal",
    "StepRun",
    "Workflow",
    "WorkflowDefinitionRow",
]
