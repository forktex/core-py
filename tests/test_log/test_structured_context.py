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

"""Tests for forktex_core.log's log_context()/async_log_context() — structured
extra fields injected into every log record emitted within a block."""

from __future__ import annotations

import pytest

from forktex_core.log import async_log_context, get_extra_fields, log_context


def test_log_context_merges_fields(capture_json):
    log, captured = capture_json()
    with log_context(org_id="org-xyz", user_id="usr-1"):
        log.info("inside context")
    rec = captured[0]
    assert rec["org_id"] == "org-xyz"
    assert rec["user_id"] == "usr-1"


def test_log_context_does_not_leak(capture_json):
    log, captured = capture_json()
    with log_context(org_id="org-xyz"):
        pass
    log.info("outside context")
    assert "org_id" not in captured[0]


def test_log_context_nested_merge(capture_json):
    log, captured = capture_json()
    with log_context(a="1"):
        with log_context(b="2"):
            log.info("nested")
    rec = captured[0]
    assert rec["a"] == "1"
    assert rec["b"] == "2"


@pytest.mark.asyncio
async def test_async_log_context(capture_json):
    log, captured = capture_json()
    async with async_log_context(request_id="req-99"):
        log.info("async context")
    assert captured[0]["request_id"] == "req-99"
    log.info("after async context")
    assert "request_id" not in captured[1]


def test_get_extra_fields_snapshots_current_context():
    """Public API for a caller wanting to inspect/propagate the current
    structured context — e.g. into a thread pool or background task — rather
    than only having it implicitly injected into log records."""
    assert get_extra_fields() == {}
    with log_context(org_id="org-xyz"):
        assert get_extra_fields() == {"org_id": "org-xyz"}
        with log_context(user_id="usr-1"):
            assert get_extra_fields() == {"org_id": "org-xyz", "user_id": "usr-1"}
    assert get_extra_fields() == {}
