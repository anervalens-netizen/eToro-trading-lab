from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from etoro_agent import executor_service_postgres_v2


class V2CanonicalPathTests(unittest.TestCase):
    def test_production_executor_uses_current_api_and_postgres(self) -> None:
        source = inspect.getsource(executor_service_postgres_v2)
        self.assertIn("PostgresRuntimeStoreV2", source)
        self.assertIn("DemoExecutionWorkerCurrentV2", source)
        self.assertNotIn("etoro_api_v2 import", source)

    def test_shadow_and_execution_decision_units_are_mutually_bounded(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops/systemd"
        shadow = (root / "etoro-v2-decision-apply.service").read_text(encoding="utf-8")
        execution = (root / "etoro-v2-decision-apply-execution.service").read_text(encoding="utf-8")
        self.assertIn("--shadow-only", shadow)
        self.assertIn("ConditionPathExists=!/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION", shadow)
        self.assertNotIn("etoro-demo-read-user-key", shadow)
        self.assertNotIn("ETORO_V2_RISK_SIGNER_SOCKET", shadow)
        self.assertIn("ConditionPathExists=/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION", execution)
        self.assertIn("etoro-demo-read-user-key", execution)
        self.assertNotIn("etoro-demo-write-user-key", execution)
        self.assertNotIn("v2-risk-signing.key", execution)
        self.assertIn("ETORO_V2_RISK_SIGNER_SOCKET", execution)

    def test_only_executor_canonical_unit_receives_write_key(self) -> None:
        root = Path(__file__).resolve().parents[1] / "ops/systemd"
        canonical = (root / "etoro-v2-executor-postgres.service").read_text(encoding="utf-8")
        self.assertIn("etoro-demo-write-user-key", canonical)
        for name in (
            "etoro-v2-coordinator.service",
            "etoro-v2-decision-apply.service",
            "etoro-v2-decision-apply-execution.service",
            "etoro-v2-role-apply.service",
            "etoro-v2-dashboard.service",
            "etoro-v2-market.service",
            "etoro-v2-reconciliation.service",
        ):
            self.assertNotIn("etoro-demo-write-user-key", (root / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
