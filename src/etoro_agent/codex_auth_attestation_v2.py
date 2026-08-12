from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CodexAuthAttestationV2:
    auth_mode: str
    account_id_sha256: str
    executable_sha256: str
    model: str
    platform_api_fallback: bool = False


def _trusted_hash(path: str | Path, label: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if _SHA256.fullmatch(value) is None:
        raise PermissionError(f"trusted {label} hash is invalid")
    return value


def attest_chatgpt_codex(
    *,
    auth_path: str | Path,
    executable_path: str | Path,
    trusted_account_hash_path: str | Path,
    trusted_executable_hash_path: str | Path,
    model: str,
) -> CodexAuthAttestationV2:
    if any(
        os.getenv(name, "").strip()
        for name in (
            "OPENAI_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "AZURE_OPENAI_ENDPOINT",
        )
    ):
        raise PermissionError("platform API fallback environment is forbidden")
    try:
        raw = json.loads(Path(auth_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        raise PermissionError("Codex authentication record is unavailable") from exc
    if not isinstance(raw, dict) or raw.get("auth_mode") != "chatgpt":
        raise PermissionError("Codex must use ChatGPT authentication")
    if raw.get("OPENAI_API_KEY") not in (None, ""):
        raise PermissionError("Codex platform API key fallback is forbidden")
    tokens = raw.get("tokens")
    if not isinstance(tokens, dict):
        raise PermissionError("ChatGPT token envelope is invalid")
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        raise PermissionError("ChatGPT account identity is unavailable")
    account_hash = hashlib.sha256(account_id.strip().encode()).hexdigest()
    if account_hash != _trusted_hash(trusted_account_hash_path, "ChatGPT account"):
        raise PermissionError("ChatGPT account identity is not deployment-trusted")
    executable = Path(executable_path)
    if not executable.is_file() or executable.is_symlink():
        raise PermissionError("Codex executable identity is invalid")
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    if executable_hash != _trusted_hash(trusted_executable_hash_path, "Codex executable"):
        raise PermissionError("Codex executable is not deployment-trusted")
    if model != "gpt-5.6-sol":
        raise PermissionError("Codex model policy is not deployment-trusted")
    return CodexAuthAttestationV2(
        "chatgpt",
        account_hash,
        executable_hash,
        model,
        False,
    )
