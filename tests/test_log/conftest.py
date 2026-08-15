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

"""Shared fixtures for tests/test_log/*.py — no containers needed (stdlib only)."""

from __future__ import annotations

import json
import logging

import pytest

from forktex_core.log import JsonFormatter, get_logger, set_trace_id


@pytest.fixture(autouse=True)
def reset_trace_id():
    """Ensure trace_id is cleared between tests."""
    set_trace_id(None)
    yield
    set_trace_id(None)


@pytest.fixture
def capture_json():
    """Factory fixture: build an isolated logger + JSON-capturing handler.

    ``handler.addFilter(_ContextFilter(...))`` is required manually here —
    it's what ``setup_logging()`` wires onto the root handler in production,
    and it's what actually sets ``record.trace_id``/``root_trace_id``/
    ``service``/``_forktex_extra`` from the current contextvars (see
    docs/development.md's "log._ContextFilter" note).

        def test_x(capture_json):
            log, captured = capture_json(service="svc1")
            log.info("hello")
            assert captured[0]["message"] == "hello"
    """

    def _factory(service: str = "test") -> tuple[logging.Logger, list[dict]]:
        from forktex_core.log import _ContextFilter  # type: ignore[attr-defined]

        captured: list[dict] = []

        class CaptureHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(json.loads(self.format(record)))

        handler = CaptureHandler()
        handler.setFormatter(JsonFormatter())  # service is injected onto the record by _ContextFilter
        handler.addFilter(_ContextFilter(service=service))

        log = get_logger(f"test.module.{service}")
        log.handlers.clear()
        log.addHandler(handler)
        log.propagate = False
        log.setLevel(logging.DEBUG)
        return log, captured

    return _factory
