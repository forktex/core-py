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

"""WorkflowDefinition — the internal model all decorators compile to."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from forktex_core.types import BaseValueObject

# Named type aliases for callable roles in workflow definitions
# NodeFn represents: async def fn(ctx: Ctx, state: dict[str, Any]) -> dict[str, Any]
NodeFn = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
RouterFn = Callable[[dict[str, Any]], str]
WhenFn = Callable[[dict[str, Any]], bool]
ReducerFn = Callable[[Any, Any], Any]


START = "__START__"
END = "__END__"


class NodeDef(BaseValueObject):
    """One node in the workflow graph.

    ``when_fn`` is evaluated against the current state dict before
    the node is dispatched; None means "always execute".
    """

    name: str
    fn: NodeFn
    max_attempts: int
    backoff: tuple[float, ...]
    when_fn: WhenFn | None = None


class StepTemplateDef(BaseValueObject):
    """A named platform step available to namespace-track definitions.

    Platform-track workflows expose a library of reusable steps that
    namespace-track definitions (tenants) can reference by name without
    having access to the underlying implementation.
    """

    name: str
    fn: NodeFn
    max_attempts: int
    backoff: tuple[float, ...]


class DirectEdge(BaseValueObject):
    """Unconditional transition to a single target node."""

    to_node: str


class ConditionalEdge(BaseValueObject):
    """Router-based branching.

    ``router_fn`` receives the current state dict and returns a key;
    ``mapping`` translates that key to a target node name.  The
    special sentinel ``END`` is a valid target value.
    """

    router_fn: RouterFn
    mapping: dict[str, str]  # routing key → target node name


class WaitEdge(BaseValueObject):
    """Edge that suspends the workflow until a named signal arrives."""

    to_node: str
    event_name: str  # signal name to wait for before transitioning


Edge = DirectEdge | ConditionalEdge | WaitEdge


class WorkflowDefinition(BaseModel):
    """Complete, validated description of a workflow.

    All decorators compile to this structure; the executor reads only
    from WorkflowDefinition, keeping concerns cleanly separated.

    Attributes:
        name:        Workflow identifier (must be unique within a namespace).
        version:     Integer version; bumped when the logic changes.
        namespace:   None = platform-track; str = tenant/namespace-track.
        state_cls:   The TypedDict class that defines the state schema.
                     None is allowed for namespace-track definitions that
                     inherit a platform-defined schema.
        nodes:       node_name → NodeDef mapping.
        edges:       from_node → list of Edge; START and END are valid keys.
        reducers:    field_name → merge function (populated from Annotated hints
                     on state_cls by state._extract_reducers).
        schedule:    Cron expression or None for on-demand workflows.
    """

    name: str
    version: int
    namespace: str | None
    state_cls: type | None
    nodes: dict[str, NodeDef] = Field(default_factory=dict)
    edges: dict[str, list[Edge]] = Field(default_factory=dict)
    reducers: dict[str, ReducerFn] = Field(default_factory=dict)
    schedule: str | None = None

    def validate(self) -> None:
        """Validate topology: all edge targets exist, no dead-ends except END.

        Raises ValueError with a descriptive message if the graph is
        structurally invalid.  Call this after all nodes and edges have
        been registered (typically at the end of the decorator).
        """
        known_nodes: set[str] = set(self.nodes.keys()) | {START, END}

        for from_node, edge_list in self.edges.items():
            # from_node must be known (START is always valid as source).
            if from_node not in known_nodes:
                raise ValueError(f"workflow {self.name!r} v{self.version}: edge from unknown node {from_node!r}")
            for edge in edge_list:
                targets = _edge_targets(edge)
                for target in targets:
                    if target not in known_nodes:
                        raise ValueError(
                            f"workflow {self.name!r} v{self.version}: "
                            f"edge from {from_node!r} targets unknown node {target!r}"
                        )

        # Every defined node (except END) must have at least one outgoing edge,
        # OR be reachable only as a terminal (i.e. the definition is intentionally
        # a dead-end before END).  We flag nodes that have no outgoing edge and
        # are NOT END — these are implicit dead-ends which are almost always bugs.
        for node_name in self.nodes:
            if node_name == END:
                continue
            if not self.edges.get(node_name):
                raise ValueError(
                    f"workflow {self.name!r} v{self.version}: "
                    f"node {node_name!r} has no outgoing edges (dead-end that is not END)"
                )

    def entry_node(self) -> str:
        """Return the first node reachable from START.

        START must have exactly one DirectEdge as its first (and
        typically only) outgoing edge.
        """
        edges_from_start = self.edges.get(START, [])
        if not edges_from_start:
            raise ValueError(f"workflow {self.name!r}: no edge from START")
        first = edges_from_start[0]
        if isinstance(first, DirectEdge):
            return first.to_node
        raise ValueError(f"workflow {self.name!r}: START edge must be DirectEdge, got {type(first).__name__}")

    def all_reachable_nodes(self) -> set[str]:
        """BFS from START; returns all node names reachable in the graph."""
        visited: set[str] = set()
        queue = [START]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for edge in self.edges.get(current, []):
                for target in _edge_targets(edge):
                    if target not in visited:
                        queue.append(target)
        return visited - {START}


__all__ = [
    "END",
    "START",
    "ConditionalEdge",
    "DirectEdge",
    "Edge",
    "NodeDef",
    "NodeFn",
    "ReducerFn",
    "RouterFn",
    "StepTemplateDef",
    "WaitEdge",
    "WhenFn",
    "WorkflowDefinition",
]


def _edge_targets(edge: Edge) -> list[str]:
    """Return all possible target node names for an edge."""
    if isinstance(edge, DirectEdge):
        return [edge.to_node]
    if isinstance(edge, ConditionalEdge):
        return list(edge.mapping.values())
    if isinstance(edge, WaitEdge):
        return [edge.to_node]
    # Exhaustive match; should never reach here if Edge union is complete.
    raise TypeError(f"Unknown edge type: {type(edge)!r}")
