from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from fastapi.responses import FileResponse
from starlette.routing import Mount

from etoro_agent.audit_anchor_v2 import AuditAnchorWriter
from etoro_agent.dashboard_v2 import DashboardServiceV2, _health_payload, create_v2_app
from etoro_agent.dashboard_worker_v2 import build_app
from etoro_agent.domain_v2 import DomainEvent
from etoro_agent.runtime_store_v2 import RuntimeStoreV2
from etoro_agent.signing_keys_v2 import generate_signing_keypair


async def _asgi_get(
    app: Any, path: str, headers: dict[str, str]
) -> tuple[int, dict[bytes, bytes], bytes]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 80),
    }
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return int(start["status"]), dict(start["headers"]), body


class V2DashboardHealthTests(unittest.TestCase):
    def test_v2_dashboard_serves_packaged_read_only_ui(self) -> None:
        app = create_v2_app(Mock())
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertTrue({"/", "/healthz", "/api/v2/snapshot", "/static"} <= paths)

        index_route = next(route for route in app.routes if getattr(route, "path", "") == "/")
        response = asyncio.run(index_route.endpoint())
        self.assertIsInstance(response, FileResponse)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(Path(response.path).name, "index.html")
        self.assertTrue(Path(response.path).is_file())

        static_route = next(
            route
            for route in app.routes
            if isinstance(route, Mount) and getattr(route, "path", "") == "/static"
        )
        self.assertEqual(
            Path(static_route.app.directory).resolve(),
            Path(response.path).resolve().parent,
        )

    def test_v2_dashboard_bundle_uses_only_read_only_v2_endpoints(self) -> None:
        static_root = Path(__file__).resolve().parents[1] / "src/etoro_agent/dashboard_static"
        html = (static_root / "index.html").read_text(encoding="utf-8")
        javascript = (static_root / "dashboard.js").read_text(encoding="utf-8")

        self.assertIn("/static/dashboard.js", html)
        self.assertIn("/api/v2/snapshot", javascript)
        self.assertIn("/healthz", javascript)
        self.assertIn('classList.toggle("hidden", executionEnabled)', javascript)
        for obsolete_or_write_path in (
            "/api/snapshot",
            "/api/control/",
            "/api/approvals/",
            'method: "POST"',
            "postJson",
            "42 isolated",
        ):
            self.assertNotIn(obsolete_or_write_path, html + javascript)

    def test_owner_boundary_serves_ui_and_static_assets_with_hardened_csp(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            secret_file = Path(folder) / "proxy-secret"
            secret_file.write_text("test-boundary-secret", encoding="utf-8")
            environment = {
                "ETORO_DASHBOARD_OWNER": "owner",
                "ETORO_TRUSTED_PROXY_IP": "127.0.0.1",
                "ETORO_PROXY_SECRET_FILE": str(secret_file),
            }
            with patch.dict(os.environ, environment):
                app = build_app("/unused.sqlite3", "config/v2-demo.json")

            trusted_headers = {
                "x-etoro-proxy-secret": "test-boundary-secret",
                "x-authentik-username": "owner",
            }
            status, response_headers, body = asyncio.run(_asgi_get(app, "/", trusted_headers))
            self.assertEqual(status, 200)
            self.assertIn(b"Trading Lab", body)
            csp = response_headers[b"content-security-policy"].decode()
            for directive in ("script-src 'self'", "style-src 'self'", "connect-src 'self'"):
                self.assertIn(directive, csp)

            status, _, body = asyncio.run(_asgi_get(app, "/static/dashboard.js", trusted_headers))
            self.assertEqual(status, 200)
            self.assertIn(b"/api/v2/snapshot", body)

            status, _, _ = asyncio.run(
                _asgi_get(app, "/", {**trusted_headers, "x-etoro-proxy-secret": "wrong"})
            )
            self.assertEqual(status, 403)

    def test_present_execution_gate_requires_every_execution_worker_heartbeat(self) -> None:
        now = datetime.now(UTC)
        heartbeats = {
            service: ("healthy", now, {})
            for service in (
                "v2-market",
                "v2-coordinator",
                "v2-reconciliation",
                "v2-role-apply",
                "v2-demo-executor",
                "v2-exit-manager",
            )
        }
        with patch("etoro_agent.dashboard_v2.execution_gate_present", return_value=True):
            health = _health_payload(
                trading_state="ACTIVE",
                heartbeats=heartbeats,
                oldest_outbox_at=None,
                oldest_unknown_at=None,
                oldest_reconciliation_at=None,
                dead_letters_total=0,
                dead_letters_recent=0,
                chain_valid=True,
                anchor_at=now,
            )
        self.assertIn("stale_heartbeats:v2-decision-apply", health["failures"])

    def test_signed_checkpoint_incremental_health_is_bounded_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / "runtime.sqlite3"
            backup = root / "backups"
            anchors = root / "anchors"
            offhost = root / "LAST_OFFHOST_OK"
            backup.mkdir()
            anchors.mkdir()
            now = datetime.now(UTC)
            store = RuntimeStoreV2(runtime)
            for service in (
                "v2-market",
                "v2-coordinator",
                "v2-reconciliation",
                "v2-role-apply",
                "v2-decision-shadow",
            ):
                store.heartbeat(
                    service,
                    "halted",
                    {"economic_drift": [], "real_money": False},
                    at=now,
                )
            store.append_event(
                DomainEvent(
                    "health-anchor-base",
                    "HealthTest",
                    4,
                    now,
                    now,
                    "health-anchor-base",
                    "",
                    "health",
                    {"safe": True},
                )
            )
            row = store.db.execute(
                "SELECT sequence,event_hash FROM v2_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence, head_hash = int(row[0]), str(row[1])

            private_key = root / "anchor.key"
            public_key = root / "anchor.pub"
            generate_signing_keypair(private_key, public_key)
            anchor = AuditAnchorWriter(private_key, anchors).anchor(head_hash, at=now)
            (anchors / "LATEST.json").write_text(
                json.dumps({**anchor.__dict__, "sequence": sequence}), encoding="utf-8"
            )
            (backup / "LAST_BACKUP_OK").touch()
            (backup / "LAST_RESTORE_DRILL_OK").touch()
            offhost.touch()

            store.append_event(
                DomainEvent(
                    "health-after-anchor",
                    "HealthTest",
                    4,
                    now,
                    now,
                    "health-after-anchor",
                    "",
                    "health",
                    {"safe": True},
                )
            )
            store.close()
            gate = root / "gate-absent"
            environment = {
                "ETORO_V2_ANCHOR_LATEST": str(anchors / "LATEST.json"),
                "ETORO_V2_ANCHOR_PUBLIC_KEY_FILE": str(public_key),
                "ETORO_V2_BACKUP_ROOT": str(backup),
                "ETORO_V2_OFFHOST_MARKER": str(offhost),
                "ETORO_V2_EXECUTION_GATE_FILE": str(gate),
            }
            service = DashboardServiceV2(runtime, "config/v2-demo.json")
            with patch.dict(os.environ, environment):
                health = service.health()
                self.assertEqual(health["status"], "locked")
                self.assertTrue(health["audit"]["incremental_chain_valid"])

                tampered = RuntimeStoreV2(runtime)
                tampered.db.execute(
                    "UPDATE v2_events SET canonical_body='{}' WHERE event_id='health-after-anchor'"
                )
                tampered.db.commit()
                tampered.close()
                health = service.health()
                self.assertEqual(health["status"], "error")
                self.assertIn("audit_chain_or_checkpoint_invalid", health["failures"])

    def test_historical_dead_letters_are_visible_without_blocking_locked_health(self) -> None:
        now = datetime.now(UTC)
        heartbeats = {
            service: ("healthy", now, {"economic_drift": []})
            for service in (
                "v2-market",
                "v2-coordinator",
                "v2-reconciliation",
                "v2-role-apply",
                "v2-decision-shadow",
            )
        }
        with (
            patch("etoro_agent.dashboard_v2.execution_gate_present", return_value=False),
            patch("etoro_agent.dashboard_v2._age_seconds", return_value=0.0),
        ):
            health = _health_payload(
                trading_state="LOCKED",
                heartbeats=heartbeats,
                oldest_outbox_at=None,
                oldest_unknown_at=None,
                oldest_reconciliation_at=None,
                dead_letters_total=4,
                dead_letters_recent=0,
                chain_valid=True,
                anchor_at=now,
            )
            self.assertEqual(health["status"], "locked")
            self.assertEqual(health["queue"]["dead_letters_total"], 4)
            recent = _health_payload(
                trading_state="LOCKED",
                heartbeats=heartbeats,
                oldest_outbox_at=None,
                oldest_unknown_at=None,
                oldest_reconciliation_at=None,
                dead_letters_total=5,
                dead_letters_recent=1,
                chain_valid=True,
                anchor_at=now,
            )
            self.assertEqual(recent["status"], "degraded")
            self.assertIn("recent_ai_dead_letters:1", recent["warnings"])

            service = Mock()
            service.health.return_value = recent
            app = create_v2_app(service)
            route = next(item for item in app.routes if getattr(item, "path", "") == "/healthz")
            response = asyncio.run(route.endpoint())
            self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
