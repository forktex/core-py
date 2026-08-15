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

"""PostgreSQL async primitives — the foundation the substrate packages build on.

Connection handling, base models, CRUD, migrations, locks, identifier
validation, constraint-error translation, reflection, and DDL constructs.
``grid``/``flow``/``space`` are expected to reuse these rather than
reimplementing them.

``ddl`` and ``reflect`` are imported as modules rather than flattened into this
namespace, because names like ``CreateTable``/``columns`` read better qualified::

    from forktex_core.database import ddl, reflect

    await conn.execute(ddl.AddColumn(col, if_not_exists=True))
    present = await reflect.columns(session, "my_table", schema="public")
"""

from forktex_core.database import ddl, reflect
from forktex_core.database.connection import (
    Database,
    DatabaseNotInitializedError,
    close_engine,
    get_session,
    init_engine,
    session_scope,
    with_transactional_session,
)
from forktex_core.database.crud import (
    ConflictError,
    PageResponse,
    ScrollResponse,
    create,
    find_one_by,
    get,
    list_all,
    paginate,
    paginate_scroll,
)
from forktex_core.database.identifiers import (
    is_identifier,
    validate_identifier,
    validate_relation,
    validate_schema,
    validate_slug,
)
from forktex_core.database.integrity import integrity_boundary, read_boundary
from forktex_core.database.locks import (
    advisory_key,
    advisory_lock,
    key_from_uuid,
    try_advisory_lock,
    xact_lock,
)
from forktex_core.database.migrate import SchemaMigrationRunner
from forktex_core.database.models import (
    AuditMixin,
    BaseDBModel,
    JsonModelColumn,
    NamespacedMixin,
    ReprMixin,
    TimestampMixin,
    UtcDateTime,
    substrate_base,
)
from forktex_core.database.pagination import (
    Page,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
)

__all__ = [
    "AuditMixin",
    "BaseDBModel",
    "ConflictError",
    "Database",
    "DatabaseNotInitializedError",
    "JsonModelColumn",
    "NamespacedMixin",
    "Page",
    "PageResponse",
    "ReprMixin",
    "SchemaMigrationRunner",
    "ScrollResponse",
    "TimestampMixin",
    "UtcDateTime",
    "advisory_key",
    "advisory_lock",
    "close_engine",
    "create",
    "ddl",
    "decode_cursor",
    "encode_cursor",
    "find_one_by",
    "get",
    "get_session",
    "init_engine",
    "integrity_boundary",
    "is_identifier",
    "key_from_uuid",
    "keyset_predicate",
    "list_all",
    "paginate",
    "paginate_scroll",
    "read_boundary",
    "reflect",
    "session_scope",
    "substrate_base",
    "try_advisory_lock",
    "validate_identifier",
    "validate_relation",
    "validate_schema",
    "validate_slug",
    "with_transactional_session",
    "xact_lock",
]
