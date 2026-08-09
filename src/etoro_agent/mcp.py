from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MCPResult:
    status_code: int
    is_success: bool
    body: Any
    x_request_id: str | None
    raw: dict[str, Any]


class EtoroMCPClient:
    _READ_PATHS = (
        re.compile(r"^/api/v1/me$"),
        re.compile(r"^/api/v1/market-data/(?:exchanges|instrument-types|instruments|instruments/rates|instruments/history/closing-price|search|stocks-industries)$"),
        re.compile(r"^/api/v1/market-data/instruments/\d+/history/candles/(?:asc|desc)/(?:OneMinute|FiveMinutes|TenMinutes|FifteenMinutes|ThirtyMinutes|OneHour|FourHours|OneDay|OneWeek)/\d+$"),
        re.compile(r"^/api/v1/trading/info/demo/(?:aggregate-portfolio|pnl|portfolio)$"),
        re.compile(r"^/api/v1/trading/info/demo/(?:close-orders|orders)/[^/]+$"),
        re.compile(r"^/api/v1/trading/info/trade/demo/history$"),
        re.compile(r"^/api/v2/trading/info/demo/(?:costs|eligibility)$"),
        re.compile(r"^/api/v2/trading/info/demo/orders:lookup$"),
        re.compile(r"^/api/v1/agent-portfolios$"),
        re.compile(r"^/api/v2/agent-portfolios/user-tokens/scopes$"),
    )

    def __init__(self, url: str = "https://mcp.public-api.etoro.com") -> None:
        self.url = url

    @staticmethod
    def _credential(name: str) -> str | None:
        """Read a credential from systemd LoadCredential or the environment.

        The file variant is preferred in production so secrets never need to be
        embedded in unit files or copied into the repository.
        """

        file_path = os.getenv(f"{name}_FILE")
        direct = os.getenv(name)
        if file_path and direct:
            raise RuntimeError(f"{name} and {name}_FILE must not be mixed")
        if file_path:
            try:
                value = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError(f"unable to read {name} credential file") from exc
            if not value:
                raise RuntimeError(f"{name} credential file is empty")
            return value
        return direct

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # Cloudflare rejects urllib's default browser signature. Use an
            # honest, stable client identifier for the official MCP endpoint.
            "User-Agent": "etoro-demo-agent/0.1 MCP-Client",
        }
        bearer = self._credential("ETORO_BEARER_TOKEN")
        user_key = self._credential("ETORO_USER_KEY")
        api_key = self._credential("ETORO_API_KEY")
        if bearer and (user_key or api_key):
            raise RuntimeError("OAuth and API-key authentication must not be mixed")
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        elif user_key:
            headers["x-user-key"] = user_key
            if api_key:
                headers["x-api-key"] = api_key
        return headers

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in {"execute-read", "execute-write"}:
            raise PermissionError("generic MCP discovery/tool execution is not exposed at runtime")
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        ).encode()
        request = urllib.request.Request(self.url, data=payload, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"MCP HTTP error {exc.code}") from exc
        data_lines = [line[6:] for line in text.splitlines() if line.startswith("data: ")]
        if not data_lines:
            raise RuntimeError("MCP response did not contain an SSE data event")
        envelope = json.loads(data_lines[-1])
        if "error" in envelope:
            raise RuntimeError(f"MCP tool error: {envelope['error']}")
        content = envelope["result"]["content"]
        return json.loads(content[0]["text"])

    def execute_read(self, path: str, query: dict[str, str] | None = None, body: str | None = None) -> MCPResult:
        if not any(pattern.fullmatch(path) for pattern in self._READ_PATHS):
            raise PermissionError("read path is not in the eToro gateway allowlist")
        if body is not None and path not in {
            "/api/v2/trading/info/demo/costs",
            "/api/v2/trading/info/demo/eligibility",
        }:
            raise PermissionError("request bodies are allowed only for DEMO read previews")
        arguments: dict[str, Any] = {"path": path}
        if query:
            arguments["query"] = query
        if body is not None:
            arguments["body"] = body
        raw = self._call_tool("execute-read", arguments)
        parsed_body = raw.get("body")
        if isinstance(parsed_body, str):
            try:
                parsed_body = json.loads(parsed_body)
            except json.JSONDecodeError:
                pass
        return MCPResult(int(raw.get("statusCode", 0)), bool(raw.get("isSuccess")), parsed_body, raw.get("xRequestId"), raw)

    def execute_demo_order(self, route: str, body_json: str, request_id: str) -> MCPResult:
        open_route = route == "/api/v2/trading/execution/demo/orders"
        close_route = re.fullmatch(
            r"/api/v1/trading/execution/demo/market-close-orders/positions/[1-9]\d*",
            route,
        )
        if not open_route and close_route is None:
            raise PermissionError("only fixed eToro DEMO open/close routes are allowed")
        if not request_id:
            raise ValueError("a stable xRequestId is required")
        body = json.loads(body_json)
        if close_route is not None and (
            not isinstance(body, dict)
            or frozenset(body) != frozenset({"InstrumentID", "UnitsToDeduct"})
            or int(body["InstrumentID"]) <= 0
            or (body["UnitsToDeduct"] is not None and Decimal(str(body["UnitsToDeduct"])) <= 0)
        ):
            raise PermissionError("invalid fixed DEMO close body")
        raw = self._call_tool(
            "execute-write",
            {
                "path": route,
                "method": "POST",
                "body": body_json,
                "xRequestId": request_id,
            },
        )
        parsed_body = raw.get("body")
        if isinstance(parsed_body, str):
            try:
                parsed_body = json.loads(parsed_body)
            except json.JSONDecodeError:
                pass
        return MCPResult(int(raw.get("statusCode", 0)), bool(raw.get("isSuccess")), parsed_body, raw.get("xRequestId"), raw)

    def _identity_with_scopes(self) -> tuple[dict[str, Any], set[str]]:
        result = self.execute_read("/api/v1/me")
        if not result.is_success or not isinstance(result.body, dict):
            raise PermissionError("eToro credentials are missing or invalid")
        scopes = set(result.body.get("scopes", []))
        return result.body, scopes

    def verify_demo_scope(self) -> dict[str, Any]:
        identity, scopes = self._identity_with_scopes()
        accepted = {
            "etoro-public:demo:read",
            "etoro-public:demo:write",
            "etoro-public:trade.demo:read",
            "etoro-public:trade.demo:write",
        }
        if scopes.isdisjoint(accepted):
            raise PermissionError("credentials have no DEMO scope")
        return identity

    def verify_isolated_demo_execution_scope(self) -> dict[str, Any]:
        """Require an environment-specific DEMO key with no REAL scope."""

        identity, scopes = self._identity_with_scopes()
        accepted_pairs = (
            {
                "etoro-public:trade.demo:read",
                "etoro-public:trade.demo:write",
            },
            {
                "etoro-public:demo:read",
                "etoro-public:demo:write",
            },
        )
        real = {
            "etoro-public:real:read",
            "etoro-public:real:write",
            "etoro-public:trade.real:read",
            "etoro-public:trade.real:write",
        }
        if not scopes.isdisjoint(real):
            raise PermissionError("isolated DEMO key must not carry any REAL scope")
        if not any(required.issubset(scopes) for required in accepted_pairs):
            raise PermissionError(
                "isolated DEMO key requires DEMO trade read and write"
            )
        return identity
