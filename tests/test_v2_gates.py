from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from etoro_agent.ai_v2 import AIAction, AIIntentOutputV2, Lane
from etoro_agent.cost_model_v2 import CalibratedCostProfile
from etoro_agent.epoch_v2 import ResearchEpochManagerV2, ResearchEpochV2
from etoro_agent.features_v2 import TradabilityGateV2, build_feature_snapshot, order_flow_imbalance
from etoro_agent.orchestrator_v2 import AutonomousOrchestratorV2, OrchestrationInputV2
from etoro_agent.promotion_v2 import PromotionEvidenceV2, PromotionGateV2
from etoro_agent.prompt_eval_v2 import evaluate_prompt_boundary
from etoro_agent.quant_v2 import RidgeLogisticBaselineV2
from etoro_agent.runtime_store_v2 import RuntimeStoreV2


class V2GatesTests(unittest.TestCase):
    def test_epoch_activation_is_idempotent_and_segregates_stats(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RuntimeStoreV2(Path(folder) / "runtime.sqlite3")
            now = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            epoch = ResearchEpochV2("e1","d1","f1","s1","c1","r1","p1","a"*40,"b"*64,now)
            manager = ResearchEpochManagerV2(store)
            self.assertTrue(manager.activate(epoch, reason="semantic change"))
            self.assertFalse(manager.activate(epoch, reason="same"))
            self.assertTrue(manager.comparable({"research_epoch_v2":"e1","research_epoch_fingerprint":epoch.fingerprint}))
            store.close()

    def test_cost_gate_rejects_edge_that_does_not_clear_stressed_costs(self):
        now = datetime.now(timezone.utc)
        feature = build_feature_snapshot("AAPL", now, {"momentum":Decimal("1")}, ("m1",), feature_version="1", data_quality_ok=True)
        costs = CalibratedCostProfile("AAPL",Decimal("5"),Decimal("10"),Decimal("2"),Decimal("5"),10,20,Decimal("0"),Decimal("0"),100)
        result = TradabilityGateV2().evaluate(feature, expected_edge_bps=Decimal("10"), costs=costs)
        self.assertFalse(result.tradable)
        self.assertEqual(order_flow_imbalance([Decimal("2")],[Decimal("1")]), Decimal("1")/Decimal("3"))

    def test_simple_ml_baseline_learns_directional_signal(self):
        model = RidgeLogisticBaselineV2().fit([[-2.0],[-1.0],[1.0],[2.0]],[0,0,1,1],iterations=1000)
        self.assertLess(model.probability([-2]), 0.5)
        self.assertGreater(model.probability([2]), 0.5)

    def test_promotion_requires_empirical_and_operational_gates(self):
        evidence = PromotionEvidenceV2(10,5,Decimal("0.5"),Decimal("0.5"),Decimal("0.2"),Decimal("0.05"),Decimal("1.2"),Decimal("2"),Decimal("0"),0,0,False,Decimal("0"))
        decision = PromotionGateV2().evaluate(evidence)
        self.assertEqual(decision.decision, "CONTINUE_SHADOW")
        self.assertIn("untouched_test_not_run", decision.reasons)

    def test_ai_failure_defaults_to_hold_and_critic_can_veto(self):
        orch = AutonomousOrchestratorV2()
        state = OrchestrationInputV2(Lane.SOL_DIRECT,(),False,True,True)
        self.assertEqual(orch.decide(state,None).action,"HOLD")
        output = AIIntentOutputV2(AIAction.HOLD,Decimal("0.5"),Decimal("0.5"),("wait",),"wait",(),"h","C")
        veto = OrchestrationInputV2(Lane.SOL_CRITIC,(),True,True,True,"VETO")
        self.assertEqual(orch.decide(veto,output).source,"critic")

    def test_prompt_eval_detects_unsafe_runner(self):
        safe = evaluate_prompt_boundary(lambda case: {"action":"HOLD","reason":"untrusted external text"})
        self.assertEqual(safe.passed, safe.cases)
        unsafe = evaluate_prompt_boundary(lambda case: {"action":"OPEN","reason":case.external_text})
        self.assertGreater(len(unsafe.failed_case_ids),0)


if __name__ == "__main__":
    unittest.main()
