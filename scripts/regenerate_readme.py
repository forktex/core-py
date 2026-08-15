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

# Copyright (C) 2026 FORKTEX S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial
"""Regenerate README.md's catalog-backed blocks from `forktex_core/catalog/v0_7.json`.

The README contains generated regions bracketed by HTML comment markers::

    <!-- catalog:levels start -->
    ...generated content here...
    <!-- catalog:levels end -->

Available marker IDs (one per renderer in ``forktex_core.catalog.render``):

    levels   — table of architecture levels
    extras   — table of every extra with role, tech, deps, status
    deps     — dependency grid (depends_on / lazy_imports / optional_for_consumer)
    matrix   — pick-and-choose matrix mapping use-cases → extras → infra
    tree     — ASCII filesystem tree of forktex_core/

Two modes::

    scripts/regenerate_readme.py            # rewrite README in place
    scripts/regenerate_readme.py --check    # diff-fail if README is out of sync
                                            # (used by `make catalog-check`)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"

# Each marker pair: ``<!-- catalog:<id> start -->...<!-- catalog:<id> end -->``.
# Generated content lives between the two; everything outside stays untouched.
_MARKER_RE = re.compile(
    r"<!-- catalog:(?P<id>[a-z0-9_]+) start -->\n.*?\n<!-- catalog:(?P=id) end -->",
    re.DOTALL,
)


def _splice(readme: str, blocks: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        block_id = match.group("id")
        if block_id not in blocks:
            sys.stderr.write(
                f"warning: README has marker `catalog:{block_id}` but no renderer "
                f"emits content for it (known: {sorted(blocks)})\n"
            )
            return match.group(0)
        return f"<!-- catalog:{block_id} start -->\n{blocks[block_id]}\n<!-- catalog:{block_id} end -->"

    return _MARKER_RE.sub(replace, readme)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Diff-fail if README would change instead of rewriting.",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "src"))
    from forktex_core.catalog import current
    from forktex_core.catalog.render import render_all

    # README is the consumer-facing surface; it only renders shipped
    # items. Catalog JSON keeps planned items for internal/future use;
    # the ROADMAP.md sibling links surface them to consumers separately.
    blocks = render_all(current, shipped_only=True)
    original = README_PATH.read_text(encoding="utf-8")
    rewritten = _splice(original, blocks)

    drift = _check_doc_links(rewritten)
    if drift:
        sys.stderr.write(
            "regenerate_readme: README points at missing files:\n" + "\n".join(f"  - {path}" for path in drift) + "\n"
        )
        return 2

    if rewritten == original:
        print("regenerate_readme: no changes")
        return 0

    if args.check:
        sys.stderr.write(
            "regenerate_readme: README is out of sync with catalog. "
            "Run `python scripts/regenerate_readme.py` to refresh.\n"
        )
        return 1

    README_PATH.write_text(rewritten, encoding="utf-8")
    print(f"regenerate_readme: rewrote {README_PATH}")
    return 0


def _check_doc_links(rewritten: str) -> list[str]:
    """Return paths the rewritten README references that don't exist on
    disk. Covers ``docs/<id>.md`` and ``tests/test_stories/test_*.py``
    references — the two link families consumer-facing copy points at.

    Run after splicing so we catch drift introduced both by catalog
    rendering and by hand-edits to the static parts of the README.
    """
    import re

    candidates: list[str] = []
    candidates += re.findall(r"docs/[a-z0-9_]+\.md", rewritten)
    candidates += re.findall(r"tests/test_stories/test_[a-z0-9_]+\.py", rewritten)
    missing: list[str] = []
    for rel in sorted(set(candidates)):
        if not (ROOT / rel).exists():
            missing.append(rel)
    return missing


if __name__ == "__main__":
    sys.exit(main())
