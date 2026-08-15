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

"""Unit tests for forktex_core.database.identifiers — no container required."""

from __future__ import annotations

import pytest

from forktex_core.database.identifiers import (
    MAX_IDENT,
    is_identifier,
    validate_identifier,
    validate_relation,
    validate_schema,
    validate_slug,
)
from forktex_core.error import BadRequestError


@pytest.mark.parametrize("name", ["a", "_a", "Name", "camelCase", "with_1_digits", "A" * MAX_IDENT])
def test_valid_identifiers(name):
    validate_identifier(name)  # must not raise


@pytest.mark.parametrize(
    "name",
    [
        "",  # empty
        "1leading_digit",
        "has-hyphen",
        "has space",
        'quote"inside',
        "semi;colon",
        "A" * (MAX_IDENT + 1),  # too long
    ],
)
def test_invalid_identifiers_raise_badrequest(name):
    with pytest.raises(BadRequestError):
        validate_identifier(name)


def test_identifier_profile_allows_mixed_case_but_schema_profile_does_not():
    """The two profiles exist precisely because they disagree: grid quotes every
    identifier it emits (so mixed case is fine), while a physical schema name
    follows Postgres's unquoted-folding rule and stays lower-case."""
    validate_identifier("MixedCase")  # fine
    with pytest.raises(BadRequestError):
        validate_schema("MixedCase")
    validate_schema("lower_case_1")  # fine


@pytest.mark.parametrize("slug", ["people", "my-table", "a_b-c", "0start"])
def test_valid_slugs(slug):
    validate_slug(slug)


@pytest.mark.parametrize("slug", ["", "-leading-hyphen", "has space", "A" * (MAX_IDENT + 1)])
def test_invalid_slugs(slug):
    with pytest.raises(BadRequestError):
        validate_slug(slug)


@pytest.mark.parametrize("relation", ["table", "schema.table", "S.T"])
def test_valid_relations(relation):
    validate_relation(relation)


@pytest.mark.parametrize(
    "relation",
    ["", ".", "a.", ".b", "a.b.c", "a b.c", 'a."b'],
)
def test_invalid_relations(relation):
    with pytest.raises(BadRequestError):
        validate_relation(relation)


def test_is_identifier_is_the_predicate_form():
    """A predicate is required where a bad name must be *skipped* rather than
    raised on — grid's sidecar reconciler drops columns whose names predate
    current validation."""
    assert is_identifier("ok_name") is True
    assert is_identifier("has space") is False
    assert is_identifier("") is False
    assert is_identifier("A" * (MAX_IDENT + 1)) is False
