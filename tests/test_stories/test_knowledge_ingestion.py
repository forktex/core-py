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

"""STORY: knowledge ingestion lifecycle.

A consumer (e.g., intelligence) builds a knowledge base from uploaded
documents. The story is decomposed into five acts; each is its own
test method so a regression in act 2 doesn't mask the diagnostics for
acts 3-5. Class-scoped state carries the staged objects (Bundle, Grids,
the uploaded blob, the chunk rows, the Qdrant handle) across acts.

  Act 1. Declare a Bundle ``kb`` bundling two Grids:
           - ``documents`` with a FILE field (the source PDF).
           - ``chunks`` with a VECTOR field (the embedding) + TEXT.
  Act 2. A user uploads ``whitepaper.pdf`` — bytes land in MinIO; the
         ``documents`` row records the descriptor.
  Act 3. The chunker splits the doc; the rich VECTOR handler upserts
         each embedding into Qdrant + strips the inline vector + stamps
         back-refs. Cross-Grid edges link each chunk to its parent doc.
  Act 4. Semantic search returns the top point's row id; we walk the
         ``has_chunk`` edge backwards to recover the parent document.
  Act 5. The consumer archives every chunk + the document. We assert
         the FILE handler deletes the blob and the VECTOR handler
         tombstones each point.

Real Postgres + real MinIO + real Qdrant. No mocks. Cleanup of the
Qdrant collection runs in the ``qdrant_collection_tracker`` finaliser
(see ``conftest.py``) so a mid-act crash doesn't leak collections.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import forktex_core.space  # noqa: F401  side-effect: register rich FILE/VECTOR
from forktex_core.grid import (
    FieldType,
    Grid,
    OnDelete,
    TableSpec,
    apply_migrations,
)
from forktex_core.grid.domain.enums import RelationShape
from forktex_core.grid.persist import GridEdge, GridRelation
from forktex_core.space import Bundle, BundleConfig, VectorDefaults
from forktex_core.space.types.file import FILE_TYPE_ID
from forktex_core.space.types.vector import VECTOR_TYPE_ID
from forktex_core.storage import register as register_storage
from forktex_core.vector import SearchQuery, register as register_vector

_SCHEMA = "forktex_grid"


async def _declare(session, *, namespace, slug, label, columns, is_system=False):
    return await Grid.declare(
        session,
        TableSpec.from_dicts(slug=slug, label=label, namespace=namespace, is_system=is_system, columns=columns),
    )


class KBState(BaseModel):
    """In-flight state carried across the five story acts."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: AsyncSession
    storage_client_name: str
    vector_client_name: str
    namespace: str
    documents: Grid | None = None
    chunks: Grid | None = None
    space: Bundle | None = None
    pdf_key: str | None = None
    pdf_bytes: bytes | None = None
    doc: object | None = None
    chunk_rows: list[object] = Field(default_factory=list)
    collection_name: str | None = None


@pytest.mark.asyncio(loop_scope="class")
class TestKnowledgeIngestion:
    """Five acts of the knowledge-ingestion story; each is its own test
    method. They run in declared order under pytest's default in-file
    ordering. Class-scoped fixtures keep the staged state alive across
    acts; cleanup runs from ``conftest.py`` finalisers."""

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def state(
        self,
        postgres_url,  # session-scoped URL; we render to str inline
        minio_config: dict,
        qdrant_url: str,
    ):
        """Stand up a per-class Postgres schema + register named storage
        and vector clients; build the shared ``KBState``. The conftest's
        ``_clients_snapshot`` fixture restores the registry between
        tests on this class boundary too."""
        fresh_schema = f"story_kb_{uuid.uuid4().hex[:8]}"
        engine = create_async_engine(
            postgres_url.render_as_string(hide_password=False),
            execution_options={"schema_translate_map": {_SCHEMA: fresh_schema}},
        )
        await apply_migrations(engine, schema=fresh_schema)
        maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

        storage_client_name = f"story-kb-storage-{uuid.uuid4().hex[:6]}"
        register_storage(
            name=storage_client_name,
            url=minio_config["url"],
            bucket=minio_config["bucket"],
            access_key=minio_config["access_key"],
            secret_key=minio_config["secret_key"],
        )
        vector_client_name = f"story-kb-vector-{uuid.uuid4().hex[:6]}"
        register_vector(name=vector_client_name, qdrant_url=qdrant_url)

        async with maker() as session:
            kb = KBState(
                session=session,
                storage_client_name=storage_client_name,
                vector_client_name=vector_client_name,
                namespace=str(uuid.uuid4()),
            )
            yield kb

        await engine.dispose()

    @pytest_asyncio.fixture(scope="class", loop_scope="class")
    async def collection_tracker(self, state: KBState):
        """Class-scoped Qdrant cleanup. Records collection names created
        during the acts; deletes them after the last act runs."""
        created: list[tuple[str, str]] = []
        yield created
        from forktex_core.vector import get_client

        for client_name, coll in created:
            try:
                await get_client(client_name).collection(coll).delete()
            except Exception:
                pass

    # ── Act 1 ────────────────────────────────────────────────────────────

    async def test_act1_declare_space_with_two_member_grids(self, state: KBState):
        documents = await _declare(
            state.session,
            namespace=state.namespace,
            slug="documents",
            label="Documents",
            columns=[
                {"key": "title", "label": "Title", "type_id": FieldType.text.value},
                {
                    "key": "source",
                    "label": "Source",
                    "type_id": FILE_TYPE_ID,
                    "config": {"client_name": state.storage_client_name, "delete_on_archive": True},
                },
            ],
        )
        chunks = await _declare(
            state.session,
            namespace=state.namespace,
            slug="chunks",
            label="Chunks",
            columns=[
                {"key": "text", "label": "Text", "type_id": FieldType.text.value},
                {
                    "key": "embedding",
                    "label": "Embedding",
                    "type_id": VECTOR_TYPE_ID,
                    "config": {
                        "storage_mode": "remote",
                        "dimensions": 4,
                        "client_name": state.vector_client_name,
                    },
                },
            ],
        )
        space = await Bundle.declare(
            state.session,
            namespace=state.namespace,
            slug="kb",
            config=BundleConfig(vector=VectorDefaults(dimensions=4, storage_mode="remote")),
            members=[documents, chunks],
        )
        state.documents = documents
        state.chunks = chunks
        state.space = space
        state.collection_name = f"{state.namespace}--chunks--embedding"

        assert space.slug == "kb"
        assert {g.slug for g in space.grids.values()} == {"documents", "chunks"}

    # ── Act 2 ────────────────────────────────────────────────────────────

    async def test_act2_user_uploads_a_document(self, state: KBState):
        assert state.documents is not None, "act 1 must run first"
        from forktex_core.storage import get_client as get_storage

        storage = get_storage(state.storage_client_name)
        state.pdf_key = f"story-kb/{uuid.uuid4()}.pdf"
        state.pdf_bytes = b"%PDF-1.4 the cat sat on the mat. the dog ran in the park."
        await storage.upload(state.pdf_key, state.pdf_bytes, content_type="application/pdf")

        state.doc = await state.documents.create(
            {
                "title": "Animals & Places",
                "source": {
                    "storage_key": state.pdf_key,
                    "filename": "animals.pdf",
                    "content_type": "application/pdf",
                    "size": len(state.pdf_bytes),
                },
            }
        )
        await state.session.commit()
        assert state.doc.values["source"]["storage_key"] == state.pdf_key
        assert await storage.exists(state.pdf_key) is True

    # ── Act 3 ────────────────────────────────────────────────────────────

    async def test_act3_chunker_writes_chunks_with_remote_embeddings(
        self, state: KBState, collection_tracker: list[tuple[str, str]]
    ):
        assert state.chunks is not None and state.doc is not None
        # Orthogonal embeddings — clean retrieval targets per chunk.
        chunk_specs = [
            ("the cat sat on the mat", [1.0, 0.0, 0.0, 0.0]),
            ("the dog ran in the park", [0.0, 1.0, 0.0, 0.0]),
            ("birds sing in the morning", [0.0, 0.0, 1.0, 0.0]),
        ]
        for text, vec in chunk_specs:
            row = await state.chunks.create({"text": text, "embedding": vec})
            state.chunk_rows.append(row)
        # Track the collection the rich VECTOR handler created so the
        # finalizer cleans it up even on later-act failure.
        collection_tracker.append((state.vector_client_name, state.collection_name))

        # Cross-Grid edges: documents → chunks. on_delete=set_null so a child
        # chunk can be archived independently (the edge is just dropped).
        has_chunk = GridRelation(
            namespace=state.namespace,
            source_table_id=state.documents.ref.id,
            target_table_id=state.chunks.ref.id,
            key="has_chunk",
            relation_type=RelationShape.one_to_many,
            on_delete=OnDelete.set_null,
        )
        state.session.add(has_chunk)
        await state.session.flush()
        for chunk in state.chunk_rows:
            state.session.add(
                GridEdge(
                    namespace=state.namespace,
                    relation_id=has_chunk.id,
                    source_row_id=state.doc.id,
                    target_row_id=chunk.id,
                )
            )
        await state.session.commit()

        # Cell shape: vector stripped, back-refs stamped.
        cat_chunk = state.chunk_rows[0]
        assert "vector" not in cat_chunk.values["embedding"]
        assert cat_chunk.values["embedding"]["point_id"] == str(cat_chunk.id)
        assert cat_chunk.values["embedding"]["collection"] == state.collection_name

    # ── Act 4 ────────────────────────────────────────────────────────────

    async def test_act4_semantic_search_walks_back_to_parent_doc(self, state: KBState):
        assert state.chunk_rows, "act 3 must run first"
        from forktex_core.vector import get_client as get_vector

        vector_client = get_vector(state.vector_client_name)
        handle = vector_client.collection(state.collection_name)
        hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(1))
        assert len(hits) == 1
        cat_chunk = state.chunk_rows[0]
        assert str(hits[0].id) == str(cat_chunk.id)

        # Walk the cross-Grid edge backwards from the search hit.
        walk = await state.space.traverse(cat_chunk.id, max_depth=1, direction="in")
        parent_ids = [n.id for n in walk.nodes if n.attrs.get("entity_slug") == "documents"]
        assert parent_ids == [str(state.doc.id)]

        # Snapshot sanity.
        snapshot = await state.space.to_graph()
        docs = [n for n in snapshot.nodes if n.attrs["entity_slug"] == "documents"]
        chunks = [n for n in snapshot.nodes if n.attrs["entity_slug"] == "chunks"]
        assert len(docs) == 1
        assert len(chunks) == 3
        assert len(snapshot.edges) == 3
        assert all(e.kind == "has_chunk" for e in snapshot.edges)

    # ── Act 5 ────────────────────────────────────────────────────────────

    async def test_act5_archive_cascades_blob_and_vector_cleanup(self, state: KBState):
        from forktex_core.storage import get_client as get_storage
        from forktex_core.vector import get_client as get_vector

        storage = get_storage(state.storage_client_name)
        # No auto-cascade — the consumer archives children explicitly.
        for chunk in state.chunk_rows:
            await state.chunks.archive(chunk.id)
        await state.documents.archive(state.doc.id)
        await state.session.commit()

        # FILE handler deleted the blob.
        assert await storage.exists(state.pdf_key) is False

        # VECTOR handler tombstoned every point.
        vector_client = get_vector(state.vector_client_name)
        handle = vector_client.collection(state.collection_name)
        post_hits = await handle.search(SearchQuery(vector=[1.0, 0.0, 0.0, 0.0]).limit(5))
        surviving = {str(h.id) for h in post_hits}
        archived = {str(c.id) for c in state.chunk_rows}
        assert surviving.isdisjoint(archived)

        # Active queries are empty.
        assert (await state.documents.query()).rows == []
        assert (await state.chunks.query()).rows == []
