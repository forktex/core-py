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

"""Minimal ``[api]`` consumer — ``create_app`` with one route + one probe.

Builds a tiny FastAPI app via ``create_app(AppConfig)``, mounts a happy-path
route and an error-path route, then exercises both through ``TestClient`` so
the example runs without binding a real port. Requires no infra.
Run with ``python examples/api_minimal.py``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from forktex_core.api import AppConfig, create_app
from forktex_core.error import NotFoundError


def build_app():
    async def db_ready() -> bool:
        return True

    app = create_app(
        AppConfig(
            title="Demo Service",
            version="0.1.0",
            description="Minimal example over [api].",
            readiness_probes={"db": db_ready},
        )
    )

    @app.get("/widgets/{widget_id}")
    async def get_widget(widget_id: str) -> dict:
        if widget_id == "missing":
            raise NotFoundError(
                f"Widget {widget_id!r} not found",
                details={"widget_id": widget_id},
            )
        return {"widget_id": widget_id, "color": "blue"}

    return app


def main() -> None:
    client = TestClient(build_app())

    # ── happy-path GET ────────────────────────────────────────────────
    happy = client.get("/widgets/abc")
    print(f"happy_status:           {happy.status_code}")
    print(f"happy_body:             {happy.json()}")
    print(f"X-Request-ID present:   {bool(happy.headers.get('X-Request-ID'))}")
    print(f"X-Frame-Options:        {happy.headers.get('X-Frame-Options')}")
    assert happy.status_code == 200
    assert happy.headers.get("X-Request-ID")
    assert happy.headers.get("X-Frame-Options") == "DENY"

    # ── error-path: AppError → ErrorEnvelope ─────────────────────────
    not_found = client.get("/widgets/missing")
    print(f"not_found_status:       {not_found.status_code}")
    print(f"not_found_envelope:     {not_found.json()}")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "not_found"

    # ── probes ────────────────────────────────────────────────────────
    print(f"/health:                {client.get('/health').status_code}")
    print(f"/health/ready:          {client.get('/health/ready').json()}")
    assert client.get("/health").status_code == 200


if __name__ == "__main__":
    main()
