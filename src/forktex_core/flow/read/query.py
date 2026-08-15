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

"""Compiling an ``InstanceQuery`` into SQL and mapping the result to a page.

The read side of runs: filters, sorting, keyset pagination and the aggregate summary. Kept
out of :mod:`persist` because it composes several tables and answers a *question*, where
persist answers "write this row".

The keyset predicate is built from the **resolved** sort column. It used to be hardcoded to
``started_at`` while ``ORDER BY`` used whatever was requested, so paging by any other field
made the two disagree and pages skipped and repeated rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from forktex_core.database.pagination import Page, decode_cursor, encode_cursor, keyset_predicate
from forktex_core.flow.domain.types import RunInfo
from forktex_core.flow.persist.mappers import to_run_info
from forktex_core.flow.persist.models import Run, StepRun
from forktex_core.iso import from_iso, to_iso
from forktex_core.log import get_logger
from forktex_core.types import JsonValue

if TYPE_CHECKING:
    from forktex_core.flow.flow import Flow
    from forktex_core.flow.read.instance import InstanceQuery

logger = get_logger(__name__)

#: Sort fields whose cursor value is an ISO-8601 timestamp and must be parsed back into an
#: aware ``datetime`` before it can be compared to a column.
_TEMPORAL_SORT_FIELDS = frozenset({"started_at", "finished_at"})


def _coerce_sort_value(sort_field: str, value: JsonValue) -> object:
    """Rehydrate a cursor's sort-key value into the column's Python type.

    Cursors are JSON, so a timestamp arrives as a string. ``status`` and
    ``workflow`` are already strings and need nothing.
    """
    if value is None:
        return None
    if sort_field in _TEMPORAL_SORT_FIELDS:
        return from_iso(str(value))
    return value


async def execute_instance_query(
    flow: Flow,
    query: InstanceQuery,
    *,
    mode: str,  # "fetch" | "count" | "summary"
    cursor: str | None = None,
    limit_override: int | None = None,
) -> Page[RunInfo] | int | dict[str, object]:
    """Execute an InstanceQuery against the DB.

    mode="fetch":   returns ``Page[RunInfo]``
    mode="count":   returns int
    mode="summary": returns dict with {total, by_status, avg_duration_seconds,
                    p95_duration_seconds, oldest_started_at, newest_started_at}
    """
    stmt = select(Run).options(selectinload(Run.steps))
    count_stmt = select(sa.func.count(Run.id))

    if query._workflow_name is not None:
        stmt = stmt.where(Run.workflow_name == query._workflow_name)
        count_stmt = count_stmt.where(Run.workflow_name == query._workflow_name)

    if query._workflow_version is not None:
        stmt = stmt.where(Run.workflow_version == query._workflow_version)
        count_stmt = count_stmt.where(Run.workflow_version == query._workflow_version)

    if query._namespace is not None:
        from sqlalchemy.dialects.postgresql import JSONB

        ns_filter = {"__namespace__": query._namespace}
        stmt = stmt.where(Run.metadata_.op("@>")(sa.bindparam("ns_filter", value=ns_filter, type_=JSONB)))
        count_stmt = count_stmt.where(Run.metadata_.op("@>")(sa.bindparam("ns_filter2", value=ns_filter, type_=JSONB)))

    if query._statuses:
        stmt = stmt.where(Run.status.in_(query._statuses))
        count_stmt = count_stmt.where(Run.status.in_(query._statuses))

    if query._metadata_filter:
        from sqlalchemy.dialects.postgresql import JSONB

        stmt = stmt.where(Run.metadata_.op("@>")(sa.bindparam("md_filter", value=query._metadata_filter, type_=JSONB)))
        count_stmt = count_stmt.where(
            Run.metadata_.op("@>")(sa.bindparam("md_filter2", value=query._metadata_filter, type_=JSONB))
        )

    if query._state_filter:
        from sqlalchemy.dialects.postgresql import JSONB

        stmt = stmt.where(Run.input.op("@>")(sa.bindparam("st_filter", value=query._state_filter, type_=JSONB)))
        count_stmt = count_stmt.where(
            Run.input.op("@>")(sa.bindparam("st_filter2", value=query._state_filter, type_=JSONB))
        )

    if query._since is not None:
        stmt = stmt.where(Run.started_at >= query._since)
        count_stmt = count_stmt.where(Run.started_at >= query._since)

    if query._until is not None:
        stmt = stmt.where(Run.started_at < query._until)
        count_stmt = count_stmt.where(Run.started_at < query._until)

    if query._triggered_by:
        stmt = stmt.where(Run.triggered_by.in_(query._triggered_by))
        count_stmt = count_stmt.where(Run.triggered_by.in_(query._triggered_by))

    # current_node filter: subquery on step_run rows that are running and
    # whose qualname ends with one of the requested node names.
    if query._current_node_filter:
        node_patterns = [f"%:{n}" for n in query._current_node_filter]
        subq = (
            select(StepRun.run_id)
            .where(
                StepRun.status == "running",
                sa.or_(*[StepRun.step_qualname.like(p) for p in node_patterns]),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(Run.id.in_(subq))
        count_stmt = count_stmt.where(Run.id.in_(subq))

    if mode == "count":
        async with flow.session() as session:
            return (await session.execute(count_stmt)).scalar_one()

    if mode == "summary":
        summary_stmt = select(
            sa.func.count(Run.id).label("total"),
            Run.status,
            sa.func.avg(sa.extract("epoch", Run.finished_at - Run.started_at)).label("avg_duration"),
            sa.func.percentile_cont(0.95)
            .within_group(sa.extract("epoch", Run.finished_at - Run.started_at).asc())
            .label("p95_duration"),
            sa.func.min(Run.started_at).label("oldest"),
            sa.func.max(Run.started_at).label("newest"),
        ).group_by(Run.status)

        # Apply the same filters (without selectinload — this is an aggregate query)
        if query._workflow_name is not None:
            summary_stmt = summary_stmt.where(Run.workflow_name == query._workflow_name)
        if query._workflow_version is not None:
            summary_stmt = summary_stmt.where(Run.workflow_version == query._workflow_version)
        if query._statuses:
            summary_stmt = summary_stmt.where(Run.status.in_(query._statuses))
        if query._since is not None:
            summary_stmt = summary_stmt.where(Run.started_at >= query._since)
        if query._until is not None:
            summary_stmt = summary_stmt.where(Run.started_at < query._until)
        if query._triggered_by:
            summary_stmt = summary_stmt.where(Run.triggered_by.in_(query._triggered_by))

        async with flow.session() as session:
            rows = (await session.execute(summary_stmt)).fetchall()
            total = sum(r.total for r in rows)
            by_status = {r.status: r.total for r in rows}
            avgs = [r.avg_duration for r in rows if r.avg_duration is not None]
            p95s = [r.p95_duration for r in rows if r.p95_duration is not None]
            olds = [r.oldest for r in rows if r.oldest is not None]
            news = [r.newest for r in rows if r.newest is not None]
            return {
                "total": total,
                "by_status": by_status,
                "avg_duration_seconds": sum(avgs) / len(avgs) if avgs else None,
                "p95_duration_seconds": max(p95s) if p95s else None,
                "oldest_started_at": min(olds) if olds else None,
                "newest_started_at": max(news) if news else None,
            }

    # The sort column has to be resolved *before* the cursor predicate, not
    # after: the previous code hardcoded the predicate to `started_at`/`id`
    # while `ORDER BY` used whichever column was requested, so sorting by
    # anything else made the two disagree and pages skipped/repeated rows.
    sort_col = {
        "started_at": Run.started_at,
        "finished_at": Run.finished_at,
        "status": Run.status,
        "workflow": Run.workflow_name,
    }.get(query._sort_field, Run.started_at)

    ascending = not query._sort_desc

    if cursor is not None:
        # Two positional values, in `ORDER BY` order: the sort key, then `id` as
        # the unique tiebreaker.
        sort_value, cursor_id = decode_cursor(cursor, expected_length=2)
        stmt = stmt.where(
            keyset_predicate(
                [
                    (sort_col, _coerce_sort_value(query._sort_field, sort_value)),
                    (Run.id, UUID(str(cursor_id))),
                ],
                ascending=ascending,
            )
        )

    # `Run.id` is the unique tiebreaker that makes the order total, so the keyset
    # predicate above and this ORDER BY stay in lockstep.
    direction = (sa.asc, sa.desc)[not ascending]
    stmt = stmt.order_by(direction(sort_col), direction(Run.id))

    # `first()` wants a single row without mutating the shared builder, hence
    # the override rather than a `.limit()` call on `query`.
    limit = query._limit if limit_override is None else limit_override
    stmt = stmt.limit(limit + 1)  # fetch one extra to detect has_more

    async with flow.session() as session:
        total_count = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(stmt)).scalars().all()

    has_more = len(rows) > limit
    page_rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and page_rows:
        last = page_rows[-1]
        boundary = getattr(last, sort_col.key)
        next_cursor = encode_cursor([to_iso(boundary) if isinstance(boundary, datetime) else boundary, str(last.id)])

    return Page[RunInfo](
        items=[to_run_info(r) for r in page_rows],
        has_more=has_more,
        next_cursor=next_cursor,
        total=total_count,
    )
