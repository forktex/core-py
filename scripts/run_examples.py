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

"""Sandbox runner for the ``examples/`` scripts.

Boots the same testcontainer set the pytest suite uses (Postgres + Redis
+ MinIO + Qdrant), exports their connection details into the env, and
runs each example as its own subprocess (``python examples/<name>.py``).
Pass criterion is exit code 0 — failures aggregate and surface in a
table at the end. Not part of ``make ci``; invoke via ``make examples``
or ``poetry run python scripts/run_examples.py``.

Why subprocess instead of in-process imports? Each example runs in a
fresh Python process — exactly what a real user does. Top-level side
effects, missing ``__main__`` guards, and sys.exit calls show up as
they would for a consumer.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

# Ensure ``tests._containers`` is importable when running from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._containers import (  # noqa: E402
    ensure_minio_bucket,
    start_minio,
    start_postgres,
    start_qdrant,
    start_redis,
)


EXAMPLES_DIR = _REPO_ROOT / "examples"


def _discover_examples() -> list[Path]:
    """Every ``examples/*.py`` that isn't underscore-prefixed."""
    return sorted(p for p in EXAMPLES_DIR.glob("*.py") if not p.name.startswith("_"))


def _run_one(example: Path, env: dict[str, str], timeout: int = 120) -> tuple[bool, float, str]:
    """Run a single example as a subprocess. Returns ``(ok, elapsed_s, output)``."""
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(example)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=_REPO_ROOT,
    )
    elapsed = time.monotonic() - started
    ok = proc.returncode == 0
    output = (proc.stdout or "") + (proc.stderr or "")
    return ok, elapsed, output


def main() -> int:
    print("=" * 70)
    print("examples sandbox — booting testcontainers (Postgres/Redis/MinIO/Qdrant)")
    print("=" * 70)

    containers: list = []
    try:
        pg_container, pg_url = start_postgres()
        containers.append(pg_container)
        redis_container, redis_url = start_redis()
        containers.append(redis_container)
        minio_container, minio_config = start_minio()
        containers.append(minio_container)
        asyncio.run(ensure_minio_bucket(minio_config))
        qdrant_container, qdrant_url = start_qdrant()
        containers.append(qdrant_container)

        env = dict(os.environ)
        env.update(
            {
                "POSTGRES_URL": pg_url.render_as_string(hide_password=False),
                "REDIS_URL": redis_url,
                "MINIO_URL": minio_config["url"],
                "MINIO_ACCESS_KEY": minio_config["access_key"],
                "MINIO_SECRET_KEY": minio_config["secret_key"],
                "MINIO_BUCKET": minio_config["bucket"],
                "QDRANT_URL": qdrant_url,
                # Ensure subprocess sees the repo so ``forktex_core`` imports cleanly.
                "PYTHONPATH": str(_REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
            }
        )

        examples = _discover_examples()
        print(f"\ndiscovered {len(examples)} example(s):\n")
        for p in examples:
            print(f"  - {p.name}")
        print()

        results: list[tuple[str, bool, float, str]] = []
        for example in examples:
            print(f"→ running {example.name} …")
            try:
                ok, elapsed, output = _run_one(example, env)
            except subprocess.TimeoutExpired as exc:
                ok = False
                elapsed = float(exc.timeout or 0)
                output = f"[timed out after {elapsed:.0f}s]\n{(exc.stdout or '') + (exc.stderr or '')}"
            results.append((example.name, ok, elapsed, output))
            marker = "✓" if ok else "✗"
            print(f"  {marker} exit-code={'0' if ok else 'fail'} in {elapsed:.1f}s")

        print()
        print("=" * 70)
        print(f"{'EXAMPLE':<28} {'RESULT':<10} {'TIME':>8}")
        print("-" * 70)
        failures = 0
        for name, ok, elapsed, _output in results:
            status = "PASS" if ok else "FAIL"
            print(f"{name:<28} {status:<10} {elapsed:>7.1f}s")
            if not ok:
                failures += 1
        print("=" * 70)

        if failures:
            print(f"\n{failures} example(s) failed:\n")
            for name, ok, _elapsed, output in results:
                if not ok:
                    print(f"--- {name} output ---")
                    print(output)
                    print(f"--- end {name} ---\n")
            return 1
        print(f"\nall {len(results)} example(s) passed.")
        return 0

    finally:
        for c in reversed(containers):
            try:
                c.stop()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
