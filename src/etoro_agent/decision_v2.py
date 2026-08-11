from __future__ import annotations

from .decision_impl_v2 import DecisionApplierV2 as _DecisionApplierV2
from .decision_impl_v2 import (
    DecisionApplyResultV2,
    DecisionPacketBuilderV2,
    DecisionPacketContextV2,
)


class DecisionApplierV2(_DecisionApplierV2):
    """Apply validated AI output to the canonical v2 intent contract."""


__all__ = [
    "DecisionApplyResultV2",
    "DecisionApplierV2",
    "DecisionPacketBuilderV2",
    "DecisionPacketContextV2",
]
