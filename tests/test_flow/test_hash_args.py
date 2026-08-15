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

"""Unit tests for flow's step-arg hashing — no container required."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from forktex_core.flow.runtime.replay import _hash_args


def test_same_instant_in_different_offsets_hashes_identically():
    """The step cache keys on args_hash, so two spellings of one instant must
    hash the same or a replay re-executes work it already did.

    The encoder previously called the value's own ``.isoformat()``, which keeps
    whatever offset the caller happened to pass; routing through
    ``forktex_core.iso.to_iso`` normalizes to UTC first.
    """
    utc = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    plus_two = datetime(2026, 1, 1, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert utc == plus_two  # same instant, different offsets

    assert _hash_args((utc,), {}, call_ordinal=1) == _hash_args((plus_two,), {}, call_ordinal=1)


def test_distinct_instants_still_hash_differently():
    a = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    b = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    assert _hash_args((a,), {}, call_ordinal=1) != _hash_args((b,), {}, call_ordinal=1)


def test_plain_date_is_hashable_and_not_confused_with_datetime():
    """`to_iso` deliberately rejects a bare `date`, so the encoder must route
    dates through `to_date_iso` — checking datetime first, since datetime is a
    subclass of date."""
    d = date(2026, 1, 1)
    dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert _hash_args((d,), {}, call_ordinal=1)  # does not raise
    assert _hash_args((d,), {}, call_ordinal=1) != _hash_args((dt,), {}, call_ordinal=1)


def test_call_ordinal_participates_in_the_hash():
    args = (datetime(2026, 1, 1, tzinfo=UTC),)
    assert _hash_args(args, {}, call_ordinal=1) != _hash_args(args, {}, call_ordinal=2)
