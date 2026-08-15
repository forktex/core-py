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

"""Tests for forktex_core.log's JsonFormatter and HumanFormatter."""

from __future__ import annotations

import logging

from forktex_core.log import HumanFormatter, log_context, set_trace_id, trace_context


def test_json_basic_fields(capture_json):
    log, captured = capture_json(service="svc1")
    log.info("hello world")
    assert len(captured) == 1
    rec = captured[0]
    assert rec["message"] == "hello world"
    assert rec["level"] == "INFO"
    assert rec["service"] == "svc1"
    assert "timestamp" in rec
    assert "logger" in rec


def test_json_trace_id_injected(capture_json):
    log, captured = capture_json()
    set_trace_id("req-abc-123")
    log.info("with trace")
    assert captured[0]["trace_id"] == "req-abc-123"


def test_json_no_trace_id_when_unset(capture_json):
    log, captured = capture_json()
    log.info("no trace")
    assert "trace_id" not in captured[0] or captured[0].get("trace_id") is None


def test_json_exception_formatting(capture_json):
    log, captured = capture_json()
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("caught error")
    rec = captured[0]
    assert "exception" in rec
    assert "ValueError" in rec["exception"]


def test_json_formatter_includes_root_trace_id_when_set(capture_json):
    log, captured = capture_json()
    with trace_context("outer"):
        with trace_context("inner"):
            log.info("nested")
    rec = captured[0]
    assert rec["root_trace_id"] == "outer"
    assert rec["trace_id"] != "outer"


def test_json_formatter_omits_root_trace_id_when_unset(capture_json):
    log, captured = capture_json()
    log.info("no trace")
    assert "root_trace_id" not in captured[0]


def test_json_extra_field_collision_core_wins(capture_json):
    log, captured = capture_json()
    with log_context(level="SPOOFED", logger="spoofed-logger"):
        log.info("hello")
    rec = captured[0]
    assert rec["level"] == "INFO"
    assert rec["logger"] != "spoofed-logger"


def test_human_formatter_fmt_override():
    formatter = HumanFormatter(fmt="%(message)s", datefmt="%H:%M")
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    assert formatter.format(record) == "hi"


def test_human_formatter_prefixes_trace_id():
    formatter = HumanFormatter(fmt="%(message)s")
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    record.trace_id = "req-abc"
    assert formatter.format(record) == "[req-abc] hi"


def test_human_formatter_no_prefix_without_trace_id():
    formatter = HumanFormatter(fmt="%(message)s")
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    assert formatter.format(record) == "hi"


def test_human_formatter_does_not_show_root_trace_id():
    """root_trace_id is a JSON/machine-correlation field only — the dev-mode
    line stays [trace_id]-only, never [trace_id/root_trace_id]."""
    formatter = HumanFormatter(fmt="%(message)s")
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    record.trace_id = "req-abc"
    record.root_trace_id = "req-root"
    assert formatter.format(record) == "[req-abc] hi"


def test_human_formatter_does_not_mutate_shared_record():
    formatter = HumanFormatter(fmt="%(message)s")
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    record.trace_id = "req-abc"
    formatter.format(record)
    assert record.msg == "hi"  # not permanently mutated to "[req-abc] hi"
