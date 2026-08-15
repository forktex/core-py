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

"""Render the architecture catalog to Markdown for README + docs.

Each renderer takes an ``ArchitectureCatalog`` and returns a Markdown string.
The README contains generated blocks bracketed by HTML comments::

    <!-- catalog:levels start -->
    ...generated table...
    <!-- catalog:levels end -->

``scripts/regenerate_readme.py`` substitutes block contents in place;
``make catalog-check`` runs the renderer and diff-fails CI on drift.
"""

from __future__ import annotations

from forktex_core.catalog.models import ArchitectureCatalog, ExtraSpec

_STATUS_BADGE = {
    "shipped": "✅ shipped",
    "in_progress": "🛠️ in progress",
    "planned": "📋 planned",
}


def render_levels_table(catalog: ArchitectureCatalog) -> str:
    """One row per level — its number, name, description, and the extras at it."""
    rows = [
        "| Level | Name | Description | Extras |",
        "|------:|:-----|:------------|:-------|",
    ]
    for lvl in catalog.levels:
        extras = " · ".join(f"`{e}`" for e in lvl.extras)
        rows.append(f"| {lvl.level} | **{lvl.name}** | {lvl.description} | {extras} |")
    return "\n".join(rows)


_DEFAULT_LEVEL_COLOR = "455A64"


def _color_for(catalog: ArchitectureCatalog, extra_id: str) -> str:
    """Per-extra color (without #) — falls back to level color, then default."""
    colors = catalog.presentation.colors
    if extra_id in colors:
        return colors[extra_id]
    extra = catalog.extra(extra_id)
    return catalog.presentation.level_colors.get(str(extra.level), _DEFAULT_LEVEL_COLOR)


def _badge_url(label: str, message: str, color: str) -> str:
    """shields.io for-the-badge URL with a label and short message.

    Both label and message are URL-encoded with shields.io's spec: spaces → %20,
    dashes → ``--``, underscores → ``__``.
    """

    def _enc(text: str) -> str:
        return text.replace("-", "--").replace("_", "__").replace(" ", "%20")

    return f"https://img.shields.io/badge/{_enc(label)}-{_enc(message)}-{color}?style=for-the-badge"


def _badge_link_for(catalog: ArchitectureCatalog, extra_id: str) -> str:
    """Markdown ``[![label](url)](docs/<id>.md)`` for use inside a table cell.

    Each badge: label = extra id, message = short tech / role hint, color from
    ``presentation.colors[extra_id]``. Linking to ``docs/<id>.md`` keeps the
    one-click navigation consumers expect.
    """
    e = catalog.extra(extra_id)
    if e.tech and e.tech.today and e.tech.today != "in-memory":
        message = e.tech.today
    elif e.tech and e.tech.today == "in-memory":
        message = "in-memory"
    else:
        message = e.label.lower().replace(" ", "%20")
    color = _color_for(catalog, extra_id)
    url = _badge_url(extra_id, message, color)
    return f"[![{extra_id}]({url})](docs/{extra_id}.md)"


def render_level_cards(catalog: ArchitectureCatalog, level_num: int) -> str:
    """Backwards-compatible centred-cards renderer.

    Kept for tooling that still asks for it; the canonical README block
    embeds the badge directly into the table's first column via
    ``render_level_group``. New code should prefer that.
    """
    lvl = catalog.level(level_num)
    parts = ['<p align="center">']
    for extra_id in lvl.extras:
        e = catalog.extra(extra_id)
        if e.tech and e.tech.today and e.tech.today != "in-memory":
            message = e.tech.today
        elif e.tech and e.tech.today == "in-memory":
            message = "in-memory"
        else:
            message = e.label.lower().replace(" ", "%20")
        color = _color_for(catalog, extra_id)
        url = _badge_url(extra_id, message, color)
        parts.append(f'  <a href="docs/{extra_id}.md"><img src="{url}" alt="{extra_id}"></a>')
    parts.append("</p>")
    return "\n".join(parts)


def render_level_group(
    catalog: ArchitectureCatalog,
    level_num: int,
    *,
    shipped_only: bool = False,
) -> str:
    """One level rendered as a focused section: description + table.

    The shields.io badge sits in the table's first column (instead of in
    a separate centred row above) so the row is visually scannable
    end-to-end without splitting attention between two blocks. Each
    badge links to ``docs/<id>.md``.

    ``shipped_only=True`` drops rows whose status isn't ``"shipped"``.
    If filtering empties the level, the description block is followed
    by a short italic placeholder so the rendered section stays visually
    whole rather than a pair of empty markers.

    Column shapes per level:

      * Level 0 (primitives) — Extra · Role · Status
      * Level 1 (role facades) — Extra · Role · Depends on · Status
      * Level 2 (substrate facades) — Extra · Role · Depends on · Lazy imports · Status
      * Level 3 (bootstraps) — Extra · Role · Required · Optional for consumer · Status
    """
    lvl = catalog.level(level_num)
    extras = [catalog.extra(eid) for eid in lvl.extras]
    if shipped_only:
        extras = [e for e in extras if e.status == "shipped"]
        if not extras:
            placeholder = f"_No shipped {lvl.name.replace('_', ' ')} at this level._"
            return f"_{lvl.description}_\n\n{placeholder}"

    def _cell(extra_id: str) -> str:
        return _badge_link_for(catalog, extra_id)

    if level_num == 0:
        rows = [
            "| Extra | Role | Status |",
            "|:------|:-----|:-------|",
        ]
        for e in extras:
            rows.append(f"| {_cell(e.id)} | {e.role} | {_status_with_phase(e)} |")
    elif level_num == 1:
        rows = [
            "| Extra | Role | Depends on | Status |",
            "|:------|:-----|:-----------|:-------|",
        ]
        for e in extras:
            deps = ", ".join(f"`{d}`" for d in e.depends_on) or "—"
            rows.append(f"| {_cell(e.id)} | {e.role} | {deps} | {_status_with_phase(e)} |")
    elif level_num == 2:
        rows = [
            "| Extra | Role | Depends on | Lazy imports | Status |",
            "|:------|:-----|:-----------|:-------------|:-------|",
        ]
        for e in extras:
            deps = ", ".join(f"`{d}`" for d in e.depends_on) or "—"
            lazy = ", ".join(f"`{d}`" for d in e.lazy_imports) or "—"
            rows.append(f"| {_cell(e.id)} | {e.role} | {deps} | {lazy} | {_status_with_phase(e)} |")
    elif level_num == 3:
        rows = [
            "| Extra | Role | Required | Optional for consumer | Status |",
            "|:------|:-----|:---------|:----------------------|:-------|",
        ]
        for e in extras:
            deps = ", ".join(f"`{d}`" for d in e.depends_on) or "—"
            opt = ", ".join(f"`{d}`" for d in e.optional_for_consumer) or "—"
            rows.append(f"| {_cell(e.id)} | {e.role} | {deps} | {opt} | {_status_with_phase(e)} |")
    else:
        raise ValueError(f"Unknown level {level_num}")

    return f"_{lvl.description}_\n\n" + "\n".join(rows)


def _status_with_phase(extra: ExtraSpec) -> str:
    badge = _STATUS_BADGE.get(extra.status, extra.status)
    if extra.status != "shipped" and extra.phase is not None:
        return f"{badge} (phase {extra.phase})"
    return badge or extra.status


def render_extras_grid(catalog: ArchitectureCatalog) -> str:
    """One row per extra — name, level, role, tech, status, phase."""
    rows = [
        "| Extra | Level | Role | Tech today | Depends on | Status |",
        "|:------|:-----:|:-----|:-----------|:-----------|:-------|",
    ]
    for extra in catalog.extras:
        tech = (extra.tech.today or "—") if extra.tech else "—"
        deps = ", ".join(f"`{d}`" for d in extra.depends_on) if extra.depends_on else "—"
        status = _STATUS_BADGE.get(extra.status, extra.status)
        if extra.status != "shipped" and extra.phase is not None:
            status = f"{status} (phase {extra.phase})"
        rows.append(f"| **`{extra.id}`** | {extra.level} | {extra.role} | {tech} | {deps} | {status} |")
    return "\n".join(rows)


def render_dependency_grid(catalog: ArchitectureCatalog) -> str:
    """One row per extra — its outgoing relations grouped by kind."""
    rows = [
        "| Extra | Depends on | Lazy-imports | Optional for consumer |",
        "|:------|:-----------|:-------------|:----------------------|",
    ]
    for extra in catalog.extras:
        deps = ", ".join(f"`{d}`" for d in extra.depends_on) or "—"
        lazy = ", ".join(f"`{d}`" for d in extra.lazy_imports) or "—"
        opt = ", ".join(f"`{d}`" for d in extra.optional_for_consumer) or "—"
        rows.append(f"| **`{extra.id}`** | {deps} | {lazy} | {opt} |")
    return "\n".join(rows)


def render_pick_and_choose_matrix(catalog: ArchitectureCatalog) -> str:
    """Use-case → required extras → infra needed.

    The matrix is curated (not derived) — it shows common consumer shapes the
    catalog supports. Each row's "extras" column comes from the catalog's
    declared dependencies; the "infra" column is computed from each extra's
    tech.infra_required.
    """
    # Curated use cases. Each maps to a set of extras; we compute the transitive
    # infra services from the catalog.
    cases: list[tuple[str, list[str]]] = [
        ("Pure tabular registers (basic field types only)", ["grid"]),
        ("Tabular registers + vectors (VECTOR field added)", ["grid", "space", "vector"]),
        ("Tabular registers + files (FILE field added)", ["grid", "space", "storage"]),
        ("Multi-grid bundle with VECTOR + FILE", ["grid", "space", "vector", "storage"]),
        ("In-memory graph analysis only", ["graph"]),
        ("Just durable workflows", ["flow"]),
        ("API server, no DB", ["api"]),
        ("API server with grid CRUD", ["api", "grid"]),
        (
            "API server with rich content + middleware",
            ["api", "grid", "space", "vector", "storage", "cache"],
        ),
        ("Background worker, pure compute", ["worker"]),
        ("Background worker with flow runs", ["worker", "flow"]),
        (
            "Background worker with grid + flow + vector embedding",
            ["worker", "flow", "grid", "space", "vector"],
        ),
    ]

    rows = [
        "| Consumer wants… | forktex_core extras | Infra services |",
        "|:----------------|:--------------------|:---------------|",
    ]
    for description, extras in cases:
        infra = sorted(_infra_services_for(catalog, extras))
        infra_str = ", ".join(infra) if infra else "(none)"
        extras_str = ", ".join(f"`{e}`" for e in extras)
        rows.append(f"| {description} | {extras_str} | {infra_str} |")
    return "\n".join(rows)


def _infra_services_for(catalog: ArchitectureCatalog, extras: list[str]) -> set[str]:
    """Walk depends_on transitively from the given extras and collect infra services.

    Skips ``lazy_imports`` and ``optional_for_consumer`` — those are surfaced
    in the dedicated dependency grid, not here.
    """
    seen: set[str] = set()
    infra: set[str] = set()
    queue: list[str] = list(extras)

    while queue:
        eid = queue.pop()
        if eid in seen:
            continue
        seen.add(eid)
        try:
            extra = catalog.extra(eid)
        except KeyError:
            continue
        if extra.tech and extra.tech.infra_required:
            infra.add(extra.tech.infra_required)
        queue.extend(extra.depends_on)

    return infra


def render_filesystem_tree(
    catalog: ArchitectureCatalog,
    *,
    shipped_only: bool = False,
) -> str:
    """ASCII tree of `forktex_core/<extra>/` derived from the catalog.

    Groups by level for readability. ``shipped_only=True`` drops rows
    whose status isn't ``"shipped"``; if a level empties out, the tree
    skips that level header entirely (the rendered README still has
    its own per-level sections that handle the placeholder above the
    tree, so suppression here is the right call).
    """
    lines = ["```", "forktex_core/"]

    for lvl in catalog.levels:
        level_extras = lvl.extras
        if shipped_only:
            level_extras = [eid for eid in lvl.extras if catalog.extra(eid).status == "shipped"]
            if not level_extras:
                continue
        lines.append(f"│  ── Level {lvl.level}: {lvl.name} ──")
        for extra_id in level_extras:
            try:
                e = catalog.extra(extra_id)
            except KeyError:
                continue
            tech = e.tech.today if e.tech and e.tech.today else "—"
            status = _STATUS_BADGE.get(e.status, e.status)
            lines.append(f"│   ├── {e.id}/  [{e.id}] → {tech}  {status}")
            lines.append(f"│   │       {e.role}")
        lines.append("│")

    lines.append("```")
    return "\n".join(lines)


def render_all(
    catalog: ArchitectureCatalog,
    *,
    shipped_only: bool = False,
) -> dict[str, str]:
    """Return a dict of marker-id → rendered content, ready to splice into README.

    Block IDs:

      * ``levels``   — overview table (one row per level)
      * ``level0``   — primitives section
      * ``level1``   — role facades section
      * ``level2``   — substrate facades section
      * ``level3``   — bootstraps section
      * ``matrix``   — pick-and-choose use-case matrix
      * ``tree``     — ASCII filesystem tree

    ``shipped_only=True`` makes the per-level + tree blocks render
    only ``status == "shipped"`` extras. The catalog JSON is unchanged;
    this is purely a rendering view for consumer-facing docs that
    shouldn't advertise unshipped work.
    """
    return {
        "levels": render_levels_table(catalog),
        "level0": render_level_group(catalog, 0, shipped_only=shipped_only),
        "level1": render_level_group(catalog, 1, shipped_only=shipped_only),
        "level2": render_level_group(catalog, 2, shipped_only=shipped_only),
        "level3": render_level_group(catalog, 3, shipped_only=shipped_only),
        "matrix": render_pick_and_choose_matrix(catalog),
        "tree": render_filesystem_tree(catalog, shipped_only=shipped_only),
    }


__all__ = [
    "render_all",
    "render_dependency_grid",
    "render_extras_grid",
    "render_filesystem_tree",
    "render_level_cards",
    "render_level_group",
    "render_levels_table",
    "render_pick_and_choose_matrix",
]
