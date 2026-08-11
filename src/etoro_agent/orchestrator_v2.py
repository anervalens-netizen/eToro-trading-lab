from __future__ import annotations

from dataclasses import dataclass

from .ai_v2 import AIIntentOutputV2, Lane


@dataclass(frozen=True)
class OrchestrationInputV2:
    lane: Lane
    deterministic_candidate_ids: tuple[str, ...]
    model_available: bool
    packet_valid: bool
    data_quality_ok: bool
    critic_verdict: str | None = None


@dataclass(frozen=True)
class OrchestrationResultV2:
    action: str
    source: str
    reason: str
    selected_candidate_id: str | None = None


class AutonomousOrchestratorV2:
    """Fail-closed lane orchestration: missing AI dependencies default to HOLD for new risk."""

    def decide(
        self,
        state: OrchestrationInputV2,
        ai_output: AIIntentOutputV2 | None,
    ) -> OrchestrationResultV2:
        if not state.data_quality_ok:
            return OrchestrationResultV2("HOLD", "deterministic", "data_quality_failure")
        if state.lane is Lane.DETERMINISTIC:
            selected = (
                state.deterministic_candidate_ids[0] if state.deterministic_candidate_ids else None
            )
            return OrchestrationResultV2(
                "OPEN" if selected else "HOLD",
                "deterministic",
                "baseline_lane",
                selected,
            )
        if not state.model_available or not state.packet_valid or ai_output is None:
            return OrchestrationResultV2("HOLD", "fallback", "ai_unavailable_or_invalid")
        if state.critic_verdict == "VETO":
            return OrchestrationResultV2("HOLD", "critic", "adversarial_veto")
        if state.critic_verdict == "DE_RISK" and ai_output.action.value == "OPEN":
            return OrchestrationResultV2("HOLD", "critic", "critic_derisk_blocks_new_risk")
        return OrchestrationResultV2(ai_output.action.value, "ai", "validated_ai_decision")
