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

"""Rules every package's public surface obeys, checked rather than trusted.

`docs/development.md`'s module checklist already requires `__all__` on every public
submodule and a lazy optional-dependency import whose error names the extra. Both were
human-verified until now, and both had live violations: `flow` exported
`_ParallelGroup`/`_PipelineStepSpec` — private-looking names in a public contract — and
`forktex_core.api` raised a bare `ModuleNotFoundError: No module named 'starlette'`
instead of naming its extra.
"""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "forktex_core"
CATALOG = SRC / "catalog" / "catalog.json"

#: Every package a consumer imports by name.
PACKAGES = sorted(e["id"] for e in json.loads(CATALOG.read_text(encoding="utf-8"))["extras"])

#: Packages whose import pulls a third-party dependency from an extra, mapped to every
#: top-level module that must be blocked to simulate a consumer without it — including
#: *transitive* ones the package imports directly. `api` declares `fastapi` but its
#: middleware imports `starlette`, so blocking only `fastapi` proves nothing: the probe
#: would succeed on starlette alone and report a false pass (it did, until this was fixed).
#:
#: `api` is also the one sanctioned exception to importing lazily — its middleware
#: subclasses `BaseHTTPMiddleware`, so the dependency is needed at class-definition
#: time. It must still name the extra in the error.
_EXTRA_BACKED = {
    "vault": ("cryptography",),
    "storage": ("aioboto3", "botocore"),
    "queue": ("arq",),
    "vector": ("qdrant_client",),
    "store": ("pymongo",),
    "api": ("fastapi", "starlette"),
}


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_declares_all(package: str):
    module = importlib.import_module(f"forktex_core.{package}")
    assert hasattr(module, "__all__"), f"forktex_core.{package} has no __all__"
    assert module.__all__, f"forktex_core.{package}.__all__ is empty"


@pytest.mark.parametrize("package", PACKAGES)
def test_no_package_exports_a_private_name(package: str):
    """A name in `__all__` is public by definition, so spelling it with a leading
    underscore tells the reader the opposite of the truth."""
    module = importlib.import_module(f"forktex_core.{package}")
    private = sorted(n for n in module.__all__ if n.startswith("_"))
    assert private == [], f"forktex_core.{package} exports private-looking names: {private}"


@pytest.mark.parametrize("package", PACKAGES)
def test_every_exported_name_resolves(package: str):
    module = importlib.import_module(f"forktex_core.{package}")
    missing = sorted(n for n in module.__all__ if not hasattr(module, n))
    assert missing == [], f"forktex_core.{package}.__all__ names unimportable attributes: {missing}"


# `__all__` ordering is deliberately NOT asserted here: ruff's RUF022 already enforces
# it on the source, and it uses an isort-style key (constants, then classes, then
# functions) that a second implementation would only get wrong and drift from.


def test_optional_dependencies_are_imported_lazily_or_name_their_extra():
    """`docs/development.md`: "Every module that requires an extra … imports its
    dependency lazily and raises `ImportError("Install forktex-core[X]…")` — never at
    module import time."

    Checked by importing each package in a subprocess with its third-party dependency
    blocked, which is the only way to observe what a consumer without the extra sees.
    """
    blocker = (
        "import builtins\n"
        "_blocked_names = {deps!r}\n"
        "_real = builtins.__import__\n"
        "def _blocked(name, *a, **k):\n"
        "    root = name.split('.')[0]\n"
        "    if root in _blocked_names:\n"
        "        raise ModuleNotFoundError(f'No module named {{root!r}}')\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _blocked\n"
        "try:\n"
        "    import forktex_core.{pkg}\n"
        "except ImportError as exc:\n"
        "    print('IMPORTERROR:' + str(exc))\n"
        "else:\n"
        "    print('LAZY')\n"
    )
    failures: list[str] = []
    for package, deps in sorted(_EXTRA_BACKED.items()):
        result = subprocess.run(
            [sys.executable, "-c", blocker.format(deps=set(deps), pkg=package)],
            capture_output=True,
            text=True,
            check=False,
        )
        out = result.stdout.strip()
        if out == "LAZY":
            continue
        if out.startswith("IMPORTERROR:"):
            message = out.removeprefix("IMPORTERROR:")
            if f"forktex-core[{package}]" not in message:
                failures.append(
                    f"forktex_core.{package}: import fails without {deps} and the error does not "
                    f"name the extra — got {message!r}"
                )
            continue
        failures.append(f"forktex_core.{package}: unexpected probe output {out!r}\n{result.stderr}")
    assert failures == [], "\n".join(failures)


def _module_docstring(path: Path) -> str | None:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_has_a_module_docstring(package: str):
    """The package docstring is where a reader lands first; an undocumented entry point
    forces them into the implementation."""
    init = SRC / package / "__init__.py"
    path = init if init.exists() else SRC / f"{package}.py"
    assert _module_docstring(path), f"{path.relative_to(SRC)} has no module docstring"


@pytest.mark.parametrize("package", PACKAGES)
def test_every_package_has_a_consumer_doc(package: str):
    """`docs/development.md` step: "Create `docs/<name>.md` (consumer reference …)"."""
    doc = SRC.parents[1] / "docs" / f"{package}.md"
    assert doc.exists(), f"missing docs/{package}.md for forktex_core.{package}"


def test_database_exports_everything_the_standards_tell_consumers_to_use():
    """`code-reuse.md` is a lookup table pointing at `forktex_core.database` names. A name it
    names must be reachable from the package root — `substrate_base`, `Page` and the cursor
    helpers were documented as the canonical choice while only importable via submodule.
    """
    import forktex_core.database as db

    documented = {
        # connection lifecycle
        "Database",
        "get_session",
        "session_scope",
        "init_engine",
        "close_engine",
        # ORM bases and the temporal type
        "BaseDBModel",
        "substrate_base",
        "UtcDateTime",
        "AuditMixin",
        "TimestampMixin",
        "NamespacedMixin",
        "JsonModelColumn",
        # the one page shape and its cursor codec
        "Page",
        "encode_cursor",
        "decode_cursor",
        "keyset_predicate",
        # locks, identifiers, integrity, migrations
        "advisory_lock",
        "try_advisory_lock",
        "xact_lock",
        "advisory_key",
        "key_from_uuid",
        "validate_identifier",
        "validate_schema",
        "validate_slug",
        "validate_relation",
        "is_identifier",
        "integrity_boundary",
        "read_boundary",
        "SchemaMigrationRunner",
        # the submodules a consumer reaches for directly
        "ddl",
        "reflect",
    }
    missing = sorted(documented - set(db.__all__))
    assert missing == [], f"documented in standards/code-reuse.md but not exported: {missing}"
