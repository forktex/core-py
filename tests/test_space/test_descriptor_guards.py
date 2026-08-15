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

"""Rich-descriptor fields come out of JSONB, so their shape is only conventional.

Both handlers read a descriptor dict written into `grid_row.payload` and then hand one of
its values to an external client. Those values were annotated `Any`, which meant nothing
checked them: `file` passed a possibly non-`str` `storage_key` to `client.delete()`, and
`vector` passed a possibly non-list `vector` to `create(dim=len(...))` — so a string
payload would have sized a Qdrant collection by character count.

Typing the boundary as `JsonValue` surfaced both. These tests cover the guards that fix
them, because a type checker proves the code is *consistent*, not that the runtime
behaviour is right.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.domain.fieldtypes.base import WriteContext

from forktex_core.space.types.vector import _as_embedding


class TestAsEmbedding:
    """`vector`'s coercion: a float vector, or `None` for anything that is not one."""

    def test_a_list_of_numbers_becomes_floats(self):
        assert _as_embedding([1, 2.5, 3]) == [1.0, 2.5, 3.0]

    def test_an_empty_list_is_a_valid_zero_length_vector(self):
        assert _as_embedding([]) == []

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("0.1,0.2,0.3", id="string"),
            pytest.param({"vector": [1.0]}, id="dict"),
            pytest.param(1.0, id="bare-float"),
            pytest.param(None, id="none"),
        ],
    )
    def test_a_non_list_is_rejected(self, value: object):
        """The string case is the one that mattered: `len("0.1,0.2,0.3")` is 11, so the
        collection would have been created with 11 dimensions."""
        assert _as_embedding(value) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param([1.0, "2.0"], id="string-element"),
            pytest.param([1.0, None], id="null-element"),
            pytest.param([1.0, [2.0]], id="nested-list"),
            pytest.param([1.0, {"a": 2}], id="dict-element"),
        ],
    )
    def test_a_list_with_a_non_numeric_element_is_rejected(self, value: object):
        assert _as_embedding(value) is None  # type: ignore[arg-type]

    def test_booleans_are_rejected_even_though_bool_is_an_int(self):
        """`isinstance(True, int)` is True in Python, so a naive numeric check accepts
        `[True, False]` and stores it as `[1.0, 0.0]` — a vector nobody meant to write."""
        assert _as_embedding([True, False]) is None  # type: ignore[arg-type]
        assert _as_embedding([1.0, True]) is None  # type: ignore[arg-type]

    def test_the_result_is_float_typed_not_int(self):
        """Qdrant wants floats; ints would round-trip differently."""
        result = _as_embedding([1, 2])
        assert result is not None
        assert all(isinstance(v, float) for v in result)


class TestFileCleanupGuard:
    """`file`'s archive hook skips a descriptor whose `storage_key` is not a string,
    rather than passing it to `client.delete()`."""

    @pytest.mark.asyncio
    async def test_a_non_string_storage_key_is_skipped_and_logged(self, monkeypatch, caplog):
        import logging

        from forktex_core.space.types import file as file_module

        deleted: list[object] = []

        class _Client:
            async def delete(self, key: object) -> None:
                deleted.append(key)

        monkeypatch.setattr("forktex_core.storage.get_client", lambda _name: _Client())

        handler = file_module.RichFileType()
        config = file_module.FileConfig(client_name="default", delete_on_archive=True)
        contexts = [
            _ctx({"storage_key": 42}),  # the bug: an int would have reached delete()
            _ctx({"storage_key": ["a", "b"]}),
            _ctx({"storage_key": "objects/real-one"}),
        ]

        with caplog.at_level(logging.WARNING):
            await handler.on_rows_archived(contexts, config=config)

        assert deleted == ["objects/real-one"], "a non-string key reached the storage client"
        assert sum("storage_key is" in r.message for r in caplog.records) == 2

    @pytest.mark.asyncio
    async def test_a_missing_or_empty_storage_key_is_skipped_silently(self, monkeypatch):
        from forktex_core.space.types import file as file_module

        deleted: list[object] = []

        class _Client:
            async def delete(self, key: object) -> None:
                deleted.append(key)

        monkeypatch.setattr("forktex_core.storage.get_client", lambda _name: _Client())
        handler = file_module.RichFileType()
        config = file_module.FileConfig(client_name="default", delete_on_archive=True)

        await handler.on_rows_archived([_ctx({}), _ctx({"storage_key": ""}), _ctx(None)], config=config)
        assert deleted == []

    @pytest.mark.asyncio
    async def test_delete_on_archive_false_deletes_nothing(self, monkeypatch):
        from forktex_core.space.types import file as file_module

        called = False

        def _get_client(_name: str) -> object:
            nonlocal called
            called = True
            raise AssertionError("must not resolve a client when cleanup is off")

        monkeypatch.setattr("forktex_core.storage.get_client", _get_client)
        handler = file_module.RichFileType()
        config = file_module.FileConfig(client_name="default", delete_on_archive=False)

        await handler.on_rows_archived([_ctx({"storage_key": "k"})], config=config)
        assert called is False


def _ctx(before_value: object) -> WriteContext:
    """A write context carrying only what the archive guard reads.

    `session` is a bare `AsyncSession` over no engine: the guards under test run before any
    database access, and constructing a real one would make these tests need a container for
    code paths that never touch it.
    """
    return WriteContext(
        session=AsyncSession(),
        namespace="acme",
        table_id=uuid.uuid7(),
        table_slug="docs",
        column_key="source",
        row_id=uuid.uuid7(),
        before_value=before_value,  # type: ignore[arg-type]
        after_value=None,
    )
