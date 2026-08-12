from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def load_private_signing_key(path: str | Path) -> Ed25519PrivateKey:
    key_path = Path(path)
    if key_path.is_symlink():
        raise PermissionError("signing key must not be a symlink")
    metadata = key_path.stat()
    mode = metadata.st_mode & 0o777
    credential_directory = os.getenv("CREDENTIALS_DIRECTORY", "")
    protected_systemd_credential = (
        bool(credential_directory)
        and key_path.is_absolute()
        and key_path.parent == Path(credential_directory)
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and metadata.st_gid == 0
        and mode == 0o440
    )
    if mode & 0o077 and not protected_systemd_credential:
        raise PermissionError("signing key must not be readable by group or others")
    seed = key_path.read_bytes()
    if len(seed) != 32:
        raise ValueError("signing key file must contain exactly 32 raw bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def load_public_verifying_key(path: str | Path) -> Ed25519PublicKey:
    key_path = Path(path)
    if key_path.is_symlink():
        raise PermissionError("verifying key must not be a symlink")
    metadata = key_path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise PermissionError("verifying key must be a regular file")
    if metadata.st_mode & 0o022:
        raise PermissionError("verifying key must not be writable by group or others")
    raw = key_path.read_bytes()
    if len(raw) != 32:
        raise ValueError("verifying key file must contain exactly 32 raw bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def generate_private_signing_key(path: str | Path) -> None:
    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    seed = Ed25519PrivateKey.generate().private_bytes(
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


def generate_signing_keypair(private_path: str | Path, public_path: str | Path) -> None:
    private_key_path = Path(private_path)
    public_key_path = Path(public_path)
    if private_key_path.exists() or public_key_path.exists():
        raise FileExistsError("signing keypair already exists")
    generate_private_signing_key(private_key_path)
    public_created = False
    try:
        public = (
            load_private_signing_key(private_key_path)
            .public_key()
            .public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        )
        descriptor = os.open(public_key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        public_created = True
        try:
            os.write(descriptor, public)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(public_key_path, 0o644)
    except Exception:
        if public_created:
            public_key_path.unlink(missing_ok=True)
        private_key_path.unlink(missing_ok=True)
        raise
