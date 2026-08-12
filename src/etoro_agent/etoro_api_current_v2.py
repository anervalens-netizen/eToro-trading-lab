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
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .rate_limit_v2 import (
    ETORO_COST_PREVIEW,
    ETORO_READ,
    ETORO_WRITE_SHARED,
    RollingWindowLimiter,
    SharedRollingWindowLimiter,
)

BASE_URL = "https://public-api.etoro.com"
DEMO_CREATE_ORDER = "/api/v2/trading/execution/demo/orders"
DEMO_CLOSE_PREFIX = "/api/v1/trading/execution/demo/market-close-orders/positions/"
DEMO_ELIGIBILITY = "/api/v2/trading/info/demo/eligibility"
DEMO_COSTS = "/api/v2/trading/info/demo/costs"
DEMO_ORDER_LOOKUP = "/api/v2/trading/info/demo/orders:lookup"
DEMO_CLOSE_INFO_PREFIX = "/api/v1/trading/info/demo/close-orders/"
DEMO_HISTORY = "/api/v1/trading/info/trade/demo/history"
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
    requested_at: datetime | None = None
    received_at: datetime | None = None
    server_date: datetime | None = None

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

    def __post_init__(self) -> None:
        values = (
            self.credit_usd,
            self.available_cash_usd,
            self.pending_manual_orders_usd,
            self.pending_orders_usd,
        )
        if not all(value.is_finite() for value in values) or min(values) < 0:
            raise ValueError("DEMO cash truth must be finite and non-negative")
        if self.available_cash_usd > self.credit_usd:
            raise ValueError("available DEMO cash cannot exceed credit")
        if not self.snapshot_hash.strip() or self.observed_at.tzinfo is None:
            raise ValueError("DEMO cash truth provenance is invalid")


@dataclass(frozen=True)
class BrokerAccountSnapshotV2:
    """One immutable, strictly decoded DEMO account observation."""

    schema_version: str
    request_id: str
    snapshot_hash: str
    requested_at: datetime
    received_at: datetime
    broker_observed_at: datetime | None
    observed_at: datetime
    credit_usd: Decimal
    available_cash_usd: Decimal
    invested_usd: Decimal
    unrealized_pnl_usd: Decimal
    equity_usd: Decimal
    gross_exposure_usd: Decimal
    pending_manual_orders_usd: Decimal
    pending_orders_usd: Decimal
    positions: tuple[Mapping[str, Any], ...]
    open_orders: tuple[Mapping[str, Any], ...]
    pending_orders: tuple[Mapping[str, Any], ...]
    foreign_activity: tuple[str, ...]

    def cash_truth(self) -> DemoCashTruth:
        return DemoCashTruth(
            self.credit_usd,
            self.available_cash_usd,
            self.pending_manual_orders_usd,
            self.pending_orders_usd,
            self.snapshot_hash,
            self.observed_at,
        )


@dataclass(frozen=True)
class PreparedDemoOpenV2:
    body: Mapping[str, Any]
    entry_rate: Decimal
    total_cost_usd: Decimal
    cost_snapshot_hash: str
    request_body_sha256: str = ""


@dataclass(frozen=True)
class PreparedDemoCloseV2:
    body: Mapping[str, Any]
    broker_position_id: str
    instrument_id: int
    quantity_before: Decimal
    broker_snapshot_hash: str
    request_body_sha256: str = ""
    quantity_rules_hash: str = ""


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
        shared_directory = os.getenv("ETORO_V2_SHARED_RATE_LIMIT_DIR", "").strip()
        self.read_limiter: RollingWindowLimiter | SharedRollingWindowLimiter
        self.write_limiter: RollingWindowLimiter | SharedRollingWindowLimiter
        self.cost_limiter: RollingWindowLimiter | SharedRollingWindowLimiter
        if shared_directory:
            self.read_limiter = SharedRollingWindowLimiter(ETORO_READ, shared_directory)
            self.write_limiter = SharedRollingWindowLimiter(ETORO_WRITE_SHARED, shared_directory)
            self.cost_limiter = SharedRollingWindowLimiter(ETORO_COST_PREVIEW, shared_directory)
        else:
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

    @classmethod
    def canonical_request_bytes(cls, value: Any) -> bytes:
        """Serialize broker economics once without binary-float conversion."""

        def encode(item: Any) -> str:
            if item is None:
                return "null"
            if type(item) is bool:
                return "true" if item else "false"
            if type(item) is int:
                return str(item)
            if isinstance(item, Decimal):
                if not item.is_finite():
                    raise ValueError("broker request decimal must be finite")
                return format(item, "f")
            if isinstance(item, float):
                raise TypeError("binary floats are forbidden in broker request bodies")
            if isinstance(item, str):
                return json.dumps(item, ensure_ascii=False)
            if isinstance(item, Mapping):
                if any(not isinstance(key, str) for key in item):
                    raise TypeError("broker request object keys must be strings")
                return (
                    "{"
                    + ",".join(
                        f"{json.dumps(key, ensure_ascii=False)}:{encode(item[key])}"
                        for key in sorted(item)
                    )
                    + "}"
                )
            if isinstance(item, (list, tuple)):
                return "[" + ",".join(encode(child) for child in item) + "]"
            raise TypeError(f"unsupported broker request type: {type(item).__name__}")

        return encode(value).encode("utf-8")

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
        read_allowed = method == "GET" and (
            path
            in {
                "/api/v1/me",
                "/api/v1/market-data/instruments/rates",
                "/api/v1/trading/info/demo/pnl",
                "/api/v1/trading/info/demo/portfolio",
                DEMO_ORDER_LOOKUP,
                DEMO_HISTORY,
            }
            or re.fullmatch(r"/api/v1/trading/info/demo/close-orders/[1-9]\d*", path)
            or re.fullmatch(
                r"/api/v1/market-data/instruments/[1-9]\d*/history/candles/asc/"
                r"(?:OneMinute|FiveMinutes|TenMinutes|FifteenMinutes|ThirtyMinutes|"
                r"OneHour|FourHours|OneDay|OneWeek)/(?:[1-9]\d{0,2}|1000)",
                path,
            )
        )
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
        encoded = None if body is None else self.canonical_request_bytes(body)
        request = urllib.request.Request(
            url, data=encoded, headers=self._headers(req_id), method=method
        )
        requested_at = datetime.now(UTC)
        try:
            # Base URL and request paths are fixed allowlists above.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError("eToro response exceeds two-megabyte cap")
                received_at = datetime.now(UTC)
                raw_server_date = response.headers.get("Date")
                server_date = (
                    parsedate_to_datetime(raw_server_date).astimezone(UTC)
                    if raw_server_date
                    else None
                )
                return ApiResponse(
                    response.status,
                    json.loads(raw) if raw else None,
                    req_id,
                    requested_at,
                    received_at,
                    server_date,
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(2_000_001)
            try:
                parsed = json.loads(raw) if raw and len(raw) <= 2_000_000 else None
            except json.JSONDecodeError:
                parsed = None
            received_at = datetime.now(UTC)
            raw_server_date = exc.headers.get("Date") if exc.headers is not None else None
            server_date = (
                parsedate_to_datetime(raw_server_date).astimezone(UTC) if raw_server_date else None
            )
            return ApiResponse(
                exc.code,
                parsed,
                req_id,
                requested_at,
                received_at,
                server_date,
            )

    def rates(self, instrument_ids: tuple[int, ...]) -> ApiResponse:
        if not instrument_ids or any(value <= 0 for value in instrument_ids):
            raise ValueError("instrument ids must be positive")
        return self._request(
            "GET",
            "/api/v1/market-data/instruments/rates",
            query={"instrumentIds": ",".join(str(value) for value in instrument_ids)},
        )

    def history_candles(
        self,
        *,
        instrument_id: int,
        interval: str,
        count: int,
    ) -> ApiResponse:
        allowed_intervals = {
            "OneMinute",
            "FiveMinutes",
            "TenMinutes",
            "FifteenMinutes",
            "ThirtyMinutes",
            "OneHour",
            "FourHours",
            "OneDay",
            "OneWeek",
        }
        if instrument_id <= 0 or interval not in allowed_intervals or not 1 <= count <= 1000:
            raise ValueError("candle history request is invalid")
        return self._request(
            "GET",
            f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/"
            f"{interval}/{count}",
        )

    def demo_pnl(self) -> ApiResponse:
        return self._request("GET", "/api/v1/trading/info/demo/pnl")

    def demo_portfolio(self) -> ApiResponse:
        return self._request("GET", "/api/v1/trading/info/demo/portfolio")

    def order_lookup(
        self,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
    ) -> ApiResponse:
        if bool(str(order_id or "").strip()) == bool(str(reference_id or "").strip()):
            raise ValueError("exactly one order lookup identity is required")
        if order_id is not None:
            normalized = str(order_id).strip()
            if not normalized.isdigit() or int(normalized) <= 0:
                raise ValueError("broker order id must be a positive integer")
            query = {"orderId": normalized}
        else:
            normalized = str(reference_id).strip()
            try:
                uuid.UUID(normalized)
            except ValueError as exc:
                raise ValueError("broker reference id must be the submitted request UUID") from exc
            query = {"referenceId": normalized}
        return self._request("GET", DEMO_ORDER_LOOKUP, query=query)

    def close_order_information(self, order_id: str) -> ApiResponse:
        normalized = str(order_id).strip()
        if not normalized.isdigit() or int(normalized) <= 0:
            raise ValueError("close order id must be a positive integer")
        return self._request("GET", DEMO_CLOSE_INFO_PREFIX + normalized)

    def trading_history(
        self,
        *,
        min_date: date,
        page: int = 1,
        page_size: int = 100,
    ) -> ApiResponse:
        if page < 1 or not 1 <= page_size <= 1000:
            raise ValueError("trading history pagination is invalid")
        return self._request(
            "GET",
            DEMO_HISTORY,
            query={
                "minDate": min_date.isoformat(),
                "page": str(page),
                "pageSize": str(page_size),
            },
        )

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

    def verify_isolated_demo_read_scope(self) -> Mapping[str, Any]:
        response = self._request("GET", "/api/v1/me")
        if not response.ok or not isinstance(response.body, Mapping):
            raise PermissionError("eToro read credentials are missing or invalid")
        scopes = {str(value) for value in response.body.get("scopes", [])}
        accepted = {
            "etoro-public:demo:read",
            "etoro-public:trade.demo:read",
        }
        if scopes.isdisjoint(accepted):
            raise PermissionError("isolated collector key requires DEMO read scope")
        if any(":write" in scope or ".real:" in scope or ":real:" in scope for scope in scopes):
            raise PermissionError("isolated collector key must not carry write or REAL scope")
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
            raw_amounts = [item[key] for key in ("amount", "value") if key in item]
            if not raw_amounts:
                raise PermissionError("DEMO cost preview amount is invalid")
            try:
                amounts = [Decimal(str(raw_amount)) for raw_amount in raw_amounts]
            except InvalidOperation as exc:
                raise PermissionError("DEMO cost preview amount is invalid") from exc
            if any(not amount.is_finite() or amount < 0 for amount in amounts):
                raise PermissionError("DEMO cost preview amount is invalid")
            if any(amount != amounts[0] for amount in amounts[1:]):
                raise PermissionError("DEMO cost preview amount fields disagree")
            amount = amounts[0]
            total += amount
        required_types = {"marketSpread", "transactionFee"}
        if not required_types <= seen:
            raise PermissionError("DEMO cost preview lacks mandatory cost components")
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
            "amount": amount_usd,
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
            "amount": amount_usd,
            "orderCurrency": "usd",
            "units": None,
            "contracts": None,
            "stopLossRate": stop_loss_rate,
            "takeProfitRate": take_profit_rate,
            "stopLossType": "fixed",
            "additionalMargin": None,
            "positionIds": None,
        }
        return PreparedDemoOpenV2(
            body,
            entry_rate,
            total_cost_usd,
            cost_snapshot_hash,
            hashlib.sha256(self.canonical_request_bytes(body)).hexdigest(),
        )

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

    def _resolve_position(self, position_id: int) -> Mapping[str, Any]:
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
        return matches[0]

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
            body=prepared.body,
            request_id=request_id,
        )

    def prepare_close_position(
        self,
        *,
        position_id: int,
        units_to_deduct: Decimal | None,
    ) -> PreparedDemoCloseV2:
        """Resolve close identity read-only before the order enters SUBMITTING."""
        if position_id <= 0 or (units_to_deduct is not None and units_to_deduct <= 0):
            raise ValueError("close order arguments are invalid")
        row = self._resolve_position(position_id)
        instrument_id = int(row.get("instrumentID", row.get("instrumentId", 0)) or 0)
        if instrument_id <= 0:
            raise PermissionError("broker position lacks instrument identity")
        raw_quantity = row.get(
            "units",
            row.get("quantity", row.get("unitsOwned", row.get("netUnits"))),
        )
        try:
            quantity = abs(Decimal(str(raw_quantity)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PermissionError("broker position quantity is invalid") from exc
        if not quantity.is_finite() or quantity <= 0:
            raise PermissionError("broker position quantity is invalid")
        if units_to_deduct is not None and units_to_deduct > quantity:
            raise PermissionError("partial close exceeds fresh broker position quantity")
        quantity_rules_hash = "full-close"
        if units_to_deduct is not None and units_to_deduct < quantity:
            try:
                raw_precision = row["unitPrecision"]
                if type(raw_precision) is not int:
                    raise TypeError("unit precision must be an integer")
                precision = raw_precision
                minimum_close = self._finite_decimal(
                    row["minimumCloseUnits"], "minimum close units", non_negative=True
                )
                minimum_residual = self._finite_decimal(
                    row["minimumResidualUnits"], "minimum residual units", non_negative=True
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise PermissionError(
                    "partial close requires fresh broker precision/minimum rules"
                ) from exc
            if not 0 <= precision <= 12:
                raise PermissionError("partial close unit precision is invalid")
            quantum = Decimal(1).scaleb(-precision)
            if units_to_deduct.quantize(quantum) != units_to_deduct:
                raise PermissionError("partial close quantity is not broker-quantized")
            residual = quantity - units_to_deduct
            if units_to_deduct < minimum_close:
                raise PermissionError("partial close is below broker minimum")
            if residual != 0 and residual < minimum_residual:
                raise PermissionError("partial close would leave broker dust")
            rules = {
                "instrument_id": instrument_id,
                "unit_precision": precision,
                "minimum_close_units": minimum_close,
                "minimum_residual_units": minimum_residual,
            }
            quantity_rules_hash = hashlib.sha256(self.canonical_request_bytes(rules)).hexdigest()
        canonical = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), default=str)
        body = {
            "InstrumentID": instrument_id,
            "UnitsToDeduct": units_to_deduct,
        }
        return PreparedDemoCloseV2(
            body,
            str(position_id),
            instrument_id,
            quantity,
            hashlib.sha256(canonical.encode()).hexdigest(),
            hashlib.sha256(self.canonical_request_bytes(body)).hexdigest(),
            quantity_rules_hash,
        )

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

    @staticmethod
    def _finite_decimal(value: Any, label: str, *, non_negative: bool = False) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"DEMO {label} is invalid")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"DEMO {label} is invalid") from exc
        if not parsed.is_finite() or (non_negative and parsed < 0):
            raise ValueError(f"DEMO {label} is invalid")
        return parsed

    @staticmethod
    def _strict_rows(portfolio: Mapping[str, Any], name: str) -> tuple[Mapping[str, Any], ...]:
        raw = portfolio.get(name, [])
        if not isinstance(raw, list):
            raise ValueError(f"DEMO {name} collection is invalid")
        rows: list[Mapping[str, Any]] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise ValueError(f"DEMO {name}[{index}] row is invalid")
            rows.append(dict(item))
        return tuple(rows)

    @staticmethod
    def _broker_identity(row: Mapping[str, Any], *, position: bool = False) -> str:
        names = (
            ("positionID", "positionId")
            if position
            else (
                "orderID",
                "orderId",
                "referenceID",
                "referenceId",
                "requestID",
                "requestId",
            )
        )
        values = {
            str(row[name]).strip() for name in names if name in row and str(row[name]).strip()
        }
        if len(values) != 1:
            kind = "position" if position else "order"
            raise ValueError(f"DEMO {kind} identity is missing or conflicting")
        return next(iter(values))

    def account_snapshot(self) -> BrokerAccountSnapshotV2:
        response = self.demo_pnl()
        if not response.ok or not isinstance(response.body, dict):
            raise RuntimeError("DEMO P&L snapshot unavailable")
        portfolio = response.body.get("clientPortfolio", response.body)
        if not isinstance(portfolio, Mapping):
            raise ValueError("DEMO P&L payload shape is invalid")
        credit = self._finite_decimal(portfolio.get("credit"), "credit", non_negative=True)
        positions = self._strict_rows(portfolio, "positions")
        open_orders = self._strict_rows(portfolio, "ordersForOpen")
        pending_orders = self._strict_rows(portfolio, "orders")

        position_ids = [self._broker_identity(row, position=True) for row in positions]
        if len(position_ids) != len(set(position_ids)):
            raise ValueError("DEMO position identities are duplicated")
        open_order_ids = [self._broker_identity(row) for row in open_orders]
        pending_order_ids = [self._broker_identity(row) for row in pending_orders]
        if len(open_order_ids) != len(set(open_order_ids)) or len(pending_order_ids) != len(
            set(pending_order_ids)
        ):
            raise ValueError("DEMO order identities are duplicated")
        if set(open_order_ids) & set(pending_order_ids):
            raise ValueError("DEMO order collections overlap")

        foreign: list[str] = []
        invested = Decimal("0")
        unrealized = Decimal("0")
        gross = Decimal("0")
        for row in positions:
            position_id = self._broker_identity(row, position=True)
            try:
                mirror_id = int(row.get("mirrorID", row.get("mirrorId", 0)) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("DEMO mirror identity is invalid") from exc
            if mirror_id != 0:
                foreign.append(f"mirror_position:{position_id}")
            amount = self._finite_decimal(row.get("amount"), "position amount", non_negative=True)
            pnl = row.get("unrealizedPnL")
            if not isinstance(pnl, Mapping):
                raise ValueError("DEMO position P&L shape is invalid")
            exposure = self._finite_decimal(
                pnl.get("exposureInAccountCurrency"), "position exposure"
            )
            unrealized_value = self._finite_decimal(pnl.get("pnL"), "position unrealized P&L")
            invested += amount
            gross += abs(exposure)
            unrealized += unrealized_value

        def pending_amount(row: Mapping[str, Any]) -> Decimal:
            return self._finite_decimal(
                row.get("amount", row.get("exposure")),
                "pending order amount",
                non_negative=True,
            )

        for row in (*open_orders, *pending_orders):
            try:
                mirror_id = int(row.get("mirrorID", row.get("mirrorId", 0)) or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("DEMO mirror identity is invalid") from exc
            if mirror_id != 0:
                foreign.append(f"mirror_order:{self._broker_identity(row)}")
        for name in ("mirrors", "copyPortfolios", "mirrorPortfolios"):
            value = portfolio.get(name, [])
            if value not in (None, [], {}):
                foreign.append(f"nonempty_{name}")

        manual = sum((pending_amount(item) for item in open_orders), Decimal("0"))
        pending = sum((pending_amount(item) for item in pending_orders), Decimal("0"))
        available = credit - manual - pending
        if available < 0:
            raise ValueError("DEMO pending orders exceed credit")
        equity = credit + invested + unrealized
        if equity <= 0:
            raise ValueError("DEMO equity is invalid")
        requested_at = response.requested_at or datetime.now(UTC)
        received_at = response.received_at or requested_at
        if requested_at.tzinfo is None or received_at.tzinfo is None or received_at < requested_at:
            raise ValueError("DEMO response timing provenance is invalid")
        broker_time = response.server_date
        if broker_time is not None and broker_time.tzinfo is None:
            raise ValueError("DEMO broker timestamp provenance is invalid")
        canonical = json.dumps(dict(portfolio), sort_keys=True, separators=(",", ":"), default=str)
        return BrokerAccountSnapshotV2(
            schema_version="etoro-demo-pnl-v1-strict",
            request_id=response.request_id,
            snapshot_hash=hashlib.sha256(canonical.encode()).hexdigest(),
            requested_at=requested_at.astimezone(UTC),
            received_at=received_at.astimezone(UTC),
            broker_observed_at=None if broker_time is None else broker_time.astimezone(UTC),
            observed_at=requested_at.astimezone(UTC),
            credit_usd=credit,
            available_cash_usd=available,
            invested_usd=invested,
            unrealized_pnl_usd=unrealized,
            equity_usd=equity,
            gross_exposure_usd=gross,
            pending_manual_orders_usd=manual,
            pending_orders_usd=pending,
            positions=positions,
            open_orders=open_orders,
            pending_orders=pending_orders,
            foreign_activity=tuple(sorted(set(foreign))),
        )

    def cash_truth(self, snapshot: BrokerAccountSnapshotV2 | None = None) -> DemoCashTruth:
        return (snapshot or self.account_snapshot()).cash_truth()
