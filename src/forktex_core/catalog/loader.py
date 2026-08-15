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

"""Load + validate the architecture catalog JSON.

Validation extends Pydantic's structural checks with cross-entity invariants:

  * every relation's ``src`` and ``dst`` reference declared extras;
  * every level's ``extras`` list mentions only declared extras at that level;
  * every extra's ``depends_on`` / ``lazy_imports`` / ``optional_for_consumer``
    references declared extras;
  * the ``depends_on`` graph is acyclic;
  * an extra at level N never depends on an extra at level > N (bottom-up flow).

Failures raise ``CatalogValidationError`` with a list of issues so the test
fixture surfaces all problems at once instead of failing on the first.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from forktex_core.catalog.models import ArchitectureCatalog
from forktex_core.error import AppError, AppErrorCode

_CATALOG_PATH = Path(__file__).parent / "catalog.json"
"""The architecture catalog. Single, unversioned source of truth — git history
is the version history. Breaking changes are expected and welcome as the
architecture matures."""


class CatalogValidationError(AppError, ValueError):
    """Raised when the catalog JSON has cross-entity invariant violations.

    An ``AppError`` so it carries the library-wide error contract instead of
    surfacing as a bare ``ValueError`` (which an HTTP boundary can only render as
    a masked 500). ``ValueError`` stays in the bases so existing
    ``except ValueError`` handlers keep matching.
    """

    code = AppErrorCode.VALIDATION

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("catalog validation failed:\n  - " + "\n  - ".join(issues))


def load_current() -> ArchitectureCatalog:
    """Load + validate the shipped catalog. Cached so the JSON is parsed once."""
    return _load(_CATALOG_PATH)


@lru_cache(maxsize=1)
def _load(path: Path) -> ArchitectureCatalog:
    raw = json.loads(path.read_text(encoding="utf-8"))
    catalog = ArchitectureCatalog.model_validate(raw)
    issues = _cross_validate(catalog)
    if issues:
        raise CatalogValidationError(issues)
    return catalog


def _cross_validate(catalog: ArchitectureCatalog) -> list[str]:
    issues: list[str] = []
    extra_ids = {e.id for e in catalog.extras}
    extra_levels = {e.id: e.level for e in catalog.extras}

    # 1. Levels reference declared extras at their own level.
    for lvl in catalog.levels:
        for extra_id in lvl.extras:
            if extra_id not in extra_ids:
                issues.append(f"level {lvl.level} ({lvl.name}) lists undeclared extra {extra_id!r}")
                continue
            if extra_levels[extra_id] != lvl.level:
                issues.append(
                    f"extra {extra_id!r} declared at level {extra_levels[extra_id]} "
                    f"but listed in level {lvl.level} ({lvl.name})"
                )

    # 2. Extras' dep references resolve.
    for extra in catalog.extras:
        for dep in extra.depends_on:
            if dep not in extra_ids:
                issues.append(f"{extra.id}.depends_on references undeclared extra {dep!r}")
        for dep in extra.lazy_imports:
            if dep not in extra_ids:
                issues.append(f"{extra.id}.lazy_imports references undeclared extra {dep!r}")
        for dep in extra.optional_for_consumer:
            if dep not in extra_ids:
                issues.append(f"{extra.id}.optional_for_consumer references undeclared extra {dep!r}")

    # 3. Relations' endpoints resolve.
    for r in catalog.relations:
        if r.src not in extra_ids:
            issues.append(f"relation {r.kind} {r.src!r} → {r.dst!r} has unknown src")
        if r.dst not in extra_ids:
            issues.append(f"relation {r.kind} {r.src!r} → {r.dst!r} has unknown dst")

    # 4. depends_on is acyclic. Cheap DFS.
    deps: dict[str, set[str]] = {e.id: set(e.depends_on) for e in catalog.extras}

    def has_cycle_from(node: str, stack: set[str]) -> str | None:
        if node in stack:
            return node
        stack = stack | {node}
        for nxt in deps.get(node, ()):
            cycle = has_cycle_from(nxt, stack)
            if cycle:
                return cycle
        return None

    for extra in catalog.extras:
        cycle_node = has_cycle_from(extra.id, set())
        if cycle_node and cycle_node == extra.id:
            issues.append(f"depends_on cycle involving {extra.id!r}")
            break  # one report is enough

    # 5. Bottom-up flow: an extra at level N must not depend on an extra at level > N.
    for extra in catalog.extras:
        for dep in extra.depends_on:
            dep_level = extra_levels.get(dep)
            if dep_level is None:
                continue  # already reported as undeclared
            if dep_level > extra.level:
                issues.append(
                    f"{extra.id} (level {extra.level}) depends on {dep} (level {dep_level}) — bottom-up flow violated"
                )

    return issues


# Module-level shortcut for ergonomic imports.
current: ArchitectureCatalog = load_current()


__all__ = ["CatalogValidationError", "current", "load_current"]
