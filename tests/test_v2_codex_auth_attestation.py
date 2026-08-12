from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from etoro_agent.codex_auth_attestation_v2 import attest_chatgpt_codex


class CodexAuthAttestationV2Tests(unittest.TestCase):
    def test_chatgpt_identity_and_executable_are_pinned_without_platform_fallback(self) -> None:
        with TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            auth = root / "auth.json"
            executable = root / "codex"
            account = root / "account.sha256"
            binary = root / "binary.sha256"
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "OPENAI_API_KEY": None,
                        "tokens": {"account_id": "owner-account"},
                    }
                ),
                encoding="utf-8",
            )
            executable.write_bytes(b"trusted-codex")
            account.write_text(hashlib.sha256(b"owner-account").hexdigest(), encoding="utf-8")
            binary.write_text(hashlib.sha256(b"trusted-codex").hexdigest(), encoding="utf-8")
            result = attest_chatgpt_codex(
                auth_path=auth,
                executable_path=executable,
                trusted_account_hash_path=account,
                trusted_executable_hash_path=binary,
                model="gpt-5.6-sol",
            )
            self.assertEqual(result.auth_mode, "chatgpt")
            self.assertFalse(result.platform_api_fallback)
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "forbidden"}),
                self.assertRaisesRegex(PermissionError, "fallback"),
            ):
                attest_chatgpt_codex(
                    auth_path=auth,
                    executable_path=executable,
                    trusted_account_hash_path=account,
                    trusted_executable_hash_path=binary,
                    model="gpt-5.6-sol",
                )

    def test_account_or_executable_drift_fails_closed(self) -> None:
        with TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            root = Path(folder)
            auth = root / "auth.json"
            executable = root / "codex"
            account = root / "account.sha256"
            binary = root / "binary.sha256"
            auth.write_text(
                '{"auth_mode":"chatgpt","OPENAI_API_KEY":null,"tokens":{"account_id":"other"}}',
                encoding="utf-8",
            )
            executable.write_bytes(b"binary")
            account.write_text("0" * 64, encoding="utf-8")
            binary.write_text(hashlib.sha256(b"binary").hexdigest(), encoding="utf-8")
            with self.assertRaisesRegex(PermissionError, "account identity"):
                attest_chatgpt_codex(
                    auth_path=auth,
                    executable_path=executable,
                    trusted_account_hash_path=account,
                    trusted_executable_hash_path=binary,
                    model="gpt-5.6-sol",
                )


if __name__ == "__main__":
    unittest.main()
