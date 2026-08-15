# `forktex_core.vault` — Symmetric encryption at rest

> Fernet-based (AES-128-CBC + HMAC-SHA256) encryption for credential blobs, with an `EncryptedJSON` SQLAlchemy column type and KEK rotation.

## Overview

`vault` protects credential dicts stored in Postgres. The `Vault` class wraps a KEK (key-encryption-key) and provides encrypt/decrypt/rotate. `EncryptedJSON` is a SQLAlchemy `TypeDecorator` that transparently encrypts on flush and decrypts on load.

```bash
pip install forktex-core[vault]   # cryptography
```

## Quick start

```python
import os
from forktex_core.vault import Vault, EncryptedJSON
from forktex_core.database import BaseDBModel, TimestampMixin
from sqlalchemy.orm import Mapped, mapped_column
import sqlalchemy as sa, uuid

vault = Vault(kek=os.environ["FORKTEX_KEK"])


class Provider(BaseDBModel, TimestampMixin):
    __tablename__ = "provider"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    credentials: Mapped[bytes | None] = mapped_column(EncryptedJSON(vault), nullable=True)


# Write
provider.credentials = {"api_key": "sk-abc", "secret": "xyz"}
session.add(provider)
await session.commit()

# Read — automatically decrypted
print(provider.credentials["api_key"])  # "sk-abc"
```

## API reference

```python
class Vault:
    def __init__(self, kek: str | bytes)
        # Any length — SHA-256 normalises to Fernet-compatible key
    def encrypt(self, data: dict[str, Any]) -> bytes
    def decrypt(self, blob: bytes) -> dict[str, Any]
    def rotate_kek(self, new_kek: str | bytes, blob: bytes) -> bytes
        # Decrypt with current KEK, re-encrypt with new_kek

class EncryptedJSON(TypeDecorator):
    impl = sa.LargeBinary    # stored as BYTEA in Postgres
    cache_ok = True
    def __init__(self, vault: Vault)
    # process_bind_param: dict → encrypt → bytes (on flush)
    # process_result_value: bytes → decrypt → dict (on load)
    # None values pass through unchanged
```

## Patterns

### Pattern 1 — KEK from environment

```python
vault = Vault(kek=os.environ["FORKTEX_KEK"])
# FORKTEX_KEK is any string (32+ chars recommended)
# SHA-256 derivation makes length irrelevant
```

### Pattern 2 — KEK rotation (full re-encrypt, not envelope encryption)

There's no separate data-encryption-key wrapped by the KEK — `rotate_kek`
fully decrypts the payload with the old key and re-encrypts it with the
new one. Cost is O(data volume), not O(1); budget for it like any other
bulk migration.

```python
old_vault = Vault(kek=os.environ["FORKTEX_KEK_OLD"])

# Rotate a single blob (decrypt old, encrypt new)
new_blob = old_vault.rotate_kek(os.environ["FORKTEX_KEK_NEW"], old_blob)

# Bulk rotation — run as a migration script. Must bypass the ORM
# TypeDecorator (it's bound to one Vault instance and always decrypts on
# load) — read/write the raw encrypted bytes via Core instead.
import sqlalchemy as sa

rows = (await session.execute(sa.text("SELECT id, credentials FROM provider"))).all()
for provider_id, raw_blob in rows:
    if raw_blob is None:
        continue
    new_blob = old_vault.rotate_kek(os.environ["FORKTEX_KEK_NEW"], raw_blob)
    await session.execute(
        sa.text("UPDATE provider SET credentials = :blob WHERE id = :id"),
        {"blob": new_blob, "id": provider_id},
    )
await session.commit()
```

## Anti-patterns

```python
# ❌ Storing KEK in code
vault = Vault(kek="hardcoded-secret")

# ✅ From environment only
vault = Vault(kek=os.environ["FORKTEX_KEK"])

# ❌ Decrypting with wrong KEK — raises cryptography.fernet.InvalidToken
wrong_vault = Vault(kek="other-kek")
wrong_vault.decrypt(blob_encrypted_with_different_key)
```

---

## Agent guide

### Edge cases

| Scenario | Behaviour |
|---|---|
| `EncryptedJSON` — `None` value | Passes through as `None` — no encryption call |
| Same data encrypted twice | Different ciphertext each time (fresh IV) — correct |
| `rotate_kek` — new blob decryptable with old KEK? | No — old KEK can't decrypt new blob |
| `kek` as `bytes` | Works — SHA-256 of bytes used |
| `EncryptedJSON` with wrong vault on load | `cryptography.fernet.InvalidToken` during SELECT |

### Error catalogue

| Error | When |
|---|---|
| `ImportError("Install 'cryptography' to use forktex_core.vault")` | `cryptography` not installed |
| `cryptography.fernet.InvalidToken` | Wrong KEK used for decrypt, or tampered blob. Deliberately **not** wrapped in an `AppError`: the same exception means both, and only an operator can tell which — collapsing it into a status code would hide that. `Vault.decrypt` logs the failure (blob length only, never plaintext, blob or key) so a mis-keyed deployment is visible in the logs and not just as a 500. A KEK rotation logs an audit line for the same reason. |

### Integration map

```
vault ──── (used via) ──── database   [EncryptedJSON is a SQLAlchemy TypeDecorator]
vault ──── (no dep on) ── cache, queue, vector, storage, graph
```

### Checklist

- [ ] `Vault` constructed once at startup from an env var, reused across the process
- [ ] KEK never hardcoded, logged, or included in error messages/exceptions
- [ ] `rotate_kek` treated as a bulk migration (full re-encrypt), not an O(1) key-swap
- [ ] Bulk rotation reads/writes raw bytes via Core (`sa.text`), not through the ORM-mapped `EncryptedJSON` column
