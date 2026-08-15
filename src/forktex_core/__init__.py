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

"""FORKTEX Core — shared infrastructure substrate for ForkTex Python services.

Modules, one library, one set of opinions:

    database — async Postgres engine, session, ORM base classes, CRUD
    cache    — async Redis client, @cached, namespaced keys, rate limiter
    flow     — Postgres-native durable execution (pipelines, graphs, agents)
    vault    — Fernet symmetric encryption, EncryptedJSON column type
    storage  — S3/MinIO object storage connector (aioboto3)
    queue    — arq background-job queue
    vector   — Qdrant multi-modal vector search
    grid     — fully-dynamic virtual database (GridTable / GridColumn / GridRow, query engine)
    graph    — typed multi-edge in-memory graph algebra
    space    — multi-Grid bundle (VECTOR / FILE handlers, cross-Grid traversal)
    log      — structured JSON logging (Loki-ready), trace_id contextvar, setup_logging
    error    — AppError hierarchy + ErrorEnvelope
    types    — base Pydantic models, frozen value objects
    api      — FastAPI factory (create_app)
    worker   — arq worker bootstrap (create_worker / run_worker)
"""

__version__ = "0.1.0"
