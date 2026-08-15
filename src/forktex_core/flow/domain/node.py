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

"""Standalone @step / @node decorator and pipeline step primitives."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forktex_core.types import BaseValueObject

# Attribute the @step decorator attaches to a function to carry its retry
# contract. Named here once so call sites read it via ``step_meta`` rather
# than repeating the dunder string.
_STEP_META_ATTR = "__forktex_step_meta__"


class _NodeMeta(BaseValueObject):
    """Retry + backoff configuration stored on the function itself.

    The graph executor reads it (via :func:`step_meta`) at dispatch time.
    Storing it on the function (rather than in a registry) means the
    function carries its own durability contract and can be used with
    any Flow instance without prior registration.
    """

    max_attempts: int
    backoff: tuple[float, ...]


def step_meta(fn: object) -> _NodeMeta | None:
    """Return the ``_NodeMeta`` attached by :func:`step`, or ``None``.

    ``None`` is a legitimate result, not a broken invariant: the
    compilers (``compile_scheduled`` / ``compile_pipeline`` / ``compile_graph``
    / ``compile_config``) and ``step_template`` all accept bare,
    undecorated callables and fall back to default retry config. The
    typed accessor centralises the attribute name and gives call sites a
    precise return type without per-site ``# type: ignore``.
    """
    meta = getattr(fn, _STEP_META_ATTR, None)
    return meta if isinstance(meta, _NodeMeta) else None


def has_step_meta(fn: object) -> bool:
    """True if ``fn`` was decorated with :func:`step` (carries ``_NodeMeta``)."""
    return isinstance(getattr(fn, _STEP_META_ATTR, None), _NodeMeta)


class StepSpec(BaseValueObject):
    """Returned by step(fn, when=cond) for use in pipeline steps=[] arrays."""

    fn: Callable[..., Any]
    when: Callable[[dict[str, Any]], bool] | None = None
    max_attempts: int | None = None
    backoff: tuple[float, ...] | None = None


class ParallelGroup(BaseValueObject):
    """Returned by parallel(a, b, c) for use in pipeline steps=[] arrays."""

    # Tuple of Callable | StepSpec
    members: tuple[Any, ...]


_DEFAULT_MAX_ATTEMPTS: int = 3
_DEFAULT_BACKOFF: tuple[float, ...] = (30.0, 120.0, 300.0)


def step(
    fn: Callable[..., Any] | None = None,
    /,
    *,
    max_attempts: int | None = None,
    backoff: tuple[float, ...] | None = None,
    when: Callable[[dict[str, Any]], bool] | None = None,
) -> Callable[..., Any] | StepSpec:
    """Dual-mode decorator + pipeline step spec.

    As a decorator (@step or @step(max_attempts=5)):
        - Attaches _NodeMeta to fn.__forktex_step_meta__
        - Returns fn unchanged (the graph executor handles durability)

    As a pipeline step spec (step(fn, when=cond)):
        - Returns StepSpec(fn=fn, when=cond, ...)
        - fn must be passed as a positional argument

    When both fn and keyword args are given (step(fn, max_attempts=5)):
        - Returns StepSpec with the given overrides
    """
    resolved_max_attempts = max_attempts if max_attempts is not None else _DEFAULT_MAX_ATTEMPTS
    resolved_backoff = backoff if backoff is not None else _DEFAULT_BACKOFF

    def _attach_meta(target: Callable[..., Any]) -> Callable[..., Any]:
        """Attach _NodeMeta to target and return it unchanged."""
        meta = _NodeMeta(
            max_attempts=resolved_max_attempts,
            backoff=resolved_backoff,
        )
        setattr(target, _STEP_META_ATTR, meta)
        return target

    if fn is None:
        # Called as @step() or @step(max_attempts=5, ...) — no fn yet.
        # when= makes no sense without fn in decorator form; if provided
        # here without fn, it will be used when the returned decorator is
        # eventually called with a function, but since it's a decorator
        # factory we just return the decorator.
        if when is not None:
            # Partial pipeline spec without fn — invalid; raise early.
            raise TypeError("step(when=...) requires fn as a positional argument")
        return _attach_meta

    # fn is provided as positional argument.
    if when is not None or (max_attempts is not None or backoff is not None):
        # At least one pipeline-specific arg given: return a PipelineStepSpec.
        return StepSpec(
            fn=fn,
            when=when,
            max_attempts=max_attempts,
            backoff=backoff,
        )

    # Bare @step with fn and no kwargs — decorate in place.
    return _attach_meta(fn)


node = step


def parallel(*members: Callable[..., Any] | StepSpec) -> ParallelGroup:
    """Group of steps that run concurrently in a pipeline."""
    return ParallelGroup(members=members)
