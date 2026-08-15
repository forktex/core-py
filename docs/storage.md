# `forktex_core.storage` — S3/MinIO object storage

> Thin async S3/MinIO connector — upload, download, delete, presigned URLs, browser form uploads. Multi-bucket services register named clients; single-bucket services use module-level functions.

## Overview

`storage` has no path conventions and no content negotiation — those are interface-adapter concerns in the consuming service. The module is a pure connector: it speaks S3, handles auth, and generates presigned URLs. The presigned URL **is** the access token — the actor presents it directly to MinIO, no additional auth header needed.

There is no object-listing API (no `list_objects`/pagination) and no S3 chunked "Multipart Upload" support (`create_multipart_upload`/`upload_part`) — `upload()` is always a single `put_object` call. Both are out of scope for this thin connector today.

```bash
pip install forktex-core[storage]   # aioboto3
```

## Quick start

```python
import forktex_core.storage as storage

# Single-bucket service
await storage.init(url="http://minio:9000", bucket="docs", access_key="key", secret_key="secret")

await storage.upload("invoices/2026/01/abc.pdf", pdf_bytes, content_type="application/pdf")
url = await storage.presign("invoices/2026/01/abc.pdf", expires_in=3600)
data = await storage.download("invoices/2026/01/abc.pdf")
await storage.close()
```

## API reference

```python
# --- Single-bucket (module-level, "default" client) ---
async def init(url, bucket, access_key, secret_key, *,
               region="us-east-1", public_url=None) -> None
async def close(name="default") -> None
async def upload(key, data, *, content_type="application/octet-stream") -> None
async def download(key) -> bytes                    # raises ObjectNotFoundError
async def delete(key) -> None
async def exists(key) -> bool
async def presign(key, expires_in=3600, *, method="get_object",
                  content_type=None, response_content_disposition=None) -> str
async def presign_post(key, *, expires_in=3600,
                       content_type=None, max_size_bytes=None) -> dict

# --- Multi-bucket (named clients) ---
def register(name, url, bucket, access_key, secret_key, *,
             region="us-east-1", public_url=None) -> StorageClient
def get_client(name="default") -> StorageClient     # raises ClientNotRegisteredError

# --- StorageClient (per-bucket) ---
class StorageClient:
    async def upload(key, data, *, content_type) -> None
    async def download(key) -> bytes
    async def delete(key) -> None
    async def exists(key) -> bool
    async def presign(key, expires_in=3600, *, method, content_type,
                      response_content_disposition) -> str
    async def presign_post(key, *, expires_in, content_type, max_size_bytes) -> dict
    async def ensure_bucket(*, public_read=False) -> None
    def direct_url(key) -> str    # only for public-read buckets

# --- Config ---
@dataclass
class StorageConfig:
    url: str; bucket: str; access_key: str; secret_key: str
    region: str = "us-east-1"; public_url: str | None = None
```

## Patterns

### Pattern 1 — Multi-bucket service

```python
from forktex_core.storage import register, get_client

# At startup
register("media", url=s3_url, bucket="news-media", **creds, public_url=pub_url)
register("messaging", url=s3_url, bucket="messaging", **creds, public_url=pub_url)
register("data-lake", url=s3_url, bucket="data-lake", **creds, public_url=pub_url)

# Usage — caller picks the right bucket
await get_client("messaging").upload(key, data, content_type="application/pdf")
url = await get_client("messaging").presign(key, expires_in=3600)
```

### Pattern 2 — Secured actor callback (presigned PUT)

```python
# Backend generates short-lived PUT URL — the S3 signature IS the access token
# Actor PUTs directly to MinIO; no JWT or other header needed
put_url = await client.presign(
    f"uploads/org-{org_id}/photo.jpg",
    expires_in=900,  # 15 minutes
    method="put_object",
    content_type="image/jpeg",  # MinIO enforces this during PUT
)

# Return put_url to the actor; actor does:
#   PUT {put_url}  with Content-Type: image/jpeg  and body = file bytes
```

### Pattern 3 — Browser file upload (presigned POST)

`multipart/form-data` here is the HTTP encoding of the browser's POST body
(an ordinary `<form>` upload) — unrelated to S3's own chunked "Multipart
Upload" API, which this module does not implement.

```python
policy = await client.presign_post(
    f"uploads/org-{org_id}/doc.pdf",
    expires_in=600,
    content_type="application/pdf",
    max_size_bytes=10 * 1024 * 1024,  # 10 MB limit enforced by MinIO
)
# policy = {"url": "http://...", "fields": {"key": ..., "Content-Type": ..., ...}}
# Browser: POST policy["url"] as multipart/form-data with policy["fields"] + file
```

### Pattern 4 — Public bucket direct URL

```python
# Only use direct_url() when bucket has public-read policy
media_client = get_client("media")
await media_client.ensure_bucket(public_read=True)  # sets S3 bucket policy

direct = media_client.direct_url("images/hero.jpg")
# → "http://cdn.example.com/news-media/images/hero.jpg"
```

## Anti-patterns

```python
# ❌ String-matching exception messages — brittle if SDK changes
if "NoSuchKey" in str(exc):
    raise ObjectNotFoundError(key)

# ✅ botocore.exceptions.ClientError with its structured error Code
# (module handles this internally — download()/exists()/ensure_bucket()
# all classify on exc.response["Error"]["Code"], never string-match)

# ❌ Storing credentials in plain text in code
StorageConfig(access_key="minioadmin", secret_key="minioadmin")  # only for dev

# ✅ Load from environment
StorageConfig(access_key=os.environ["S3_ACCESS_KEY"], ...)

# ❌ Using direct_url() for private buckets — returns unsigned URL
media_client.direct_url("private/secret.pdf")  # 403 when accessed

# ✅ Use presign() for private bucket objects
await media_client.presign("private/secret.pdf", expires_in=3600)
```

---

## Agent guide

### Canonical forms

**Startup registration (multi-bucket):**
```python
from forktex_core.storage import register

register(
    "media",
    url=settings.s3_endpoint,
    bucket=settings.s3_bucket_media,
    access_key=settings.s3_access_key,
    secret_key=settings.s3_secret_key,
    public_url=settings.s3_public_endpoint,  # for presigned URLs
)
```

**The presigned URL flow (secured callback):**
```
Client → POST /api/files/upload-url   (JWT auth)
Backend → verifies auth
Backend → await client.presign(key, method="put_object", content_type="image/jpeg", expires_in=900)
Backend → returns {"upload_url": "...", "key": "..."}
Client → PUT upload_url  (no auth header — signature is in the URL)
Client → POST /api/files/confirm  {"key": "..."}  (tells backend upload is done)
```

**presign_post response shape:**
```python
result = await client.presign_post("uploads/doc.pdf", content_type="application/pdf")
# result = {
#     "url": "http://minio:9000/my-bucket",
#     "fields": {
#         "key": "uploads/doc.pdf",
#         "Content-Type": "application/pdf",
#         "AWSAccessKeyId": "...",
#         "policy": "...",
#         "signature": "...",
#     }
# }
# Browser FormData: append each field, then append file as "file" field
```

### Edge cases

| Scenario | Behaviour |
|---|---|
| `download(missing_key)` | `ObjectNotFoundError(key)` |
| `exists(missing_key)` | Returns `False` |
| `delete(missing_key)` | No-op (S3 delete is idempotent) |
| `get_client("unregistered")` | `ClientNotRegisteredError` with list of registered names |
| `presign(key, method="put_object", content_type="image/jpeg")` | Includes `ContentType` in signature — MinIO enforces it during PUT |
| `public_url` not set | Presigned URLs use internal `url` — may not be browser-reachable from outside Docker |
| `ensure_bucket()` when the bucket already exists | No-op, no policy change unless `public_read=True` |
| `ensure_bucket()` — `head_bucket` fails with 403 (permission denied) | Raises `StorageError`, does **not** attempt `create_bucket` — only a genuine 404/`NoSuchBucket` triggers creation |
| `direct_url(key)` — key contains spaces/`#`/`?`/etc. | URL-encoded automatically (`urllib.parse.quote`) |

### Error catalogue

| Error | When |
|---|---|
| `ImportError("Install 'forktex-core[storage]' (aioboto3) to use forktex_core.storage")` | `aioboto3` not installed |
| `ObjectNotFoundError(key)` | `download()` or `exists()` — key doesn't exist |
| `ClientNotRegisteredError` | `get_client(name)` — name was never `register()`ed |
| `StorageError("storage download failed")` / `StorageError("storage existence check failed")` | Any other S3/network error. The message is **fixed**: a botocore message can quote request ids, ARNs and headers, so the detail is logged (with the AWS error code) and reaches the caller only via `__cause__`. |
| the driver error, unwrapped | `ensure_bucket()` on a non-404 `head_bucket` failure — a 403 must not be reinterpreted as "let's create it". Logged before it propagates. |

Every mutation and failure path is logged: client register/deregister, bucket
creation, object upload (`debug`) and delete (`info`), a public-read policy
application (`warning` — it makes the bucket world-readable), and each error path
with the bucket, key and AWS code.

### Integration map

```
storage ──── (no dep on) ─── db, cache, flow, vault, queue, vector, data, log
```

### Checklist

- [ ] `public_url` set when internal Docker URL differs from browser-reachable URL
- [ ] `register()` called at startup before any route handles requests
- [ ] `method="put_object"` + `content_type=` used for actor upload callbacks
- [ ] `presign_post()` used for browser form uploads (not `presign(method="put_object")`)
- [ ] `direct_url()` used only for public-read buckets
- [ ] `ensure_bucket(public_read=True)` called before `direct_url()` in staging/dev
