from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import asdict
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import RiskLimits
from .market import INSTRUMENTS_BY_SYMBOL
from .models import ApprovedOrder, CloseIntent, RiskContext, RiskResult, TradeIntent


DEMO_ORDER_ROUTE = "/api/v2/trading/execution/demo/orders"
DEMO_CLOSE_ROUTE_PATTERN = re.compile(
    r"^/api/v1/trading/execution/demo/market-close-orders/positions/([1-9]\d*)$"
)
_BODY_KEYS = frozenset(
    {
        "action",
        "transaction",
        "symbol",
        "settlementType",
        "orderType",
        "leverage",
        "amount",
        "orderCurrency",
        "stopLossRate",
        "takeProfitRate",
        "stopLossType",
    }
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return sorted(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def load_private_signing_key(path: str | Path) -> Ed25519PrivateKey:
    key_path = Path(path)
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError("risk signing key must not be readable by group or others")
    seed = key_path.read_bytes()
    if len(seed) != 32:
        raise ValueError("risk signing key file must contain exactly 32 raw bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_public_verifying_key(path: str | Path) -> Ed25519PublicKey:
    key_path = Path(path)
    mode = key_path.stat().st_mode & 0o777
    if mode & 0o022:
        raise PermissionError("risk verifying key must not be writable by group or others")
    raw = key_path.read_bytes()
    if len(raw) != 32:
        raise ValueError("risk verifying key file must contain exactly 32 raw bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def generate_private_signing_key(path: str | Path) -> None:
    """Provision a new local signer seed without ever returning or printing it."""

    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, seed)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def generate_signing_keypair(
    private_path: str | Path, public_path: str | Path
) -> None:
    private_key_path = Path(private_path)
    public_key_path = Path(public_path)
    if private_key_path.exists() or public_key_path.exists():
        raise FileExistsError("risk signing keypair already exists")
    generate_private_signing_key(private_key_path)
    public_created = False
    try:
        public = load_private_signing_key(private_key_path).public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        descriptor = os.open(
            public_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
        )
        public_created = True
        try:
            os.write(descriptor, public)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        if public_created:
            public_key_path.unlink(missing_ok=True)
        private_key_path.unlink(missing_ok=True)
        raise


class OrderVerifier:
    """Public-key-only verifier used by execution processes."""

    def __init__(self, limits: RiskLimits, public_key: bytes | Ed25519PublicKey) -> None:
        self.limits = limits
        self._public_key = (
            public_key
            if isinstance(public_key, Ed25519PublicKey)
            else Ed25519PublicKey.from_public_bytes(public_key)
        )

    @staticmethod
    def _canonical(order: ApprovedOrder) -> bytes:
        value = asdict(order)
        value.pop("seal", None)
        return canonical_json(value).encode()

    def verify(self, order: ApprovedOrder, now: int | None = None) -> bool:
        try:
            signature = base64.urlsafe_b64decode(order.seal.encode())
            self._public_key.verify(signature, self._canonical(order))
        except (InvalidSignature, ValueError, TypeError):
            return False
        current = int(time.time()) if now is None else now
        if order.signature_algorithm != "Ed25519" or order.account_mode != "DEMO":
            return False
        is_open = order.route == DEMO_ORDER_ROUTE
        is_close = DEMO_CLOSE_ROUTE_PATTERN.fullmatch(order.route) is not None
        if not (is_open or is_close) or order.method != "POST":
            return False
        if current > order.expires_at or current < order.issued_at - 5:
            return False
        if not order.request_id or not order.intent_hash or not order.risk_snapshot_hash:
            return False
        try:
            body = json.loads(order.body_json)
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(body, dict):
            return False
        if is_close:
            if frozenset(body) != frozenset({"InstrumentID", "UnitsToDeduct"}):
                return False
            try:
                instrument_id = int(body["InstrumentID"])
                units = body["UnitsToDeduct"]
                valid_units = units is None or Decimal(str(units)) > 0
            except (ValueError, TypeError):
                return False
            return instrument_id > 0 and valid_units
        if frozenset(body) != _BODY_KEYS:
            return False
        try:
            amount = Decimal(str(body["amount"]))
            stop_rate = Decimal(str(body["stopLossRate"]))
            take_rate = Decimal(str(body["takeProfitRate"]))
            leverage = int(body["leverage"])
        except (KeyError, ValueError, TypeError):
            return False
        return (
            body.get("action") == "open"
            and body.get("transaction") in {"buy", "sellShort"}
            and body.get("symbol") in self.limits.allowed_symbols
            and body.get("settlementType") == "cfd"
            and body.get("orderType") == "mkt"
            and body.get("orderCurrency") == "usd"
            and body.get("stopLossType") == "fixed"
            and Decimal("0") < amount <= self.limits.max_order_notional_usd
            and 1 <= leverage <= self.limits.max_leverage
            and stop_rate > 0
            and take_rate > 0
        )


class DeterministicRiskEngine:
    """Only this component can mint an execution-capable DEMO envelope."""

    def __init__(
        self,
        limits: RiskLimits,
        signing_key: bytes | Ed25519PrivateKey | None = None,
    ) -> None:
        if signing_key is None:
            signing_key = Ed25519PrivateKey.generate()
        elif isinstance(signing_key, bytes):
            if len(signing_key) != 32:
                raise ValueError("Ed25519 private seed must be exactly 32 bytes")
            signing_key = Ed25519PrivateKey.from_private_bytes(signing_key)
        self.limits = limits
        self._private_key = signing_key
        self._public_key = signing_key.public_key()
        self.risk_config_hash = canonical_hash(asdict(limits))

    def public_key_bytes(self) -> bytes:
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def verifier(self) -> OrderVerifier:
        return OrderVerifier(self.limits, self._public_key)

    def evaluate(self, intent: TradeIntent, context: RiskContext) -> RiskResult:
        reasons: list[str] = []
        symbol = intent.symbol.strip().upper()
        notional = intent.amount_usd * Decimal(intent.leverage)
        now = context.evaluated_at or int(time.time())
        if context.kill_switch_active:
            reasons.append("kill_switch_active")
        if not context.data_quality_ok:
            reasons.append("data_quality_failure")
        if not context.audit_writable:
            reasons.append("audit_unavailable")
        if not context.reconciliation_ok:
            reasons.append("reconciliation_drift")
        if symbol not in self.limits.allowed_symbols:
            reasons.append("symbol_not_allowed")
        if intent.amount_usd <= 0 or intent.amount_usd > self.limits.max_order_notional_usd:
            reasons.append("order_notional_limit")
        symbol_leverage_limit = 1 if symbol in {"AAPL", "TSLA", "BTC", "ETH"} else self.limits.max_leverage
        if intent.leverage < 1 or intent.leverage > symbol_leverage_limit:
            reasons.append("leverage_limit")
        if notional * intent.stop_loss_fraction > self.limits.max_trade_risk_usd:
            reasons.append("trade_risk_limit")
        if context.symbol_exposure_usd + notional > self.limits.max_symbol_exposure_usd:
            reasons.append("symbol_exposure_limit")
        if context.gross_exposure_usd + notional > self.limits.max_gross_exposure_usd:
            reasons.append("gross_exposure_limit")
        if context.correlated_exposure_usd + notional > self.limits.max_correlated_exposure_usd:
            reasons.append("correlated_exposure_limit")
        if context.open_positions >= self.limits.max_open_positions:
            reasons.append("open_position_limit")
        if context.trades_today >= self.limits.max_trades_per_day:
            reasons.append("trade_count_limit")
        if context.daily_pnl_usd <= -self.limits.max_daily_loss_usd:
            reasons.append("daily_loss_limit")
        if context.weekly_pnl_usd <= -self.limits.max_weekly_loss_usd:
            reasons.append("weekly_loss_limit")
        if context.monthly_pnl_usd <= -self.limits.max_monthly_loss_usd:
            reasons.append("monthly_loss_limit")
        if context.last_trade_at and now - context.last_trade_at < self.limits.min_trade_interval_seconds:
            reasons.append("trade_cooldown")
        if context.quote_observed_at and (
            now < context.quote_observed_at
            or now - context.quote_observed_at > self.limits.max_quote_age_seconds
        ):
            reasons.append("stale_quote")
        if context.peak_equity_usd <= 0 or context.equity_usd <= 0:
            reasons.append("invalid_equity")
        elif (
            context.peak_equity_usd - context.equity_usd
        ) / context.peak_equity_usd >= self.limits.max_drawdown_fraction:
            reasons.append("drawdown_limit")
        if not self.limits.min_stop_loss_fraction <= intent.stop_loss_fraction <= self.limits.max_stop_loss_fraction:
            reasons.append("stop_loss_limit")
        if intent.take_profit_fraction <= 0:
            reasons.append("invalid_take_profit")
        if not Decimal("0") <= intent.confidence <= Decimal("1"):
            reasons.append("invalid_confidence")
        if context.bid <= 0 or context.ask <= 0 or context.ask < context.bid:
            reasons.append("invalid_market_price")
        else:
            mid = (context.bid + context.ask) / Decimal("2")
            if mid <= 0 or (context.ask - context.bid) / mid > self.limits.max_spread_fraction:
                reasons.append("spread_limit")
        if reasons:
            return RiskResult(False, tuple(sorted(set(reasons))))

        is_buy = intent.side.value == "buy"
        stop_loss_rate = (
            context.bid * (Decimal("1") - intent.stop_loss_fraction)
            if is_buy
            else context.ask * (Decimal("1") + intent.stop_loss_fraction)
        )
        take_profit_rate = (
            context.ask * (Decimal("1") + intent.take_profit_fraction)
            if is_buy
            else context.bid * (Decimal("1") - intent.take_profit_fraction)
        )
        body = {
            "action": "open",
            "transaction": "buy" if is_buy else "sellShort",
            "symbol": symbol,
            "settlementType": "cfd",
            "orderType": "mkt",
            "leverage": intent.leverage,
            "amount": float(intent.amount_usd),
            "orderCurrency": "usd",
            "stopLossRate": float(stop_loss_rate),
            "takeProfitRate": float(take_profit_rate),
            "stopLossType": "fixed",
        }
        intent_hash = canonical_hash(asdict(intent))
        risk_snapshot_hash = canonical_hash(asdict(context))
        # The risk decision and hashes remain deterministic; the broker request
        # identity must be unique so two evaluations in the same second cannot
        # share an approval or idempotency key.
        proposal_id = str(uuid.uuid4())
        unsigned: dict[str, object] = {
            "proposal_id": proposal_id,
            "route": DEMO_ORDER_ROUTE,
            "method": "POST",
            "body_json": canonical_json(body),
            "issued_at": now,
            "expires_at": now + self.limits.approval_ttl_seconds,
            "account_mode": "DEMO",
            "request_id": proposal_id,
            "intent_hash": intent_hash,
            "risk_snapshot_hash": risk_snapshot_hash,
            "risk_config_hash": self.risk_config_hash,
            "quote_observed_at": context.quote_observed_at or now,
            "signature_algorithm": "Ed25519",
        }
        signature = self._private_key.sign(canonical_json(unsigned).encode())
        seal = base64.urlsafe_b64encode(signature).decode()
        return RiskResult(True, (), ApprovedOrder(**unsigned, seal=seal))

    def evaluate_close(self, intent: CloseIntent, context: RiskContext) -> RiskResult:
        """Mint a sealed DEMO reduce-only close; kill state never blocks exits."""

        reasons: list[str] = []
        symbol = intent.symbol.strip().upper()
        now = context.evaluated_at or int(time.time())
        if symbol not in self.limits.allowed_symbols:
            reasons.append("symbol_not_allowed")
        expected_instrument = INSTRUMENTS_BY_SYMBOL.get(symbol)
        if (
            intent.position_id <= 0
            or intent.instrument_id <= 0
            or expected_instrument is None
            or expected_instrument.instrument_id != intent.instrument_id
        ):
            reasons.append("invalid_position_identity")
        if intent.units_to_deduct is not None and intent.units_to_deduct <= 0:
            reasons.append("invalid_close_units")
        if not context.audit_writable:
            reasons.append("audit_unavailable")
        if not context.reconciliation_ok:
            reasons.append("reconciliation_drift")
        if reasons:
            return RiskResult(False, tuple(sorted(set(reasons))))
        route = (
            "/api/v1/trading/execution/demo/market-close-orders/positions/"
            f"{intent.position_id}"
        )
        body = {
            "InstrumentID": intent.instrument_id,
            "UnitsToDeduct": (
                None if intent.units_to_deduct is None else float(intent.units_to_deduct)
            ),
        }
        unsigned: dict[str, object] = {
            "proposal_id": str(uuid.uuid4()),
            "route": route,
            "method": "POST",
            "body_json": canonical_json(body),
            "issued_at": now,
            "expires_at": now + self.limits.approval_ttl_seconds,
            "account_mode": "DEMO",
            "request_id": "",
            "intent_hash": canonical_hash(asdict(intent)),
            "risk_snapshot_hash": canonical_hash(asdict(context)),
            "risk_config_hash": self.risk_config_hash,
            "quote_observed_at": context.quote_observed_at or now,
            "signature_algorithm": "Ed25519",
        }
        unsigned["request_id"] = unsigned["proposal_id"]
        signature = self._private_key.sign(canonical_json(unsigned).encode())
        return RiskResult(
            True,
            (),
            ApprovedOrder(
                **unsigned,
                seal=base64.urlsafe_b64encode(signature).decode(),
            ),
        )

    def verify(self, order: ApprovedOrder, now: int | None = None) -> bool:
        """Compatibility shim; executors should receive ``verifier()`` only."""
        return self.verifier().verify(order, now)
