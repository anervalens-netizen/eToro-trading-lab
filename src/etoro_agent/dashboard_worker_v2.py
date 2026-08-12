from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
from typing import Any

from .dashboard_v2 import (
    DashboardServiceV2,
    PostgresDashboardServiceV2,
    create_v2_app,
)


def build_app(
    runtime: str,
    config: str,
    *,
    postgres_dsn_file: str = "",
) -> Any:
    from fastapi import Request
    from fastapi.responses import JSONResponse

    service: DashboardServiceV2 | PostgresDashboardServiceV2
    if postgres_dsn_file:
        dsn = Path(postgres_dsn_file).read_text(encoding="utf-8").strip()
        if not dsn:
            raise RuntimeError("v2 dashboard PostgreSQL DSN is empty")
        service = PostgresDashboardServiceV2(dsn, config)
    else:
        service = DashboardServiceV2(runtime, config)
    app = create_v2_app(service)
    owner = os.getenv("ETORO_DASHBOARD_OWNER", "").strip()
    trusted_proxy = os.getenv("ETORO_TRUSTED_PROXY_IP", "").strip()
    secret_file = os.getenv("ETORO_PROXY_SECRET_FILE", "").strip()
    boundary_secret = Path(secret_file).read_text(encoding="utf-8").strip() if secret_file else ""
    if not owner or not boundary_secret:
        raise RuntimeError("v2 dashboard requires owner identity and proxy boundary secret")

    @app.middleware("http")
    async def owner_boundary(request: Request, call_next: Any) -> Any:
        client = request.client.host if request.client else ""
        if (
            request.url.path != "/healthz"
            and trusted_proxy
            and not hmac.compare_digest(client, trusted_proxy)
        ):
            response = JSONResponse({"detail": "untrusted proxy"}, status_code=403)
        elif request.url.path != "/healthz" and not hmac.compare_digest(
            request.headers.get("x-etoro-proxy-secret", ""), boundary_secret
        ):
            response = JSONResponse(
                {"detail": "proxy boundary authentication failed"}, status_code=403
            )
        elif request.url.path != "/healthz" and not hmac.compare_digest(
            request.headers.get("x-authentik-username", ""), owner
        ):
            response = JSONResponse({"detail": "owner identity is not authorized"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Owner-only eToro Trading Lab v2 dashboard")
    parser.add_argument("--runtime", default="/var/lib/etoro-agent/v2.sqlite3")
    parser.add_argument("--postgres-dsn-file", default="")
    parser.add_argument("--config", default="config/v2-demo.json")
    parser.add_argument("--uds", default="/run/etoro-agent/v2-dashboard.sock")
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(
        build_app(
            args.runtime,
            args.config,
            postgres_dsn_file=args.postgres_dsn_file,
        ),
        uds=args.uds,
        access_log=False,
    )


if __name__ == "__main__":
    main()
