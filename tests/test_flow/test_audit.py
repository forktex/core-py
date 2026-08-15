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

"""Unit tests for ``forktex-flow audit``.

The audit gate is the mechanical enforcement of the versioning
contract: a workflow body change without a ``version=`` bump must
fail CI. These tests construct tiny synthetic entrypoint modules,
write/inspect manifest files in ``tmp_path``, and drive
``audit_workflows(...)`` directly so the check stays testcontainer-free.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from forktex_core.flow import audit


def _write_module(
    pkg_dir: Path,
    name: str,
    body: str,
) -> str:
    """Write ``<pkg_dir>/<name>.py`` with ``body`` (dedented). Return
    the dotted module path, scoped under a unique package so subsequent
    re-imports don't collide with cached state from earlier tests."""
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return f"{pkg_dir.name}.{name}"


def _purge_test_packages(packages: set[str]) -> None:
    """Drop test-scaffold modules from ``sys.modules`` so the next
    ``importlib.import_module`` re-reads from disk. Also drops every
    Flow-bearing module under those packages so the audit CLI's
    ``sys.modules`` walk doesn't pick up stale Flow instances from a
    prior run."""
    for mod_name in list(sys.modules):
        for pkg in packages:
            if mod_name == pkg or mod_name.startswith(pkg + "."):
                sys.modules.pop(mod_name, None)


@pytest.fixture
def entrypoint_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Yield a builder that writes a Python module on disk and returns
    its dotted import path. The builder also purges any prior tmp
    package from ``sys.modules`` before writing — so a sequence of
    builds within one test simulates sequential CI runs (each re-imports
    cleanly and the audit CLI sees only the latest registered Flow)."""
    monkeypatch.syspath_prepend(str(tmp_path))
    created_packages: set[str] = set()

    def _build(pkg_name: str, mod_name: str, body: str) -> str:
        # Purge ALL previously created tmp packages so the audit CLI's
        # sys.modules walk only sees the freshly built one.
        _purge_test_packages(created_packages)
        dotted = _write_module(tmp_path / pkg_name, mod_name, body)
        created_packages.add(pkg_name)
        return dotted

    yield _build

    _purge_test_packages(created_packages)


def _workflow_module_body(version: int, body_token: str) -> str:
    """Synthesise a tiny entrypoint that constructs a Flow and
    registers one pipeline whose step body is parametrised by
    ``body_token`` (so changing the token changes the source hash)."""
    return f"""
        from forktex_core.flow import Flow, Ctx, step

        flow = Flow(database_url="postgresql+asyncpg://x:y@localhost/z")

        @step
        async def demo_step(ctx: Ctx, state: dict) -> dict:
            marker = {body_token!r}
            return {{**state, "marker": marker}}

        @flow.pipeline("demo.wf", version={version})
        class DemoWf:
            steps = [demo_step]
    """


# ── Tests ────────────────────────────────────────────────────────────


def test_audit_passes_when_manifest_matches(entrypoint_factory, tmp_path, capsys):
    entrypoint = entrypoint_factory("audit_match_pkg", "ep", _workflow_module_body(1, "v1-original"))
    manifest = tmp_path / "m.json"

    # First seed the manifest from current state via --update so the
    # subsequent audit sees a matching hash.
    assert audit.audit_workflows(entrypoint, manifest=manifest, update=True).ok

    report = audit.audit_workflows(entrypoint, manifest=manifest)
    assert report.ok


def test_audit_fails_on_unbumped_change(entrypoint_factory, tmp_path, capsys):
    # Seed a manifest at v1 with the original body.
    ep_orig = entrypoint_factory("audit_drift_pkg_a", "ep", _workflow_module_body(1, "v1-original"))
    manifest = tmp_path / "m.json"
    assert audit.audit_workflows(ep_orig, manifest=manifest, update=True).ok

    # Now simulate the developer editing the body without bumping the
    # version: same (name, version=1) — different body literal.
    ep_drifted = entrypoint_factory("audit_drift_pkg_b", "ep", _workflow_module_body(1, "v1-EDITED-WITHOUT-BUMP"))
    report = audit.audit_workflows(ep_drifted, manifest=manifest)
    assert not report.ok
    assert len(report.violations) == 1
    manifest_entry, registered_entry = report.violations[0]
    assert manifest_entry.name == registered_entry.name == "demo.wf"
    assert manifest_entry.ast_hash != registered_entry.ast_hash
    assert "drift violation" in report.summary
    assert "version=2" in report.summary  # the suggested fix references version+1


def test_audit_passes_after_version_bump(entrypoint_factory, tmp_path):
    # Seed at v1.
    ep_orig = entrypoint_factory("audit_bump_pkg_a", "ep", _workflow_module_body(1, "v1-original"))
    manifest = tmp_path / "m.json"
    assert audit.audit_workflows(ep_orig, manifest=manifest, update=True).ok

    # Developer edits the body AND bumps version to 2 — this is the
    # legal change. v1 stays unchanged in the manifest; v2 is new.
    ep_bumped = entrypoint_factory(
        "audit_bump_pkg_b",
        "ep",
        _workflow_module_body(2, "v2-new-body"),
    )
    report = audit.audit_workflows(ep_bumped, manifest=manifest)
    # No drift on (demo.wf, 1); v2 is reported as new, which is not a violation.
    assert report.ok
    assert report.violations == ()
    assert [e.version for e in report.new] == [2]


def test_audit_update_writes_manifest(entrypoint_factory, tmp_path):
    entrypoint = entrypoint_factory("audit_write_pkg", "ep", _workflow_module_body(1, "fresh"))
    manifest = tmp_path / "fresh.json"
    assert not manifest.exists()

    report = audit.audit_workflows(entrypoint, manifest=manifest, update=True)
    assert report.ok
    assert "wrote 1 entries" in report.summary
    assert manifest.exists()

    payload = json.loads(manifest.read_text())
    assert isinstance(payload, list)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["name"] == "demo.wf"
    assert entry["version"] == 1
    assert isinstance(entry["ast_hash"], str) and entry["ast_hash"]


def test_audit_handles_missing_manifest_with_helpful_error(entrypoint_factory, tmp_path, capsys):
    """A missing manifest file is treated as an empty baseline: no
    drift can be detected, so the audit passes and reports the
    workflow as new. (The first --update run is what creates the
    manifest in CI.)"""
    entrypoint = entrypoint_factory("audit_missing_pkg", "ep", _workflow_module_body(1, "fresh"))
    manifest = tmp_path / "does-not-exist.json"

    report = audit.audit_workflows(entrypoint, manifest=manifest)
    assert report.ok
    assert [e.name for e in report.new] == ["demo.wf"]
    assert "new (or version-bumped)" in report.summary


def test_audit_corrupt_manifest_reports_clear_error(entrypoint_factory, tmp_path):
    entrypoint = entrypoint_factory("audit_corrupt_pkg", "ep", _workflow_module_body(1, "x"))
    manifest = tmp_path / "corrupt.json"
    manifest.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        audit.audit_workflows(entrypoint, manifest=manifest)
    assert "not valid JSON" in str(exc_info.value)


def test_audit_propagates_an_unimportable_entrypoint():
    """The library raises; the caller decides whether that is fatal. The CLI used to
    swallow it into exit code 2, which a consumer's CI could not distinguish from a
    genuine drift failure."""
    with pytest.raises(ImportError):
        audit.audit_workflows("definitely_not_a_real_module_xyzzy")
