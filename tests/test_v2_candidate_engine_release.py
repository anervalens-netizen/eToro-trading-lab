from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from etoro_agent.backtest_v2 import HistoricalBar
from etoro_agent.candidates_v2 import CandidateEngineV2, generate_core_candidate_batch
from etoro_agent.parity_v2 import ParityHarnessV2
from etoro_agent.risk_v2 import CapitalMandate
from etoro_agent.strategy_release_v2 import (
    StrategyReleaseManifestV2,
    StrategyReleaseVerifierV2,
)
from etoro_agent.strategy_v2 import StrategyFamilyEngine


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def manifest_for(
    engine: CandidateEngineV2, now: datetime, **changes: object
) -> StrategyReleaseManifestV2:
    base = StrategyReleaseManifestV2(
        strategy_release_id="strategy-release-test-1",
        engine_version=engine.version,
        engine_hash=engine.engine_hash,
        parameters_hash=engine.parameters_hash,
        feature_schema_id="features-test-1",
        feature_schema_hash=engine.feature_schema_hash,
        calendar_release_id="calendar-test-1",
        calendar_hash=digest("calendar"),
        cost_model_release_id="cost-test-1",
        cost_model_hash=engine.cost_model_hash,
        observed_round_trip_cost_bps_p95={
            "AAPL": Decimal("12.5"),
            "EURUSD": Decimal("8.25"),
        },
        cost_observation_sample_size=100,
        cost_observed_through=now - timedelta(hours=1),
        cost_stress_multiple=Decimal("2"),
        point_in_time_dataset_id="dataset-test-1",
        point_in_time_dataset_hash=digest("dataset"),
        execution_simulator_id="simulator-test-1",
        execution_simulator_hash=digest("simulator"),
        oos_evidence_id="oos-test-1",
        oos_evidence_hash=digest("oos"),
        promotion_decision_id="promotion-test-1",
        promotion_evidence_hash=digest("promotion"),
        soak_evidence_id="soak-test-1",
        soak_evidence_hash=digest("soak"),
        oos_gate_passed=True,
        promotion_decision="PROMOTE",
        soak_gate_passed=True,
        adverse_execution_gate_passed=True,
        cost_stress_gate_passed=True,
        valid_from=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
    )
    return replace(base, **changes)


def verifier_for(
    engine: CandidateEngineV2, manifest: StrategyReleaseManifestV2
) -> StrategyReleaseVerifierV2:
    return StrategyReleaseVerifierV2(
        engine,
        trusted_manifest_hash=manifest.manifest_hash,
        expected_feature_schema_hash=engine.feature_schema_hash,
        expected_calendar_hash=digest("calendar"),
        expected_cost_model_hash=engine.cost_model_hash,
    )


class CanonicalCandidateEngineReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = CandidateEngineV2()
        self.closes = tuple(Decimal("100") + Decimal(index) for index in range(50))
        self.highs = tuple(value + Decimal("0.5") for value in self.closes)
        self.lows = tuple(value - Decimal("0.5") for value in self.closes)

    def test_candidate_batch_has_golden_hash_and_family_facade_delegates(self) -> None:
        batch = generate_core_candidate_batch(
            "AAPL",
            self.closes,
            self.highs,
            self.lows,
            input_snapshot_hash="a" * 64,
        )
        self.assertEqual(
            batch.batch_hash,
            "29f02cb2a41e63b3e7806c7c1e50a5ed47298face4d5d2df3613aea382f32b6e",
        )
        facade = StrategyFamilyEngine()
        self.assertEqual(
            facade.trend_breakout("AAPL", self.highs, self.lows, self.closes),
            self.engine.trend_breakout("AAPL", self.closes, self.highs, self.lows),
        )
        self.assertEqual(
            facade.mean_reversion("AAPL", self.closes),
            self.engine.mean_reversion("AAPL", self.closes),
        )
        self.assertEqual(
            facade.statistical_baseline("AAPL", self.closes),
            self.engine.statistical_baseline("AAPL", self.closes),
        )

    def test_strategy_release_verifier_requires_real_pinned_gate_evidence(self) -> None:
        now = datetime(2026, 8, 12, 12, tzinfo=UTC)
        manifest = manifest_for(self.engine, now)
        verified = verifier_for(self.engine, manifest).verify(manifest, now=now)
        self.assertEqual(verified.manifest_hash, manifest.manifest_hash)
        self.assertEqual(verified.stressed_cost_bps("aapl"), Decimal("25.0"))

        with self.assertRaisesRegex(PermissionError, "no observed cost"):
            verified.stressed_cost_bps("SPX500")

        unpromoted = manifest_for(self.engine, now, oos_gate_passed=False)
        with self.assertRaisesRegex(PermissionError, "oos_gate_not_passed"):
            verifier_for(self.engine, unpromoted).verify(unpromoted, now=now)

        mismatched = manifest_for(self.engine, now, engine_hash=digest("other-engine"))
        with self.assertRaisesRegex(PermissionError, "engine_hash_mismatch"):
            verifier_for(self.engine, mismatched).verify(mismatched, now=now)

        with self.assertRaisesRegex(ValueError, "finite and positive"):
            manifest_for(
                self.engine,
                now,
                observed_round_trip_cost_bps_p95={"AAPL": Decimal("Infinity")},
            )

        with self.assertRaisesRegex(ValueError, "pinned SHA-256"):
            StrategyReleaseVerifierV2(
                self.engine,
                trusted_manifest_hash="",
                expected_feature_schema_hash=self.engine.feature_schema_hash,
                expected_calendar_hash=digest("calendar"),
                expected_cost_model_hash=self.engine.cost_model_hash,
            )

    def test_only_canonical_release_parity_can_be_promotion_evidence(self) -> None:
        now = datetime(2026, 8, 12, 12, tzinfo=UTC)
        bars = tuple(
            HistoricalBar(
                now + timedelta(minutes=15 * index),
                Decimal("100") + Decimal(index),
                Decimal("100.5") + Decimal(index),
                Decimal("99.5") + Decimal(index),
                Decimal("100") + Decimal(index),
            )
            for index in range(50)
        )
        mandate = CapitalMandate(
            frozenset({"AAPL"}),
            Decimal("500"),
            Decimal("50"),
            Decimal("1000"),
            Decimal("1000"),
            1,
            Decimal("100"),
            Decimal("100"),
            Decimal("100"),
            Decimal("0.20"),
            Decimal("0.30"),
            60,
            Decimal("100"),
            Decimal("500"),
        )
        manifest = manifest_for(self.engine, now)
        release = verifier_for(self.engine, manifest).verify(manifest, now=now)
        harness = ParityHarnessV2(
            mandate,
            starting_equity=Decimal("1000"),
            spread_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
        )
        result = harness.compare_canonical("AAPL", bars, self.engine, release)
        self.assertTrue(result.passed, result)
        self.assertTrue(result.candidate_hashes_match)
        self.assertTrue(result.promotion_eligible)
        self.assertEqual(result.strategy_release_id, manifest.strategy_release_id)

        benchmark = harness.compare("AAPL", bars[:3], lambda *_: None)
        self.assertTrue(benchmark.passed)
        self.assertFalse(benchmark.promotion_eligible)
        self.assertIn("benchmark-only", benchmark.detail)


if __name__ == "__main__":
    unittest.main()
