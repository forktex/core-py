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

"""The camelCase-on-the-wire promise has to hold for a plain `model_dump()`.

`BaseAppModel` documents "snake_case in Python, camelCase on the wire", but the
alias generator alone only applies aliases when a caller passes `by_alias=True`.
Every call site that forgot emitted snake_case and contradicted the error
envelope travelling on the same connection, so `serialize_by_alias` is now part
of the config and this pins it.
"""

from __future__ import annotations

from forktex_core.types import BaseAppModel, BaseWireValueObject


class _Payload(BaseAppModel):
    trace_id: str
    retry_after_seconds: int | None = None


class _Frozen(BaseWireValueObject):
    started_at: str


def test_model_dump_emits_camel_case_without_by_alias():
    dumped = _Payload(trace_id="abc", retry_after_seconds=3).model_dump()
    assert dumped == {"traceId": "abc", "retryAfterSeconds": 3}


def test_model_dump_json_emits_camel_case_too():
    assert '"traceId"' in _Payload(trace_id="abc").model_dump_json()


def test_frozen_wire_value_objects_follow_the_same_rule():
    assert _Frozen(started_at="2026-01-01T00:00:00Z").model_dump() == {"startedAt": "2026-01-01T00:00:00Z"}


def test_both_input_shapes_are_still_accepted():
    """The point of the convention is that curl and TypeScript clients can each
    use their native spelling, so serialisation must not narrow validation."""
    assert _Payload(trace_id="a").trace_id == "a"
    assert _Payload.model_validate({"traceId": "a"}).trace_id == "a"
    assert _Payload.model_validate({"trace_id": "a"}).trace_id == "a"
