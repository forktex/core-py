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

"""Shared testcontainer factories.

The pytest fixtures in :mod:`tests.conftest` and the standalone example
sandbox in :mod:`scripts.run_examples` both need to boot the same set
of services (Postgres 17, Redis 7, MinIO, Qdrant, MongoDB). Image tags are
pinned (not ``:latest``) so CI is reproducible and a breaking upstream
push can't fail the suite without a deliberate bump here. The factory
functions below centralise that bring-up so the two callers stay in
lockstep on container versions and config.

Each ``start_*`` returns ``(container, payload)``. The caller is
responsible for ``container.stop()`` on teardown. ``payload`` is the
shape consumers actually use (a URL string, a config dict, etc.).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy.engine import URL


def start_postgres() -> tuple[Any, URL]:
    """Postgres 17 (alpine). Returns ``(container, asyncpg URL)``."""
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:17-alpine")
    container.start()
    raw = container.get_connection_url()
    parsed = urlparse(raw)
    url = URL.create(
        drivername="postgresql+asyncpg",
        username=parsed.username,
        password=parsed.password,
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
        query={"ssl": "disable"},
    )
    return container, url


def start_redis() -> tuple[Any, str]:
    """Redis 7 (alpine). Returns ``(container, redis://host:port/0)``."""
    from testcontainers.redis import RedisContainer

    container = RedisContainer("redis:7-alpine")
    container.start()
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6379)
    return container, f"redis://{host}:{port}/0"


def start_minio(bucket: str = "test-bucket") -> tuple[Any, dict[str, str]]:
    """MinIO. Returns ``(container, {url, bucket, access_key, secret_key})``.

    The caller is responsible for ensuring ``bucket`` exists before
    using it — see :func:`ensure_minio_bucket` for an async helper.
    Splitting the bucket-create out lets this factory be called from
    both sync (sandbox script) and async (pytest_asyncio fixture)
    contexts without an ``asyncio.run`` inside an active loop.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = (
        DockerContainer("minio/minio:RELEASE.2025-09-07T16-13-09Z")
        .with_command("server /data --console-address :9001")
        .with_env("MINIO_ROOT_USER", "minioadmin")
        .with_env("MINIO_ROOT_PASSWORD", "minioadmin")
        .with_exposed_ports(9000, 9001)
    )
    container.start()
    wait_for_logs(container, "API", timeout=30)

    host = container.get_container_host_ip()
    port = container.get_exposed_port(9000)
    url = f"http://{host}:{port}"
    config = {
        "url": url,
        "bucket": bucket,
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
    }
    return container, config


async def ensure_minio_bucket(config: dict[str, str]) -> None:
    """Idempotent ``create_bucket`` for the bucket named in ``config``.

    No-op if ``aioboto3`` isn't installed — consumers that don't pull
    the ``[storage]`` extra don't need a bucket anyway.
    """
    try:
        import aioboto3
    except ImportError:
        return

    session = aioboto3.Session(
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
    )
    async with session.client("s3", endpoint_url=config["url"]) as s3:
        try:
            await s3.create_bucket(Bucket=config["bucket"])
        except Exception:
            pass  # already exists


def start_qdrant() -> tuple[Any, str]:
    """Qdrant. Returns ``(container, http://host:port)``."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer("qdrant/qdrant:v1.18.1").with_exposed_ports(6333, 6334)
    container.start()
    wait_for_logs(container, "Qdrant gRPC", timeout=30)
    host = container.get_container_host_ip()
    port = container.get_exposed_port(6333)
    return container, f"http://{host}:{port}"


def start_mongo() -> tuple[Any, str]:
    """MongoDB, configured as a single-node replica set so transactions work.

    MongoDB only supports multi-document transactions (``session.start_transaction()``)
    against a replica set or sharded cluster — never a standalone ``mongod``, which is
    what this container would be without the ``--replSet`` override below.

    Auth is deliberately disabled for this container (``username=""``/``password=""``,
    overriding ``MongoDbContainer``'s own default-to-``"test"`` fallback). Enabling it
    makes the official image's entrypoint boot a *temporary* unauthenticated ``mongod``
    first (to run init scripts / create the root user), then stop it and restart the
    real one — a transition that reliably breaks when combined with a custom
    ``--replSet`` command override (confirmed: the container never becomes reachable
    within 40+ seconds in that combination). Skipping auth removes that dance entirely.

    Returns ``(container, "mongodb://host:port/?directConnection=true")``. Callers must
    keep using ``directConnection=true`` (never switch to a ``replicaSet=rs0`` URI) —
    the replica set's advertised member host (``localhost:27017``, the container's own
    internal view of itself) is only reachable from *inside* the container.
    ``directConnection=true`` still supports transactions on a replica-set member; it
    just skips full topology discovery.
    """
    import time

    from pymongo import MongoClient
    from testcontainers.mongodb import MongoDbContainer

    container = MongoDbContainer("mongo:7", username=None, password=None).with_command(
        "mongod --replSet rs0 --bind_ip_all"
    )
    container.username = ""
    container.password = ""
    container.start()

    host = container.get_container_host_ip()
    port = container.get_exposed_port(27017)
    url = f"mongodb://{host}:{port}/?directConnection=true"

    admin = MongoClient(url, serverSelectionTimeoutMS=10_000)
    try:
        admin.admin.command("ping")
        # Self-identify via the container's OWN internal view (port 27017), not
        # the externally Docker-mapped port — replSetInitiate rejects a member
        # host that doesn't match the node's own bound address.
        admin.admin.command(
            "replSetInitiate",
            {"_id": "rs0", "members": [{"_id": 0, "host": "localhost:27017"}]},
        )
        for _ in range(30):
            time.sleep(1)
            try:
                if admin.admin.command("replSetGetStatus").get("myState") == 1:
                    break
            except Exception:
                continue
        else:
            raise RuntimeError("MongoDB replica set did not reach PRIMARY state in time")
    finally:
        admin.close()

    return container, url
