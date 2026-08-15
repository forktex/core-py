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

"""Regressions for `InstanceQuery.fetch()`'s two pagination defects.

Neither was covered by any test before — `.fetch()` had zero call sites in the
suite, which is exactly why both survived:

1. the keyset cursor predicate was hardcoded to `started_at`/`id` while
   `ORDER BY` used whichever column `sort()` requested, so any other sort field
   made the two disagree and pages skipped and repeated rows;
2. `fetch()` `cast()`-ed the engine's bare 3-tuple to `InstancePage`, a model
   that was never constructed anywhere — `cast` does nothing at runtime, so
   `page.items` raised `AttributeError`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from forktex_core import iso
from forktex_core.flow import Flow
from forktex_core.flow.persist.models import Run

pytestmark = pytest.mark.asyncio


async def _seed(flow: Flow, count: int) -> list[uuid.UUID]:
    """Insert `count` completed runs, each with a distinct `finished_at` that
    sorts in the *opposite* order to `started_at`.

    The inversion is the point: a predicate built on `started_at` while the query
    orders by `finished_at` then walks the set backwards, which is what makes the
    old bug produce wrong pages rather than merely inefficient ones.
    """
    base = iso.now()
    ids: list[uuid.UUID] = []
    async with flow.session() as session:
        for i in range(count):
            run_id = uuid.uuid7()
            ids.append(run_id)
            session.add(
                Run(
                    id=run_id,
                    workflow_name="pager.wf",
                    workflow_version=1,
                    status="completed",
                    input={},
                    metadata_={},
                    started_at=base + timedelta(minutes=i),
                    finished_at=base + timedelta(minutes=count - i),
                )
            )
    return ids


async def _page_through(flow: Flow, *, field: str, desc: bool, page_size: int) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(20):  # generous bound; asserts below catch a stuck cursor
        page = await flow.query().workflow("pager.wf").sort(field, desc=desc).limit(page_size).fetch(cursor)
        seen.extend(i.instance_id for i in page.items)
        if not page.has_more:
            return seen
        assert page.next_cursor is not None, "has_more with no cursor — cannot advance"
        cursor = page.next_cursor
    raise AssertionError("pagination did not terminate")


async def test_fetch_returns_a_real_page_whose_items_work(flow: Flow):
    await _seed(flow, 3)
    page = await flow.query().workflow("pager.wf").limit(2).fetch()

    # `page.items` is the access that used to raise AttributeError on a tuple.
    assert len(page.items) == 2
    assert page.total == 3
    assert page.has_more is True
    assert page.next_cursor is not None
    assert {i.workflow_name for i in page.items} == {"pager.wf"}
    # And the items are bound instances, not raw rows.
    assert all(i.instance_id is not None for i in page.items)


async def test_last_page_reports_no_more_and_no_cursor(flow: Flow):
    await _seed(flow, 2)
    page = await flow.query().workflow("pager.wf").limit(5).fetch()
    assert len(page.items) == 2
    assert page.has_more is False
    assert page.next_cursor is None


@pytest.mark.parametrize("field", ["started_at", "finished_at", "status", "workflow"])
@pytest.mark.parametrize("desc", [True, False])
async def test_paging_never_skips_or_repeats_for_any_sort_field(flow: Flow, field: str, desc: bool):
    """The core regression: with the predicate pinned to `started_at`, sorting by
    `finished_at` dropped and duplicated rows. `status`/`workflow` are constant
    across the seeded rows, which exercises the tiebreaker-only path."""
    expected = set(await _seed(flow, 7))

    seen = await _page_through(flow, field=field, desc=desc, page_size=2)

    assert len(seen) == len(set(seen)), f"duplicate rows across pages sorting by {field}"
    assert set(seen) == expected, f"rows skipped across pages sorting by {field}"


async def test_pages_preserve_the_requested_order(flow: Flow):
    """A cursor that disagreed with `ORDER BY` could also yield rows out of
    order, so assert the sequence, not just the set."""
    await _seed(flow, 6)

    seen = await _page_through(flow, field="finished_at", desc=False, page_size=2)

    ordered = await flow.query().workflow("pager.wf").sort("finished_at", desc=False).limit(100).fetch()
    assert seen == [i.instance_id for i in ordered.items]


async def test_first_returns_a_single_instance(flow: Flow):
    """`first()` passed `limit_override=1` to an engine that had no such
    parameter, so it raised `TypeError` before it could return anything."""
    await _seed(flow, 3)
    instance = await flow.query().workflow("pager.wf").sort("finished_at", desc=False).first()
    assert instance is not None
    assert instance.workflow_name == "pager.wf"

    ordered = await flow.query().workflow("pager.wf").sort("finished_at", desc=False).limit(100).fetch()
    assert instance.instance_id == ordered.items[0].instance_id


async def test_first_returns_none_when_nothing_matches(flow: Flow):
    assert await flow.query().workflow("absent.wf").first() is None


async def test_a_malformed_cursor_is_a_bad_request(flow: Flow):
    """Previously `decode_cursor` swallowed everything and returned `None`, so a
    corrupt cursor silently restarted from page 1 — a paging client would loop."""
    from forktex_core.error import BadRequestError

    await _seed(flow, 2)
    with pytest.raises(BadRequestError):
        await flow.query().workflow("pager.wf").limit(1).fetch("not-a-cursor")
