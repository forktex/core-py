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

"""Tests for forktex_core.log.setup_logging() — the single process-startup
entry point: format selection, custom handlers, non-blocking queue mode, env
var overrides, and quiet-logger silencing."""

from __future__ import annotations

import json
import logging
import time

from forktex_core.log import HumanFormatter, setup_logging


def test_setup_logging_debug_uses_human_formatter(capsys):
    setup_logging(service="test", debug=True)
    log = logging.getLogger("test.human")
    log.debug("debug message")
    # In debug mode, output is human-readable (not JSON)
    captured = capsys.readouterr()
    # Should not be valid JSON
    try:
        json.loads(captured.out.strip())
        valid_json = True
    except json.JSONDecodeError, ValueError:
        valid_json = False
    assert not valid_json or captured.out == ""


def test_setup_logging_json_forced_in_debug(capture_json):
    setup_logging(service="test", debug=True, json=True)
    log, captured = capture_json()
    log.info("forced json")
    assert captured[0]["level"] == "INFO"


def test_setup_logging_custom_handlers():
    captured: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    setup_logging(service="test", handlers=[CaptureHandler()], json=True)
    log = logging.getLogger("test.custom_handlers")
    log.info("via custom handler")
    assert captured == ["via custom handler"]


def test_setup_logging_queue_mode_delivers_records():
    captured: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    setup_logging(service="test", queue=True, handlers=[CaptureHandler()], json=True)
    log = logging.getLogger("test.queue")
    log.info("queued message")

    deadline = time.monotonic() + 2
    while not captured and time.monotonic() < deadline:
        time.sleep(0.01)

    assert captured == ["queued message"]


def test_setup_logging_env_level_override(monkeypatch):
    monkeypatch.setenv("FORKTEX_LOG_LEVEL", "DEBUG")
    setup_logging(service="test")
    assert logging.getLogger().level == logging.DEBUG


def test_setup_logging_env_debug_override(monkeypatch):
    monkeypatch.setenv("FORKTEX_LOG_DEBUG", "true")
    setup_logging(service="test")
    assert logging.getLogger().level == logging.DEBUG
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, HumanFormatter)


def test_setup_logging_env_json_override(monkeypatch):
    monkeypatch.setenv("FORKTEX_LOG_JSON", "0")
    setup_logging(service="test", debug=False)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, HumanFormatter)


def test_setup_logging_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("FORKTEX_LOG_DEBUG", "true")
    setup_logging(service="test", debug=False)
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_quiet_defaults_opt_out():
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    setup_logging(service="test", quiet_defaults=False)
    assert logging.getLogger("httpx").level == logging.NOTSET


def test_setup_logging_quiet_defaults_applied_by_default():
    setup_logging(service="test")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_custom_quiet_loggers():
    logging.getLogger("some.noisy.thing").setLevel(logging.NOTSET)
    setup_logging(service="test", quiet=["some.noisy.thing"], quiet_level=logging.ERROR)
    assert logging.getLogger("some.noisy.thing").level == logging.ERROR


def test_setup_logging_fmt_datefmt_passthrough():
    setup_logging(service="test", debug=True, fmt="%(message)s", datefmt="%H:%M")
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, HumanFormatter)
    record = logging.LogRecord("x", logging.INFO, "path", 1, "hi", None, None)
    assert handler.formatter.format(record) == "hi"


def test_setup_logging_level_filters_records():
    captured: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    setup_logging(service="test", level=logging.WARNING, handlers=[CaptureHandler()], json=True)
    log = logging.getLogger("test.level_filter")
    log.info("should be filtered out")
    log.warning("should pass through")
    assert captured == ["should pass through"]


def test_setup_logging_repeated_calls_with_different_configs():
    """setup_logging() is documented as idempotent/safe to call more than
    once — confirm a second call with a *different* config fully replaces
    the first's handler/formatter, not just adds to it."""
    first_captured: list[str] = []
    second_captured: list[str] = []

    class FirstHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            first_captured.append(record.getMessage())

    class SecondHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            second_captured.append(record.getMessage())

    setup_logging(service="a", handlers=[FirstHandler()], json=True, level=logging.INFO)
    setup_logging(service="b", handlers=[SecondHandler()], debug=True)

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, HumanFormatter)  # second config won (debug=True)

    logging.getLogger("test.repeated").info("only second handler should see this")
    assert first_captured == []
    assert second_captured == ["only second handler should see this"]
