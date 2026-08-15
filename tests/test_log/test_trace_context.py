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

"""Tests for forktex_core.log's trace_context()/async_trace_context() and the
trace_id/root_trace_id contextvars they scope — usable in any Python process,
not just behind TraceIDMiddleware."""

from __future__ import annotations

import pytest

from forktex_core.log import async_trace_context, get_root_trace_id, get_trace_id, set_trace_id, trace_context


def test_trace_context_mints_when_no_value_given():
    with trace_context() as trace_id:
        assert get_trace_id() == trace_id
        assert len(trace_id) == 36


def test_trace_context_uses_explicit_value():
    with trace_context("job-abc") as trace_id:
        assert trace_id == "job-abc"
        assert get_trace_id() == "job-abc"


def test_trace_context_restores_previous_value_on_exit():
    set_trace_id("outer")
    with trace_context("inner"):
        assert get_trace_id() == "inner"
    assert get_trace_id() == "outer"


def test_trace_context_restores_on_exception():
    set_trace_id("outer")
    with pytest.raises(ValueError):
        with trace_context("inner"):
            raise ValueError("boom")
    assert get_trace_id() == "outer"


@pytest.mark.asyncio
async def test_async_trace_context_scopes_and_restores():
    set_trace_id("outer")
    async with async_trace_context("job-99") as trace_id:
        assert trace_id == "job-99"
        assert get_trace_id() == "job-99"
    assert get_trace_id() == "outer"


def test_trace_context_establishes_root_trace_id_once():
    assert get_root_trace_id() is None
    with trace_context("outer") as outer_id:
        assert get_root_trace_id() == outer_id
        with trace_context("inner") as inner_id:
            assert inner_id != outer_id
            assert get_root_trace_id() == outer_id  # unchanged by the nested call
        assert get_root_trace_id() == outer_id
    assert get_root_trace_id() is None


@pytest.mark.asyncio
async def test_async_trace_context_shares_root_across_nesting():
    async with async_trace_context("outer") as outer_id:
        async with async_trace_context("inner") as inner_id:
            assert inner_id != outer_id
            assert get_root_trace_id() == outer_id
    assert get_root_trace_id() is None
