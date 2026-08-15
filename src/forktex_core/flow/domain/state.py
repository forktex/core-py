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

"""State reduction: extract Annotated reducers and merge partial updates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, get_args, get_origin, get_type_hints

__all__ = ["apply_state_update"]

ReducerFn = Callable[[Any, Any], Any]


def _extract_reducers(state_cls: type) -> dict[str, ReducerFn]:
    """Extract {field: reducer_fn} from a TypedDict with Annotated hints.

    Annotated[list[str], operator.add] → reducer = operator.add for that field.
    Everything else → not in the returned dict (last-write-wins).

    Only the second argument of Annotated is inspected; if it is callable
    it is treated as the reducer.  Non-callable metadata is ignored so
    that Annotated can carry other information (e.g. documentation strings)
    without breaking the extractor.
    """
    reducers: dict[str, ReducerFn] = {}

    try:
        hints = get_type_hints(state_cls, include_extras=True)
    except Exception:
        # TypedDicts defined at module-level in <string> (e.g. REPL) may
        # not resolve forward refs; fall back gracefully.
        hints = getattr(state_cls, "__annotations__", {})

    for field_name, hint in hints.items():
        if get_origin(hint) is not Annotated:
            continue
        args = get_args(hint)
        # args[0] is the wrapped type; args[1:] are the metadata values.
        for meta in args[1:]:
            if callable(meta):
                reducers[field_name] = meta
                break  # first callable wins

    return reducers


def apply_state_update(
    current: dict[str, Any],
    update: dict[str, Any],
    reducers: dict[str, ReducerFn],
) -> dict[str, Any]:
    """Merge partial update dict into current state using declared reducers.

    For keys with a reducer: reducer(existing, new_value).
    For keys without: new_value overwrites.
    If key not in current: new_value is used directly (no reducer call on None).
    Returns a new dict; does not mutate current.
    """
    result = dict(current)
    for key, new_value in update.items():
        if key in reducers and key in current:
            result[key] = reducers[key](current[key], new_value)
        else:
            result[key] = new_value
    return result
