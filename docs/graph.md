# `forktex_core.graph` — In-memory typed-graph algebra

> Pure-Python, multi-edge typed-graph model plus BFS/DFS/closure/shortest-path/cycle algorithms — no infra dependency, no persistence, no backend.

## Overview

`graph` is a generic in-memory graph primitive consumers compose into bigger substrates. Unlike the other six Level-2 facades, it wraps no external infra — no Redis, no Postgres, no S3. `GraphNode`/`GraphEdge` carry a free-form `kind: str` so each consumer brings its own vocabulary (`forktex-py[graph]` uses `module`/`contains`; an intelligence consumer might use `person`/`account`/`works_at`).

Multi-edge by construction: edges with the same `(kind, src_id, dst_id, attrs)` collapse into one; differ on any of those three and they coexist between the same pair of nodes.

What ships: `Graph`/`GraphNode`/`GraphEdge`/`GraphMeta` Pydantic models, deterministic edge IDs (blake2s over `kind:src->dst:attrs`), lazy adjacency indices, BFS/DFS/transitive-closure/shortest-path/cycle-detection algorithms, subgraph extraction, deterministic (sorted) JSON round-trip.

What does **not** ship: persistence (use `forktex_core.grid` — see `docs/space.md`'s `Bundle.to_graph()` for the bridge), Cypher/SPARQL/DSL parsing, and any non-in-memory backend.

```bash
poetry add forktex-core   # always available — no [graph] extra to opt into
```

## Quick start

```python
from forktex_core.graph import Graph, GraphNode, transitive_closure, shortest_path

g = Graph.empty()
for nid in ("a", "b", "c"):
    g.add_node(GraphNode(id=nid, kind="n"))
g.add_edge("k", "a", "b")
g.add_edge("k", "b", "c")

assert transitive_closure(g, "a") == {"a", "b", "c"}
assert shortest_path(g, "a", "c") == ["a", "b", "c"]

# Round-trip stable JSON
payload = g.sorted().model_dump_json()
g2 = Graph.model_validate_json(payload)
```

## Errors

```python
class NodeNotFoundError(NotFoundError, KeyError)      # an edge names a missing node
class InvalidDirectionError(BadRequestError, ValueError)  # neighbors(direction=...)
```

Both are `AppError` subclasses, so an HTTP boundary renders them with a real
status instead of a masked 500. The plain-Python base (`KeyError` / `ValueError`)
stays in the bases because it is what callers already catch.

## API reference

```python
class GraphMeta(BaseModel):
    name: str | None = None
    generated_at: str | None = None
    schema_version: int = 1

class GraphNode(BaseModel):
    id: str; kind: str; name: str | None = None
    attrs: dict[str, Any] = {}

class GraphEdge(BaseModel):
    id: str          # deterministic — see edge_id()
    kind: str; src_id: str; dst_id: str
    attrs: dict[str, Any] = {}

def edge_id(kind: str, src_id: str, dst_id: str, attrs: dict | None = None) -> str
    # "<kind>:<src>-><dst>:<8hex-blake2s-of-sorted-json-attrs>"

class Graph(BaseModel):
    meta: GraphMeta; nodes: list[GraphNode]; edges: list[GraphEdge]

    def add_node(self, node: GraphNode) -> GraphNode
        # Idempotent on node.id — returns the EXISTING node if id already present
        # (the passed-in `node` argument is discarded silently in that case).
    def add_edge(self, kind, src_id, dst_id, attrs=None) -> GraphEdge
        # Idempotent on the deterministic edge id. NodeNotFoundError if either
        # endpoint isn't already a node in the graph.
    def node(self, node_id: str) -> GraphNode | None
    def has_node(self, node_id: str) -> bool
    def out_edges(self, node_id: str, *, kind: str | None = None) -> list[GraphEdge]
    def in_edges(self, node_id: str, *, kind: str | None = None) -> list[GraphEdge]
    def neighbors(self, node_id, *, kind=None, direction="out") -> list[GraphNode]
        # direction: "out" | "in" | "both" — else raises InvalidDirectionError
    def by_kind(self, kind: str) -> list[GraphNode]
    def edges_by_kind(self, kind: str) -> list[GraphEdge]
    def sorted(self) -> Graph          # new Graph, deterministically ordered, deep-copied
    def merge(self, other: Graph) -> Graph   # mutates self; other's nodes/edges deep-copied in
    @classmethod
    def empty(cls, meta: GraphMeta | None = None) -> Graph
    @classmethod
    def from_iterables(cls, nodes, edges, *, meta=None) -> Graph

# --- Algorithms (forktex_core.graph.algebra) — all accept edge_kind=/direction= ---
def bfs(graph, start_id, *, edge_kind=None, direction="out") -> list[str]
def dfs(graph, start_id, *, edge_kind=None, direction="out") -> list[str]
def transitive_closure(graph, start_id, *, edge_kind=None, direction="out") -> set[str]
def shortest_path(graph, src_id, dst_id, *, edge_kind=None, direction="out") -> list[str] | None
def cycles(graph, *, edge_kind=None) -> list[list[str]]
    # Tarjan SCC — one entry per cyclic component (size > 1) or self-loop (size 1)

# --- Subgraphs (forktex_core.graph.subgraph) ---
def induced_subgraph(graph, node_ids) -> Graph
    # Kept nodes + every edge whose BOTH endpoints are kept. Deep-copied — independent of source.
def subgraph_around(graph, start_id, *, max_depth=1, edge_kind=None, direction="both") -> Graph
    # BFS radius around start_id, then induced_subgraph() on the reached set.
```

## Patterns

### Pattern 1 — Build incrementally, then snapshot for disk

```python
g = Graph.empty(GraphMeta(name="org-chart"))
g.add_node(GraphNode(id="alice", kind="person"))
g.add_node(GraphNode(id="acme", kind="org"))
g.add_edge("works_at", "alice", "acme", {"role": "eng"})

# sorted() gives byte-stable JSON — same content always serialises identically,
# so committed snapshots don't churn on insertion-order noise.
Path("org.json").write_text(g.sorted().model_dump_json())
```

### Pattern 2 — Constrain traversal with `edge_kind` instead of pre-filtering

```python
# ❌ Don't build a filtered copy just to traverse it
filtered = induced_subgraph(g, [n.id for n in g.nodes if ...])
reached = transitive_closure(filtered, "alice")

# ✅ Pass edge_kind — the algorithm filters during traversal, no copy
reached = transitive_closure(g, "alice", edge_kind="works_at")
```

### Pattern 3 — Bounded-radius subgraph for a UI "explore" view

```python
from forktex_core.graph import subgraph_around

# Everyone within 2 hops of alice, following edges in either direction
view = subgraph_around(g, "alice", max_depth=2, direction="both")
```

### Pattern 4 — Detect cycles before treating edges as a DAG

```python
from forktex_core.graph import cycles

found = cycles(g, edge_kind="depends_on")
if found:
    raise ValueError(f"dependency cycle: {found[0]}")
```

## Anti-patterns

```python
# ❌ Assuming add_node(existing_node) updates the stored node's fields
old = g.add_node(GraphNode(id="a", kind="n", name="Old"))
new = g.add_node(GraphNode(id="a", kind="n", name="New"))
assert new.name == "Old"  # add_node is idempotent — the second call is a no-op,
                           # it returns the ORIGINAL node, the "New" one is discarded

# ✅ Mutate attrs on the returned node directly, or add_node once
node = g.add_node(GraphNode(id="a", kind="n", name="Old"))
node.attrs["updated"] = True

# ❌ Assuming a node/edge dict from a subgraph is a live view of the source
sub = induced_subgraph(g, ["a"])
sub.node("a").attrs["x"] = 1   # sorted()/induced_subgraph()/merge() all deep-copy —
assert g.node("a").attrs.get("x") is None   # this is true; source is untouched

# ❌ Passing an unvalidated direction string
g.neighbors("a", direction="outbound")  # raises InvalidDirectionError — must be "out"|"in"|"both"

# ❌ Expecting weighted shortest paths
shortest_path(g, "a", "z")  # every edge counts as 1 — encode weight in `attrs`
                             # and run your own Dijkstra if you need weighted paths
```

---

## Agent guide

### Canonical forms

**Build, query, snapshot:**
```python
g = Graph.empty()
g.add_node(GraphNode(id="a", kind="n"))
g.add_node(GraphNode(id="b", kind="n"))
g.add_edge("k", "a", "b", {"weight": 3})

g.has_node("a")                      # True
g.neighbors("a", direction="out")    # [GraphNode(id="b", ...)]
g.sorted().model_dump_json()         # byte-stable snapshot
```

**Bridge from `space` (see `docs/space.md`):**
```python
from forktex_core.space import Bundle

bundle: Bundle = ...
snapshot = await bundle.to_graph()   # materialises a forktex_core.graph.Graph
reached = transitive_closure(snapshot, some_row_id, edge_kind="references")
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `bfs`/`dfs`/`cycles` on an empty graph | Returns `[]` |
| `bfs`/`dfs` with unknown `start_id` | Returns `[]` (not an error) |
| `shortest_path` with unknown `src_id`/`dst_id` | Returns `None` |
| `shortest_path(g, "a", "a")` | Returns `["a"]` if `"a"` exists |
| `add_node` with a duplicate id | Returns the **existing** node; the new argument is discarded |
| `add_edge` with a duplicate `(kind, src, dst, attrs)` | Returns the **existing** edge (multi-edge collapse) |
| `add_edge` with an unknown endpoint | Raises `KeyError` |
| `neighbors`/`bfs`/etc. with an invalid `direction` | Raises `ValueError` |
| Self-loop (`add_edge("k", "a", "a")`) | Valid; `cycles()` reports it as a size-1 cycle |
| Cyclic cluster + a disconnected acyclic component | `cycles()` reports only the cyclic cluster |
| `sorted()` / `merge()` / `induced_subgraph()` result mutated | Independent — all three deep-copy nodes/edges, never alias the source |
| `induced_subgraph()` with ids not in the graph | Silently ignored — kept set only includes ids that exist |
| `subgraph_around()` with unknown `start_id` | Returns an empty `Graph` (same `meta`, no nodes/edges) |

### Error catalogue

| Error | When |
|---|---|
| `KeyError` | `add_edge()` — `src_id` or `dst_id` isn't a node in the graph yet |
| `ValueError` | `neighbors()`/`bfs()`/`dfs()`/etc. — `direction` isn't `"out"`/`"in"`/`"both"` |

### Integration map

```
graph ──── (used by) ──── space   [Bundle.to_graph() materialises a Graph snapshot]
graph ──── (no dep on) ── database, cache, queue, vector, storage, vault
```

### Checklist

- [ ] Nodes added before the edges that reference them (`add_edge` raises `KeyError` otherwise)
- [ ] `edge_kind=`/`direction=` used to constrain algorithms instead of pre-building a filtered subgraph
- [ ] `.sorted()` called before writing a `Graph` to disk (byte-stable, diff-friendly)
- [ ] Weighted-path needs handled via `attrs` + a custom algorithm — `shortest_path()` is unweighted
- [ ] Treated `add_node()`/`add_edge()` as idempotent lookups, not upserts, when re-adding a known id
