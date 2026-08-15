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

"""Story-track scoped fixtures: state-space discipline.

Two finalisers run after every story-track test (PASS or FAIL):

  - ``_clients_snapshot`` (autouse) restores the
    ``forktex_core.storage._clients`` and ``forktex_core.vector._clients``
    module-level dicts to their pre-test contents. Tests register clients
    under per-test names; the snapshot prevents stray names from
    accumulating across the session.

  - ``qdrant_collection_tracker`` (opt-in) is a list test code appends
    ``(client_name, collection_name)`` tuples to. The fixture finalizer
    deletes each collection from Qdrant. Story acts that create
    collections via the rich VECTOR handler should append to this list
    so the Qdrant container stays clean even when an assertion mid-act
    aborts the test.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _clients_snapshot():
    from forktex_core import storage as _s
    from forktex_core import vector as _v

    pre_s = dict(_s._clients)
    pre_v = dict(_v._clients)
    try:
        yield
    finally:
        _s._clients.clear()
        _s._clients.update(pre_s)
        _v._clients.clear()
        _v._clients.update(pre_v)


@pytest_asyncio.fixture
async def qdrant_collection_tracker():
    created: list[tuple[str, str]] = []
    yield created
    from forktex_core.vector import get_client

    for client_name, coll in created:
        try:
            await get_client(client_name).collection(coll).delete()
        except Exception:
            # Best-effort cleanup; if the collection wasn't created or
            # the client is gone, move on. The snapshot fixture above
            # restores the client registry separately.
            pass
