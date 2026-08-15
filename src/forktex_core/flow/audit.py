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

"""Workflow version-drift detection — the durability contract, as a library check.

Every ``@flow.workflow`` and every registered graph carries an AST hash. The durability
contract requires that a workflow at ``version=N`` is deterministically the same code body
each time the runtime invokes it: editing the body without bumping the version silently
breaks replay-on-resume for in-flight runs.

:func:`audit_workflows` compares the registered AST hashes against a checked-in manifest and
returns an :class:`AuditReport`. It used to be a ``forktex-flow`` console script; a library
exposes functions, and a CI gate is the consumer's to wire up:

    report = audit_workflows("myapp.flow_setup")
    if report.violations:
        raise SystemExit(report.summary)

    # after a deliberate version bump
    audit_workflows("myapp.flow_setup", update=True)
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from forktex_core.log import get_logger
from forktex_core.types import BaseValueObject

logger = get_logger(__name__)

__all__ = ["AuditReport", "audit_workflows"]

_DEFAULT_MANIFEST = "forktex_flow_manifest.json"


class _Entry(BaseValueObject):
    name: str
    version: int
    ast_hash: str


def _collect_registered_entries(entrypoint: str) -> list[_Entry]:
    """Import ``entrypoint`` (Python module path) so its decorator
    side effects fire, then collect the registry of every Flow that
    was constructed during the import."""
    importlib.import_module(entrypoint)

    # The simplest registry inventory: walk every imported module's
    # objects, find Flow instances, read their internal registry.
    # This avoids requiring a global registry that consumers must
    # remember to use.
    from forktex_core.flow.flow import Flow

    seen: set[tuple[str, int]] = set()
    entries: list[_Entry] = []
    # Snapshot a list (not the live view) since attribute access during
    # the walk can trigger lazy imports that mutate sys.modules.
    flow_instances: list[Flow] = []
    flow_ids: set[int] = set()
    import warnings

    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        # Many stdlib modules lazy-warn on attribute access (e.g.
        # `typing.io`, `testcontainers.core.config.RYUK_*`). Suppress
        # those during the walk — they're noise, not signal.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            for attr in dir(mod):
                try:
                    value = getattr(mod, attr)
                except Exception:
                    continue
                if not isinstance(value, Flow):
                    continue
                if id(value) in flow_ids:
                    continue
                flow_ids.add(id(value))
                flow_instances.append(value)

    for value in flow_instances:
        for (name, version, _namespace), wf_def in value._registry.definitions.items():
            key = (name, version)
            if key in seen:
                continue
            seen.add(key)
            # Compute a stable hash from the node functions' source code.
            import hashlib
            import inspect

            pieces: list[str] = []
            for node_def in wf_def.nodes.values():
                try:
                    pieces.append(inspect.getsource(node_def.fn))
                except OSError, TypeError:
                    pieces.append(repr(node_def.fn))
            raw = "\n".join(sorted(pieces))
            ast_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            entries.append(_Entry(name=name, version=version, ast_hash=ast_hash))
    entries.sort(key=lambda e: (e.name, e.version))
    return entries


def _load_manifest(path: Path) -> list[_Entry]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"manifest at {path} is not valid JSON: {exc}\n"
            f"Either delete the file (the next --update run will recreate it) "
            f"or fix the syntax."
        ) from exc
    if not isinstance(raw, list):
        raise SystemExit(f"manifest at {path} must be a JSON array of {{name, version, ast_hash}} objects")
    out: list[_Entry] = []
    for item in raw:
        out.append(
            _Entry.model_validate(
                {
                    "name": item["name"],
                    "version": int(item["version"]),
                    "ast_hash": item.get("ast_hash", ""),
                }
            )
        )
    return out


def _write_manifest(path: Path, entries: Iterable[_Entry]) -> None:
    path.write_text(
        json.dumps([e.model_dump() for e in entries], indent=2) + "\n",
        encoding="utf-8",
    )


def _diff(
    registered: list[_Entry], manifest: list[_Entry]
) -> tuple[list[_Entry], list[_Entry], list[tuple[_Entry, _Entry]]]:
    """Return ``(new, removed, changed)`` partitions.

    ``changed`` lists ``(manifest_entry, registered_entry)`` tuples
    where the same ``(name, version)`` exists in both but the AST
    hashes differ — these are the violations.
    """
    by_key_reg = {(e.name, e.version): e for e in registered}
    by_key_man = {(e.name, e.version): e for e in manifest}

    new = [e for e in registered if (e.name, e.version) not in by_key_man]
    removed = [e for e in manifest if (e.name, e.version) not in by_key_reg]
    changed: list[tuple[_Entry, _Entry]] = []
    for key, m_entry in by_key_man.items():
        r_entry = by_key_reg.get(key)
        if r_entry is None:
            continue
        if r_entry.ast_hash != m_entry.ast_hash and m_entry.ast_hash:
            changed.append((m_entry, r_entry))
    return new, removed, changed


def _format_violation(m: _Entry, r: _Entry) -> str:
    return (
        f"  workflow {m.name!r} version={m.version} body changed without a version bump\n"
        f"      manifest hash:   {m.ast_hash}\n"
        f"      registered hash: {r.ast_hash}\n"
        f"    Fix: either revert the body change, or bump the decorator to "
        f"version={m.version + 1} and run `forktex-flow audit … --update`."
    )


def _format_summary(new: list[_Entry], removed: list[_Entry], changed: list[tuple[_Entry, _Entry]]) -> str:
    lines = []
    if changed:
        lines.append(f"forktex-flow audit: {len(changed)} drift violation(s):")
        for m, r in changed:
            lines.append(_format_violation(m, r))
    if new:
        lines.append("forktex-flow audit: new (or version-bumped) workflows:")
        for e in new:
            lines.append(f"  {e.name!r} version={e.version} (hash={e.ast_hash[:12]}…)")
    if removed:
        lines.append("forktex-flow audit: workflows removed since last manifest:")
        for e in removed:
            lines.append(f"  {e.name!r} version={e.version}")
    return "\n".join(lines)


class AuditReport(BaseValueObject):
    """The outcome of one audit.

    ``violations`` is the list that matters: a workflow whose body changed while its version
    did not. ``new`` and ``removed`` are informational — adding or retiring a workflow is not
    a contract break.
    """

    new: tuple[_Entry, ...] = ()
    removed: tuple[_Entry, ...] = ()
    violations: tuple[tuple[_Entry, _Entry], ...] = ()
    summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations


def audit_workflows(
    entrypoint: str,
    *,
    manifest: str | Path = _DEFAULT_MANIFEST,
    update: bool = False,
) -> AuditReport:
    """Compare registered workflow AST hashes against ``manifest``.

    ``entrypoint`` is a module path that constructs the Flow(s) and registers every workflow
    and graph as a side effect of import. Raises ``ImportError`` if it cannot be imported —
    the caller decides whether that is fatal.

    ``update=True`` rewrites the manifest from the current registered state instead of
    comparing, for use after a deliberate version bump.
    """
    manifest_path = Path(manifest)
    registered = _collect_registered_entries(entrypoint)

    if update:
        _write_manifest(manifest_path, registered)
        return AuditReport(summary=f"wrote {len(registered)} entries to {manifest_path}")

    new, removed, changed = _diff(registered, _load_manifest(manifest_path))
    return AuditReport(
        new=tuple(new),
        removed=tuple(removed),
        violations=tuple(changed),
        summary=_format_summary(new, removed, changed),
    )
