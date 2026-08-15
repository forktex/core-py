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

"""Pydantic schema for the architecture catalog.

The catalog describes ``forktex_core``'s four-level architecture:

  * **Level 0** — primitives. Zero-infra, always pulled (``log``, ``error``,
    ``types``, ``iso``).
  * **Level 1** — role facades. Role-named, each over one infra service
    (``database``, ``cache``, ``queue``, ``vector``, ``storage``, ``store``,
    ``vault``, ``graph``). The role name allows tech to swap without naming churn.
  * **Level 2** — substrate facades. User-facing pillars (``grid``, ``space``,
    ``flow``). Compose level-0 + level-1.
  * **Level 3** — bootstraps. Process-level wiring (``api``, ``worker``).

A planned Level-1 ``tech_adapters`` layer (raw postgres/redis/qdrant/minio/mongo
clients beneath the role facades) was investigated and retired without being
built — see ``docs/ARCHITECTURE.md`` for the rationale. Role facades hold their
own infra clients directly; there is no adapter layer beneath them.

The schema enforces internal consistency (no orphan relations, levels match
what each extra declares, depends_on graph is acyclic). See ``loader.py`` for
the cross-validation that runs on load.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExtraKind = Literal["primitive", "facade", "bootstrap"]
"""How an extra slots into the architecture.

  * ``primitive``  — level 0; zero-dep utility (log, error, types).
  * ``facade``     — level 1 (role facades) or level 2 (substrate facades);
                     composes lower levels into a role- or user-facing pillar.
  * ``bootstrap``  — level 3; runtime wiring for a process (api, worker).
"""


RelationKind = Literal["depends_on", "lazy_imports", "consumes"]
"""Edge type in the catalog graph.

  * ``depends_on`` — hard dependency declared in pyproject extras.
  * ``lazy_imports``— soft dependency imported inside method bodies; only
                      pulled when a specific feature (e.g. VECTOR field) is used.
  * ``consumes``    — optional integration; the consumer wires it explicitly
                      (e.g. ``[api]`` mounts ``[grid]``'s router if installed).
"""


Status = Literal["shipped", "in_progress", "planned"]
"""Lifecycle of an extra.

  * ``shipped``    — code present in the current release.
  * ``in_progress``— actively being built in the current phase.
  * ``planned``    — declared in the catalog; deferred to a later phase.
"""


class TechBacking(BaseModel):
    """Which technology backs an extra today, and what alternatives may swap in."""

    today: str | None = None
    """Current tech, e.g. ``"postgres"``. ``None`` for pure-Python extras."""

    alternatives_future: list[str] = Field(default_factory=list)
    """Possible swaps, e.g. ``["qdrant→[pgvector]"]``."""

    infra_required: str | None = None
    """The infra service this requires running. ``None`` for in-memory."""

    model_config = ConfigDict(frozen=True)


class ExtraSpec(BaseModel):
    """One catalog entry — a single extra with its role, deps, and status."""

    id: str
    """The extra's identifier, e.g. ``"grid"`` or ``"database"``. Lowercase, snake_case."""

    level: Literal[0, 1, 2, 3]
    """Architecture level — 0 (primitive), 1 (role facade), 2 (substrate facade),
    3 (bootstrap)."""

    kind: ExtraKind
    """Kind tag matching the level."""

    label: str
    """Human-readable name for the README, e.g. ``"Vector Store"``."""

    role: str
    """One-line role description, the *what*."""

    tech: TechBacking | None = None
    """Tech binding — set when an extra wraps one specific infra technology
    (e.g. Qdrant for ``vector``). ``None`` for pure-Python extras (``graph``)."""

    depends_on: list[str] = Field(default_factory=list)
    """Other extras this one mandates. Listed in pyproject extras."""

    lazy_imports: list[str] = Field(default_factory=list)
    """Other extras imported inside method bodies. Pulled only on feature use."""

    optional_for_consumer: list[str] = Field(default_factory=list)
    """Extras a consumer may add to enable additional surface (e.g. ``[api]``
    works alone but pairs with ``[grid]`` to mount the CRUD router)."""

    exports: list[str] = Field(default_factory=list)
    """Public symbols re-exported from the extra's ``__init__.py``."""

    status: Status = "planned"
    """Lifecycle. Updated phase-by-phase as the plan executes."""

    phase: int | None = None
    """Phase number that introduces or completes this extra. ``None`` if shipped."""

    model_config = ConfigDict(frozen=True)


class Relation(BaseModel):
    """A typed edge between two extras in the catalog graph."""

    kind: RelationKind
    src: str
    dst: str
    note: str | None = None

    model_config = ConfigDict(frozen=True)


class Level(BaseModel):
    """One of the four architecture levels."""

    level: Literal[0, 1, 2, 3]
    name: Literal["primitives", "role_facades", "substrate_facades", "bootstraps"]
    description: str
    extras: list[str]

    model_config = ConfigDict(frozen=True)


class CatalogPresentation(BaseModel):
    """Hints for rendering the catalog as Markdown / HTML / web UI."""

    level_colors: dict[str, str]
    role_icons: dict[str, str] = Field(default_factory=dict)
    colors: dict[str, str] = Field(default_factory=dict)
    """Per-extra hex color (without ``#``) for badge rendering. Falls back to
    a level-based default if not specified."""

    model_config = ConfigDict(frozen=True)


class ArchitectureCatalog(BaseModel):
    """The full catalog. Validated on load (see loader.py).

    The catalog is unversioned by design: it always reflects the current
    architectural target. Code-base history serves as the version history;
    breaking changes are expected as the architecture matures.
    """

    levels: list[Level]
    extras: list[ExtraSpec]
    relations: list[Relation]
    presentation: CatalogPresentation

    model_config = ConfigDict(frozen=True)

    def extra(self, id: str) -> ExtraSpec:
        """Look up an extra by id. Raises ``KeyError`` if not found."""
        for e in self.extras:
            if e.id == id:
                return e
        raise KeyError(f"No extra with id {id!r} in catalog")

    def extras_at_level(self, level: int) -> list[ExtraSpec]:
        """All extras at the given level, in declaration order."""
        return [e for e in self.extras if e.level == level]

    def level(self, level: int) -> Level:
        """Look up a level descriptor by level number."""
        for lvl in self.levels:
            if lvl.level == level:
                return lvl
        raise KeyError(f"No level {level} in catalog")

    def relations_from(self, extra_id: str) -> list[Relation]:
        """All relations originating at the given extra."""
        return [r for r in self.relations if r.src == extra_id]

    def relations_to(self, extra_id: str) -> list[Relation]:
        """All relations targeting the given extra."""
        return [r for r in self.relations if r.dst == extra_id]


__all__ = [
    "ArchitectureCatalog",
    "CatalogPresentation",
    "ExtraKind",
    "ExtraSpec",
    "Level",
    "Relation",
    "RelationKind",
    "Status",
    "TechBacking",
]
