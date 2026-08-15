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

"""The agentic ops surface — an agent drives the grid entirely through ``run(space, op, args)``.

Every op validates JSON args into a Pydantic model and calls the same ``Namespace``/``Grid`` a system
consumer uses (one implementation). ``tool_schemas()`` gives the agent each tool's JSON-Schema.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from forktex_core.grid.errors import BadRequestError
from forktex_core.grid.ops import TOOLS, run, tool_schemas
from forktex_core.grid.namespace import Namespace

NS = "agent"

PEOPLE = {
    "namespace": NS,
    "tables": {
        "people": {
            "slug": "people",
            "label": "People",
            "columns": [
                {"key": "name", "label": "Name", "type_id": "text", "is_required": True},
                {"key": "age", "label": "Age", "type_id": "integer"},
            ],
        }
    },
}


def test_tool_schemas_are_json_and_cover_every_tool() -> None:
    schemas = tool_schemas()
    json.dumps(schemas)  # every tool contract is plain JSON-Schema
    assert set(schemas) == set(TOOLS)
    assert {"describe_schema", "apply_schema", "query", "insert", "patch", "archive"} <= set(schemas)
    # the alias is honoured so an agent sends {"schema": {...}}, not schema_doc
    assert "schema" in schemas["apply_schema"]["properties"]


async def test_agent_drives_grid_end_to_end_via_run(session: AsyncSession) -> None:
    space = Namespace(session, NS)

    plan = await run(space, "apply_schema", {"schema": PEOPLE, "prune": True})
    assert plan["plan"]["changes"] and plan["dry_run"] is False

    described = await run(space, "describe_schema", {})
    assert set(described["tables"]) == {"people"}

    inserted = await run(
        space, "insert", {"table": "people", "rows": [{"name": "Ann", "age": 30}, {"name": "Bob", "age": 25}]}
    )
    assert len(inserted["rows"]) == 2

    got = await run(space, "query", {"table": "people", "filter": {"column": "age", "op": "gte", "value": 30}})
    assert [r["values"]["name"] for r in got["rows"]] == ["Ann"]


async def test_run_parity_with_direct_facade(session: AsyncSession) -> None:
    # the agentic path bottoms out in the same Namespace core as a system consumer
    a = await run(
        Namespace(session, "viarun"), "apply_schema", {"schema": {**PEOPLE, "namespace": "viarun"}, "prune": True}
    )
    b = await Namespace(session, "viadirect").apply({**PEOPLE, "namespace": "viadirect"}, prune=True)
    assert [c["op"] for c in a["plan"]["changes"]] == [c["op"] for c in b["plan"]["changes"]]


async def test_unknown_op_is_rejected(session: AsyncSession) -> None:
    with pytest.raises(BadRequestError, match="unknown grid op"):
        await run(Namespace(session, NS), "nope", {})
