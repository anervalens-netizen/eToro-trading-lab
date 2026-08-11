from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

BASE_URL = "https://public-api.etoro.com"
DEMO_OPEN_BY_AMOUNT = "/api/v1/trading/execution/demo/market-open-orders/by-amount"
DEMO_CLOSE_PREFIX = "/api/v1/trading/execution/demo/market-close-orders/positions/"

_READ_ALLOWLIST = (
    re.compile(r"^/api/v1/me$"),
    re.compile(r"^/api/v1/market-data/instruments/rates$"),
    re.compile(r"^/api/v1/market-data/search$"),
    re.compile(r"^/api/v1/trading/info/demo/(?:pnl|portfolio|aggregate-portfolio)$"),
)


@dataclass(frozen=True)
class ApiResponse:
    status_code: int
    body: Any
    request_id: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(frozen=True)
class DemoCashTruth:
    credit_usd: Decimal
    available_cash_usd: Decimal
    pending_manual_orders_usd: Decimal
    pending_orders_usd: Decimal
    snapshot_hash: str
    observed_at: datetime


class EtoroPublicApiDemoClientV2:
    """Pinned direct Public API adapter with DEMO-only writes.

    REAL execution routes are intentionally not representable by this class.
    """

    def __init__(self, base_url: str = BASE_URL, timeout_seconds: int = 20) -> None:
        if base_url != BASE_URL:
            raise ValueError("eToro API egress is pinned to the official public-api host")
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _credential(name: str) -> str:
        file_path = os.getenv(f"{name}_FILE")
        direct = os.getenv(name)
        if file_path and direct:
            raise RuntimeError(f"{name} and {name}_FILE cannot both be configured")
        value = (
            Path(file_path).read_text(encoding="utf-8").strip()
            if file_path
            else (direct or "").strip()
        )
        if not value:
            raise RuntimeError(f"{name} credential is unavailable")
        return value

    def _headers(self, request_id: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "etoro-trading-lab-v2/1.0",
            "x-request-id": request_id,
            "x-api-key": self._credential("ETORO_API_KEY"),
            "x-user-key": self._credential("ETORO_USER_KEY"),
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ApiResponse:
        normalized = method.upper()
        read_allowed = normalized == "GET" and any(
            pattern.fullmatch(path) for pattern in _READ_ALLOWLIST
        )
        write_allowed = normalized == "POST" and (
            path == DEMO_OPEN_BY_AMOUNT
            or re.fullmatch(
                r"/api/v1/trading/execution/demo/market-close-orders/positions/[1-9]\d*", path
            )
        )
        if not read_allowed and not write_allowed:
            raise PermissionError("path/method is outside the DEMO-only Public API allowlist")
        req_id = request_id or str(uuid.uuid4())
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers=self._headers(req_id),
            method=normalized,
        )
        try:
            # Base URL and request paths are fixed allowlists above.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("eToro API response exceeded two-megabyte cap")
                parsed = json.loads(raw.decode("utf-8")) if raw else None
                return ApiResponse(response.status, parsed, req_id)
        except urllib.error.HTTPError as exc:
            raw = exc.read(2_000_001)
            if len(raw) > 2_000_000:
                raw = b""
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else None
            except json.JSONDecodeError:
                parsed = None
            return ApiResponse(exc.code, parsed, req_id)

    def rates(self, instrument_ids: tuple[int, ...]) -> ApiResponse:
        if not instrument_ids or any(value <= 0 for value in instrument_ids):
            raise ValueError("instrument ids must be positive")
        return self._request(
            "GET",
            "/api/v1/market-data/instruments/rates",
            query={"instrumentIds": ",".join(str(value) for value in instrument_ids)},
        )

    def demo_pnl(self) -> ApiResponse:
        return self._request("GET", "/api/v1/trading/info/demo/pnl")

    def demo_portfolio(self) -> ApiResponse:
        return self._request("GET", "/api/v1/trading/info/demo/portfolio")

    def open_by_amount(
        self,
        *,
        instrument_id: int,
        amount_usd: Decimal,
        is_buy: bool,
        leverage: int = 1,
        request_id: str,
        stop_loss_rate: Decimal | None = None,
        take_profit_rate: Decimal | None = None,
    ) -> ApiResponse:
        if instrument_id <= 0 or amount_usd <= 0 or leverage < 1:
            raise ValueError("open order arguments are invalid")
        body: dict[str, Any] = {
            "InstrumentId": instrument_id,
            "Amount": float(amount_usd),
            "Leverage": leverage,
            "IsBuy": bool(is_buy),
        }
        if stop_loss_rate is not None:
            if stop_loss_rate <= 0:
                raise ValueError("stop loss rate must be positive")
            body["StopLossRate"] = float(stop_loss_rate)
        if take_profit_rate is not None:
            if take_profit_rate <= 0:
                raise ValueError("take profit rate must be positive")
            body["TakeProfitRate"] = float(take_profit_rate)
        return self._request("POST", DEMO_OPEN_BY_AMOUNT, body=body, request_id=request_id)

    def close_position(
        self,
        *,
        position_id: int,
        units_to_deduct: Decimal | None,
        request_id: str,
    ) -> ApiResponse:
        if position_id <= 0 or (units_to_deduct is not None and units_to_deduct <= 0):
            raise ValueError("close order arguments are invalid")
        return self._request(
            "POST",
            f"{DEMO_CLOSE_PREFIX}{position_id}",
            body={"UnitsToDeduct": None if units_to_deduct is None else float(units_to_deduct)},
            request_id=request_id,
        )

    def cash_truth(self) -> DemoCashTruth:
        response = self.demo_pnl()
        if not response.ok or not isinstance(response.body, dict):
            raise RuntimeError("DEMO P&L snapshot unavailable")
        portfolio = response.body.get("clientPortfolio", response.body)
        if not isinstance(portfolio, dict):
            raise ValueError("DEMO P&L payload shape is invalid")
        credit = Decimal(str(portfolio.get("credit", "0")))
        orders_for_open = portfolio.get("ordersForOpen", [])
        orders = portfolio.get("orders", [])
        if not isinstance(orders_for_open, list) or not isinstance(orders, list):
            raise ValueError("DEMO pending orders shape is invalid")
        manual = sum(
            (
                Decimal(str(item.get("amount", 0)))
                for item in orders_for_open
                if isinstance(item, dict)
                and int(item.get("mirrorID", item.get("mirrorId", 0)) or 0) == 0
            ),
            Decimal("0"),
        )
        pending = sum(
            (Decimal(str(item.get("amount", 0))) for item in orders if isinstance(item, dict)),
            Decimal("0"),
        )
        available = credit - manual - pending
        canonical = json.dumps(portfolio, sort_keys=True, separators=(",", ":"), default=str)
        import hashlib

        return DemoCashTruth(
            credit,
            max(Decimal("0"), available),
            manual,
            pending,
            hashlib.sha256(canonical.encode()).hexdigest(),
            datetime.now(UTC),
        )
