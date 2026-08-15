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

"""Tests for forktex_core.vault — pure crypto (no container) + SQLAlchemy integration."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

pytest.importorskip("cryptography", reason="cryptography not installed")

from forktex_core.database import BaseDBModel, TimestampMixin
from forktex_core.vault import EncryptedJSON, Vault

KEK = "super-secret-key-for-testing-only"
KEK2 = "another-secret-key-for-rotation"

_vault_for_model = Vault(kek=KEK)


class _VaultSecret(BaseDBModel, TimestampMixin):
    """Module-level model so SQLAlchemy can resolve Mapped[] annotations."""

    __tablename__ = "vault_secret_test"

    id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    payload: Mapped[bytes | None] = mapped_column(EncryptedJSON(_vault_for_model), nullable=True)


# ---------------------------------------------------------------------------
# Pure crypto tests (no container needed)
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip():
    vault = Vault(kek=KEK)
    data = {"api_key": "sk-abc", "org_id": "org-xyz", "nested": {"a": 1}}
    blob = vault.encrypt(data)
    assert isinstance(blob, bytes)
    assert vault.decrypt(blob) == data


def test_encrypt_empty_dict():
    vault = Vault(kek=KEK)
    assert vault.decrypt(vault.encrypt({})) == {}


def test_encrypt_unicode_values():
    vault = Vault(kek=KEK)
    data = {"msg": "Ünïcödé héré 🎉", "lang": "ro"}
    assert vault.decrypt(vault.encrypt(data)) == data


def test_different_keks_produce_different_blobs():
    blob1 = Vault(kek=KEK).encrypt({"secret": "value"})
    blob2 = Vault(kek=KEK2).encrypt({"secret": "value"})
    assert blob1 != blob2


def test_same_kek_produces_different_blobs_each_call():
    # Fernet uses a fresh IV per encrypt — two encryptions of the same data differ
    vault = Vault(kek=KEK)
    data = {"x": 1}
    assert vault.encrypt(data) != vault.encrypt(data)


def test_wrong_kek_raises_on_decrypt():
    from cryptography.fernet import InvalidToken

    blob = Vault(kek=KEK).encrypt({"x": 1})
    with pytest.raises(InvalidToken):
        Vault(kek=KEK2).decrypt(blob)


def test_tampered_blob_raises():
    from cryptography.fernet import InvalidToken

    vault = Vault(kek=KEK)
    blob = bytearray(vault.encrypt({"x": 1}))
    blob[10] ^= 0xFF  # flip bits
    with pytest.raises(InvalidToken):
        vault.decrypt(bytes(blob))


def test_rotate_kek_roundtrip():
    vault = Vault(kek=KEK)
    data = {"credentials": "token-abc"}
    blob = vault.encrypt(data)
    new_blob = vault.rotate_kek(KEK2, blob)

    assert Vault(kek=KEK2).decrypt(new_blob) == data

    from cryptography.fernet import InvalidToken

    with pytest.raises(InvalidToken):
        Vault(kek=KEK2).decrypt(blob)  # old blob invalid with new KEK


def test_rotate_kek_bytes_form():
    vault = Vault(kek=KEK)
    data = {"k": "v"}
    blob = vault.encrypt(data)
    new_blob = vault.rotate_kek(KEK2.encode(), blob)
    assert Vault(kek=KEK2).decrypt(new_blob) == data


def test_kek_bytes_input():
    vault = Vault(kek=KEK.encode())
    data = {"key": "val"}
    assert vault.decrypt(vault.encrypt(data)) == data


def test_kek_length_independence():
    # Short and long KEKs both work (SHA-256 normalises them)
    for kek in ["a", "x" * 128, "中文キー"]:
        v = Vault(kek=kek)
        d = {"k": kek}
        assert v.decrypt(v.encrypt(d)) == d


def test_empty_string_kek_still_works():
    v = Vault(kek="")
    assert v.decrypt(v.encrypt({"k": "v"})) == {"k": "v"}


def test_repr_does_not_leak_key_material():
    """The default dataclass-style repr of the underlying Fernet object
    embeds the derived key in cleartext — Vault must override __repr__ so
    a stray log/traceback/error-reporter capture never exposes it."""
    v = Vault(kek=KEK)
    assert KEK not in repr(v)
    assert "signing_key" not in repr(v).lower()
    assert "encryption_key" not in repr(v).lower()


def test_non_json_native_values_are_stringified_not_rejected():
    """encrypt() uses json.dumps(..., default=str) — matches the same
    fallback convention used by forktex_core.cache.serialize(). Non-native
    values (e.g. raw bytes) round-trip as their str() form, not as their
    original type — callers must pre-serialize anything that needs exact
    round-tripping."""
    vault = Vault(kek=KEK)
    data = {"raw": b"\x00\x01binary"}
    blob = vault.encrypt(data)
    assert vault.decrypt(blob) == {"raw": str(b"\x00\x01binary")}


# ---------------------------------------------------------------------------
# SQLAlchemy EncryptedJSON column integration (requires Postgres container)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encrypted_json_column_roundtrip(postgres_url_str: str, fresh_schema: str):
    """EncryptedJSON must encrypt on flush and decrypt transparently on load."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {None: fresh_schema, "forktex_grid": fresh_schema}},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    secret_data = {"api_key": "sk-test", "org_id": "org-1", "score": 42}

    async with maker() as session:
        row = _VaultSecret(payload=secret_data)
        session.add(row)
        await session.commit()
        row_id = row.id

    async with maker() as session:
        loaded = await session.get(_VaultSecret, row_id)
        assert loaded is not None
        assert loaded.payload == secret_data

    await engine.dispose()


@pytest.mark.asyncio
async def test_encrypted_json_wrong_vault_on_load_raises(postgres_url_str: str, fresh_schema: str):
    """Loading a row through an EncryptedJSON column bound to the WRONG
    Vault must raise InvalidToken on SELECT, not silently return garbage
    or None — a decrypt failure must never look like a missing value."""
    from cryptography.fernet import InvalidToken
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {None: fresh_schema, "forktex_grid": fresh_schema}},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        row = _VaultSecret(payload={"api_key": "sk-wrong-vault-test"})
        session.add(row)
        await session.commit()
        row_id = row.id

    # A second mapping of the same table, bound to a DIFFERENT vault.
    wrong_vault = Vault(kek=KEK2)

    class _Base(DeclarativeBase):
        pass

    class _VaultSecretWrongVault(_Base):
        __tablename__ = "vault_secret_test"
        id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), primary_key=True)
        payload: Mapped[bytes | None] = mapped_column(EncryptedJSON(wrong_vault), nullable=True)

    async with maker() as session:
        with pytest.raises(InvalidToken):
            await session.get(_VaultSecretWrongVault, row_id)

    await engine.dispose()


@pytest.mark.asyncio
async def test_encrypted_json_null_roundtrip(postgres_url_str: str, fresh_schema: str):
    """Null payload must survive flush/load without decryption attempt."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(
        postgres_url_str,
        execution_options={"schema_translate_map": {None: fresh_schema, "forktex_grid": fresh_schema}},
    )
    async with engine.begin() as conn:
        await conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{fresh_schema}"'))
        await conn.run_sync(BaseDBModel.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

    async with maker() as session:
        row = _VaultSecret(payload=None)
        session.add(row)
        await session.commit()
        row_id = row.id

    async with maker() as session:
        loaded = await session.get(_VaultSecret, row_id)
        assert loaded.payload is None

    await engine.dispose()
