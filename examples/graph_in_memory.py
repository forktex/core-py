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

"""``[graph]`` standalone — build a multi-edge typed graph in memory.

Builds a tiny graph of people + accounts with two edge kinds, runs the
shipped graph algebra, and round-trips through deterministic JSON. No
infra dependencies — pure Python. Run with ``python examples/graph_in_memory.py``.
"""

from __future__ import annotations

from forktex_core.graph import (
    Graph,
    GraphNode,
    bfs,
    cycles,
    induced_subgraph,
    shortest_path,
    transitive_closure,
)
from forktex_core.graph.models import GraphMeta


def main() -> None:
    # ── 1. Build the demo graph ───────────────────────────────────────
    #
    #   alice ──knows──▶ bob
    #     │                │
    #     │ owns           │ owns
    #     ▼                ▼
    #   acme ──parent_of──▶ acme_eu
    g = Graph.empty(GraphMeta(name="demo"))
    for nid, kind in [
        ("alice", "person"),
        ("bob", "person"),
        ("acme", "account"),
        ("acme_eu", "account"),
    ]:
        g.add_node(GraphNode(id=nid, kind=kind, name=nid.title()))
    g.add_edge("knows", "alice", "bob", {"since": 2018})
    g.add_edge("owns", "alice", "acme")
    g.add_edge("owns", "bob", "acme_eu")
    g.add_edge("parent_of", "acme", "acme_eu")

    print(f"nodes: {len(g.nodes)}")
    print(f"edges: {len(g.edges)}")

    # ── 2. Exercise the graph algebra ─────────────────────────────────
    print(f"closure_from_alice:    {sorted(transitive_closure(g, 'alice'))}")
    print(f"alice → acme_eu path:  {shortest_path(g, 'alice', 'acme_eu')}")
    print(f"bfs_order_from_alice:  {bfs(g, 'alice')}")
    print(f"has_cycles:            {bool(cycles(g))}")

    # ── 3. Induced subgraph (drop bob) ────────────────────────────────
    sub = induced_subgraph(g, ["alice", "acme", "acme_eu"])
    print(f"subgraph nodes:        {len(sub.nodes)}")

    # ── 4. Deterministic JSON round-trip ──────────────────────────────
    parsed = Graph.model_validate_json(g.sorted().model_dump_json())
    assert {n.id for n in g.nodes} == {n.id for n in parsed.nodes}
    assert {e.id for e in g.edges} == {e.id for e in parsed.edges}
    print("json round-trip: ok")


if __name__ == "__main__":
    main()
