#!/usr/bin/env python3

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

"""Render the latest grid perf run as a markdown table.

Reads the newest ``tests/test_grid/test_limits/.runs/*.jsonl`` (or a
path passed via ``--run``), groups records by ``(op, mode)`` and tier,
and prints a table whose cells are ``grid_ms / native_ms (Nx)``.

Soft-baseline tool: it never fails the build, never enforces a
threshold. Cells with ``grid_ms >= 500`` are flagged with a ``⚠`` so
the eye lands on cliffs, but no exit code is non-zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


_RUNS_DIR = Path(__file__).resolve().parent.parent / "tests" / "test_grid" / "test_limits" / ".runs"


def _latest_run() -> Path | None:
    if not _RUNS_DIR.exists():
        return None
    candidates = sorted(_RUNS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def _merge_grid_and_native(records: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Records arrive as separate ``grid`` and ``native`` lines per
    (name, tier, mode). Fold them into a single dict so the renderer can
    show both columns side by side.
    """
    merged: dict[tuple[str, str, int], dict[str, Any]] = defaultdict(dict)
    for r in records:
        key = (r["name"], r.get("mode", "?"), r["tier"])
        if "grid" in r:
            merged[key]["grid"] = r["grid"]
            merged[key]["row_count"] = r.get("row_count", 0)
            merged[key]["extra"] = r.get("extra", {})
        if "native" in r:
            merged[key]["native"] = r["native"]
    return merged


def _format_cell(entry: dict[str, Any]) -> str:
    grid = entry.get("grid", {})
    native = entry.get("native", {})
    g = grid.get("median_ms")
    n = native.get("median_ms")
    if g is None and n is None:
        return "—"
    flag = " ⚠" if (g is not None and g >= 500) else ""
    if g is None:
        return f"— / {n:.1f}"
    if n is None:
        # Write-side / numbering ops record only a grid number.
        return f"{g:.1f}{flag}"
    ratio = g / n if n > 0 else 0
    return f"{g:.1f} / {n:.1f} ({ratio:.1f}x){flag}"


def render(records: list[dict[str, Any]]) -> str:
    merged = _merge_grid_and_native(records)
    # Group by (mode, op), columns = sorted tiers.
    by_mode: dict[str, dict[str, dict[int, dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    tiers_seen: set[int] = set()
    for (name, mode, tier), entry in merged.items():
        by_mode[mode][name][tier] = entry
        tiers_seen.add(tier)
    tiers = sorted(tiers_seen)
    if not tiers:
        return "(no records)"

    out: list[str] = []
    sample = next(iter(records))
    out.append(f"# grid perf report — {sample.get('git_sha', '?')} on {sample.get('platform', '?')}")
    out.append("")
    out.append("Cells: `grid_ms / native_ms (Nx)` — median over 5 iterations after one warmup.")
    out.append("Flagged with ⚠ when `grid_ms >= 500`.")
    out.append("")

    for mode in sorted(by_mode):
        out.append(f"## {mode}")
        out.append("")
        header = ["op"] + [f"n={t:,}" for t in tiers]
        out.append("| " + " | ".join(header) + " |")
        out.append("|" + "|".join(["---"] * len(header)) + "|")
        for op in sorted(by_mode[mode]):
            cells = [op]
            for tier in tiers:
                entry = by_mode[mode][op].get(tier, {})
                cells.append(_format_cell(entry))
            out.append("| " + " | ".join(cells) + " |")
        out.append("")

    out.append("---")
    out.append(
        f"git_sha={sample.get('git_sha', '?')}  python={sample.get('python', '?')}  "
        f"cpu_count={sample.get('cpu_count', '?')}  loadavg_1m={sample.get('loadavg_1m', '?')}"
    )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", type=Path, default=None, help="path to a JSONL run (defaults to newest)")
    args = ap.parse_args()

    path = args.run or _latest_run()
    if path is None or not path.exists():
        print("No perf runs found. Run `make test-perf` first.", file=sys.stderr)
        return 0  # soft baseline: never fail the build
    records = _load(path)
    if not records:
        print(f"Empty run: {path}", file=sys.stderr)
        return 0
    print(render(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
