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

"""Flow's persistence layer: the ORM tables and one module per aggregate.

`models` declares the tables on `substrate_base("forktex_flow")` — its own `MetaData`, so a
consumer's `BaseDBModel.metadata.create_all()` never tries to build flow's schema.
`migrations` owns the forward-only SQL. The aggregate modules (`runs`, `steps`, `signals`,
`definitions`) hold the reads and writes for one table each; `mappers` turns a row into a
domain value object.

This layer emits **no raw SQL**. Every statement is a SQLAlchemy construct, which is also
what lets `schema_translate_map` rewrite the schema per Flow instance for free.
"""
