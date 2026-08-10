from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


@dataclass(frozen=True)
class AuditAnchor:
    anchor_id: str
    head_event_hash: str
    signature: str
    algorithm: str
    anchored_at: str
    destination: str


class AuditAnchorWriter:
    """Signs the event-chain head and writes an append-only anchor artifact off-host/NAS."""

    def __init__(self, private_key_path: str | Path, destination: str | Path) -> None:
        self.private_key_path = Path(private_key_path)
        self.destination = Path(destination)
        self.destination.mkdir(parents=True, exist_ok=True)
        if self.private_key_path.stat().st_mode & 0o077:
            raise PermissionError("anchor signing key must be mode 0600 or stricter")
        raw = self.private_key_path.read_bytes()
        if len(raw) != 32:
            raise ValueError("anchor signing key must contain 32 raw Ed25519 bytes")
        self.key = Ed25519PrivateKey.from_private_bytes(raw)

    def anchor(self, head_event_hash: str, *, at: datetime | None = None) -> AuditAnchor:
        if len(head_event_hash) != 64 or any(c not in "0123456789abcdef" for c in head_event_hash):
            raise ValueError("event head must be a lowercase SHA-256 hash")
        timestamp = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        message = f"{head_event_hash}:{timestamp.isoformat()}".encode()
        signature = base64.urlsafe_b64encode(self.key.sign(message)).decode()
        anchor_id = f"anchor-{hashlib.sha256(message).hexdigest()[:24]}"
        anchor = AuditAnchor(
            anchor_id, head_event_hash, signature, "Ed25519", timestamp.isoformat(), str(self.destination)
        )
        target = self.destination / f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{anchor_id}.json"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, (json.dumps(anchor.__dict__, sort_keys=True) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return anchor

    @staticmethod
    def verify(anchor: AuditAnchor, public_key: bytes) -> bool:
        try:
            key = Ed25519PublicKey.from_public_bytes(public_key)
            message = f"{anchor.head_event_hash}:{anchor.anchored_at}".encode()
            key.verify(base64.urlsafe_b64decode(anchor.signature.encode()), message)
            return True
        except Exception:
            return False
