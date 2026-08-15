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

"""Rich FieldType handlers shipped with the ``[space]`` extra.

Importing this module **replaces** the bare ``[grid]`` FILE handler
with the descriptor + lifecycle-hook variant that auto-cleans blobs
on row archive. The replacement is global to the process; for tests
that need the bare handler, swap directly via ``_TYPES[FILE] = ...``.

The VECTOR handler ships in a follow-up phase.
"""

# Side-effect imports: register the rich FILE + VECTOR handlers.
from forktex_core.space.types import file as _file  # noqa: F401
from forktex_core.space.types import vector as _vector  # noqa: F401

__all__: list[str] = []
