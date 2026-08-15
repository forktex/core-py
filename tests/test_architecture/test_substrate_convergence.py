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

"""`flow` and `grid` serve different purposes on the same skeleton.

They had drifted into two philosophies: `flow` flat with everything at the root and 59
deferred imports to break its cycles, `grid` a five-layer DAG whose layers were named
`app/` and `_kernel/`. Both now follow `standards/package-layout.md` — a role-named DAG,
`errors.py` where `errors.py` always is, the same migration entry point.

These assert the shape, not the behaviour, so a future package can be checked against the
same rules and a drift shows up here rather than in a reviewer's memory.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "forktex_core"
SUBSTRATES = ("flow", "grid")

#: Layer directories any substrate may have, in dependency order. A package needs only the
#: ones its purpose calls for — `grid` has no `runtime/` because it does not execute
#: anything; `flow` has no `write/` because its writes are the aggregates in `persist/`.
LAYER_ORDER = ("domain", "persist", "read", "write", "runtime")

#: Directories that would signal a return to a generic container name.
FORBIDDEN_DIRS = {"app", "_kernel", "common", "utils", "helpers", "core", "lib", "misc"}

#: Module names that carry no information about their contents.
FORBIDDEN_MODULES = {"core.py", "utils.py", "helpers.py", "misc.py", "base.py", "api.py"}


def _dirs(package: str) -> set[str]:
    root = SRC / package
    return {p.name for p in root.iterdir() if p.is_dir() and p.name != "__pycache__"}


@pytest.mark.parametrize("package", SUBSTRATES)
def test_no_generic_container_directories(package: str):
    found = _dirs(package) & FORBIDDEN_DIRS
    assert found == set(), (
        f"forktex_core.{package} has generic layer name(s) {sorted(found)} — name the layer "
        f"after the role it plays (see standards/package-layout.md)"
    )


@pytest.mark.parametrize("package", SUBSTRATES)
def test_no_generic_module_names(package: str):
    root = SRC / package
    found = sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py") if p.name in FORBIDDEN_MODULES)
    # `domain/fieldtypes/base.py` is the field-type ABC — `base` names exactly what it is.
    found = [f for f in found if f != "domain/fieldtypes/base.py"]
    assert found == [], f"forktex_core.{package} has uninformative module name(s): {found}"


@pytest.mark.parametrize("package", SUBSTRATES)
def test_every_layer_is_a_known_role(package: str):
    unknown = _dirs(package) - set(LAYER_ORDER)
    assert unknown == set(), (
        f"forktex_core.{package} has layer(s) {sorted(unknown)} outside the canonical set "
        f"{LAYER_ORDER} — either it is a role worth adding to the standard, or it is a bag"
    )


@pytest.mark.parametrize("package", SUBSTRATES)
def test_errors_live_in_errors_py(package: str):
    assert (SRC / package / "errors.py").exists(), (
        f"forktex_core.{package} has no errors.py — the same concern lives under the same name in every package"
    )


@pytest.mark.parametrize("package", SUBSTRATES)
def test_the_domain_layer_does_no_io(package: str):
    """`domain/` is the layer a reader can learn the vocabulary from. An I/O import there
    means the vocabulary cannot be understood without meeting a session."""
    io_roots = {"sqlalchemy", "asyncpg", "redis", "httpx", "aioboto3", "qdrant_client", "pymongo"}
    violations: list[str] = []
    for path in sorted((SRC / package / "domain").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            roots: set[str] = set()
            if isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            elif isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            if roots & io_roots:
                violations.append(f"{path.relative_to(SRC)}: imports {sorted(roots & io_roots)}")
    # grid's field types declare their SQL column types, which is a type declaration rather
    # than I/O — the one sanctioned exception, and it is why this lists offenders per file.
    if package == "grid":
        violations = [v for v in violations if "fieldtypes" not in v and "spec.py" not in v]
    assert violations == [], "\n  ".join(violations)


@pytest.mark.parametrize("package", SUBSTRATES)
def test_the_facade_is_named_after_itself(package: str):
    """No `core.py`. The module holding the public class is named after the class."""
    root = SRC / package
    facades = {"flow": ["flow.py"], "grid": ["grid.py", "namespace.py"]}[package]
    for name in facades:
        assert (root / name).exists(), f"expected forktex_core.{package}.{name}"
    assert not (root / "core.py").exists()


def test_both_substrates_expose_the_same_migration_entry_point():
    """One way to bring up a substrate schema, so a consumer's alembic hook can drive either."""
    from forktex_core.flow import apply_migrations as flow_apply
    from forktex_core.grid import apply_migrations as grid_apply

    for fn, schema in ((flow_apply, "forktex_flow"), (grid_apply, "forktex_grid")):
        params = inspect.signature(fn).parameters
        assert list(params)[:2] == ["engine", "schema"]
        assert params["schema"].default == schema
        for name, param in list(params.items())[2:]:
            assert param.default is not inspect.Parameter.empty, f"{name} must be optional"


def test_no_substrate_ships_a_console_script():
    """A library exposes functions; a console script is a consumer concern. `flow` shipped
    `forktex-flow` for two releases — the capability is now `flow.audit_workflows`."""
    import tomllib

    pyproject = tomllib.loads((SRC.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert scripts == {}, f"library declares console scripts: {scripts}"
