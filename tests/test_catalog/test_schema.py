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

"""Catalog schema + cross-validation tests."""

from __future__ import annotations

import pytest

from forktex_core.catalog import current
from forktex_core.catalog.loader import _cross_validate
from forktex_core.catalog.models import ArchitectureCatalog


# ── shipped catalog ─────────────────────────────────────────────────────


def test_current_catalog_loads():
    assert current.levels
    assert current.extras
    assert current.relations
    assert current.presentation


def test_each_level_has_extras():
    for level in current.levels:
        assert level.extras, f"level {level.level} ({level.name}) is empty"


def test_extras_grouped_by_level_match_levels_listing():
    for level in current.levels:
        listed = set(level.extras)
        actual = {e.id for e in current.extras_at_level(level.level)}
        assert listed == actual, f"level {level.level}: levels.extras={sorted(listed)} vs actual={sorted(actual)}"


def test_extra_ids_are_unique():
    ids = [e.id for e in current.extras]
    assert len(ids) == len(set(ids)), "duplicate extra ids in catalog"


def test_relations_endpoints_resolve():
    extra_ids = {e.id for e in current.extras}
    for r in current.relations:
        assert r.src in extra_ids, f"relation src {r.src!r} unknown"
        assert r.dst in extra_ids, f"relation dst {r.dst!r} unknown"


def test_depends_on_resolves():
    extra_ids = {e.id for e in current.extras}
    for e in current.extras:
        for dep in e.depends_on:
            assert dep in extra_ids, f"{e.id}.depends_on references unknown {dep!r}"


def test_bottom_up_flow():
    """An extra at level N never depends on something at level > N."""
    levels = {e.id: e.level for e in current.extras}
    for e in current.extras:
        for dep in e.depends_on:
            assert levels[dep] <= e.level, (
                f"{e.id} (lvl {e.level}) depends on {dep} (lvl {levels[dep]}) — violates bottom-up flow"
            )


def test_lookup_helpers():
    grid = current.extra("grid")
    assert grid.id == "grid"
    assert grid.level == 2

    with pytest.raises(KeyError):
        current.extra("nonexistent")

    with pytest.raises(KeyError):
        current.level(99)

    space_relations_out = current.relations_from("space")
    assert any(r.dst == "grid" for r in space_relations_out)
    assert any(r.dst == "graph" for r in space_relations_out)


# ── synthetic invalid catalogs ──────────────────────────────────────────


def _minimal_valid() -> dict:
    return {
        "levels": [
            {"level": 0, "name": "primitives", "description": "L0", "extras": ["log"]},
            {"level": 1, "name": "role_facades", "description": "L1", "extras": ["postgres"]},
        ],
        "extras": [
            {
                "id": "log",
                "level": 0,
                "kind": "primitive",
                "label": "Log",
                "role": "logs",
                "status": "shipped",
            },
            {
                "id": "postgres",
                "level": 1,
                "kind": "facade",
                "label": "Postgres",
                "role": "raw postgres adapter",
                "depends_on": ["log"],
                "status": "shipped",
                "tech": {"today": "postgres", "infra_required": "postgres"},
            },
        ],
        "relations": [],
        "presentation": {"level_colors": {"0": "neutral", "1": "info"}},
    }


def test_minimal_valid_catalog_passes_validation():
    catalog = ArchitectureCatalog.model_validate(_minimal_valid())
    assert _cross_validate(catalog) == []


def test_relation_endpoint_unknown_fails():
    bad = _minimal_valid()
    bad["relations"] = [{"kind": "depends_on", "src": "log", "dst": "missing"}]
    catalog = ArchitectureCatalog.model_validate(bad)
    issues = _cross_validate(catalog)
    assert any("missing" in i for i in issues)


def test_bottom_up_violation_fails():
    bad = _minimal_valid()
    bad["extras"][0]["depends_on"] = ["postgres"]  # log (lvl 0) depends on postgres (lvl 1)
    catalog = ArchitectureCatalog.model_validate(bad)
    issues = _cross_validate(catalog)
    assert any("bottom-up" in i for i in issues)


def test_level_lists_undeclared_extra_fails():
    bad = _minimal_valid()
    bad["levels"][0]["extras"].append("unknown")
    catalog = ArchitectureCatalog.model_validate(bad)
    issues = _cross_validate(catalog)
    assert any("unknown" in i for i in issues)


def test_extras_immutable():
    """ExtraSpec is frozen — guarantees the catalog is read-only at runtime."""
    grid = current.extra("grid")
    with pytest.raises(Exception):
        grid.id = "tampered"  # type: ignore[misc]
