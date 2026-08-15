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

"""Catalog renderer output checks."""

from __future__ import annotations

from forktex_core.catalog import current
from forktex_core.catalog.render import (
    render_all,
    render_dependency_grid,
    render_extras_grid,
    render_filesystem_tree,
    render_levels_table,
    render_pick_and_choose_matrix,
)


def test_levels_table_has_one_row_per_level():
    rendered = render_levels_table(current)
    # Header (1) + separator (1) + levels (4) = 6 lines minimum.
    lines = rendered.splitlines()
    assert lines[0].startswith("| Level "), "missing header"
    assert lines[1].startswith("|--"), "missing separator"
    body = [ln for ln in lines[2:] if ln.startswith("|")]
    assert len(body) == len(current.levels), f"expected {len(current.levels)} body rows, got {len(body)}"


def test_extras_grid_has_one_row_per_extra():
    rendered = render_extras_grid(current)
    lines = rendered.splitlines()
    body = [ln for ln in lines[2:] if ln.startswith("|")]
    assert len(body) == len(current.extras)


def test_dependency_grid_mentions_every_extra():
    rendered = render_dependency_grid(current)
    for e in current.extras:
        assert f"`{e.id}`" in rendered, f"extra {e.id!r} missing from dependency grid"


def test_pick_and_choose_matrix_only_uses_declared_extras():
    """The matrix is curated; every referenced extra must exist in the catalog."""
    rendered = render_pick_and_choose_matrix(current)
    declared = {f"`{e.id}`" for e in current.extras}
    # Pull all backtick-quoted tokens from the matrix and assert they're in the catalog.
    import re

    referenced = set(re.findall(r"`[a-z][a-z_]*`", rendered))
    unknown = referenced - declared
    assert not unknown, f"matrix references undeclared extras: {sorted(unknown)}"


def test_pick_and_choose_matrix_infers_infra_correctly():
    """Spot-check: the 'Pure tabular registers' row should require only postgres."""
    rendered = render_pick_and_choose_matrix(current)
    row = next(line for line in rendered.splitlines() if "Pure tabular registers" in line)
    # Format: "| <case> | <extras> | <infra> |"
    cells = [c.strip() for c in row.split("|")]
    infra_cell = cells[3]
    assert infra_cell == "postgres", f"expected `postgres`, got `{infra_cell}`"


def test_filesystem_tree_includes_each_level():
    rendered = render_filesystem_tree(current)
    for n in range(4):
        assert f"Level {n}:" in rendered, f"tree missing Level {n}"


def test_render_all_returns_known_blocks():
    blocks = render_all(current)
    assert set(blocks) == {"levels", "level0", "level1", "level2", "level3", "matrix", "tree"}
    for name, content in blocks.items():
        assert content.strip(), f"block {name!r} rendered empty"


def test_each_level_group_renders_correct_extras():
    """Per-level block lists exactly that level's extras — no more, no less.

    Each row's first column carries a markdown badge link
    ``[![<id>](badge-url)](docs/<id>.md)``; we look for that anchor."""
    from forktex_core.catalog.render import render_level_group

    for level_num in (0, 1, 2, 3):
        rendered = render_level_group(current, level_num)
        expected_ids = {e.id for e in current.extras_at_level(level_num)}
        for eid in expected_ids:
            anchor = f"](docs/{eid}.md)"
            assert anchor in rendered, f"level {level_num} missing badge link for {eid!r}"
        # No extras from other levels appear as row anchors.
        for e in current.extras:
            if e.level != level_num and f"](docs/{e.id}.md)" in rendered:
                raise AssertionError(f"level {level_num} group contains row for {e.id!r} which is at level {e.level}")


def test_level_cards_render_one_badge_per_extra():
    """Each level's cards render exactly N badges where N = extras at that level."""
    from forktex_core.catalog.render import render_level_cards

    for level_num in (0, 1, 2, 3):
        rendered = render_level_cards(current, level_num)
        expected = current.extras_at_level(level_num)
        # Each badge is an <a href=…><img …></a>.
        anchor_count = rendered.count('<a href="docs/')
        assert anchor_count == len(expected), f"level {level_num}: expected {len(expected)} cards, got {anchor_count}"
        for e in expected:
            assert f"docs/{e.id}.md" in rendered, f"level {level_num} missing card for {e.id}"


def test_level_cards_use_per_extra_color_when_set():
    """presentation.colors[id] should drive the badge color."""
    from forktex_core.catalog.render import render_level_cards

    rendered = render_level_cards(current, 1)
    # database is set to 336791 in catalog.json
    assert "336791" in rendered, "database badge color missing"
    # cache is set to DC382D
    assert "DC382D" in rendered, "cache badge color missing"
