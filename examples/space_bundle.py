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

"""``Bundle`` bundle with FILE + VECTOR fields end-to-end.

Declares a Bundle that bundles a ``documents`` Grid (with a FILE field that
uploads to MinIO) and a ``chunks`` Grid (with a VECTOR field stored remotely
in Qdrant). Writes rows, cross-Grid-traverses the relation graph, runs a
Qdrant similarity search, then archives a chunk and confirms the Qdrant
point is deleted.

Needs Postgres + MinIO + Qdrant. Set env vars:

  POSTGRES_URL          asyncpg URL
  MINIO_URL             http://host:port
  MINIO_ACCESS_KEY      MinIO access key
  MINIO_SECRET_KEY      MinIO secret key
  MINIO_BUCKET          bucket name (default: test-bucket)
  QDRANT_URL            http://host:port

Run with the env set: ``python examples/space_bundle.py``.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich FILE + VECTOR
from forktex_core.grid import (
    FieldType,
    Grid,
    RelationShape,
    RelationSpec,
    TableSpec,
    apply_migrations,
    declare_relation,
)
from forktex_core.space import Bundle, BundleConfig, VectorDefaults
from forktex_core.space.types.file import FILE_TYPE_ID
from forktex_core.space.types.vector import VECTOR_TYPE_ID

_SCHEMA = "forktex_grid"


async def _register_clients(*, bucket: str) -> tuple[str, str, str]:
    """Register a storage + vector client from env vars; pre-upload a blob.

    Returns ``(storage_client_name, vector_client_name, storage_key)`` so the
    example body can reference the registrations by name. Both clients get a
    fresh per-run identifier to avoid colliding with previous example runs
    that may have left state in the same Redis/MinIO/Qdrant instance.
    """
    from forktex_core.storage import register as storage_register
    from forktex_core.vector import register as vector_register

    storage_name = f"example-storage-{uuid.uuid4().hex[:6]}"
    vector_name = f"example-vector-{uuid.uuid4().hex[:6]}"

    storage_register(
        storage_name,
        os.environ["MINIO_URL"],
        bucket,
        os.environ["MINIO_ACCESS_KEY"],
        os.environ["MINIO_SECRET_KEY"],
    )
    vector_register(vector_name, os.environ["QDRANT_URL"])

    # Pre-upload a blob so the documents Grid can reference it.
    from forktex_core.storage import get_client as get_storage_client

    storage_key = f"examples/{uuid.uuid4().hex}/whitepaper.pdf"
    await get_storage_client(storage_name).upload(storage_key, b"%PDF-1.4 example blob", content_type="application/pdf")
    return storage_name, vector_name, storage_key


async def main() -> None:
    postgres_url = os.environ["POSTGRES_URL"]
    bucket = os.environ.get("MINIO_BUCKET", "test-bucket")
    storage_name, vector_name, storage_key = await _register_clients(bucket=bucket)

    # ── Schema bring-up (unique per run) ──────────────────────────────
    fresh_schema = f"space_demo_{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(
        postgres_url,
        execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
    )
    await apply_migrations(engine, schema=fresh_schema)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    namespace = str(uuid.uuid4())

    async with maker() as session:
        # ── 1. Two member Grids: documents (FILE) + chunks (VECTOR) ───
        documents = await Grid.declare(
            session,
            TableSpec.from_dicts(
                slug="documents",
                label="Documents",
                namespace=namespace,
                columns=[
                    {"key": "title", "label": "Title", "type_id": FieldType.text.value},
                    {
                        "key": "attachment",
                        "label": "Attachment",
                        "type_id": FILE_TYPE_ID,
                        "config": {"client_name": storage_name, "delete_on_archive": True},
                    },
                ],
            ),
        )
        chunks = await Grid.declare(
            session,
            TableSpec.from_dicts(
                slug="chunks",
                label="Chunks",
                namespace=namespace,
                columns=[
                    {"key": "text", "label": "Text", "type_id": FieldType.text.value},
                    {
                        "key": "embedding",
                        "label": "Embedding",
                        "type_id": VECTOR_TYPE_ID,
                        "config": {"storage_mode": "remote", "dimensions": 4, "client_name": vector_name},
                    },
                ],
            ),
        )

        # ── 2. Bundle into a Bundle ────────────────────────────────────
        space = await Bundle.declare(
            session,
            namespace=namespace,
            slug="kb",
            label="Knowledge Base",
            config=BundleConfig(vector=VectorDefaults(dimensions=4, storage_mode="remote")),
            members=[documents, chunks],
        )
        print(f"space:    {space.slug}")
        print(f"members:  {sorted(g.slug for g in space.grids.values())}")

        # ── 3. Write rich-content rows ────────────────────────────────
        doc = await documents.create(
            {
                "title": "Whitepaper v1",
                "attachment": {
                    "storage_key": storage_key,
                    "filename": "whitepaper.pdf",
                    "content_type": "application/pdf",
                },
            }
        )
        chunk_a = await chunks.create({"text": "Hello world", "embedding": [1.0, 0.0, 0.0, 0.0]})
        chunk_b = await chunks.create({"text": "Goodbye sky", "embedding": [0.0, 1.0, 0.0, 0.0]})
        await session.commit()

        # In remote mode the VECTOR cell is stripped to a back-ref descriptor.
        chunk_a_cell = chunk_a.values["embedding"]
        assert "collection" in chunk_a_cell and "point_id" in chunk_a_cell
        assert "vector" not in chunk_a_cell
        print(f"vector back-ref: {chunk_a_cell}")
        print(f"file descriptor: {doc.values['attachment']}")

        # ── 4. Cross-Grid relation + traversal ────────────────────────
        await declare_relation(
            session,
            RelationSpec(key="has_chunk", source="documents", target="chunks", shape=RelationShape.one_to_many),
            namespace,
        )
        await documents.relate("has_chunk", doc.id, chunk_a.id)

        graph = await space.to_graph()
        print(f"graph: {len(graph.nodes)} nodes, {len(graph.edges)} edges")

        sub = await space.traverse(doc.id, max_depth=1, direction="out")
        print(f"doc out-neighbors: {sorted(n.id for n in sub.nodes)}")

        # ── 5. Qdrant similarity search hits chunk_a ──────────────────
        from forktex_core.vector import SearchQuery, get_client

        client = get_client(vector_name)
        handle = client.collection(f"{namespace}--chunks--embedding")
        hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
        assert hits and str(hits[0].id) == str(chunk_a.id)
        print(f"qdrant hits: {len(hits)} (top = chunk_a)")

        # ── 6. Archive cleanup: Qdrant point + MinIO blob removed ─────
        await chunks.archive(chunk_b.id)
        await session.commit()
        post = await handle.search(SearchQuery(vector=[0.0, 1.0, 0.0, 0.0]).limit(5))
        assert not any(str(h.id) == str(chunk_b.id) for h in post)
        print("chunk_b archived: qdrant point deleted")

        await handle.delete()  # don't leave a collection behind

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
