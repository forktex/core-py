# forktex_core.vault

Fernet-based symmetric encryption (AES-128-CBC + HMAC-SHA256) for credential dicts at rest, plus
an `EncryptedJSON` SQLAlchemy column type that encrypts on flush and decrypts on load.

## Install

```bash
pip install forktex-core[vault]   # cryptography
```

The `cryptography.fernet` import is deferred to `Vault.__init__`, so a missing dependency raises
**when you construct `Vault(kek)`** — not at import time. Note the message names the raw package,
not the extra:

```
ImportError: Install 'cryptography' to use forktex_core.vault
```

`sqlalchemy` is imported at module level (it is a core dependency), so `import forktex_core.vault`
itself always succeeds.

## Wiring

**Shape C — consumer-owned object, no global state.** There is no registry and no `init()`. Build
one `Vault` at startup from an environment variable and pass it where it is needed; the same
instance must be handed to every `EncryptedJSON` column so loads decrypt with the key that wrote
them.

```python
import os

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from forktex_core.database import BaseDBModel
from forktex_core.vault import EncryptedJSON, Vault

vault = Vault(kek=os.environ["FORKTEX_KEK"])


class Provider(BaseDBModel):
    __tablename__ = "provider"
    id: Mapped[sa.Uuid] = mapped_column(sa.Uuid, primary_key=True)
    credentials: Mapped[dict | None] = mapped_column(EncryptedJSON(vault), nullable=True)
```

Because the model definition captures the `Vault` at class-definition time, a process that imports
these models needs the KEK in its environment — API and worker alike.

## Public surface

```python
from forktex_core.vault import EncryptedJSON, Vault
```

| Name | Description |
|---|---|
| `Vault(kek)` | Wraps a Fernet key derived from `kek` (`str` or `bytes`) via SHA-256, so any length works. |
| `Vault.encrypt(data: dict) -> bytes` | `json.dumps(default=str)` then Fernet-encrypt. |
| `Vault.decrypt(blob: bytes) -> dict` | Fernet-decrypt then `json.loads`. |
| `Vault.rotate_kek(new_kek, blob) -> bytes` | Decrypt with the current KEK, re-encrypt under `new_kek`. |
| `EncryptedJSON(vault)` | SQLAlchemy `TypeDecorator` over `LargeBinary` (BYTEA in Postgres). `cache_ok = True`. `None` passes through unencrypted in both directions. |

`Vault.__repr__` returns `Vault(<redacted>)` — the default Fernet repr embeds the derived key in
cleartext and would otherwise reach logs, tracebacks and error reporters that capture locals.

## Errors

| Raised | When | Catch? |
|---|---|---|
| `ImportError("Install 'cryptography' to use forktex_core.vault")` | `Vault(kek)` without `cryptography`. | No — install it. |
| `cryptography.fernet.InvalidToken` | `decrypt()` (and therefore `rotate_kek()` and any `EncryptedJSON` load) with the wrong KEK or a tampered/corrupt blob. | Only at an operational boundary. |

`InvalidToken` is passed through deliberately rather than wrapped in an `AppError` — the same
exception means "wrong KEK" and "tampered ciphertext", and only an operator can distinguish them.
Collapsing it into a status code would hide that. `decrypt()` logs the failure with the blob length
only — never the plaintext, blob or key.

## Gotchas

- **No envelope encryption.** There is no data-encryption-key wrapped by the KEK, so `rotate_kek`
  fully decrypts and re-encrypts each payload. It is O(1) per call but a full rotation is a bulk
  migration over every encrypted row, not a key swap.
- **Bulk rotation must bypass the ORM.** `EncryptedJSON` is bound to one `Vault` and always decrypts
  on load, so a rotation script has to read and write the raw bytes through Core:

  ```python
  import sqlalchemy as sa

  rows = (await session.execute(sa.text("SELECT id, credentials FROM provider"))).all()
  for provider_id, raw_blob in rows:
      if raw_blob is None:
          continue
      await session.execute(
          sa.text("UPDATE provider SET credentials = :blob WHERE id = :id"),
          {"blob": old_vault.rotate_kek(new_kek, raw_blob), "id": provider_id},
      )
  ```

- **`encrypt` only accepts a dict.** It is typed `dict[str, Any]` and always JSON-serialises; there
  is no bytes-in/bytes-out path.
- **`default=str` is lossy.** Anything JSON cannot represent — `datetime`, `Decimal`, `UUID` — is
  stringified on encrypt and comes back as a string. Round-trips are not type-preserving.
- **Ciphertext is non-deterministic** (fresh IV per call), so an encrypted column cannot be indexed,
  compared, or used in a `WHERE` clause.
- **The KEK is not stretched.** Derivation is a single SHA-256, not a KDF — the KEK must itself be
  high-entropy material, not a password.
- **`rotate_kek` logs an audit line** at `info` level on every re-wrap (blob length only).
- **A mis-keyed deployment fails on SELECT**, not at startup: nothing validates the KEK until the
  first row is decrypted.
