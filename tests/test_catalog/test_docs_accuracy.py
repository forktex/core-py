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

"""Docs cannot claim an API the library does not have.

`docs/` had 16 stale import claims when this was written — the removed 3.0
procedural façade (`list_tables`, `describe_table`, `query_rows`, `bind_table`),
`space.Space`/`SpaceConfig` after the rename to `Bundle`/`BundleConfig`, and two
modules (`grid.schemas`, `grid.enums`) that never existed under those names. Every
one of them would have been the first thing a new consumer typed.

This checks the *import* claims specifically, because they are the mechanically
verifiable part of a doc page. Prose accuracy still needs a human.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOC_PATHS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]

_PAREN_IMPORT = re.compile(r"^from (forktex_core[\w.]*) import \(([^)]*)\)", re.M)
_FLAT_IMPORT = re.compile(r"^from (forktex_core[\w.]*) import ([\w, ]+)$", re.M)


def _claims(text: str) -> list[tuple[str, str]]:
    """Every ``(module, name)`` pair a doc says is importable."""
    out: list[tuple[str, str]] = []
    for pattern in (_PAREN_IMPORT, _FLAT_IMPORT):
        for match in pattern.finditer(text):
            module = match.group(1)
            for raw in re.split(r"[,\n]", match.group(2)):
                name = raw.strip().split("#")[0].strip()
                if name.isidentifier():
                    out.append((module, name))
    return out


@pytest.mark.parametrize("doc", DOC_PATHS, ids=lambda p: p.name)
def test_documented_imports_resolve(doc: Path):
    unresolved: list[str] = []
    for module, name in _claims(doc.read_text(encoding="utf-8")):
        try:
            obj = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 — any import failure is a doc bug
            unresolved.append(f"{module} ({type(exc).__name__})")
            continue
        if hasattr(obj, name):
            continue
        # `from pkg import submodule` is legal even though the attribute only
        # appears once the submodule is imported — resolve it that way before
        # calling it stale.
        try:
            importlib.import_module(f"{module}.{name}")
        except ModuleNotFoundError:
            unresolved.append(f"{module}.{name}")
    assert unresolved == [], f"{doc.name} documents names that do not exist: {unresolved}"
