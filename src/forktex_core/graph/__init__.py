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

"""Level-1 ``[graph]`` extra — pure-Python multi-edge typed-graph algebra.

A generic in-memory graph primitive consumers compose into bigger
substrates. ``GraphNode`` / ``GraphEdge`` carry a free-form ``kind: str``
so each consumer brings its own vocabulary (forktex-py[graph] uses
``project_root``/``module``/``contains``; an intelligence consumer can
use ``person``/``account``/``works_at``). Multi-edge by construction:
edges with the same kind, endpoints, and attrs collapse; differ on any
of those three and they coexist between the same pair of nodes.

What ships:
  - ``GraphNode`` / ``GraphEdge`` / ``Graph`` Pydantic models.
  - Deterministic edge IDs via blake2s over (kind, src, dst, attrs).
  - O(1) lazy adjacency (out_edges / in_edges / neighbors).
  - Algorithms: BFS, DFS, transitive closure, shortest path, cycle detection.
  - Subgraph extract / induce.
  - Deterministic JSON serialisation (sorted), parse round-trip.

What does NOT ship:
  - Persistence. Use ``forktex_core.grid`` (level 3 [grid]) for that.
  - Cypher / SPARQL / DSL parsing. Out of scope.
  - Backends. Tomorrow Neo4j/dgraph adapters land as peer extras; today
    this is in-memory only. The user-facing API stays stable.
"""

from forktex_core.graph.algebra import (
    bfs,
    cycles,
    dfs,
    shortest_path,
    transitive_closure,
)
from forktex_core.graph.models import (
    EdgeKind,
    Graph,
    GraphEdge,
    GraphNode,
    InvalidDirectionError,
    NodeKind,
    NodeNotFoundError,
    edge_id,
)
from forktex_core.graph.subgraph import induced_subgraph, subgraph_around

__all__ = [
    "EdgeKind",
    "Graph",
    "GraphEdge",
    "GraphNode",
    "InvalidDirectionError",
    "NodeKind",
    "NodeNotFoundError",
    "bfs",
    "cycles",
    "dfs",
    "edge_id",
    "induced_subgraph",
    "shortest_path",
    "subgraph_around",
    "transitive_closure",
]
