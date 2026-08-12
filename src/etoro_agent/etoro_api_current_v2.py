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
from math import isfinite
from pathlib import Path
from typing import Any

from .rate_limit_v2 import (
    ETORO_CLOSE_RESERVE,
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
class DecodedBrokerRateV2:
    instrument_id: int
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    sequence_or_event_id: str
    raw: Mapping[str, Any]


def _strict_broker_decimal(
    value: Any,
    label: str,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PermissionError(f"broker {label} is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PermissionError(f"broker {label} is invalid") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (non_negative and parsed < 0):
        raise PermissionError(f"broker {label} is invalid")
    return parsed


def _strict_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PermissionError(f"broker {label} must be a positive integer")
    return value


def _strict_int_alias(value: Mapping[str, Any], aliases: tuple[str, ...], label: str) -> int:
    present = [name for name in aliases if name in value]
    if not present:
        raise PermissionError(f"broker {label} is missing")
    decoded = [_strict_positive_int(value[name], label) for name in present]
    if any(item != decoded[0] for item in decoded[1:]):
        raise PermissionError(f"broker {label} aliases disagree")
    return decoded[0]


def decode_broker_timestamp_v2(value: Any) -> datetime:
    if isinstance(value, bool):
        raise PermissionError("broker timestamp is invalid")
    if isinstance(value, (int, float)):
        numeric = float(value)
        if not isfinite(numeric) or numeric <= 0:
            raise PermissionError("broker timestamp is invalid")
        seconds = numeric / 1000 if numeric > 10_000_000_000 else numeric
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise PermissionError("broker timestamp is invalid") from exc
    if not isinstance(value, str) or not value.strip():
        raise PermissionError("broker timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PermissionError("broker timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PermissionError("broker timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def decode_broker_rate_v2(response: ApiResponse, *, instrument_id: int) -> DecodedBrokerRateV2:
    """Decode one current broker rate without identity or time fallbacks."""

    expected_instrument_id = _strict_positive_int(instrument_id, "expected instrument identity")
    if not response.ok or not isinstance(response.body, Mapping):
        raise PermissionError("fresh broker rate is unavailable")
    rows = response.body.get("rates")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise PermissionError("fresh broker rate is unavailable")
    row = rows[0]
    decoded_instrument_id = _strict_int_alias(
        row, ("instrumentID", "instrumentId"), "rate instrument identity"
    )
    if decoded_instrument_id != expected_instrument_id:
        raise PermissionError("fresh broker rate instrument mismatch")
    bid = _strict_broker_decimal(row.get("bid"), "rate bid", positive=True)
    ask = _strict_broker_decimal(row.get("ask"), "rate ask", positive=True)
    if ask < bid:
        raise PermissionError("fresh broker rate spread is invalid")
    timestamps = [name for name in ("date", "timestamp") if name in row]
    if not timestamps:
        raise PermissionError("fresh broker rate lacks provenance time")
    observed = [decode_broker_timestamp_v2(row[name]) for name in timestamps]
    if any(value != observed[0] for value in observed[1:]):
        raise PermissionError("broker timestamp aliases disagree")
    sequence_values = [row[name] for name in ("sequence", "eventId") if name in row]
    if sequence_values:
        if any(
            isinstance(value, bool) or not isinstance(value, (str, int))
            for value in sequence_values
        ):
            raise PermissionError("broker rate event identity is invalid")
        normalized_sequence = [str(value).strip() for value in sequence_values]
        if any(not value for value in normalized_sequence):
            raise PermissionError("broker rate event identity is invalid")
        if any(value != normalized_sequence[0] for value in normalized_sequence[1:]):
            raise PermissionError("broker rate event identity aliases disagree")
        sequence = normalized_sequence[0]
    else:
        sequence = "rest"
    return DecodedBrokerRateV2(
        decoded_instrument_id,
        bid,
        ask,
        observed[0],
        sequence,
        dict(row),
    )


def _strict_scopes(body: Mapping[str, Any]) -> frozenset[str]:
    raw = body.get("scopes")
    if not isinstance(raw, list) or not raw:
        raise PermissionError("eToro credential scopes are missing or malformed")
    scopes: list[str] = []
    for value in raw:
        if not isinstance(value, str) or value != value.strip():
            raise PermissionError("eToro credential scope is malformed")
        if not re.fullmatch(
            r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*"
            r"(?::[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*)+",
            value,
        ):
            raise PermissionError("eToro credential scope is malformed")
        scopes.append(value)
    if len(set(scopes)) != len(scopes):
        raise PermissionError("eToro credential scopes are duplicated")
    return frozenset(scopes)


def _has_real_scope(scopes: frozenset[str]) -> bool:
    return any(
        "real" in {segment.lower() for segment in re.split(r"[.:_-]", scope)} for scope in scopes
    )


def _has_write_scope(scopes: frozenset[str]) -> bool:
    return any(
        "write" in {segment.lower() for segment in re.split(r"[.:_-]", scope)} for scope in scopes
    )


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
class BrokerOrderIdentityV2:
    order_id: str | None
    client_reference_id: str | None

    def __post_init__(self) -> None:
        if self.order_id is None and self.client_reference_id is None:
            raise ValueError("DEMO order identity is missing")

    @property
    def display_id(self) -> str:
        return self.order_id or str(self.client_reference_id)


def _strict_identity_family(
    row: Mapping[str, Any],
    aliases: tuple[str, ...],
    *,
    label: str,
    required: bool,
) -> str | None:
    present = [name for name in aliases if name in row]
    if not present:
        if required:
            raise ValueError(f"DEMO {label} identity is missing")
        return None
    values: list[str] = []
    for name in present:
        raw = row[name]
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValueError(f"DEMO {label} identity is invalid")
        normalized = str(raw).strip()
        if (
            not normalized
            or len(normalized) > 128
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized) is None
            or (type(raw) is int and raw <= 0)
        ):
            raise ValueError(f"DEMO {label} identity is invalid")
        values.append(normalized)
    if any(value != values[0] for value in values[1:]):
        raise ValueError(f"DEMO {label} identity aliases conflict")
    return values[0]


def decode_broker_position_identity_v2(row: Mapping[str, Any]) -> str:
    value = _strict_identity_family(
        row,
        ("positionID", "positionId"),
        label="position",
        required=True,
    )
    if value is None:  # defensive for type narrowing; required=True rejects this path
        raise ValueError("DEMO position identity is missing")
    return value


def decode_broker_order_identity_v2(row: Mapping[str, Any]) -> BrokerOrderIdentityV2:
    """Keep broker and client order identities distinct while validating aliases."""

    return BrokerOrderIdentityV2(
        order_id=_strict_identity_family(
            row,
            ("orderID", "orderId"),
            label="broker order",
            required=False,
        ),
        client_reference_id=_strict_identity_family(
            row,
            ("referenceID", "referenceId", "requestID", "requestId"),
            label="client order reference",
            required=False,
        ),
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
        close_priority: bool = False,
        write_budget_acquired: bool = False,
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
        if type(close_priority) is not bool or type(write_budget_acquired) is not bool:
            raise TypeError("broker write budget flags must be boolean")
        is_close_write = bool(write_allowed and path != DEMO_CREATE_ORDER)
        if write_allowed and close_priority != is_close_write:
            raise PermissionError("broker write priority does not match the DEMO route")
        if not write_allowed and (close_priority or write_budget_acquired):
            raise PermissionError("broker write budget flags require a DEMO write route")
        if read_allowed:
            self.read_limiter.acquire()
        elif path == DEMO_COSTS:
            self.cost_limiter.acquire()
        elif not write_budget_acquired:
            self.write_limiter.acquire(
                priority=close_priority,
                reserve=ETORO_CLOSE_RESERVE,
            )
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
        if not instrument_ids or any(
            type(value) is not int or value <= 0 for value in instrument_ids
        ):
            raise ValueError("instrument ids must be positive")
        return self._request(
            "GET",
            "/api/v1/market-data/instruments/rates",
            query={"instrumentIds": ",".join(str(value) for value in instrument_ids)},
        )

    def acquire_demo_write_budget(self, *, close_priority: bool) -> None:
        """Reserve shared quota before the executor records SUBMITTING."""

        if type(close_priority) is not bool:
            raise TypeError("broker close-priority flag must be boolean")
        self.write_limiter.acquire(
            priority=close_priority,
            reserve=ETORO_CLOSE_RESERVE,
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
        scopes = _strict_scopes(response.body)
        accepted_pairs = (
            {"etoro-public:trade.demo:read", "etoro-public:trade.demo:write"},
            {"etoro-public:demo:read", "etoro-public:demo:write"},
        )
        if _has_real_scope(scopes):
            raise PermissionError("isolated DEMO key must not carry any REAL scope")
        if not any(required <= scopes for required in accepted_pairs):
            raise PermissionError("isolated DEMO key requires DEMO trade read and write")
        return response.body

    def verify_isolated_demo_read_scope(self) -> Mapping[str, Any]:
        response = self._request("GET", "/api/v1/me")
        if not response.ok or not isinstance(response.body, Mapping):
            raise PermissionError("eToro read credentials are missing or invalid")
        scopes = _strict_scopes(response.body)
        accepted = {
            "etoro-public:demo:read",
            "etoro-public:trade.demo:read",
        }
        if scopes.isdisjoint(accepted):
            raise PermissionError("isolated collector key requires DEMO read scope")
        if _has_write_scope(scopes) or _has_real_scope(scopes):
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
        if type(is_buy) is not bool:
            raise PermissionError("broker order direction must be an exact boolean")
        if type(leverage) is not int or leverage <= 0:
            raise PermissionError("broker order leverage must be a positive integer")
        if not isinstance(amount_usd, Decimal) or not amount_usd.is_finite() or amount_usd <= 0:
            raise PermissionError("broker order amount must be finite and positive")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise PermissionError("broker order symbol must be canonical")
        if not eligibility.ok or not isinstance(eligibility.body, Mapping):
            raise PermissionError("broker eligibility is unavailable")
        body = eligibility.body
        rows = body.get("eligibilities")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise PermissionError("broker eligibility is unavailable")
        if body.get("currency") != "USD":
            raise PermissionError("broker eligibility currency is not USD")
        row = rows[0]
        required_row_fields = {
            "symbol",
            "allowOpenPosition",
            "minPositionExposure",
            "leverageConfigs",
        }
        if not required_row_fields <= set(row):
            raise PermissionError("broker eligibility fields are incomplete")
        if row["symbol"] != symbol:
            raise PermissionError("broker eligibility symbol mismatch")
        if type(row["allowOpenPosition"]) is not bool:
            raise PermissionError("broker eligibility allow-open flag is invalid")
        if not row["allowOpenPosition"]:
            raise PermissionError("instrument is not currently eligible")
        minimum_exposure = _strict_broker_decimal(
            row["minPositionExposure"], "minimum position exposure", positive=True
        )
        if amount_usd * Decimal(leverage) < minimum_exposure:
            raise PermissionError("amount is below broker minimum exposure")
        direction = "long" if is_buy else "short"
        raw_configs = row["leverageConfigs"]
        if not isinstance(raw_configs, list) or not raw_configs:
            raise PermissionError("broker leverage configurations are unavailable")
        required_config_fields = {
            "direction",
            "leverageValues",
            "allowStopLossTakeProfit",
            "minPositionAmount",
            "settlementType",
            "minStopLossPercentage",
            "maxStopLossPercentage",
            "minTakeProfitPercentage",
            "maxTakeProfitPercentage",
        }
        decoded_configs: list[tuple[Mapping[str, Any], str, tuple[int, ...], Decimal]] = []
        for item in raw_configs:
            if not isinstance(item, Mapping) or not required_config_fields <= set(item):
                raise PermissionError("broker leverage configuration fields are incomplete")
            raw_direction = item["direction"]
            if not isinstance(raw_direction, str) or raw_direction.lower() not in {"long", "short"}:
                raise PermissionError("broker leverage configuration direction is invalid")
            raw_leverages = item["leverageValues"]
            if not isinstance(raw_leverages, list) or not raw_leverages:
                raise PermissionError("broker leverage values are invalid")
            leverages = tuple(
                _strict_positive_int(value, "leverage value") for value in raw_leverages
            )
            if len(set(leverages)) != len(leverages):
                raise PermissionError("broker leverage values are duplicated")
            if type(item["allowStopLossTakeProfit"]) is not bool:
                raise PermissionError("broker stop/take permission flag is invalid")
            minimum_amount = _strict_broker_decimal(
                item["minPositionAmount"], "minimum position amount", positive=True
            )
            minimum_stop = _strict_broker_decimal(
                item["minStopLossPercentage"], "minStopLossPercentage", positive=True
            )
            maximum_stop = _strict_broker_decimal(
                item["maxStopLossPercentage"], "maxStopLossPercentage", positive=True
            )
            minimum_take = _strict_broker_decimal(
                item["minTakeProfitPercentage"], "minTakeProfitPercentage", positive=True
            )
            maximum_take = _strict_broker_decimal(
                item["maxTakeProfitPercentage"], "maxTakeProfitPercentage", positive=True
            )
            if maximum_stop < minimum_stop or maximum_take < minimum_take:
                raise PermissionError("broker stop/take percentage bounds are invalid")
            settlement = item["settlementType"]
            if not isinstance(settlement, str) or settlement.lower() not in {
                "cfd",
                "real",
                "realfutures",
                "margintrade",
            }:
                raise PermissionError("broker settlement type is unsupported")
            decoded_configs.append((item, raw_direction.lower(), leverages, minimum_amount))
        configs = [
            item
            for item, item_direction, leverages, minimum_amount in decoded_configs
            if item_direction == direction
            and leverage in leverages
            and item["allowStopLossTakeProfit"] is True
            and amount_usd >= minimum_amount
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
        expected_instrument_id = _strict_positive_int(
            instrument_id, "expected cost instrument identity"
        )
        if not isinstance(symbol, str) or not symbol or symbol != symbol.upper():
            raise PermissionError("DEMO cost preview expected symbol is invalid")
        body = preview.body
        decoded_instrument_id = _strict_int_alias(
            body, ("instrumentId", "instrumentID"), "cost instrument identity"
        )
        if decoded_instrument_id != expected_instrument_id:
            raise PermissionError("DEMO cost preview instrument mismatch")
        if not isinstance(body.get("symbol"), str) or body["symbol"] != symbol:
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
            cost_type = item.get("costType")
            if not isinstance(cost_type, str):
                raise PermissionError("DEMO cost preview type is invalid")
            if cost_type not in allowed_types or cost_type in seen:
                raise PermissionError("DEMO cost preview type is unknown or duplicated")
            seen.add(cost_type)
            if item.get("currency") != "USD":
                raise PermissionError("DEMO cost preview currency is not USD")
            raw_amounts = [item[key] for key in ("amount", "value") if key in item]
            if not raw_amounts:
                raise PermissionError("DEMO cost preview amount is invalid")
            amounts = [
                _strict_broker_decimal(raw_amount, "cost preview amount", non_negative=True)
                for raw_amount in raw_amounts
            ]
            if any(amount != amounts[0] for amount in amounts[1:]):
                raise PermissionError("DEMO cost preview amount fields disagree")
            amount = amounts[0]
            total += amount
        required_types = {"marketSpread", "transactionFee"}
        if not required_types <= seen:
            raise PermissionError("DEMO cost preview lacks mandatory cost components")
        if "lastUpdated" not in body:
            raise PermissionError("DEMO cost preview timestamp is missing")
        last_updated = decode_broker_timestamp_v2(body["lastUpdated"])
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
        rate = decode_broker_rate_v2(self.rates((instrument_id,)), instrument_id=instrument_id)
        entry_rate = rate.ask if is_buy else rate.bid
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
        write_budget_acquired: bool = False,
    ) -> ApiResponse:
        """Perform the single DEMO open write after the caller records SUBMITTING."""
        if body.get("action") != "open" or int(body.get("instrumentId", 0) or 0) <= 0:
            raise ValueError("prepared DEMO open body is invalid")
        return self._request(
            "POST",
            DEMO_CREATE_ORDER,
            body=body,
            request_id=request_id,
            write_budget_acquired=write_budget_acquired,
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
        write_budget_acquired: bool = False,
    ) -> ApiResponse:
        """Perform the single DEMO close write after the caller records SUBMITTING."""
        if position_id <= 0 or int(body.get("InstrumentID", 0) or 0) <= 0:
            raise ValueError("prepared DEMO close body is invalid")
        return self._request(
            "POST",
            f"{DEMO_CLOSE_PREFIX}{position_id}",
            body=body,
            request_id=request_id,
            close_priority=True,
            write_budget_acquired=write_budget_acquired,
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

        position_ids = [decode_broker_position_identity_v2(row) for row in positions]
        if len(position_ids) != len(set(position_ids)):
            raise ValueError("DEMO position identities are duplicated")
        open_order_identities = [decode_broker_order_identity_v2(row) for row in open_orders]
        pending_order_identities = [decode_broker_order_identity_v2(row) for row in pending_orders]

        def family_values(identities: list[BrokerOrderIdentityV2], family: str) -> list[str]:
            return [
                value for identity in identities if (value := getattr(identity, family)) is not None
            ]

        open_broker_ids = family_values(open_order_identities, "order_id")
        pending_broker_ids = family_values(pending_order_identities, "order_id")
        open_references = family_values(open_order_identities, "client_reference_id")
        pending_references = family_values(pending_order_identities, "client_reference_id")
        if (
            len(open_broker_ids) != len(set(open_broker_ids))
            or len(pending_broker_ids) != len(set(pending_broker_ids))
            or len(open_references) != len(set(open_references))
            or len(pending_references) != len(set(pending_references))
        ):
            raise ValueError("DEMO order identities are duplicated")
        if (set(open_broker_ids) & set(pending_broker_ids)) or (
            set(open_references) & set(pending_references)
        ):
            raise ValueError("DEMO order collections overlap")

        foreign: list[str] = []
        invested = Decimal("0")
        unrealized = Decimal("0")
        gross = Decimal("0")
        for row in positions:
            position_id = decode_broker_position_identity_v2(row)
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
                foreign.append(f"mirror_order:{decode_broker_order_identity_v2(row).display_id}")
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
