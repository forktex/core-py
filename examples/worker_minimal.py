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

"""Minimal ``[worker]`` consumer — register a task, build a worker, exit.

Demonstrates the worker surface: ``@queue.task`` registers an async task, and
``WorkerConfig`` configures a consumer. The example only builds + inspects it —
consuming for real blocks on the arq loop, which is what the three hosts do:

- ``run_worker(config)`` — a standalone entrypoint that owns the process;
- ``async with background(config)`` — embedded next to an API, drained on exit;
- ``run_worker_pool(config, processes=N)`` — one worker per OS process.

Set ``REDIS_URL`` (defaults to ``redis://localhost:6379/9``).
Run with ``python examples/worker_minimal.py``.
"""

from __future__ import annotations

import os

from forktex_core import queue
from forktex_core.worker import WorkerConfig, create_worker


@queue.task()
async def greet(ctx: dict, name: str) -> str:
    """Toy task: greets a name and returns the greeting."""
    return f"Hello, {name}!"


def main() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/9")

    config = WorkerConfig(
        redis_url=redis_url,
        queue_name="examples",
        max_jobs=4,
        job_timeout=60,
    )
    worker = create_worker(config)

    print(f"redis_url:                {redis_url}")
    print(f"queue_name:               {worker.queue_name}")
    print(f"max_jobs:                 {worker.max_jobs}")
    print(f"job_timeout_s:            {worker.job_timeout_s}")
    print(f"registered_functions:     {len(worker.functions)}")
    print(f"'greet' registered:       {'greet' in worker.functions}")

    assert "greet" in worker.functions

    # In a real entrypoint you would pick a host for the consumer:
    #
    #   from forktex_core.worker import run_worker
    #   run_worker(config)                       # own the process
    #
    #   async with background(config):           # inside a FastAPI lifespan
    #       yield
    #
    #   run_worker_pool(config, processes=4)     # CPU-bound tasks
    #
    # Each blocks (or runs) until the host stops it.


if __name__ == "__main__":
    main()
