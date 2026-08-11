from __future__ import annotations

import argparse
import contextlib
import json
import os
import pwd
import signal
import socket
import struct
import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .codec_v2 import decode_dataclass
from .config_v2 import AppConfigV2, load_config_v2
from .domain_v2 import BPS, OrderCommand, Side, canonical_json, utc
from .risk import load_private_signing_key, load_public_verifying_key
from .risk_seal_v2 import (
    SOL_MASTER_CLOSE,
    SOL_MASTER_OPEN,
    RiskCommandSignerV2,
    RiskCommandVerifierV2,
    risk_mandate_hash,
)
from .systemd_notify_v2 import ready, watchdog

MAX_FRAME_BYTES = 128 * 1024
PROTOCOL_VERSION = 1


def _wire_command(command: OrderCommand) -> dict[str, Any]:
    value = json.loads(canonical_json(asdict(command)))
    if not isinstance(value, dict):  # pragma: no cover - dataclass serialization invariant
        raise TypeError("risk command did not serialize as an object")
    return value


def _read_frame(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = connection.recv(min(16 * 1024, MAX_FRAME_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FRAME_BYTES:
            raise ValueError("risk signer request exceeds the frame limit")
        if b"\n" in chunk:
            break
    frame = b"".join(chunks)
    if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
        raise ValueError("risk signer request must contain exactly one JSON frame")
    return frame[:-1]


def _write_frame(connection: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("risk signer response exceeds the frame limit")
    connection.sendall(payload)


def validate_signing_request(
    command: OrderCommand,
    config: AppConfigV2,
    *,
    now: datetime,
) -> None:
    """Revalidate the fixed DEMO mandate before the isolated key is used."""

    current = utc(now)
    if command.risk_seal or command.risk_payload_hash:
        raise PermissionError("risk signer accepts only unsigned commands")
    if command.signature_algorithm != "Ed25519" or command.account_mode != "DEMO":
        raise PermissionError("risk signer is fixed to Ed25519 DEMO commands")
    expected_source = SOL_MASTER_CLOSE if command.reduce_only else SOL_MASTER_OPEN
    if (
        command.proposal_source != expected_source
        or command.proposal_source not in config.allowed_proposal_sources
    ):
        raise PermissionError("risk signer proposal source is outside the standing DEMO mandate")
    if command.risk_config_hash != risk_mandate_hash(config.mandate):
        raise PermissionError("risk signer mandate hash mismatch")
    if command.symbol not in config.mandate.allowed_symbols or command.symbol not in config.symbols:
        raise PermissionError("risk signer symbol is outside the fixed catalog")
    if command.created_at > current + timedelta(seconds=5):
        raise PermissionError("risk signer command time is in the future")
    if command.expires_at <= current or command.expires_at - command.created_at > timedelta(
        seconds=90
    ):
        raise PermissionError("risk signer command lifetime is invalid")
    if len(canonical_json(asdict(command)).encode("utf-8")) > 64 * 1024:
        raise PermissionError("risk signer command is oversized")

    mandate = config.mandate
    if command.reduce_only:
        full_close = (
            command.units_to_deduct is None
            and command.quantity is not None
            and command.reduce_position_quantity is not None
            and command.quantity == command.reduce_position_quantity
        )
        partial_close = (
            command.units_to_deduct is not None
            and command.quantity is not None
            and command.reduce_position_quantity is not None
            and command.units_to_deduct == command.quantity
            and command.quantity < command.reduce_position_quantity
        )
        if (
            command.amount_usd != 0
            or command.quantity is None
            or not (full_close or partial_close)
            or not (command.broker_position_id or "").strip()
        ):
            raise PermissionError("risk signer reduce-only identity or units are invalid")
        return

    rule = config.broker_rules.get(command.symbol)
    if rule is None:
        raise PermissionError("risk signer lacks fixed broker rules for the symbol")
    reference = cast(Decimal, command.reference_entry)
    minimum = cast(Decimal, command.min_acceptable_entry)
    maximum = cast(Decimal, command.max_acceptable_entry)
    stop_fraction = cast(Decimal, command.stop_loss_fraction)
    take_fraction = cast(Decimal, command.take_profit_fraction)
    slippage_bps = cast(Decimal, command.max_slippage_bps)
    max_loss = cast(Decimal, command.max_loss_usd)
    loss_budget = cast(Decimal, command.available_loss_budget_usd)
    notional_budget = cast(Decimal, command.available_notional_budget_usd)
    slots = cast(int, command.available_order_slots)
    if command.quantity is not None or command.units_to_deduct is not None:
        raise PermissionError("risk signer open command cannot carry reduce-only units")
    if command.broker_position_id is not None:
        raise PermissionError("risk signer open command cannot target a broker position")
    if command.amount_usd > mandate.max_order_usd or command.amount_usd > notional_budget:
        raise PermissionError("risk signer order exceeds the sealed notional budget")
    if not rule.min_stop_fraction <= stop_fraction <= rule.max_stop_fraction:
        raise PermissionError("risk signer stop fraction is outside fixed broker rules")
    if take_fraction <= 0:
        raise PermissionError("risk signer take-profit fraction is invalid")
    if not 0 <= slippage_bps <= mandate.max_mid_drift_bps:
        raise PermissionError("risk signer slippage bound exceeds the mandate")
    band_bps = max(abs(minimum / reference - 1), abs(maximum / reference - 1)) * BPS
    if band_bps > mandate.max_mid_drift_bps:
        raise PermissionError("risk signer execution band exceeds the mandate")
    projected_loss = command.amount_usd * (stop_fraction + slippage_bps / BPS)
    if projected_loss > max_loss or max_loss > mandate.max_trade_risk_usd or max_loss > loss_budget:
        raise PermissionError("risk signer projected loss exceeds the sealed budget")
    if slots < 1 or slots > mandate.max_open_positions:
        raise PermissionError("risk signer order-slot budget is invalid")
    entry = reference
    stop = entry * (1 - stop_fraction if command.side is Side.BUY else 1 + stop_fraction)
    take = entry * (1 + take_fraction if command.side is Side.BUY else 1 - take_fraction)
    if command.side is Side.BUY and not stop < entry < take:
        raise PermissionError("risk signer long stop/take direction is invalid")
    if command.side is Side.SELL and not take < entry < stop:
        raise PermissionError("risk signer short stop/take direction is invalid")


class SocketRiskCommandSignerV2:
    """Public-key-verifying client for the isolated local signer service."""

    def __init__(
        self,
        socket_path: str | Path,
        public_key: Ed25519PublicKey,
        *,
        expected_risk_config_hash: str,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout_seconds = timeout_seconds
        self._verifier = RiskCommandVerifierV2(
            public_key,
            expected_risk_config_hash=expected_risk_config_hash,
        )

    @classmethod
    def from_public_key_file(
        cls,
        socket_path: str | Path,
        public_key_path: str | Path,
        *,
        expected_risk_config_hash: str,
        timeout_seconds: float = 2.0,
    ) -> SocketRiskCommandSignerV2:
        return cls(
            socket_path,
            load_public_verifying_key(public_key_path),
            expected_risk_config_hash=expected_risk_config_hash,
            timeout_seconds=timeout_seconds,
        )

    def verifier(self, *, expected_risk_config_hash: str) -> RiskCommandVerifierV2:
        if expected_risk_config_hash != self._verifier.expected_risk_config_hash:
            raise ValueError("risk signer client mandate hash mismatch")
        return self._verifier

    def seal(self, command: OrderCommand) -> OrderCommand:
        if command.risk_seal or command.risk_payload_hash:
            raise ValueError("risk signer client accepts only unsigned commands")
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "command": _wire_command(command),
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_seconds)
            client.connect(self.socket_path)
            _write_frame(client, request)
            raw = _read_frame(client)
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
            raise RuntimeError("risk signer returned an invalid protocol envelope")
        if set(value) == {"protocol_version", "error"}:
            raise PermissionError("isolated risk signer rejected the command")
        if set(value) != {"protocol_version", "command"} or not isinstance(value["command"], dict):
            raise RuntimeError("risk signer response schema is invalid")
        sealed = decode_dataclass(OrderCommand, value["command"])
        unsigned_response = _wire_command(sealed)
        unsigned_response["risk_payload_hash"] = ""
        unsigned_response["risk_seal"] = ""
        if unsigned_response != _wire_command(command):
            raise PermissionError("risk signer changed the unsigned command")
        if not self._verifier.verify(sealed):
            raise PermissionError("risk signer returned an invalid or expired signature")
        return sealed


class RiskSignerServerV2:
    def __init__(
        self,
        *,
        socket_path: str | Path,
        config: AppConfigV2,
        signer: RiskCommandSignerV2,
        allowed_peer_uid: int,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.config = config
        self.signer = signer
        self.allowed_peer_uid = allowed_peer_uid
        self._stop = False
        self._ready = threading.Event()

    def wait_until_ready(self, timeout_seconds: float) -> bool:
        return self._ready.wait(timeout_seconds)

    @staticmethod
    def _peer_uid(connection: socket.socket) -> int:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _, uid, _ = struct.unpack("3i", raw)
        return uid

    def stop(self, *_: object) -> None:
        self._stop = True

    def _handle(self, connection: socket.socket) -> None:
        if self._peer_uid(connection) != self.allowed_peer_uid:
            raise PermissionError("risk signer peer is not authorized")
        value = json.loads(_read_frame(connection))
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol_version", "command"}
            or value.get("protocol_version") != PROTOCOL_VERSION
            or not isinstance(value.get("command"), dict)
        ):
            raise ValueError("risk signer request schema is invalid")
        command = decode_dataclass(OrderCommand, value["command"])
        validate_signing_request(command, self.config, now=datetime.now(UTC))
        sealed = self.signer.seal(command)
        _write_frame(
            connection,
            {"protocol_version": PROTOCOL_VERSION, "command": _wire_command(sealed)},
        )

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        previous_umask = os.umask(0o007)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(self.socket_path))
                # The requesters group needs connect access; SO_PEERCRED still enforces the UID.
                os.chmod(self.socket_path, 0o660)  # nosec B103
                listener.listen(16)
                listener.settimeout(1.0)
                self._ready.set()
                ready()
                while not self._stop:
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        watchdog()
                        continue
                    with connection:
                        connection.settimeout(2.0)
                        try:
                            self._handle(connection)
                        except Exception as exc:
                            with contextlib.suppress(OSError):
                                _write_frame(
                                    connection,
                                    {
                                        "protocol_version": PROTOCOL_VERSION,
                                        "error": type(exc).__name__,
                                    },
                                )
        finally:
            self._ready.clear()
            os.umask(previous_umask)
            self.socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated no-network v2 DEMO risk signer")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--allowed-peer-user", required=True)
    args = parser.parse_args()
    config = load_config_v2(args.config)
    allowed_uid = pwd.getpwnam(args.allowed_peer_user).pw_uid
    server = RiskSignerServerV2(
        socket_path=args.socket,
        config=config,
        signer=RiskCommandSignerV2(load_private_signing_key(args.private_key)),
        allowed_peer_uid=allowed_uid,
    )
    signal.signal(signal.SIGTERM, server.stop)
    signal.signal(signal.SIGINT, server.stop)
    server.serve_forever()


if __name__ == "__main__":
    main()
