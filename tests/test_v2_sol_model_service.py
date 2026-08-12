from __future__ import annotations

import importlib.resources
import json
import socket
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from etoro_agent.ai_v2 import AIRole, DecisionPacketV2
from etoro_agent.codex_auth_attestation_v2 import CodexAuthAttestationV2
from etoro_agent.sol_model_service_v2 import (
    PROTOCOL_VERSION,
    IsolatedModelError,
    IsolatedModelTransportError,
    SolModelClientV2,
    _read_frame,
    _write_frame,
    evaluate_claim,
    model_command,
    process_request,
)
from etoro_agent.sol_runner_v2 import submit_error

TEST_ATTESTATION = CodexAuthAttestationV2("chatgpt", "a" * 64, "b" * 64, "gpt-5.6-sol", False)


def claim(role: AIRole = AIRole.MARKET_REGIME_ANALYST) -> dict[str, object]:
    now = datetime(2026, 8, 12, 8, tzinfo=UTC)
    packet = DecisionPacketV2(
        "packet-model-service",
        now.isoformat(),
        (now + timedelta(minutes=5)).isoformat(),
        "D_sol_plus_critic",
        "ENTRY_REVIEW",
        ("market",),
        "feature",
        "b" * 64,
        "r" * 64,
        {},
        (),
        None,
        ("market",),
    )
    return {
        "packet_id": packet.packet_id,
        "packet_hash": packet.packet_hash,
        "packet": packet.__dict__,
        "role": role.value,
        "lane": packet.lane,
        "attempt": 1,
        "claim_token": "claim-token",
        "expires_at": packet.expires_at,
    }


class SolModelServiceV2Tests(unittest.TestCase):
    def test_model_schemas_use_the_strict_structured_output_subset(self) -> None:
        unsupported = {
            "$schema",
            "allOf",
            "exclusiveMaximum",
            "exclusiveMinimum",
            "if",
            "maxItems",
            "maxLength",
            "maxProperties",
            "maximum",
            "minItems",
            "minLength",
            "minProperties",
            "minimum",
            "then",
        }

        def verify(value) -> None:
            if isinstance(value, dict):
                self.assertFalse(unsupported & set(value))
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                    self.assertEqual(set(value.get("required", [])), set(value["properties"]))
                for child in value.values():
                    verify(child)
            elif isinstance(value, list):
                for child in value:
                    verify(child)

        package = importlib.resources.files("etoro_agent")
        for name in (
            "regime_v2.schema.json",
            "critic_v2.schema.json",
            "portfolio_decision_v2.schema.json",
        ):
            verify(json.loads(package.joinpath(name).read_text(encoding="utf-8")))

    @patch(
        "etoro_agent.sol_model_service_v2._model_attestation",
        return_value=TEST_ATTESTATION,
    )
    def test_evaluator_uses_direct_codex_argv_and_returns_strict_telemetry(
        self, _attestation: object
    ) -> None:
        seen: list[tuple[str, ...]] = []

        def run(command, *, input_text, timeout):
            seen.append(command)
            output = Path(command[command.index("--output-last-message") + 1])
            output.write_text(
                '{"regime_probabilities":{"trend":"1"},"event_risk":"LOW",'
                '"liquidity_risk":"LOW","evidence_refs":["market"],"summary":"trend"}',
                encoding="utf-8",
            )
            self.assertTrue(input_text)
            self.assertEqual(timeout, 240)
            return '{"usage":{"input_tokens":10,"output_tokens":5,"reasoning_tokens":2}}\n'

        output, telemetry = evaluate_claim(claim(), AIRole.MARKET_REGIME_ANALYST, run_command=run)
        self.assertEqual(output["event_risk"], "LOW")
        self.assertEqual(telemetry["run"]["input_tokens"], 10)
        self.assertEqual(telemetry["run"]["status"], "COMPLETED")
        self.assertIn("gpt-5.6-sol:chatgpt:", telemetry["attested_model_id"])
        self.assertNotIn("sudo", seen[0])
        self.assertNotIn("systemd-run", seen[0])
        self.assertIn("--ephemeral", seen[0])
        self.assertIn("read-only", seen[0])

    def test_process_request_rejects_role_mismatch(self) -> None:
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "role": AIRole.ADVERSARIAL_CRITIC.value,
            "claim": claim(AIRole.MARKET_REGIME_ANALYST),
        }
        with self.assertRaisesRegex(ValueError, "role mismatch"):
            process_request(request)

    def test_one_frame_protocol_rejects_trailing_payload(self) -> None:
        with tempfile.TemporaryFile() as stream:
            stream.write(b'{"protocol_version":1}\n{}\n')
            stream.seek(0)
            with self.assertRaisesRegex(ValueError, "exactly one frame"):
                _read_frame(stream)

    def test_socket_client_round_trip_uses_strict_one_frame_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "model.sock"
            ready = threading.Event()

            def server() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        with connection.makefile("rb") as reader:
                            request = _read_frame(reader)
                        self.assertEqual(request["role"], AIRole.MARKET_REGIME_ANALYST.value)
                        with connection.makefile("wb") as writer:
                            _write_frame(
                                writer,
                                {
                                    "protocol_version": PROTOCOL_VERSION,
                                    "output": {"event_risk": "LOW"},
                                    "telemetry": {
                                        "prompt_hash": "p" * 64,
                                        "run": {},
                                        "attested_model_id": "gpt-5.6-sol:chatgpt:a:b:no-platform-fallback",
                                    },
                                },
                            )

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(2))
            output, telemetry = SolModelClientV2(path).evaluate(
                claim(), AIRole.MARKET_REGIME_ANALYST
            )
            thread.join(2)
            self.assertEqual(output, {"event_risk": "LOW"})
            self.assertEqual(telemetry["prompt_hash"], "p" * 64)

    def test_socket_client_preserves_terminal_and_retryable_error_types(self) -> None:
        def response_error(error_type: str) -> IsolatedModelError:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "model.sock"
                ready = threading.Event()

                def server() -> None:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                        listener.bind(str(path))
                        listener.listen(1)
                        ready.set()
                        connection, _ = listener.accept()
                        with connection:
                            with connection.makefile("rb") as reader:
                                _read_frame(reader)
                            with connection.makefile("wb") as writer:
                                _write_frame(
                                    writer,
                                    {
                                        "protocol_version": PROTOCOL_VERSION,
                                        "error_type": error_type,
                                    },
                                )

                thread = threading.Thread(target=server)
                thread.start()
                self.assertTrue(ready.wait(2))
                with self.assertRaises(IsolatedModelError) as caught:
                    SolModelClientV2(path).evaluate(claim(), AIRole.MARKET_REGIME_ANALYST)
                thread.join(2)
                return caught.exception

        for error_type, retryable in (("ValueError", False), ("RuntimeError", True)):
            with self.subTest(error_type=error_type):
                caught = response_error(error_type)
                self.assertEqual(caught.error_type, error_type)
                self.assertIs(caught.retryable, retryable)

    def test_runner_submits_preserved_model_error_classification(self) -> None:
        def submitted_envelope(error_type: str) -> dict[str, object]:
            envelopes: list[dict[str, object]] = []

            def run(command, *, input_text=None, timeout=120):
                self.assertTrue(command)
                self.assertEqual(timeout, 30)
                envelopes.append(json.loads(input_text))
                return ""

            with patch("etoro_agent.sol_runner_v2._run", side_effect=run):
                submit_error(
                    claim(),
                    AIRole.MARKET_REGIME_ANALYST,
                    IsolatedModelError(error_type),
                    time.monotonic(),
                )
            return envelopes[0]

        for error_type, retryable in (("JSONDecodeError", False), ("TimeoutExpired", True)):
            with self.subTest(error_type=error_type):
                envelope = submitted_envelope(error_type)
                self.assertEqual(envelope["run"]["error_type"], error_type)
                self.assertIs(envelope["retryable"], retryable)

    def test_socket_absence_and_timeout_are_retryable_transport_errors(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            absent = Path(folder) / "absent.sock"
            with self.assertRaises(IsolatedModelTransportError):
                SolModelClientV2(absent, timeout_seconds=0.05).evaluate(
                    claim(), AIRole.MARKET_REGIME_ANALYST
                )

            path = Path(folder) / "timeout.sock"
            ready = threading.Event()
            release = threading.Event()

            def server() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                    listener.bind(str(path))
                    listener.listen(1)
                    ready.set()
                    connection, _ = listener.accept()
                    with connection:
                        with connection.makefile("rb") as reader:
                            _read_frame(reader)
                        release.wait(2)

            thread = threading.Thread(target=server)
            thread.start()
            self.assertTrue(ready.wait(2))
            try:
                with self.assertRaises(IsolatedModelTransportError):
                    SolModelClientV2(path, timeout_seconds=0.05).evaluate(
                        claim(), AIRole.MARKET_REGIME_ANALYST
                    )
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())

        envelopes: list[dict[str, object]] = []

        def run(command, *, input_text=None, timeout=120):
            envelopes.append(json.loads(input_text))
            return ""

        with patch("etoro_agent.sol_runner_v2._run", side_effect=run):
            submit_error(
                claim(),
                AIRole.MARKET_REGIME_ANALYST,
                IsolatedModelTransportError("socket unavailable"),
                time.monotonic(),
            )
        self.assertEqual(envelopes[0]["run"]["error_type"], "IsolatedModelTransportError")
        self.assertIs(envelopes[0]["retryable"], True)

    def test_model_command_is_fixed_and_sandbox_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            command = model_command(root / "schema", root / "output", root)
        self.assertEqual(command[1:3], ("exec", "--model"))
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-1], "-")


if __name__ == "__main__":
    unittest.main()
