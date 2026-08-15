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

"""Canonical ISO-8601 date/time handling for ForkTex Python services.

Zero extra dependencies — stdlib only. The one place that decides how a
datetime becomes text and back: always UTC, always ``datetime.isoformat()``'s
default precision/offset shape (``+00:00``, microseconds only when nonzero).
A naive input is assumed to already be UTC rather than raising, since that's
the shape every existing caller in this codebase already relied on before
this module existed — it isn't a new leniency being introduced. Pass
``strict=True`` to ``to_iso()``/``from_iso()`` to raise on naive input
instead — useful for a caller with no such existing assumption to preserve
(e.g. an external consumer of this library).

``log`` (JSON timestamps), ``grid`` (canonical stored/indexed temporal text),
``flow`` (pagination cursors, retry timestamps), and ``database`` (JSON-column
datetime fields) all delegate here instead of each hand-rolling the same
UTC-normalization dance — before this module existed they had drifted (some
forced UTC, some didn't; none agreed on precision).

    from forktex_core.iso import now, to_iso, from_iso

    now()                          # datetime.now(timezone.utc)
    to_iso(now())                  # "2026-08-12T10:30:00.123456+00:00"
    from_iso("2026-08-12T10:30:00+00:00")  # naive input is assumed UTC too
"""

from __future__ import annotations

from datetime import UTC, date, datetime

__all__ = ["from_date_iso", "from_iso", "now", "to_date_iso", "to_iso"]


def now() -> datetime:
    """The one canonical "current time" call — always UTC-aware."""
    return datetime.now(UTC)


def to_iso(value: datetime, *, strict: bool = False) -> str:
    """Canonical ISO-8601 text for a datetime: naive is assumed UTC, aware is
    converted to UTC, then rendered via ``datetime.isoformat()``.

    Pass ``strict=True`` to raise ``ValueError`` on naive input instead of
    assuming UTC — useful for a caller with no existing "naive means UTC"
    assumption to preserve.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{value!r} is not a datetime — use to_date_iso() for date values")
    if value.tzinfo is None:
        if strict:
            raise ValueError(f"{value!r} is a naive datetime — pass an aware one, or strict=False")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def from_iso(value: str, *, strict: bool = False) -> datetime:
    """Parse ISO-8601 text back to a UTC-aware datetime (see :func:`to_iso`).

    Pass ``strict=True`` to raise ``ValueError`` if ``value`` has no offset
    (would otherwise be assumed UTC).
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if strict:
            raise ValueError(f"{value!r} has no UTC offset — pass an offset, or strict=False")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def to_date_iso(value: date) -> str:
    """Canonical ISO-8601 text for a calendar date (``YYYY-MM-DD``).

    ``datetime`` is a subclass of ``date``, so a full ``datetime`` is
    accepted too — its date component is used (``datetime.isoformat()``
    on a ``datetime`` would otherwise emit the full timestamp, not just
    ``YYYY-MM-DD``)."""
    if isinstance(value, datetime):
        value = value.date()
    return value.isoformat()


def from_date_iso(value: str) -> date:
    """Parse ``YYYY-MM-DD`` text back to a :class:`date`."""
    return date.fromisoformat(value)
