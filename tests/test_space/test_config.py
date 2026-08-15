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

"""Tests for Bundle config records (no DB needed)."""

from __future__ import annotations

import pytest

from forktex_core.space import (
    BundleConfig,
    StorageDefaults,
    SyncSourceConfig,
    VectorDefaults,
)


def test_default_space_config_is_empty_but_well_formed():
    config = BundleConfig()
    assert config.vector.storage_mode == "remote"
    assert config.vector.dimensions is None
    assert config.storage.bucket is None
    assert config.edge_vocab == ()


def test_space_config_round_trip_through_json():
    cfg = BundleConfig(
        vector=VectorDefaults(model="bge-small", dimensions=384, storage_mode="both"),
        storage=StorageDefaults(bucket="rich-content", prefix="leads/"),
        edge_vocab=("contains", "depends_on"),
    )
    payload = cfg.model_dump()
    restored = BundleConfig.model_validate(payload)
    assert restored == cfg


def test_value_objects_are_frozen():
    vd = VectorDefaults(model="x")
    with pytest.raises(Exception):
        vd.model = "y"  # type: ignore[misc]


def test_sync_source_config_holds_opaque_options():
    src = SyncSourceConfig(
        kind="intelligence:codebase",
        options={"root": "/tmp/repo", "ignore": [".git"]},
        schedule="0 * * * *",
    )
    payload = src.model_dump()
    restored = SyncSourceConfig.model_validate(payload)
    assert restored.kind == "intelligence:codebase"
    assert restored.options == {"root": "/tmp/repo", "ignore": [".git"]}
    assert restored.schedule == "0 * * * *"


def test_invalid_storage_mode_rejected():
    with pytest.raises(Exception):
        VectorDefaults(storage_mode="diagonal")  # type: ignore[arg-type]
