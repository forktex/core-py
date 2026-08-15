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

"""Log formatters: JSON (Loki-compatible) and human-readable (dev)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from forktex_core.iso import to_iso

# Fields that are part of the standard LogRecord but should NOT be forwarded
# as extra structured fields — they're either already mapped or are internal.
_SKIP_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "taskName",
        "trace_id",
        "root_trace_id",
        "service",
        "_forktex_extra",
    }
)


class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured Loki ingestion.

    Output per line (newline-delimited JSON)::

        {
          "timestamp": "2026-05-02T14:30:00.123456+00:00",
          "level": "INFO",
          "logger": "network.crm.contacts",
          "service": "network",
          "message": "contact created",
          "trace_id": "req-abc-123",
          "org_id": "org-xyz",
          "exception": "Traceback (most recent call last): ..."   // only on error
        }

    Loki label recommendations:
      - ``service`` — log stream selector (low cardinality)
      - ``level`` — stream selector
      - ``trace_id`` — structured metadata query (not label)

    Precedence: core fields (``timestamp``/``level``/``logger``/``service``/
    ``message``/``trace_id``/``root_trace_id``/``exception``) always win. A ``log_context()`` or
    ``extra={}`` field with a colliding name is silently dropped rather than
    overwriting the real value — e.g. ``extra={"level": "spoofed"}`` never
    changes the record's actual ``level``.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = to_iso(datetime.fromtimestamp(record.created, tz=UTC))
        doc: dict[str, Any] = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
        }
        # `service` is injected onto the record by _ContextFilter (single source).
        service = getattr(record, "service", None)
        if service:
            doc["service"] = service

        doc["message"] = record.getMessage()

        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            doc["trace_id"] = trace_id

        root_trace_id = getattr(record, "root_trace_id", None)
        if root_trace_id:
            doc["root_trace_id"] = root_trace_id

        extra = getattr(record, "_forktex_extra", {})
        for key, val in extra.items():
            doc.setdefault(key, val)

        for key, val in record.__dict__.items():
            if key not in _SKIP_FIELDS and not key.startswith("_"):
                doc.setdefault(key, val)

        if record.exc_info and record.exc_info[1]:
            doc["exception"] = self.formatException(record.exc_info)

        return json.dumps(doc, default=str)


class HumanFormatter(logging.Formatter):
    """Readable formatter for local development.

    Output::

        2026-05-02 14:30:00 | INFO     | network.crm | [req-abc] contact created
    """

    FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    DATEFMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, fmt: str | None = None, datefmt: str | None = None) -> None:
        super().__init__(fmt=fmt or self.FMT, datefmt=datefmt or self.DATEFMT)

    def format(self, record: logging.LogRecord) -> str:
        trace_id = getattr(record, "trace_id", None)
        if not trace_id:
            return super().format(record)
        # Prefix the trace id without permanently mutating the shared record
        # (a record may be handled by multiple handlers).
        original = record.msg
        record.msg = f"[{trace_id}] {record.msg}"
        try:
            return super().format(record)
        finally:
            record.msg = original


__all__ = ["HumanFormatter", "JsonFormatter"]
