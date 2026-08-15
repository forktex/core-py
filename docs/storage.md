# forktex_core.storage

Thin async S3/MinIO connector for opaque binary objects: upload, download, delete, existence
checks, presigned GET/PUT URLs and presigned browser POST policies. No path conventions, no
content negotiation, no image processing — those belong in the consuming service.

## Install

```bash
pip install forktex-core[storage]   # aioboto3
```

The import is deferred to `StorageClient.__init__`, which `register()` (and therefore `init()`)
calls. So a missing extra raises **at `register()`/`init()`**, not at `import forktex_core.storage`:

```
ImportError: Install 'forktex-core[storage]' (aioboto3) to use forktex_core.storage
```

`botocore` (pulled in by `aioboto3`) is imported inside the methods that classify its errors.

## Wiring

**Shape B — named-client registry.** `register(name, ...)` builds a `StorageClient` and stores it
in a module dict; `get_client(name)` fetches it; `deregister(name)` drops it. `init(...)` is
`register("default", ...)`, and the module-level `upload`/`download`/`delete`/`exists`/`presign`/
`presign_post` functions all operate on `get_client()` — i.e. the `"default"` client.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from forktex_core.storage import deregister, register


@asynccontextmanager
async def lifespan(app: FastAPI):
    register(
        "media",
        url=settings.s3_endpoint,
        bucket=settings.s3_bucket_media,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
        public_url=settings.s3_public_endpoint,
    )
    yield
    deregister("media")


app = FastAPI(lifespan=lifespan)
```

The registry is a plain module-level dict and therefore **per-process**. A worker process must run
its own `register()` calls at startup; it does not inherit the API process's clients. The symptom
of forgetting is `ClientNotRegisteredError` on the first job, not at boot.

`register()` is idempotent by name — registering the same name again replaces the previous client.

## Public surface

```python
from forktex_core.storage import (
    ClientNotRegisteredError,
    ObjectNotFoundError,
    StorageClient,
    StorageConfig,
    StorageError,
    close,
    delete,
    deregister,
    download,
    exists,
    get_client,
    init,
    presign,
    presign_post,
    register,
    upload,
)
```

| Name | Description |
|---|---|
| `register(name, url, bucket, access_key, secret_key, *, region="us-east-1", public_url=None)` | Build and store a named client; returns it. |
| `get_client(name="default")` | Look up a registered client. |
| `deregister(name="default")` | Drop a name; returns the dropped client or `None`. Idempotent. |
| `init(...)` | Async. `register("default", ...)` with the same arguments. |
| `close(name="default")` | Async. Delegates to `deregister`; nothing is actually closed. |
| `upload(key, data, *, content_type=...)` | Async. `put_object` on the default client. Overwrites. |
| `download(key)` | Async. Returns `bytes`. |
| `delete(key)` | Async. `delete_object`; a missing key is a no-op. |
| `exists(key)` | Async. `head_object` → `bool`. |
| `presign(key, expires_in=3600, *, method="get_object", content_type=None, response_content_disposition=None)` | Async. Presigned URL. |
| `presign_post(key, *, expires_in=3600, content_type=None, max_size_bytes=None)` | Async. `{"url", "fields"}` for a browser form upload. |
| `StorageClient` | Per-bucket client. Same six operations plus `ensure_bucket(*, public_read=False)` and `direct_url(key)`. |
| `StorageConfig` | Frozen Pydantic value object: `url`, `bucket`, `access_key`, `secret_key`, `region`, `public_url`. |
| `StorageError` | Base error, `AppErrorCode.INTERNAL`. |
| `ObjectNotFoundError` | `AppErrorCode.NOT_FOUND`. |
| `ClientNotRegisteredError` | `AppErrorCode.INTERNAL`. |

Presigned PUT for an untrusted uploader — the S3 signature *is* the token, no extra auth header:

```python
put_url = await get_client("media").presign(
    f"uploads/org-{org_id}/photo.jpg",
    expires_in=900,
    method="put_object",
    content_type="image/jpeg",   # enforced by the server during the PUT
)
```

Browser form upload. The `multipart/form-data` here is the HTTP encoding of an ordinary `<form>`
POST — unrelated to S3's chunked Multipart Upload API, which this module does not implement:

```python
policy = await get_client("media").presign_post(
    f"uploads/org-{org_id}/doc.pdf",
    expires_in=600,
    content_type="application/pdf",
    max_size_bytes=10 * 1024 * 1024,
)
# policy["url"] + policy["fields"] → browser posts them alongside the file
```

## Errors

All three error classes subclass `AppError`, so an HTTP transport renders them with a real status
instead of a masked 500.

| Raised | When | Catch? |
|---|---|---|
| `ImportError` | `register()`/`init()` without `aioboto3`. | No — install the extra. |
| `ClientNotRegisteredError` | `get_client(name)` for an unregistered name. Message lists the registered names. | No — a wiring bug. |
| `ObjectNotFoundError(key)` | `download()` on a missing key. | Yes, when a missing object is expected. |
| `StorageError("storage download failed")` | `download()` on any other S3/network failure. | Yes, at a request boundary. |
| `StorageError("storage existence check failed")` | `exists()` on any non-404 failure. | Yes, at a request boundary. |
| `botocore.exceptions.ClientError`, unwrapped | `ensure_bucket()` when `head_bucket` fails with anything other than 404/`NoSuchBucket`. | Only if you call `ensure_bucket` outside startup. |

`StorageError` messages are deliberately fixed strings: a botocore message can quote request ids,
ARNs and headers, so the detail is logged (with the AWS error code) and reaches the caller only via
`__cause__`.

## Gotchas

- **`close()` closes nothing.** It just deregisters. Boto clients are created per call and closed
  by their own context manager, so there is no long-lived connection to release. The `async` shape
  exists for callers that already `await` it.
- **`exists()` returns `False` for a missing key** — it does not raise `ObjectNotFoundError`. Only
  `download()` does.
- **`upload()` overwrites silently.** There is no conditional-put or if-none-match option.
- **No listing API.** There is no `list_objects` and no pagination; keep your own index.
- **`public_url` matters for presigning.** Presigned URLs and `direct_url()` are generated against
  `public_url or url`; ordinary uploads/downloads always use `url`. If the internal Docker endpoint
  differs from the browser-reachable one and you leave `public_url` unset, the URLs you hand out
  will not resolve from outside the network.
- **`content_type` only enters the signature for `method="put_object"`**, and
  `response_content_disposition` only for `method="get_object"`; the other combination is dropped
  without warning.
- **`direct_url()` performs no auth check.** It is only correct against a bucket with a public-read
  policy. The key is URL-encoded with `urllib.parse.quote`.
- **`ensure_bucket(public_read=True)` makes the bucket world-readable** and logs at `warning` level
  when it applies the policy.
- **`ensure_bucket()` only creates on a genuine 404/`NoSuchBucket`.** A 403 from `head_bucket`
  propagates rather than being reinterpreted as "create it".
- **Credentials are `repr`-suppressed.** `StorageConfig.access_key`/`secret_key` use
  `Field(repr=False)` so they do not leak into logs or tracebacks — but `StorageConfig` is a frozen
  Pydantic value object, not a dataclass, so do not construct it positionally.
