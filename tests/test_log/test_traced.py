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

"""Tests for forktex_core.log.traced — the @traced decorator: entry/exit/
exception logging + a scoped trace_id, for sync and async callables (worker
job handlers, flow steps, CLI entry points)."""

from __future__ import annotations

import logging

import pytest

from forktex_core.log import get_root_trace_id, get_trace_id, log_context, traced


def test_traced_sync_returns_value_and_scopes_trace_id():
    seen_trace_ids = []

    @traced
    def add(a, b):
        seen_trace_ids.append(get_trace_id())
        return a + b

    assert get_trace_id() is None
    assert add(2, 3) == 5
    assert seen_trace_ids[0] is not None
    assert get_trace_id() is None  # restored after the call


def test_traced_sync_fresh_trace_id_per_call():
    seen = []

    @traced
    def fn():
        seen.append(get_trace_id())

    fn()
    fn()
    assert seen[0] != seen[1]


def test_traced_sync_reraises_and_logs_exception(caplog):
    @traced
    def boom():
        raise ValueError("nope")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="nope"):
            boom()
    assert any("failed" in r.message for r in caplog.records)


def test_traced_logs_start_and_finish(caplog):
    @traced
    def fn():
        return 1

    with caplog.at_level(logging.INFO):
        fn()
    messages = [r.message for r in caplog.records]
    assert any("started" in m for m in messages)
    assert any("finished" in m for m in messages)


def test_traced_custom_name_and_level(caplog):
    @traced(name="custom.label", level=logging.DEBUG)
    def fn():
        return 1

    with caplog.at_level(logging.DEBUG):
        fn()
    assert any("custom.label" in r.message for r in caplog.records)
    assert all(r.levelno == logging.DEBUG for r in caplog.records if "custom.label" in r.message)


@pytest.mark.asyncio
async def test_traced_async_returns_value_and_scopes_trace_id():
    seen_trace_ids = []

    @traced
    async def add(a, b):
        seen_trace_ids.append(get_trace_id())
        return a + b

    assert await add(2, 3) == 5
    assert seen_trace_ids[0] is not None
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_traced_async_reraises_and_logs_exception(caplog):
    @traced
    async def boom():
        raise ValueError("async nope")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError, match="async nope"):
            await boom()
    assert any("failed" in r.message for r in caplog.records)


def test_traced_nested_shares_root_trace_id():
    results = {}

    @traced
    def nested_inner():
        results["inner"] = (get_trace_id(), get_root_trace_id())

    @traced
    def nested_outer():
        results["outer"] = (get_trace_id(), get_root_trace_id())
        nested_inner()

    nested_outer()
    assert results["outer"][0] != results["inner"][0]
    assert results["outer"][1] == results["inner"][1]


def test_traced_sync_returning_none():
    @traced
    def fire_and_forget():
        pass  # implicit None

    assert fire_and_forget() is None


@pytest.mark.asyncio
async def test_traced_async_returning_none():
    @traced
    async def fire_and_forget():
        pass

    assert await fire_and_forget() is None


def test_traced_nested_inside_log_context(capture_json):
    """log_context()'s structured fields and @traced's trace_id scoping are
    independent contextvars — nesting one inside the other shouldn't disturb
    either."""
    log, captured = capture_json()

    @traced
    def step():
        log.info("inside traced step")
        return get_trace_id()

    with log_context(org_id="org-xyz"):
        trace_id = step()

    rec = captured[0]
    assert rec["org_id"] == "org-xyz"
    assert rec["trace_id"] == trace_id
