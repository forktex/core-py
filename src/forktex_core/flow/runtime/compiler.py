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

"""Compiler functions that convert workflow declarations into
:class:`~forktex_core.flow.domain.definition.WorkflowDefinition`.

Four entry points:

- :func:`compile_scheduled` — single async function → one-node workflow
- :func:`compile_pipeline` — class with ``steps`` list → linear chain
- :func:`compile_graph` — class with ``topology`` list → arbitrary DAG
- :func:`compile_config` — JSON-like config dict (namespace-track) → definition

Edge helper factory functions :func:`edge`, :func:`conditional`, and
:func:`wait_edge` are also exported here and consumed by ``@flow.graph``
class bodies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from forktex_core.flow.domain.definition import (
    END,
    START,
    ConditionalEdge,
    DirectEdge,
    Edge,
    NodeDef,
    NodeFn,
    ReducerFn,
    RouterFn,
    WaitEdge,
    WhenFn,
    WorkflowDefinition,
)
from forktex_core.flow.domain.node import (
    ParallelGroup,
    StepSpec,
    _NodeMeta,
    has_step_meta,
    step_meta,
)
from forktex_core.flow.domain.state import _extract_reducers

if TYPE_CHECKING:
    from forktex_core.flow.domain.definition import StepTemplateDef
    from forktex_core.flow.runtime.ctx import Ctx

_DEFAULT_MAX_ATTEMPTS: int = 3
_DEFAULT_BACKOFF: tuple[float, ...] = (30.0, 120.0, 300.0)


def edge(from_node: str, to_node: str) -> tuple[str, str, str]:
    """Declare a direct edge in a graph topology list.

    Usage inside a ``@flow.graph`` class body::

        topology = [
            edge("provision", "configure"),
            edge("configure", "__END__"),
        ]
    """
    return ("edge", from_node, to_node)


def conditional(
    from_node: str, router_fn: RouterFn, mapping: dict[str, str]
) -> tuple[str, str, RouterFn, dict[str, str]]:
    """Declare a conditional (router) edge in a graph topology list.

    ``router_fn`` receives the current state dict and returns a key
    that is looked up in ``mapping`` to resolve the target node name.

    Usage::

        topology = [
            conditional(
                "check_health",
                lambda state: "ok" if state.get("healthy") else "fail",
                {"ok": "deploy", "fail": "rollback"},
            ),
        ]
    """
    return ("conditional", from_node, router_fn, mapping)


def wait_edge(from_node: str, to_node: str, *, on: str) -> tuple[str, str, str, str]:
    """Declare a wait-for-event edge in a graph topology list.

    The runtime suspends at ``from_node`` until the external event
    ``on`` is received (via ``flow.send_signal``), then advances to
    ``to_node``.

    Usage::

        topology = [
            wait_edge("email_pending", "verified", on="email.verified"),
        ]
    """
    return ("wait_edge", from_node, to_node, on)


def _resolve_step(item: NodeFn | StepSpec) -> tuple[NodeFn, _NodeMeta | None, WhenFn | None]:
    """Extract ``(fn, meta, when_fn)`` from a step array item.

    ``item`` may be:
    - a plain callable (``@step``-decorated or bare)
    - a :class:`~forktex_core.flow.domain.node.StepSpec`

    ``meta`` is ``None`` when the function was not ``@step``-decorated.
    """
    if isinstance(item, StepSpec):
        fn = item.fn
        when_fn = item.when
        # PipelineStepSpec may carry explicit retry config; stash it
        # so the caller can override defaults.
        meta: _NodeMeta | None = step_meta(fn)
        if item.max_attempts is not None or item.backoff is not None:
            # Explicit spec values win over decorator metadata.
            meta = _NodeMeta(
                max_attempts=item.max_attempts
                if item.max_attempts is not None
                else (meta.max_attempts if meta else _DEFAULT_MAX_ATTEMPTS),
                backoff=item.backoff if item.backoff is not None else (meta.backoff if meta else _DEFAULT_BACKOFF),
            )
        return fn, meta, when_fn

    # Plain callable.
    fn = item
    meta = step_meta(fn)
    return fn, meta, None


def _node_retry(meta: _NodeMeta | None) -> tuple[int, tuple[float, ...]]:
    """Resolve max_attempts + backoff from node metadata with defaults."""
    if meta is None:
        return _DEFAULT_MAX_ATTEMPTS, _DEFAULT_BACKOFF
    return meta.max_attempts, meta.backoff


def compile_scheduled(
    fn: NodeFn,
    *,
    name: str,
    version: int,
    cron: str,
    state_cls: type | None = None,
) -> WorkflowDefinition:
    """Compile a single async function into a scheduled one-node workflow.

    Topology: ``START → <fn_name> → END``.

    Reads the ``@step`` retry config if present; falls back to
    :data:`_DEFAULT_MAX_ATTEMPTS` / :data:`_DEFAULT_BACKOFF`.
    """
    meta: _NodeMeta | None = step_meta(fn)
    max_attempts, backoff = _node_retry(meta)
    node_name = getattr(fn, "__name__", "run")

    nodes: dict[str, NodeDef] = {
        node_name: NodeDef(
            name=node_name,
            fn=fn,
            max_attempts=max_attempts,
            backoff=backoff,
        )
    }

    edges: dict[str, list[Edge]] = {
        START: [DirectEdge(to_node=node_name)],
        node_name: [DirectEdge(to_node=END)],
    }

    reducers: dict[str, ReducerFn] = {}
    if state_cls is not None:
        reducers = _extract_reducers(state_cls)

    return WorkflowDefinition(
        name=name,
        version=version,
        namespace=None,
        state_cls=state_cls,
        nodes=nodes,
        edges=edges,
        reducers=reducers,
        schedule=cron,
    )


def compile_pipeline(
    cls: type,
    *,
    name: str,
    version: int,
    cron: str | None = None,
    state_cls: type | None = None,
) -> WorkflowDefinition:
    """Compile a class with a ``steps`` list into a linear workflow.

    Each item in ``cls.steps`` may be:
    - a plain callable
    - a :class:`~forktex_core.flow.domain.node.StepSpec` (with optional
      ``when`` guard and explicit retry config)
    - a :class:`~forktex_core.flow.domain.node.ParallelGroup` (members run
      concurrently via ``asyncio.gather`` in a single synthetic node)

    Topology: ``START → step0 → step1 → … → stepN → END``.

    ``when_fn`` on a step is copied to :attr:`NodeDef.when_fn`; the
    executor skips the node when ``when_fn(state)`` returns ``False``
    without breaking the linear chain.
    """
    steps: list[Any] = list(cls.steps)
    nodes: dict[str, NodeDef] = {}
    node_order: list[str] = []

    for i, item in enumerate(steps):
        if isinstance(item, ParallelGroup):
            # Synthesise a single node whose body gathers all members.
            node_name = f"__parallel_{i}__"
            member_fns: list[NodeFn] = []
            for member in item.members:
                if isinstance(member, StepSpec):
                    member_fns.append(member.fn)
                else:
                    member_fns.append(member)

            # Close over member_fns to avoid late-binding issues in the loop.
            async def _parallel_body(
                ctx: Ctx, state: dict[str, Any], member_fns: list[NodeFn] = member_fns
            ) -> dict[str, Any]:
                results = await asyncio.gather(*[fn(ctx, state) for fn in member_fns])
                merged: dict[str, Any] = {}
                for r in results:
                    if isinstance(r, dict):
                        merged.update(r)
                return merged

            _parallel_body.__name__ = node_name
            _parallel_body.__qualname__ = node_name

            nodes[node_name] = NodeDef(
                name=node_name,
                fn=_parallel_body,
                max_attempts=_DEFAULT_MAX_ATTEMPTS,
                backoff=_DEFAULT_BACKOFF,
            )
            node_order.append(node_name)
            continue

        fn, meta, when_fn = _resolve_step(item)
        max_attempts, backoff = _node_retry(meta)
        node_name = getattr(fn, "__name__", f"step_{i}")
        # If the same function appears multiple times, disambiguate.
        if node_name in nodes:
            node_name = f"{node_name}_{i}"

        nodes[node_name] = NodeDef(
            name=node_name,
            fn=fn,
            max_attempts=max_attempts,
            backoff=backoff,
            when_fn=when_fn,
        )
        node_order.append(node_name)

    # Build linear edges: START → n0 → n1 → … → nN → END
    edges: dict[str, list[Edge]] = {}
    if node_order:
        edges[START] = [DirectEdge(to_node=node_order[0])]
        for j in range(len(node_order) - 1):
            edges[node_order[j]] = [DirectEdge(to_node=node_order[j + 1])]
        edges[node_order[-1]] = [DirectEdge(to_node=END)]
    else:
        edges[START] = [DirectEdge(to_node=END)]

    reducers: dict[str, ReducerFn] = {}
    effective_state_cls = state_cls or getattr(cls, "state_cls", None)
    if effective_state_cls is not None:
        reducers = _extract_reducers(effective_state_cls)

    return WorkflowDefinition(
        name=name,
        version=version,
        namespace=None,
        state_cls=effective_state_cls,
        nodes=nodes,
        edges=edges,
        reducers=reducers,
        schedule=cron,
    )


def compile_graph(
    cls: type,
    *,
    name: str,
    version: int,
    state_cls: type | None = None,
) -> WorkflowDefinition:
    """Compile a class with a ``topology`` list into an arbitrary-DAG workflow.

    The class declares:
    - ``topology`` — list of :func:`edge` / :func:`conditional` /
      :func:`wait_edge` tuples describing the graph shape.
    - ``entry`` (optional) — entry node name; inferred from the first
      non-``START`` target in ``topology`` when absent.
    - ``terminal`` (optional) — terminal node name; inferred from any
      node whose outgoing edges include ``END`` when absent.
    - ``nodes`` (optional dict) — explicit ``{name: callable}`` map.
      When absent, node functions are auto-discovered by scanning class
      attributes for callables carrying ``__forktex_step_meta__``.

    Any string name referenced as a node in edges must resolve to a
    callable (via the ``nodes`` dict or auto-discovery) or raise
    ``ValueError``, with the exception of the sentinel constants
    ``START`` (``"__START__"``) and ``END`` (``"__END__"``).
    """
    topology: list[Any] = list(cls.topology)

    discovered: dict[str, NodeFn] = {}
    for attr_name in dir(cls):
        if attr_name.startswith("__") and not attr_name.startswith("__forktex"):
            continue
        try:
            attr = getattr(cls, attr_name)
        except AttributeError:
            continue
        if callable(attr) and has_step_meta(attr):
            discovered[attr_name] = cast(NodeFn, attr)

    # Explicit ``nodes`` dict wins over auto-discovery; merge with
    # auto-discovered as fallback for names not in the explicit dict.
    explicit_nodes: dict[str, NodeFn] = dict(getattr(cls, "nodes", {}) or {})
    fn_lookup: dict[str, NodeFn] = {**discovered, **explicit_nodes}

    edges: dict[str, list[Edge]] = {}

    for spec in topology:
        kind = spec[0]

        if kind == "edge":
            _, from_node, to_node = spec
            edges.setdefault(from_node, []).append(DirectEdge(to_node=to_node))

        elif kind == "conditional":
            _, from_node, router_fn, mapping = spec
            edges.setdefault(from_node, []).append(ConditionalEdge(router_fn=router_fn, mapping=mapping))

        elif kind == "wait_edge":
            _, from_node, to_node, event_name = spec
            edges.setdefault(from_node, []).append(WaitEdge(to_node=to_node, event_name=event_name))

        else:
            raise ValueError(f"compile_graph: unknown topology entry kind {kind!r}")

    referenced: set[str] = set()
    for from_node, edge_list in edges.items():
        if from_node not in (START, END):
            referenced.add(from_node)
        for e in edge_list:
            if isinstance(e, DirectEdge) and e.to_node not in (START, END):
                referenced.add(e.to_node)
            elif isinstance(e, ConditionalEdge):
                for target in e.mapping.values():
                    if target not in (START, END):
                        referenced.add(target)
            elif isinstance(e, WaitEdge) and e.to_node not in (START, END):
                referenced.add(e.to_node)

    nodes: dict[str, NodeDef] = {}
    for node_name in referenced:
        if node_name not in fn_lookup:
            raise ValueError(
                f"compile_graph({name!r}): node {node_name!r} is referenced in topology "
                "but has no matching function in the class (add it to ``nodes`` dict or "
                "decorate with ``@step`` from forktex_core.flow)."
            )
        fn = fn_lookup[node_name]
        meta: _NodeMeta | None = step_meta(fn)
        max_attempts, backoff = _node_retry(meta)
        nodes[node_name] = NodeDef(
            name=node_name,
            fn=fn,
            max_attempts=max_attempts,
            backoff=backoff,
        )

    reducers: dict[str, ReducerFn] = {}
    effective_state_cls = state_cls or getattr(cls, "state_cls", None)
    if effective_state_cls is not None:
        reducers = _extract_reducers(effective_state_cls)

    return WorkflowDefinition(
        name=name,
        version=version,
        namespace=None,
        state_cls=effective_state_cls,
        nodes=nodes,
        edges=edges,
        reducers=reducers,
        schedule=None,
    )


def _config_when_guard(spec: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """Build a simple field-equality guard from a JSON-safe spec dict.

    ``{"field": "connectivity_ok", "is": True}`` → ``lambda state: state.get("connectivity_ok") == True``

    Only field-equality checks are supported (no lambdas) so that the
    config is safely round-trippable through JSON without code eval.
    """
    field_name: str = spec["field"]
    expected: Any = spec["is"]

    def _guard(state: dict[str, Any]) -> bool:
        return state.get(field_name) == expected

    _guard.__name__ = f"when_{field_name}_is_{expected}"
    return _guard


def compile_config(
    config: dict[str, Any],
    step_template_registry: dict[str, StepTemplateDef],
) -> WorkflowDefinition:
    """Compile a JSON-like config dict into a :class:`WorkflowDefinition`.

    Supported types: ``"pipeline"`` and ``"graph"``.  Namespace is
    intentionally left as ``None`` — the caller sets it after the fact
    so the compiler stays stateless.

    Pipeline config example::

        {
            "type": "pipeline",
            "steps": [
                "network.reroute_traffic",
                {"step": "network.send_alert", "when": {"field": "ok", "is": false}}
            ]
        }

    Graph config example::

        {
            "type": "graph",
            "entry": "detect",
            "topology": [
                {"from": "__START__", "to": "detect"},
                {"from": "detect", "to": "reroute", "on": "peer.down"},
                {"from": "detect", "to": "__END__", "when": {"field": "peer_ok", "is": true}},
                {"from": "reroute", "to": "verify"},
                {"from": "verify", "to": "__END__"}
            ]
        }

    ``when`` specs use only field-equality checks — no lambdas — so
    the config is safe to deserialise from tenant-supplied JSON.
    """
    cfg_type: str = config.get("type", "pipeline")
    name: str = config.get("name", "__unnamed__")
    version: int = int(config.get("version", 1))

    def _resolve_template(step_name: str) -> NodeFn:
        tpl = step_template_registry.get(step_name)
        if tpl is None:
            raise ValueError(f"compile_config: step template {step_name!r} not found in registry")
        # StepTemplateDef is expected to have a ``fn`` attribute.
        return tpl.fn  # type: ignore[attr-defined]

    if cfg_type == "pipeline":
        nodes: dict[str, NodeDef] = {}
        node_order: list[str] = []

        for i, step_spec in enumerate(config.get("steps", [])):
            if isinstance(step_spec, str):
                fn = _resolve_template(step_spec)
                when_fn: Callable[[dict[str, Any]], bool] | None = None
                node_name: str = step_spec.rsplit(".", 1)[-1]
            else:
                fn = _resolve_template(step_spec["step"])
                raw_when = step_spec.get("when")
                when_fn = _config_when_guard(raw_when) if raw_when else None
                node_name = step_spec["step"].rsplit(".", 1)[-1]

            # Disambiguate duplicate names.
            if node_name in nodes:
                node_name = f"{node_name}_{i}"

            meta: _NodeMeta | None = step_meta(fn)
            max_attempts, backoff = _node_retry(meta)

            nodes[node_name] = NodeDef(
                name=node_name,
                fn=fn,
                max_attempts=max_attempts,
                backoff=backoff,
                when_fn=when_fn,
            )
            node_order.append(node_name)

        # Linear edges.
        edges: dict[str, list[Edge]] = {}
        if node_order:
            edges[START] = [DirectEdge(to_node=node_order[0])]
            for j in range(len(node_order) - 1):
                edges[node_order[j]] = [DirectEdge(to_node=node_order[j + 1])]
            edges[node_order[-1]] = [DirectEdge(to_node=END)]
        else:
            edges[START] = [DirectEdge(to_node=END)]

        return WorkflowDefinition(
            name=name,
            version=version,
            namespace=None,
            state_cls=None,
            nodes=nodes,
            edges=edges,
            reducers={},
            schedule=None,
        )

    elif cfg_type == "graph":
        nodes_g: dict[str, NodeDef] = {}
        edges_g: dict[str, list[Edge]] = {}

        for topo_entry in config.get("topology", []):
            from_node: str = topo_entry["from"]
            to_node: str = topo_entry["to"]
            event_name: str | None = topo_entry.get("on")
            raw_when_g: dict[str, Any] | None = topo_entry.get("when")

            if event_name:
                # Wait edge.
                edges_g.setdefault(from_node, []).append(WaitEdge(to_node=to_node, event_name=event_name))
            elif raw_when_g:
                # Conditional expressed as a field-equality check.
                # We use a ConditionalEdge with a single-entry mapping
                # when the predicate is True; otherwise just a direct
                # edge guarded by when_fn is more accurate.  Since the
                # existing edge types don't have a "guarded DirectEdge"
                # primitive, we encode this as a ConditionalEdge whose
                # router returns "yes"/"no" and mapping only has "yes".
                guard_fn = _config_when_guard(raw_when_g)

                def _router(
                    state: dict[str, Any], _gf: Callable[[dict[str, Any]], bool] = guard_fn, _target: str = to_node
                ) -> str:
                    return "yes" if _gf(state) else "skip"

                edges_g.setdefault(from_node, []).append(
                    ConditionalEdge(
                        router_fn=_router,
                        mapping={"yes": to_node, "skip": END},
                    )
                )
            else:
                edges_g.setdefault(from_node, []).append(DirectEdge(to_node=to_node))

        # Collect node names to build NodeDefs.
        referenced_g: set[str] = set()
        for from_node, edge_list in edges_g.items():
            if from_node not in (START, END):
                referenced_g.add(from_node)
            for e in edge_list:
                if isinstance(e, DirectEdge) and e.to_node not in (START, END):
                    referenced_g.add(e.to_node)
                elif isinstance(e, ConditionalEdge):
                    for t in e.mapping.values():
                        if t not in (START, END):
                            referenced_g.add(t)
                elif isinstance(e, WaitEdge) and e.to_node not in (START, END):
                    referenced_g.add(e.to_node)

        for node_name_g in referenced_g:
            fn_g = _resolve_template(node_name_g)
            meta_g: _NodeMeta | None = step_meta(fn_g)
            max_attempts_g, backoff_g = _node_retry(meta_g)
            nodes_g[node_name_g] = NodeDef(
                name=node_name_g,
                fn=fn_g,
                max_attempts=max_attempts_g,
                backoff=backoff_g,
            )

        return WorkflowDefinition(
            name=name,
            version=version,
            namespace=None,
            state_cls=None,
            nodes=nodes_g,
            edges=edges_g,
            reducers={},
            schedule=None,
        )

    else:
        raise ValueError(f"compile_config: unknown workflow type {cfg_type!r}; expected 'pipeline' or 'graph'")


__all__ = [
    "compile_config",
    "compile_graph",
    "compile_pipeline",
    "compile_scheduled",
    "conditional",
    "edge",
    "wait_edge",
]
