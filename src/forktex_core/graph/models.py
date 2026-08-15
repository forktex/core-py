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

"""Pydantic models for the ``[graph]`` extra.

The wire format is the Pydantic envelope (``Graph(meta, nodes, edges)``).
Adjacency indices are private and rebuilt lazily on first lookup —
they're never serialised. Mutation (``add_node`` / ``add_edge``) keeps
them in sync incrementally; freshly-parsed graphs trigger a one-shot
rebuild on the first read access.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from forktex_core.error import AppErrorCode, BadRequestError, NotFoundError


class NodeNotFoundError(NotFoundError, KeyError):
    """An edge referenced a node id the graph does not contain.

    A ``NotFoundError`` so a graph miss carries the same contract as any other
    missing resource; ``KeyError`` stays in the bases because these are raised
    from id lookups and callers catch it as such.
    """

    code = AppErrorCode.NOT_FOUND


class InvalidDirectionError(BadRequestError, ValueError):
    """``neighbors(direction=…)`` was given something other than out/in/both.

    ``ValueError`` stays in the bases for the same reason as
    :class:`NodeNotFoundError`'s ``KeyError``: it is what callers already catch.
    """

    code = AppErrorCode.BAD_REQUEST


# Generic str — consumers bring their own vocabulary. forktex-py[graph]
# narrows these to FSD-specific Literals; here in core they stay open.
NodeKind = str
EdgeKind = str


class GraphMeta(BaseModel):
    """Wire-level metadata. Optional but useful for round-trip provenance."""

    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    generated_at: str | None = None
    schema_version: int = 1


class GraphNode(BaseModel):
    """A typed graph node. ``id`` must be unique within a Graph."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: NodeKind
    name: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A typed graph edge.

    ``id`` is computed from ``(kind, src_id, dst_id, attrs)`` so any two
    edges with the same shape collapse, while edges that differ in kind
    or attributes coexist between the same node pair (multi-edge).
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: EdgeKind
    src_id: str
    dst_id: str
    attrs: dict[str, Any] = Field(default_factory=dict)


def edge_id(kind: str, src_id: str, dst_id: str, attrs: dict[str, Any] | None = None) -> str:
    """Deterministic edge id including a short hash of attrs.

    Format: ``"<kind>:<src>-><dst>:<8hex>"``. Stable across runs given
    the same inputs (JSON-canonicalised attrs + sorted keys).
    """
    payload = json.dumps(attrs or {}, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.blake2s(payload, digest_size=4).hexdigest()
    return f"{kind}:{src_id}->{dst_id}:{digest}"


class Graph(BaseModel):
    """Typed multi-edge graph with deterministic serialisation.

    Adjacency indices (``_by_id`` / ``_out`` / ``_in``) are private and
    excluded from serialisation. Lookup methods rebuild them lazily on
    first access; mutation methods keep them in sync.
    """

    model_config = ConfigDict(extra="ignore")

    meta: GraphMeta = Field(default_factory=GraphMeta)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)

    _by_id: dict[str, GraphNode] = PrivateAttr(default_factory=dict)
    _out: dict[str, list[GraphEdge]] = PrivateAttr(default_factory=dict)
    _in: dict[str, list[GraphEdge]] = PrivateAttr(default_factory=dict)
    _edge_ids: set[str] = PrivateAttr(default_factory=set)
    _indexed: bool = PrivateAttr(default=False)

    def add_node(self, node: GraphNode) -> GraphNode:
        """Idempotent on ``node.id``. Returns the existing node if one
        with the same id already exists, otherwise the inserted one."""
        self._ensure_index()
        if node.id in self._by_id:
            return self._by_id[node.id]
        self.nodes.append(node)
        self._by_id[node.id] = node
        self._out.setdefault(node.id, [])
        self._in.setdefault(node.id, [])
        return node

    def add_edge(
        self,
        kind: EdgeKind,
        src_id: str,
        dst_id: str,
        attrs: dict[str, Any] | None = None,
    ) -> GraphEdge:
        """Idempotent on the deterministic edge id. Both endpoints must
        already exist as nodes — a missing endpoint raises ``KeyError``."""
        self._ensure_index()
        if src_id not in self._by_id:
            raise NodeNotFoundError(f"unknown src_id: {src_id!r}")
        if dst_id not in self._by_id:
            raise NodeNotFoundError(f"unknown dst_id: {dst_id!r}")
        attrs = attrs or {}
        eid = edge_id(kind, src_id, dst_id, attrs)
        if eid in self._edge_ids:
            return next(e for e in self.edges if e.id == eid)
        edge = GraphEdge(id=eid, kind=kind, src_id=src_id, dst_id=dst_id, attrs=attrs)
        self.edges.append(edge)
        self._edge_ids.add(eid)
        self._out.setdefault(src_id, []).append(edge)
        self._in.setdefault(dst_id, []).append(edge)
        return edge

    def _ensure_index(self) -> None:
        if self._indexed:
            return
        self._by_id.clear()
        self._out.clear()
        self._in.clear()
        self._edge_ids.clear()
        for n in self.nodes:
            self._by_id[n.id] = n
            self._out.setdefault(n.id, [])
            self._in.setdefault(n.id, [])
        for e in self.edges:
            self._edge_ids.add(e.id)
            self._out.setdefault(e.src_id, []).append(e)
            self._in.setdefault(e.dst_id, []).append(e)
        self._indexed = True

    def node(self, node_id: str) -> GraphNode | None:
        self._ensure_index()
        return self._by_id.get(node_id)

    def has_node(self, node_id: str) -> bool:
        self._ensure_index()
        return node_id in self._by_id

    def out_edges(self, node_id: str, *, kind: EdgeKind | None = None) -> list[GraphEdge]:
        self._ensure_index()
        edges = self._out.get(node_id, [])
        return [e for e in edges if kind is None or e.kind == kind]

    def in_edges(self, node_id: str, *, kind: EdgeKind | None = None) -> list[GraphEdge]:
        self._ensure_index()
        edges = self._in.get(node_id, [])
        return [e for e in edges if kind is None or e.kind == kind]

    def neighbors(
        self,
        node_id: str,
        *,
        kind: EdgeKind | None = None,
        direction: str = "out",
    ) -> list[GraphNode]:
        """Adjacent nodes via ``direction`` ∈ ``{"out", "in", "both"}``."""
        self._ensure_index()
        if direction == "out":
            edges = self.out_edges(node_id, kind=kind)
            return [self._by_id[e.dst_id] for e in edges if e.dst_id in self._by_id]
        if direction == "in":
            edges = self.in_edges(node_id, kind=kind)
            return [self._by_id[e.src_id] for e in edges if e.src_id in self._by_id]
        if direction == "both":
            seen: dict[str, GraphNode] = {}
            for e in self.out_edges(node_id, kind=kind):
                if e.dst_id in self._by_id:
                    seen.setdefault(e.dst_id, self._by_id[e.dst_id])
            for e in self.in_edges(node_id, kind=kind):
                if e.src_id in self._by_id:
                    seen.setdefault(e.src_id, self._by_id[e.src_id])
            return list(seen.values())
        raise InvalidDirectionError(f"direction must be 'out' | 'in' | 'both', got {direction!r}")

    def by_kind(self, kind: NodeKind) -> list[GraphNode]:
        return [n for n in self.nodes if n.kind == kind]

    def edges_by_kind(self, kind: EdgeKind) -> list[GraphEdge]:
        return [e for e in self.edges if e.kind == kind]

    def sorted(self) -> Graph:
        """Return a new Graph with deterministically sorted nodes + edges.

        Use before writing to disk so the file is byte-stable across runs
        (same input → same output → no diff churn). Nodes/edges are deep
        copies — mutating the result's ``attrs`` never affects ``self``
        (Pydantic doesn't copy already-validated model instances by
        default, so without this, "sorted" would silently alias the
        originals).
        """

        def _edge_key(e: GraphEdge) -> tuple[str, str, str, str]:
            return (e.kind, e.src_id, e.dst_id, e.id)

        sorted_nodes = [n.model_copy(deep=True) for n in sorted(self.nodes, key=lambda n: n.id)]
        sorted_edges = [e.model_copy(deep=True) for e in sorted(self.edges, key=_edge_key)]
        return Graph(meta=self.meta, nodes=sorted_nodes, edges=sorted_edges)

    def merge(self, other: Graph) -> Graph:
        """Merge ``other`` into self (mutating self). Self's meta is kept.

        Nodes/edges are deep-copied in — self and ``other`` never end up
        sharing a mutable ``attrs`` dict after this call.
        """
        for n in other.nodes:
            self.add_node(n.model_copy(deep=True))
        for e in other.edges:
            self.add_edge(e.kind, e.src_id, e.dst_id, copy.deepcopy(e.attrs))
        return self

    @classmethod
    def empty(cls, meta: GraphMeta | None = None) -> Graph:
        return cls(meta=meta or GraphMeta(), nodes=[], edges=[])

    @classmethod
    def from_iterables(
        cls,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        *,
        meta: GraphMeta | None = None,
    ) -> Graph:
        g = cls.empty(meta)
        for n in nodes:
            g.add_node(n)
        for e in edges:
            g.add_edge(e.kind, e.src_id, e.dst_id, e.attrs)
        return g


__all__ = [
    "EdgeKind",
    "Graph",
    "GraphEdge",
    "GraphMeta",
    "GraphNode",
    "NodeKind",
    "edge_id",
]
