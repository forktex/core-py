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

"""The level DAG, enforced instead of asserted in prose.

`docs/ARCHITECTURE.md` and `catalog/catalog.json` both state the rule — "every module
lives at a level and may import only **lower** levels + third-party, never a sibling
facade or upward" — and `docs/ARCHITECTURE.md` claims it is "verified downward-only
across all modules". Until this file, that verification was a human reading imports.

The levels come from `catalog.json`, which is the machine source of truth, so a new
module cannot be added to one place and forgotten in the other.
"""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "forktex_core"
CATALOG = SRC / "catalog" / "catalog.json"

#: Build tooling, not a runtime module — `docs/ARCHITECTURE.md` says so explicitly.
#: It renders `catalog.json` into the README and is imported by nothing at runtime.
_NOT_LEVELLED = {"catalog"}

#: Modules that live in the tree but are not catalog extras.
_UNLEVELLED_MODULES = {"alembic", "common", "fractal"}


def _levels() -> dict[str, int]:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    return {e["id"]: e["level"] for e in catalog["extras"]}


def _package_of(module: Path) -> str:
    return module.relative_to(SRC).parts[0].removesuffix(".py")


def _internal_imports(path: Path) -> set[str]:
    """Every `forktex_core.<pkg>` this file imports, at any nesting depth.

    Function-local and `TYPE_CHECKING` imports count: a lazy import is still a
    dependency, and hiding an upward one inside a function would defeat the rule.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            parts = node.module.split(".")
            if parts[0] == "forktex_core":
                if len(parts) > 1:
                    found.add(parts[1])
                else:
                    # `from forktex_core import queue` — the names are the packages.
                    found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "forktex_core" and len(parts) > 1:
                    found.add(parts[1])
    return found


def _levelled_modules() -> list[Path]:
    levels = _levels()
    return [p for p in sorted(SRC.rglob("*.py")) if _package_of(p) in levels and _package_of(p) not in _NOT_LEVELLED]


def test_catalog_covers_every_shipped_module():
    """A module absent from the catalog has no declared level, so the rule below
    cannot police it — that gap is how drift starts."""
    levels = _levels()
    shipped = {
        p.relative_to(SRC).parts[0].removesuffix(".py")
        for p in SRC.iterdir()
        if (p.is_dir() and (p / "__init__.py").exists()) or (p.suffix == ".py" and p.name != "__init__.py")
    }
    shipped -= {"__pycache__"} | _UNLEVELLED_MODULES
    missing = sorted(shipped - set(levels) - _NOT_LEVELLED)
    assert missing == [], f"shipped modules with no level in catalog.json: {missing}"


def test_no_module_imports_a_higher_level():
    levels = _levels()
    violations: list[str] = []
    for path in _levelled_modules():
        pkg = _package_of(path)
        for imported in _internal_imports(path):
            if imported not in levels or imported == pkg:
                continue
            if levels[imported] > levels[pkg]:
                rel = path.relative_to(SRC.parent.parent)
                violations.append(f"{rel}: L{levels[pkg]} {pkg} imports L{levels[imported]} {imported}")
    assert violations == [], "upward imports break the level DAG:\n  " + "\n  ".join(sorted(violations))


#: Sibling couplings that are sanctioned, each with its recorded reason. The rule in
#: `catalog.json` bans a "sibling-**facade**" import, not any same-level import — and
#: `docs/ARCHITECTURE.md` documents the L0 ones deliberately: "`types` now depends on
#: `iso` (a Level-0 sibling, for `UtcDateTime`/`UtcDate`) … 'no dependency on a
#: facade/substrate module' still does [describe it]". Listing them explicitly means a
#: *new* coupling is a deliberate act that shows up in review, not a silent one.
_SANCTIONED_SIBLINGS = {
    ("types", "iso"): "UtcDateTime/UtcDate serialise through iso",
    ("error", "types"): "ErrorEnvelope is a BaseAppModel",
    ("log", "iso"): "log records stamp canonical UTC timestamps",
    ("space", "grid"): "space is documented as a wrapper over grid",
}


def test_no_module_imports_an_unsanctioned_sibling():
    """Two modules at the same level must compose in the layer above, not reach
    sideways — except where the coupling is declared above."""
    levels = _levels()
    violations: list[str] = []
    for path in _levelled_modules():
        pkg = _package_of(path)
        for imported in _internal_imports(path):
            if imported not in levels or imported == pkg:
                continue
            if levels[imported] == levels[pkg] and (pkg, imported) not in _SANCTIONED_SIBLINGS:
                rel = path.relative_to(SRC.parent.parent)
                violations.append(f"{rel}: {pkg} imports sibling {imported} (both L{levels[pkg]})")
    assert violations == [], (
        "unsanctioned sibling imports break the level DAG — add a reason to "
        "_SANCTIONED_SIBLINGS only if the coupling is genuinely intended:\n  " + "\n  ".join(sorted(violations))
    )


def test_no_facade_imports_a_sibling_facade():
    """The half of the rule with no exceptions: L1 role facades are peers over
    different infrastructure, so one reaching for another would make a service that
    wants Redis drag in Postgres."""
    levels = _levels()
    violations = [
        f"{path.relative_to(SRC)}: {_package_of(path)} imports sibling facade {imported}"
        for path in _levelled_modules()
        if levels[_package_of(path)] == 1
        for imported in _internal_imports(path)
        if imported != _package_of(path) and levels.get(imported) == 1
    ]
    assert violations == [], "\n  ".join(sorted(violations))


def test_every_sanctioned_sibling_is_still_real():
    """A stale exemption is a rule that has quietly stopped applying."""
    levels = _levels()
    live = {
        (_package_of(path), imported)
        for path in _levelled_modules()
        for imported in _internal_imports(path)
        if imported in levels and levels[imported] == levels[_package_of(path)] and imported != _package_of(path)
    }
    unused = sorted(pair for pair in _SANCTIONED_SIBLINGS if pair not in live)
    assert unused == [], f"_SANCTIONED_SIBLINGS lists couplings that no longer exist: {unused}"


def test_level_0_primitives_import_no_other_forktex_module_except_level_0():
    """L0 is the floor: if a primitive reaches for a facade, nothing below it is safe
    to import from anywhere."""
    levels = _levels()
    zero = {m for m, lvl in levels.items() if lvl == 0}
    violations: list[str] = []
    for path in _levelled_modules():
        pkg = _package_of(path)
        if pkg not in zero:
            continue
        for imported in _internal_imports(path):
            if imported in levels and imported not in zero:
                violations.append(f"{path.relative_to(SRC)}: L0 {pkg} imports {imported}")
    assert violations == [], "\n  ".join(violations)


@pytest.mark.parametrize("package", ["log", "error", "types", "iso"])
def test_each_primitive_is_importable_alone(package: str):
    """A primitive that only works once something else is imported is not a primitive."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", f"import forktex_core.{package}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"forktex_core.{package} failed to import alone:\n{result.stderr}"


def _function_local_imports(path: Path) -> int:
    """Count `forktex_core` imports inside a function body.

    Two kinds of indented import are excluded, because counting them conflates *mandated*
    practice with the thing being measured:

    - `if TYPE_CHECKING:` blocks — the correct way to name a type without taking a runtime
      dependency. A grep-based count cannot tell these apart, and the first version of this
      check reported 47 for `flow` where the real figure was 30.
    - imports inside a `try:` — the lazy optional-dependency guard `docs/development.md`
      *requires*, so the `ImportError` can name the extra. Counting these marked `space`'s
      three `[storage]`/`[vector]` guards as debt to be removed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded: set[int] = {
        inner.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        for inner in ast.walk(node)
        if isinstance(inner, (ast.Import, ast.ImportFrom))
    }
    total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.Import, ast.ImportFrom)) or inner.lineno in guarded:
                continue
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("forktex_core"):
                total += 1
            elif isinstance(inner, ast.Import) and any(a.name.startswith("forktex_core") for a in inner.names):
                total += 1
    return total


def test_report_cycle_debt_per_package():
    """Not a threshold — a visible number, so a restructure can be judged.

    A function-local `forktex_core` import is the measurable cost of an unlayered package:
    each one exists to dodge a cycle that correct layering would not create.
    """
    counts: dict[str, int] = defaultdict(int)
    for path in sorted(SRC.rglob("*.py")):
        counts[_package_of(path)] += _function_local_imports(path)

    print("\ncycle debt (function-local forktex_core imports; TYPE_CHECKING and lazy-extra guards excluded):")
    for pkg, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n:
            print(f"  {pkg:10} {n}")
    # Actuals, so a new deferred import in an already-layered package fails here.
    # Actuals for every package, so any new deferred import fails here. The three
    # non-zero figures are the remaining genuine cycles, all in leaf-adjacent helpers.
    clean = {
        "database": 0,
        "cache": 0,
        "queue": 0,
        "store": 0,
        "storage": 0,
        "space": 0,
        "flow": 1,
        "grid": 2,
        "graph": 2,
    }
    regressed = {p: (counts[p], limit) for p, limit in clean.items() if counts[p] > limit}
    assert regressed == {}, f"cycle debt grew in already-layered packages: {regressed}"
