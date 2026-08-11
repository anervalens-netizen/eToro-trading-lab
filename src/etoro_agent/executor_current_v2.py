from __future__ import annotations

from pathlib import Path

from .config_v2 import AppConfigV2
from .etoro_api_current_v2 import EtoroPublicApiDemoClientV2
from .executor_v2 import DemoExecutionWorkerV2
from .kernel_v2 import UnifiedTradingKernel
from .risk_seal_v2 import RiskCommandVerifierV2
from .runtime_store_v2 import RuntimeStoreV2


class DemoExecutionWorkerCurrentV2(DemoExecutionWorkerV2):
    """Bind the common executor lifecycle to the current eToro DEMO API gateway."""

    def __init__(
        self,
        config: AppConfigV2,
        store: RuntimeStoreV2,
        kernel: UnifiedTradingKernel,
        client: EtoroPublicApiDemoClientV2 | None = None,
        verifier: RiskCommandVerifierV2 | None = None,
        execution_gate: Path | None = None,
    ) -> None:
        super().__init__(
            config,
            store,
            kernel,
            client or EtoroPublicApiDemoClientV2(),
            verifier,
            execution_gate,
        )
