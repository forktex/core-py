# Changelog

All notable changes to `forktex-core` are documented here. This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

 - TODO

## [0.1.0] — 2026-08-08

Clean baseline after a full reset. `forktex-core` is a layered, pick-and-choose Python substrate for ForkTex services. The architecture is described, once, in [`catalog/catalog.json`](src/forktex_core/catalog/catalog.json) and rendered into the README.

### Modules

- **Level 0 — primitives:** `log` (structured JSON logging + trace-id contextvar), `error` (`AppError` hierarchy + `ErrorEnvelope` + `to_envelope`), `types` (`BaseAppModel`, `BaseValueObject`, `BaseWireValueObject`).
- **Level 1 — role facades:** `database` (async Postgres/SQLAlchemy — CRUD, advisory locks, migrations), `cache` (Redis), `queue` (arq), `vector` (Qdrant), `storage` (S3/MinIO), `vault` (Fernet + `EncryptedJSON`), `graph` (in-memory typed multi-edge algebra).
- **Level 2 — substrate facades:** `grid` (runtime, tenant-defined tabular schema engine over Postgres — the flagship), `space` (multi-grid bundle + `VECTOR`/`FILE` field types + cross-grid traversal), `flow` (Postgres-native durable workflow execution).
- **Level 3 — bootstraps:** `api` (FastAPI factory), `worker` (arq worker bootstrap).

### Notes

- Pre-1.0: the public API is not yet a stability commitment — minor versions may break until `1.0.0`.
- Dual-licensed: AGPL-3.0-or-later OR LicenseRef-ForkTex-Commercial.
