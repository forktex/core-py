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

"""Subgraph extraction + JSON round-trip tests."""

from __future__ import annotations

from forktex_core.graph import Graph, GraphNode, induced_subgraph, subgraph_around
from forktex_core.graph.models import GraphMeta


def _line_graph(length: int = 5) -> Graph:
    g = Graph.empty(GraphMeta(name="line"))
    for i in range(length):
        g.add_node(GraphNode(id=f"n{i}", kind="n"))
    for i in range(length - 1):
        g.add_edge("k", f"n{i}", f"n{i + 1}")
    return g


def test_induced_subgraph_keeps_only_listed_nodes():
    g = _line_graph(5)
    sub = induced_subgraph(g, ["n1", "n2", "n3"])
    assert {n.id for n in sub.nodes} == {"n1", "n2", "n3"}
    # Edges with one endpoint outside the set are dropped.
    assert {(e.src_id, e.dst_id) for e in sub.edges} == {("n1", "n2"), ("n2", "n3")}


def test_induced_subgraph_ignores_unknown_ids():
    g = _line_graph(3)
    sub = induced_subgraph(g, ["n0", "ghost"])
    assert {n.id for n in sub.nodes} == {"n0"}
    assert sub.edges == []


def test_induced_subgraph_is_independent_of_source():
    """Mutating a node's attrs on the subgraph must not affect the source
    — the docstring promises "its own private adjacency index" and that
    "mutations don't affect the source"."""
    g = _line_graph(3)
    g.node("n0").attrs["count"] = 1

    sub = induced_subgraph(g, ["n0"])
    sub.node("n0").attrs["count"] = 999

    assert g.node("n0").attrs["count"] == 1


def test_subgraph_around_max_depth_zero_returns_only_start():
    g = _line_graph(5)
    sub = subgraph_around(g, "n2", max_depth=0)
    assert {n.id for n in sub.nodes} == {"n2"}
    assert sub.edges == []


def test_subgraph_around_grows_radius():
    g = _line_graph(5)
    sub = subgraph_around(g, "n2", max_depth=1)
    # Both directions: n1, n2, n3.
    assert {n.id for n in sub.nodes} == {"n1", "n2", "n3"}


def test_subgraph_around_directional():
    g = _line_graph(5)
    fwd = subgraph_around(g, "n2", max_depth=2, direction="out")
    assert {n.id for n in fwd.nodes} == {"n2", "n3", "n4"}
    back = subgraph_around(g, "n2", max_depth=2, direction="in")
    assert {n.id for n in back.nodes} == {"n0", "n1", "n2"}


def test_subgraph_around_unknown_start_returns_empty():
    g = _line_graph(3)
    sub = subgraph_around(g, "ghost", max_depth=2)
    assert sub.nodes == []
    assert sub.edges == []


def test_json_round_trip_preserves_topology():
    """Build → serialise → parse → identical topology + lookups work."""
    g = _line_graph(4)
    g.add_edge("other", "n1", "n3", {"weight": 0.5})

    payload = g.sorted().model_dump_json()
    g2 = Graph.model_validate_json(payload)

    assert {n.id for n in g.nodes} == {n.id for n in g2.nodes}
    assert {e.id for e in g.edges} == {e.id for e in g2.edges}
    # Adjacency works after parse (lazy index).
    assert [n.id for n in g2.neighbors("n1")] == sorted([n.id for n in g.neighbors("n1")])
