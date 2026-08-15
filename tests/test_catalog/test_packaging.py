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

"""The catalog, the docs and `pyproject.toml` have to agree on the extras.

pip only *warns* on an unknown extra, which is how `forktex-core[worker]` shipped
documented but undeclared: a consumer following `docs/worker.md` got
`forktex_core.worker` with no arq behind it and an ImportError at first use.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "src" / "forktex_core" / "catalog" / "catalog.json"

_INSTALL_RE = re.compile(r"forktex-core\[([a-z,\s]+)\]")


@pytest.fixture(scope="module")
def declared_extras() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["project"]["optional-dependencies"])


@pytest.fixture(scope="module")
def catalog_extras() -> set[str]:
    return {e["id"] for e in json.loads(CATALOG.read_text(encoding="utf-8"))["extras"]}


def test_every_catalog_extra_is_installable(declared_extras: set[str], catalog_extras: set[str]):
    """The catalog is the documented map of the library, so every id it names must
    resolve as an install target — even the always-bundled ones, whose extras are
    deliberately empty."""
    missing = sorted(catalog_extras - declared_extras)
    assert missing == [], f"catalog names extras that pyproject does not declare: {missing}"


def test_no_extra_is_declared_without_a_catalog_entry(declared_extras: set[str], catalog_extras: set[str]):
    """The other direction: an undocumented extra is an extra nobody will find."""
    extra = sorted(declared_extras - catalog_extras - {"all"})
    assert extra == [], f"pyproject declares extras the catalog does not describe: {extra}"


def test_every_documented_install_target_exists(declared_extras: set[str]):
    """Scan the install commands the docs actually tell people to run."""
    sources = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    unknown: dict[str, set[str]] = {}
    for path in sources:
        for match in _INSTALL_RE.finditer(path.read_text(encoding="utf-8")):
            named = {part.strip() for part in match.group(1).split(",") if part.strip()}
            if bad := named - declared_extras:
                unknown.setdefault(path.name, set()).update(bad)
    assert unknown == {}, f"docs reference undeclared extras: {unknown}"


def test_all_covers_every_third_party_dependency(declared_extras: set[str]):
    """`[all]` is what a consumer installs to get everything; an extra missing
    from it is a silently absent dependency."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = data["project"]["optional-dependencies"]
    every = {dep for name, deps in optional.items() if name != "all" for dep in deps}
    assert every - set(optional["all"]) == set(), "[all] is missing a dependency declared by another extra"
