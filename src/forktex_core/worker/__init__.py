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

"""Level-3 ``[worker]`` extra — the queue consumer, and the hosts that can run it.

Mirror image of ``[api]`` for background work. ``[queue]`` owns *what* runs
(``@task``, ``enqueue``); this package owns *where a consumer lives*:

- :class:`Worker` — the consumer as an object: an async lifecycle
  (hooks + queue pool) plus an awaitable ``run()``. Every wrapper below is a
  thin host around it.
- :func:`run_worker` — standalone process entrypoint. Owns ``asyncio.run`` and
  lets arq install the SIGTERM/SIGINT drain.
- :func:`background` — async context manager for a host that already owns the
  loop and the signals, e.g. an API's lifespan consuming its own queue
  in-process. Cancels with a bounded drain on exit.
- :func:`run_worker_pool` — one worker per OS process, for CPU-bound tasks a
  single event loop cannot parallelise. The parent supervises and forwards
  signals; it does not restart children.

The three hosts exist because signal and loop ownership belong to the host, not
to the worker: the previous single ``run_worker`` claimed both, so a consumer
could only ever be its own process.

What this extra does NOT do:
  - Register tasks. Consumers import their task modules with
    side-effect-only imports (or pass a list of registered functions
    to ``[queue]``'s registry).
  - Wire a flow driver. Consumers that use ``[flow]`` register a
    ``startup_hooks`` callable that calls ``flow.start_driver()``.
  - Restart or back off failed processes. That is the process manager's job.

Mandatory deps: ``[queue]`` + level 0 (``[log]``/``[error]``/``[types]``).
Optional consumer-wired: ``[database]`` (advisory locks), ``[grid]``
(if tasks operate on grids), ``[flow]`` (if pipelines are wired in).
"""

from forktex_core.worker.factory import (
    DEFAULT_DRAIN_TIMEOUT,
    StartupHook,
    Worker,
    WorkerConfig,
    background,
    create_worker,
    run_worker,
    run_worker_pool,
)

__all__ = [
    "DEFAULT_DRAIN_TIMEOUT",
    "StartupHook",
    "Worker",
    "WorkerConfig",
    "background",
    "create_worker",
    "run_worker",
    "run_worker_pool",
]
