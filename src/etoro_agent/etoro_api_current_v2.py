from __future__ import annotations

import hashlib
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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .rate_limit_v2 import ETORO_COST_PREVIEW, ETORO_READ, ETORO_WRITE_SHARED, RollingWindowLimiter

BASE_URL = "https://public-api.etoro.com"
DEMO_CREATE_ORDER = "/api/v2/trading/execution/demo/orders"
DEMO_CLOSE_PREFIX = "/api/v1/trading/execution/demo/market-close-orders/positions/"
DEMO_ELIGIBILITY = "/api/v2/trading/info/demo/eligibility"
DEMO_COSTS = "/api/v2/trading/info/demo/costs"
COST_PREVIEW_MAX_AGE_SECONDS = 120

INSTRUMENT_SYMBOL = {
    1: "EURUSD",
    27: "SPX500",
    28: "NSDQ100",
    1001: "AAPL",
    1111: "TSLA",
    100000: "BTC",
    100001: "ETH",
    17: "OIL",
    22: "NATGAS",
}


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


@dataclass(frozen=True)
class PreparedDemoOpenV2:
    body: Mapping[str, Any]
    entry_rate: Decimal
    total_cost_usd: Decimal
    cost_snapshot_hash: str


class EtoroPublicApiDemoClientV2:
    """Current eToro Public API gateway with DEMO-only write routes.

    It preserves the executor-facing v2 interface while translating opens to the
    current /api/v2/.../demo/orders endpoint and closes to the current position
    market-close endpoint. REAL execution paths are not representable.
    """

    def __init__(self, base_url: str = BASE_URL, timeout_seconds: int = 20) -> None:
        if base_url != BASE_URL:
            raise ValueError("eToro API egress is pinned to public-api.etoro.com")
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.read_limiter = RollingWindowLimiter(ETORO_READ)
        self.write_limiter = RollingWindowLimiter(ETORO_WRITE_SHARED)
        self.cost_limiter = RollingWindowLimiter(ETORO_COST_PREVIEW)

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
        cost_preview: bool = False,
    ) -> ApiResponse:
        method = method.upper()
        read_allowed = method == "GET" and path in {
            "/api/v1/me",
            "/api/v1/market-data/instruments/rates",
            "/api/v1/trading/info/demo/pnl",
            "/api/v1/trading/info/demo/portfolio",
        }
        preview_allowed = method == "POST" and path in {DEMO_ELIGIBILITY, DEMO_COSTS}
        write_allowed = method == "POST" and (
            path == DEMO_CREATE_ORDER
            or re.fullmatch(
                r"/api/v1/trading/execution/demo/market-close-orders/positions/[1-9]\d*", path
            )
        )
        if not (read_allowed or preview_allowed or write_allowed):
            raise PermissionError("path/method is outside the DEMO-only allowlist")
        if read_allowed:
            self.read_limiter.acquire()
        elif path == DEMO_COSTS:
            self.cost_limiter.acquire()
        else:
            self.write_limiter.acquire()
        req_id = request_id or str(uuid.uuid4())
        try:
            uuid.UUID(req_id)
        except ValueError as exc:
            raise ValueError("x-request-id must be a UUID") from exc
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        encoded = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(
            url, data=encoded, headers=self._headers(req_id), method=method
        )
        try:
            # Base URL and request paths are fixed allowlists above.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("eToro response exceeds two-megabyte cap")
                return ApiResponse(response.status, json.loads(raw) if raw else None, req_id)
        except urllib.error.HTTPError as exc:
            raw = exc.read(2_000_001)
            try:
                parsed = json.loads(raw) if raw and len(raw) <= 2_000_000 else None
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

    def verify_isolated_demo_execution_scope(self) -> Mapping[str, Any]:
        response = self._request("GET", "/api/v1/me")
        if not response.ok or not isinstance(response.body, Mapping):
            raise PermissionError("eToro execution credentials are missing or invalid")
        scopes = {str(value) for value in response.body.get("scopes", [])}
        accepted_pairs = (
            {"etoro-public:trade.demo:read", "etoro-public:trade.demo:write"},
            {"etoro-public:demo:read", "etoro-public:demo:write"},
        )
        real_scopes = {
            "etoro-public:real:read",
            "etoro-public:real:write",
            "etoro-public:trade.real:read",
            "etoro-public:trade.real:write",
        }
        if not scopes.isdisjoint(real_scopes):
            raise PermissionError("isolated DEMO key must not carry any REAL scope")
        if not any(required <= scopes for required in accepted_pairs):
            raise PermissionError("isolated DEMO key requires DEMO trade read and write")
        return response.body

    def eligibility(self, symbol: str) -> ApiResponse:
        return self._request(
            "POST",
            DEMO_ELIGIBILITY,
            body={"symbols": [symbol.upper()], "currency": "USD"},
        )

    def cost_preview(self, order_body: Mapping[str, Any]) -> ApiResponse:
        allowed = {
            "action",
            "transaction",
            "symbol",
            "instrumentId",
            "settlementType",
            "orderType",
            "leverage",
            "amount",
            "orderCurrency",
            "positionIds",
        }
        body = {
            key: value for key, value in order_body.items() if key in allowed and value is not None
        }
        return self._request("POST", DEMO_COSTS, body=body, cost_preview=True)

    @staticmethod
    def _select_configuration(
        eligibility: ApiResponse,
        *,
        symbol: str,
        amount_usd: Decimal,
        is_buy: bool,
        leverage: int,
    ) -> tuple[Mapping[str, Any], str]:
        body = eligibility.body if isinstance(eligibility.body, dict) else {}
        rows = body.get("eligibilities", [])
        if not eligibility.ok or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PermissionError("broker eligibility is unavailable")
        if str(body.get("currency", "USD")).upper() != "USD":
            raise PermissionError("broker eligibility currency is not USD")
        row = rows[0]
        if str(row.get("symbol", symbol)).upper() != symbol:
            raise PermissionError("broker eligibility symbol mismatch")
        if not bool(row.get("allowOpenPosition")):
            raise PermissionError("instrument is not currently eligible")
        if amount_usd * Decimal(leverage) < Decimal(str(row.get("minPositionExposure", "0"))):
            raise PermissionError("amount is below broker minimum exposure")
        direction = "long" if is_buy else "short"
        configs = [
            item
            for item in row.get("leverageConfigs", [])
            if isinstance(item, Mapping)
            and str(item.get("direction", "")).lower() == direction
            and leverage in [int(value) for value in item.get("leverageValues", [])]
            and bool(item.get("allowStopLossTakeProfit", False))
            and amount_usd >= Decimal(str(item.get("minPositionAmount", "0")))
        ]
        if not configs:
            raise PermissionError("no exact broker leverage/direction configuration")
        preferred = "real" if is_buy and symbol in {"AAPL", "TSLA", "BTC", "ETH"} else "cfd"
        config = next(
            (item for item in configs if str(item.get("settlementType", "")).lower() == preferred),
            None,
        )
        config = config or next(
            (item for item in configs if str(item.get("settlementType", "")).lower() == "cfd"),
            None,
        )
        config = config or (configs[0] if len(configs) == 1 else None)
        if config is None:
            raise PermissionError("broker settlement type is ambiguous")
        settlement = str(config["settlementType"]).lower()
        if settlement not in {"cfd", "real", "realfutures", "margintrade"}:
            raise PermissionError("broker settlement type is unsupported")
        canonical_settlement = {
            "realfutures": "realFutures",
            "margintrade": "marginTrade",
        }.get(settlement, settlement)
        return config, canonical_settlement

    @staticmethod
    def _validate_stop_take(
        config: Mapping[str, Any],
        entry: Decimal,
        stop: Decimal,
        take: Decimal,
        *,
        is_buy: bool,
    ) -> None:
        if is_buy and not stop < entry < take:
            raise PermissionError("long order requires stop < entry < take-profit")
        if not is_buy and not take < entry < stop:
            raise PermissionError("short order requires take-profit < entry < stop")
        stop_pct = abs(entry - stop) / entry * Decimal("100")
        take_pct = abs(take - entry) / entry * Decimal("100")
        if (
            not Decimal(str(config["minStopLossPercentage"]))
            <= stop_pct
            <= Decimal(str(config["maxStopLossPercentage"]))
        ):
            raise PermissionError("stop-loss is outside broker bounds")
        if (
            not Decimal(str(config["minTakeProfitPercentage"]))
            <= take_pct
            <= Decimal(str(config["maxTakeProfitPercentage"]))
        ):
            raise PermissionError("take-profit is outside broker bounds")

    @staticmethod
    def _validated_cost_breakdown(
        preview: ApiResponse,
        *,
        instrument_id: int,
        symbol: str,
    ) -> tuple[Decimal, str]:
        if not preview.ok or not isinstance(preview.body, Mapping):
            raise PermissionError("DEMO cost preview failed")
        body = preview.body
        if int(body.get("instrumentId", body.get("instrumentID", 0)) or 0) != instrument_id:
            raise PermissionError("DEMO cost preview instrument mismatch")
        response_symbol = str(body.get("symbol", symbol) or symbol).upper()
        if response_symbol != symbol:
            raise PermissionError("DEMO cost preview symbol mismatch")
        costs = body.get("costs")
        if not isinstance(costs, list) or not costs:
            raise PermissionError("DEMO cost preview lacks a complete cost list")
        allowed_types = {
            "markup",
            "marketSpread",
            "transactionFee",
            "overnightFee",
            "overWeekendFee",
            "sdrt",
        }
        seen: set[str] = set()
        total = Decimal("0")
        for item in costs:
            if not isinstance(item, Mapping):
                raise PermissionError("DEMO cost preview component is invalid")
            cost_type = str(item.get("costType", ""))
            if cost_type not in allowed_types or cost_type in seen:
                raise PermissionError("DEMO cost preview type is unknown or duplicated")
            seen.add(cost_type)
            if str(item.get("currency", "")).upper() != "USD":
                raise PermissionError("DEMO cost preview currency is not USD")
            try:
                amount = Decimal(str(item.get("amount", "NaN")))
            except InvalidOperation as exc:
                raise PermissionError("DEMO cost preview amount is invalid") from exc
            if not amount.is_finite() or amount < 0:
                raise PermissionError("DEMO cost preview amount is invalid")
            total += amount
        last_updated = datetime.fromisoformat(
            str(body.get("lastUpdated", "")).replace("Z", "+00:00")
        )
        if last_updated.tzinfo is None:
            raise PermissionError("DEMO cost preview timestamp is not timezone-aware")
        age_seconds = (datetime.now(UTC) - last_updated.astimezone(UTC)).total_seconds()
        if age_seconds < -5 or age_seconds > COST_PREVIEW_MAX_AGE_SECONDS:
            raise PermissionError("DEMO cost preview timestamp is stale or future-dated")
        canonical = json.dumps(dict(body), sort_keys=True, separators=(",", ":"), default=str)
        return total, hashlib.sha256(canonical.encode()).hexdigest()

    def open_by_amount(
        self,
        *,
        instrument_id: int,
        amount_usd: Decimal,
        is_buy: bool,
        leverage: int,
        request_id: str,
        stop_loss_rate: Decimal,
        take_profit_rate: Decimal,
    ) -> ApiResponse:
        rates = self.rates((instrument_id,))
        rows = rates.body.get("rates", []) if rates.ok and isinstance(rates.body, dict) else []
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PermissionError("fresh rate is unavailable before create order")
        entry_rate = Decimal(str(rows[0]["ask"] if is_buy else rows[0]["bid"]))
        prepared = self.prepare_open_by_amount(
            instrument_id=instrument_id,
            amount_usd=amount_usd,
            is_buy=is_buy,
            leverage=leverage,
            entry_rate=entry_rate,
            stop_loss_rate=stop_loss_rate,
            take_profit_rate=take_profit_rate,
        )
        return self.submit_prepared_open(prepared.body, request_id=request_id)

    def prepare_open_by_amount(
        self,
        *,
        instrument_id: int,
        amount_usd: Decimal,
        is_buy: bool,
        leverage: int,
        entry_rate: Decimal,
        stop_loss_rate: Decimal,
        take_profit_rate: Decimal,
    ) -> PreparedDemoOpenV2:
        """Validate the exact final entry and return a cost-bound DEMO write body."""
        symbol = INSTRUMENT_SYMBOL.get(instrument_id)
        if symbol is None:
            raise ValueError("instrument is outside the fixed v2 catalog")
        eligibility = self.eligibility(symbol)
        config, settlement = self._select_configuration(
            eligibility, symbol=symbol, amount_usd=amount_usd, is_buy=is_buy, leverage=leverage
        )
        if entry_rate <= 0:
            raise PermissionError("final entry rate is invalid")
        self._validate_stop_take(
            config,
            entry_rate,
            stop_loss_rate,
            take_profit_rate,
            is_buy=is_buy,
        )
        transaction = "buy" if is_buy else "sellShort"
        cost_request = {
            "action": "open",
            "transaction": transaction,
            "instrumentId": instrument_id,
            "settlementType": settlement,
            "orderType": "mkt",
            "leverage": leverage,
            "amount": float(amount_usd),
            "orderCurrency": "usd",
        }
        preview = self.cost_preview(cost_request)
        total_cost_usd, cost_snapshot_hash = self._validated_cost_breakdown(
            preview,
            instrument_id=instrument_id,
            symbol=symbol,
        )
        body = {
            "action": "open",
            "transaction": transaction,
            "symbol": None,
            "instrumentId": instrument_id,
            "settlementType": settlement,
            "orderType": "mkt",
            "triggerRate": None,
            "leverage": leverage,
            "amount": float(amount_usd),
            "orderCurrency": "usd",
            "units": None,
            "contracts": None,
            "stopLossRate": float(stop_loss_rate),
            "takeProfitRate": float(take_profit_rate),
            "stopLossType": "fixed",
            "additionalMargin": None,
            "positionIds": None,
        }
        return PreparedDemoOpenV2(body, entry_rate, total_cost_usd, cost_snapshot_hash)

    def submit_prepared_open(
        self,
        body: Mapping[str, Any],
        *,
        request_id: str,
    ) -> ApiResponse:
        """Perform the single DEMO open write after the caller records SUBMITTING."""
        if body.get("action") != "open" or int(body.get("instrumentId", 0) or 0) <= 0:
            raise ValueError("prepared DEMO open body is invalid")
        return self._request(
            "POST",
            DEMO_CREATE_ORDER,
            body=body,
            request_id=request_id,
        )

    def _resolve_instrument_id(self, position_id: int) -> int:
        portfolio = self.demo_portfolio()
        if not portfolio.ok or not isinstance(portfolio.body, dict):
            raise PermissionError("DEMO portfolio is unavailable before close")
        root = portfolio.body.get("clientPortfolio", portfolio.body)
        positions = root.get("positions", []) if isinstance(root, dict) else []
        matches = [
            item
            for item in positions
            if isinstance(item, Mapping)
            and int(item.get("positionID", item.get("positionId", 0)) or 0) == position_id
        ]
        if len(matches) != 1:
            raise PermissionError("close requires exactly one reconciled broker position")
        instrument_id = int(matches[0].get("instrumentID", matches[0].get("instrumentId", 0)) or 0)
        if instrument_id <= 0:
            raise PermissionError("broker position lacks instrument identity")
        return instrument_id

    def close_position(
        self,
        *,
        position_id: int,
        units_to_deduct: Decimal | None,
        request_id: str,
    ) -> ApiResponse:
        prepared = self.prepare_close_position(
            position_id=position_id,
            units_to_deduct=units_to_deduct,
        )
        return self.submit_prepared_close(
            position_id=position_id,
            body=prepared,
            request_id=request_id,
        )

    def prepare_close_position(
        self,
        *,
        position_id: int,
        units_to_deduct: Decimal | None,
    ) -> Mapping[str, Any]:
        """Resolve close identity read-only before the order enters SUBMITTING."""
        if position_id <= 0 or (units_to_deduct is not None and units_to_deduct <= 0):
            raise ValueError("close order arguments are invalid")
        instrument_id = self._resolve_instrument_id(position_id)
        return {
            "InstrumentID": instrument_id,
            "UnitsToDeduct": None if units_to_deduct is None else float(units_to_deduct),
        }

    def submit_prepared_close(
        self,
        *,
        position_id: int,
        body: Mapping[str, Any],
        request_id: str,
    ) -> ApiResponse:
        """Perform the single DEMO close write after the caller records SUBMITTING."""
        if position_id <= 0 or int(body.get("InstrumentID", 0) or 0) <= 0:
            raise ValueError("prepared DEMO close body is invalid")
        return self._request(
            "POST",
            f"{DEMO_CLOSE_PREFIX}{position_id}",
            body=body,
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
        open_orders = portfolio.get("ordersForOpen", [])
        pending_orders = portfolio.get("orders", [])
        if not isinstance(open_orders, list) or not isinstance(pending_orders, list):
            raise ValueError("DEMO pending order collections are invalid")
        manual = sum(
            (
                Decimal(str(item.get("amount", 0)))
                for item in open_orders
                if isinstance(item, dict)
                and int(item.get("mirrorID", item.get("mirrorId", 0)) or 0) == 0
            ),
            Decimal("0"),
        )
        pending = sum(
            (
                Decimal(str(item.get("amount", 0)))
                for item in pending_orders
                if isinstance(item, dict)
            ),
            Decimal("0"),
        )
        canonical = json.dumps(portfolio, sort_keys=True, separators=(",", ":"), default=str)
        return DemoCashTruth(
            credit,
            max(Decimal("0"), credit - manual - pending),
            manual,
            pending,
            hashlib.sha256(canonical.encode()).hexdigest(),
            datetime.now(UTC),
        )
