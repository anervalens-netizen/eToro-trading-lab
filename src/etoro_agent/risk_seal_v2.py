from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .domain_v2 import OrderCommand, canonical_hash, canonical_json, utc
from .risk_v2 import CapitalMandate

SOL_MASTER_OPEN = "sol_master_open"
SOL_MASTER_CLOSE = "sol_master_close"
STANDING_DEMO_PROPOSAL_SOURCES = frozenset({SOL_MASTER_OPEN, SOL_MASTER_CLOSE})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def risk_mandate_hash(mandate: CapitalMandate) -> str:
    value = asdict(mandate)
    if "allowed_symbols" in value:
        value["allowed_symbols"] = sorted(value["allowed_symbols"])
    return canonical_hash(value)


def _payload(command: OrderCommand) -> bytes:
    value = asdict(command)
    value.pop("risk_payload_hash", None)
    value.pop("risk_seal", None)
    return canonical_json(value).encode("utf-8")


def _payload_hash(command: OrderCommand) -> str:
    return hashlib.sha256(_payload(command)).hexdigest()


class RiskCommandVerifierV2:
    """Public-key-only execution boundary for persisted v2 commands."""

    def __init__(
        self,
        public_key: Ed25519PublicKey,
        *,
        expected_risk_config_hash: str,
        allowed_sources: frozenset[str] = STANDING_DEMO_PROPOSAL_SOURCES,
    ) -> None:
        if not _SHA256.fullmatch(expected_risk_config_hash):
            raise ValueError("expected v2 risk config hash is invalid")
        if not allowed_sources or not allowed_sources <= STANDING_DEMO_PROPOSAL_SOURCES:
            raise ValueError("v2 proposal source allowlist exceeds the DEMO standing mandate")
        self._public_key = public_key
        self.expected_risk_config_hash = expected_risk_config_hash
        self.allowed_sources = allowed_sources

    def verify(self, command: OrderCommand, *, now: datetime | None = None) -> bool:
        current = utc(now or datetime.now(command.created_at.tzinfo))
        expected_source = SOL_MASTER_CLOSE if command.reduce_only else SOL_MASTER_OPEN
        if (
            command.account_mode != "DEMO"
            or command.signature_algorithm != "Ed25519"
            or command.proposal_source != expected_source
            or command.proposal_source not in self.allowed_sources
            or command.risk_config_hash != self.expected_risk_config_hash
            or not _SHA256.fullmatch(command.risk_config_hash)
            or not _SHA256.fullmatch(command.risk_payload_hash)
            or current > command.expires_at
            or current < command.created_at - timedelta(seconds=5)
        ):
            return False
        digest = _payload_hash(command)
        if not hmac.compare_digest(digest, command.risk_payload_hash):
            return False
        try:
            signature = base64.b64decode(
                command.risk_seal.encode("ascii"), altchars=b"-_", validate=True
            )
            self._public_key.verify(signature, digest.encode("ascii"))
        except (InvalidSignature, ValueError, TypeError, UnicodeError):
            return False
        return True


class RiskCommandSealerV2(Protocol):
    """Minimal authority exposed to the deterministic trading kernel."""

    def seal(self, command: OrderCommand) -> OrderCommand: ...


class RiskCommandSignerV2:
    """Deterministic risk component that alone can mint executable v2 commands."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._private_key = private_key

    @classmethod
    def generate(cls) -> RiskCommandSignerV2:
        return cls(Ed25519PrivateKey.generate())

    def seal(self, command: OrderCommand) -> OrderCommand:
        expected_source = SOL_MASTER_CLOSE if command.reduce_only else SOL_MASTER_OPEN
        if command.account_mode != "DEMO" or command.proposal_source != expected_source:
            raise PermissionError("v2 command is outside the fixed DEMO proposal boundary")
        if not _SHA256.fullmatch(command.risk_config_hash):
            raise ValueError("v2 command risk config hash is invalid")
        digest = _payload_hash(command)
        signature = self._private_key.sign(digest.encode("ascii"))
        return replace(
            command,
            risk_payload_hash=digest,
            risk_seal=base64.urlsafe_b64encode(signature).decode("ascii"),
        )

    def verifier(
        self,
        *,
        expected_risk_config_hash: str,
        allowed_sources: frozenset[str] = STANDING_DEMO_PROPOSAL_SOURCES,
    ) -> RiskCommandVerifierV2:
        return RiskCommandVerifierV2(
            self._private_key.public_key(),
            expected_risk_config_hash=expected_risk_config_hash,
            allowed_sources=allowed_sources,
        )
