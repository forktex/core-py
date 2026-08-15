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

"""Tests for forktex_core.iso — no containers needed (stdlib only)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from forktex_core.iso import from_date_iso, from_iso, now, to_date_iso, to_iso


def test_now_is_utc_aware():
    n = now()
    assert n.tzinfo is timezone.utc


def test_to_iso_naive_is_assumed_utc():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert to_iso(naive) == "2026-01-01T12:00:00+00:00"


def test_to_iso_aware_converts_to_utc():
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert to_iso(aware) == "2026-01-01T10:00:00+00:00"


def test_to_iso_preserves_microseconds_only_when_present():
    assert to_iso(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)) == "2026-01-01T12:00:00+00:00"
    assert to_iso(datetime(2026, 1, 1, 12, 0, 0, 500, tzinfo=timezone.utc)) == "2026-01-01T12:00:00.000500+00:00"


def test_to_iso_full_microsecond_precision():
    assert to_iso(datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)) == "2026-01-01T12:00:00.123456+00:00"


def test_to_iso_rejects_a_plain_date():
    """A plain `date` (not `datetime`) is a common mistake — reject it with a
    clear error pointing at to_date_iso(), not an AttributeError on .tzinfo."""
    with pytest.raises(TypeError, match="use to_date_iso"):
        to_iso(date(2026, 1, 1))  # type: ignore[arg-type]


def test_from_iso_accepts_z_suffix():
    """datetime.fromisoformat() accepts a trailing 'Z' since Python 3.11 —
    confirm it's correctly treated as UTC, not left naive."""
    parsed = from_iso("2026-01-01T12:00:00Z")
    assert parsed == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_from_iso_naive_is_assumed_utc():
    parsed = from_iso("2026-01-01T12:00:00")
    assert parsed == datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_from_iso_aware_converts_to_utc():
    parsed = from_iso("2026-01-01T12:00:00+02:00")
    assert parsed == datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_to_iso_strict_raises_on_naive_input():
    with pytest.raises(ValueError, match="naive"):
        to_iso(datetime(2026, 1, 1, 12, 0, 0), strict=True)


def test_to_iso_strict_accepts_aware_input():
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert to_iso(aware, strict=True) == "2026-01-01T12:00:00+00:00"


def test_to_iso_default_strict_false_unchanged():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    assert to_iso(naive) == to_iso(naive, strict=False) == "2026-01-01T12:00:00+00:00"


def test_from_iso_strict_raises_on_naive_input():
    with pytest.raises(ValueError, match="offset"):
        from_iso("2026-01-01T12:00:00", strict=True)


def test_from_iso_strict_accepts_aware_input():
    parsed = from_iso("2026-01-01T12:00:00+02:00", strict=True)
    assert parsed == datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_from_iso_default_strict_false_unchanged():
    assert from_iso("2026-01-01T12:00:00") == from_iso("2026-01-01T12:00:00", strict=False)


def test_from_iso_malformed_string_raises_value_error():
    with pytest.raises(ValueError):
        from_iso("not-a-date")


def test_from_date_iso_malformed_string_raises_value_error():
    with pytest.raises(ValueError):
        from_date_iso("not-a-date")


def test_to_date_iso_accepts_a_full_datetime():
    """datetime is a subclass of date — to_date_iso() extracts the date
    component rather than emitting the full timestamp text."""
    assert to_date_iso(datetime(2026, 1, 1, 15, 30, 0)) == "2026-01-01"


def test_to_iso_from_iso_round_trip():
    n = now()
    assert from_iso(to_iso(n)) == n


def test_to_date_iso():
    assert to_date_iso(date(2026, 1, 1)) == "2026-01-01"


def test_from_date_iso():
    assert from_date_iso("2026-01-01") == date(2026, 1, 1)


def test_to_date_iso_from_date_iso_round_trip():
    d = date(2026, 6, 15)
    assert from_date_iso(to_date_iso(d)) == d


def test_grid_datetime_normalize_output_unchanged():
    """Regression guard: grid's DateTimeType.normalize relocated its UTC
    normalization into to_iso() — the stored/indexed text must not change."""
    from forktex_core.grid.domain.fieldtypes.base import EmptyConfig
    from forktex_core.grid.domain.fieldtypes.temporal import DateTimeType

    handler = DateTimeType()
    config = EmptyConfig()
    naive = datetime(2026, 1, 1, 12, 0, 0)
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    assert handler.normalize(naive, config=config) == "2026-01-01T12:00:00+00:00"
    assert handler.normalize(aware, config=config) == "2026-01-01T07:00:00+00:00"
    assert handler.normalize("2026-01-01T12:00:00+00:00", config=config) == "2026-01-01T12:00:00+00:00"


def test_grid_date_normalize_output_unchanged():
    from forktex_core.grid.domain.fieldtypes.base import EmptyConfig
    from forktex_core.grid.domain.fieldtypes.temporal import DateType

    handler = DateType()
    config = EmptyConfig()
    assert handler.normalize(date(2026, 1, 1), config=config) == "2026-01-01"
    assert handler.normalize(datetime(2026, 1, 1, 12, 0, 0), config=config) == "2026-01-01"
    assert handler.normalize("2026-01-01", config=config) == "2026-01-01"
