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

"""Symmetric encryption for credential blobs at rest.

Fernet-based (AES-128-CBC + HMAC-SHA256). There's no envelope encryption —
no separate data-encryption-key wrapped by the KEK — so KEK rotation fully
decrypts each payload with the old key and re-encrypts it with the new one.
``rotate_kek`` is O(1) per call, but a full rotation is a bulk migration
over every encrypted row, not an O(1) key-swap.

    vault = Vault(kek=os.environ["FORKTEX_KEK"])
    blob = vault.encrypt({"api_key": "sk-..."})   # → bytes
    data = vault.decrypt(blob)                     # → {"api_key": "sk-..."}

    # Rotate to a new master key (returns re-wrapped blob)
    new_blob = vault.rotate_kek(new_kek, blob)

    # Column type for SQLAlchemy models
    class Provider(BaseDBModel, AuditMixin):
        __tablename__ = "provider"
        credentials: Mapped[bytes] = mapped_column(EncryptedJSON(vault))

Requires: pip install cryptography
"""

import base64
import hashlib
import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy import TypeDecorator
from sqlalchemy.engine import Dialect

from forktex_core.log import get_logger

logger = get_logger(__name__)


def _derive_fernet_key(kek: str | bytes) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary-length KEK."""
    if isinstance(kek, str):
        kek = kek.encode()
    digest = hashlib.sha256(kek).digest()
    return base64.urlsafe_b64encode(digest)


class Vault:
    """Fernet-based symmetric encryption wrapper.

    A single ``Vault`` instance is typically created at app startup from an
    env var and reused across the process lifetime.

    Args:
        kek: Key-encryption-key. Any str/bytes; a Fernet key is derived via
             SHA-256 so length is not constrained.
    """

    def __init__(self, kek: str | bytes) -> None:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise ImportError("Install 'cryptography' to use forktex_core.vault") from exc
        self._fernet = Fernet(_derive_fernet_key(kek))

    def __repr__(self) -> str:
        # Never show _fernet — its default repr embeds the derived key in
        # cleartext, which would otherwise leak into logs/tracebacks/error
        # reporters that capture local variables (e.g. Sentry breadcrumbs).
        return f"{self.__class__.__name__}(<redacted>)"

    def encrypt(self, data: dict[str, Any]) -> bytes:
        """Serialize ``data`` to JSON and encrypt. Returns opaque bytes."""
        plaintext = json.dumps(data, default=str).encode()
        return self._fernet.encrypt(plaintext)

    def decrypt(self, blob: bytes) -> dict[str, Any]:
        """Decrypt ``blob`` and deserialize to dict.

        A failure raises ``cryptography.fernet.InvalidToken`` unchanged rather
        than a wrapped ``AppError``: the same exception means "wrong KEK" and
        "tampered ciphertext", and only the operator can tell which. Collapsing
        it into a status code would hide that, so the distinction is left to the
        log line below and the exception is passed through — a deliberate
        exception to this library's "everything is an ``AppError``" rule.

        Nothing about the plaintext, the blob, or the key is logged.
        """
        try:
            plaintext = self._fernet.decrypt(blob)
        except Exception:
            logger.error(
                "vault: decryption failed — wrong KEK or tampered ciphertext",
                extra={"blob_bytes": len(blob)},
            )
            raise
        return json.loads(plaintext)

    def rotate_kek(self, new_kek: str | bytes, blob: bytes) -> bytes:
        """Decrypt with current KEK, re-encrypt with ``new_kek``."""
        from cryptography.fernet import Fernet

        data = self.decrypt(blob)
        new_fernet = Fernet(_derive_fernet_key(new_kek))
        # A KEK rotation is a security-relevant event and there is no envelope
        # layer to make it cheap, so each re-wrap is worth an audit line.
        logger.info("vault: re-wrapping blob under a new KEK", extra={"blob_bytes": len(blob)})
        return new_fernet.encrypt(json.dumps(data, default=str).encode())


class EncryptedJSON(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts/decrypts dicts.

    Store as BYTEA in Postgres; encrypt on flush, decrypt on load.

    Usage::

        vault = Vault(kek=os.environ["FORKTEX_KEK"])

        class Provider(BaseDBModel):
            __tablename__ = "provider"
            credentials: Mapped[bytes] = mapped_column(EncryptedJSON(vault))

        # Write
        provider.credentials = {"api_key": "sk-..."}

        # Read — automatically decrypted
        print(provider.credentials["api_key"])
    """

    impl = sa.LargeBinary
    cache_ok = True

    def __init__(self, vault: Vault) -> None:
        super().__init__()
        self._vault = vault

    def process_bind_param(self, value: dict | None, dialect: Dialect) -> bytes | None:
        if value is None:
            return None
        return self._vault.encrypt(value)

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> dict | None:
        if value is None:
            return None
        # Decryption happens per row on load; `Vault.decrypt` logs the failure so
        # a mis-keyed deployment is visible in the logs and not just in a 500.
        return self._vault.decrypt(value)


__all__ = ["EncryptedJSON", "Vault"]
