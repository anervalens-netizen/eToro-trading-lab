from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from .agent import TradingAgent
from .audit import AuditLog
from .backtest import load_closes, run_backtest
from .config import load_config
from .market import MarketDataCollector
from .mcp import EtoroMCPClient
from .models import KillState
from .risk import generate_private_signing_key
from .strategy import MovingAverageStrategy


def _paths(args: argparse.Namespace) -> tuple[Path, AuditLog]:
    runtime = Path(args.runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime, AuditLog(runtime / "audit.sqlite3")


def main() -> None:
    parser = argparse.ArgumentParser(description="eToro paper/DEMO trading agent; REAL trading is unsupported")
    parser.add_argument("--config", default="config/demo.json")
    parser.add_argument("--runtime", default="runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once")
    run.add_argument("symbol")
    run.add_argument("--execute-demo", action="store_true", help="after printing the exact DEMO request, prompt for one-time operator approval and execute")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("symbol")
    backtest.add_argument("csv")
    sub.add_parser("status")
    sub.add_parser("kill")
    resume = sub.add_parser("resume")
    resume.add_argument("--confirm", default="")
    sub.add_parser("init-security")
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", default=8765, type=int)
    dashboard.add_argument("--uds", default="")
    sub.add_parser("shadow-once")
    shadow_worker = sub.add_parser("shadow-worker")
    shadow_worker.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    config = load_config(args.config)
    runtime, audit = _paths(args)

    if args.command == "kill":
        (runtime / "KILL_SWITCH").touch(exist_ok=True)
        audit.set_kill_state(KillState.LOCKED, "cli-owner", "manual kill")
        print("KILL_STATE=LOCKED")
    elif args.command == "resume":
        if args.confirm != "RESUME_DEMO":
            raise SystemExit("resume requires --confirm RESUME_DEMO")
        (runtime / "KILL_SWITCH").unlink(missing_ok=True)
        audit.set_kill_state(KillState.ACTIVE, "cli-owner", "explicit resume")
        print("KILL_STATE=ACTIVE")
    elif args.command == "init-security":
        key_path = runtime / "risk-signing.key"
        generate_private_signing_key(key_path)
        audit.set_kill_state(KillState.LOCKED, "cli-owner", "security initialized")
        print(f"SIGNER=PROVISIONED path={key_path} mode=0600 kill=LOCKED")
    elif args.command == "status":
        print(
            json.dumps(
                {
                    "mode": config.account_mode,
                    "real_money": False,
                    "demo_execution_enabled": config.etoro_demo_execution_enabled,
                    "kill_state": audit.kill_state().value,
                    "kill_file": (runtime / "KILL_SWITCH").exists(),
                    "pending_approvals": len(audit.list_pending()),
                    "audit_events": audit.event_count(),
                    "audit_chain_valid": audit.verify_chain(),
                },
                indent=2,
            )
        )
    elif args.command == "dashboard":
        import uvicorn

        from .dashboard import DashboardService, create_app

        service = DashboardService(audit.path, runtime)
        application = create_app(service, control_audit=audit)
        if args.uds:
            uvicorn.run(application, uds=args.uds)
        else:
            uvicorn.run(application, host=args.host, port=args.port)
    elif args.command in {"shadow-once", "shadow-worker"}:
        from .engine import AutonomousShadowEngine

        engine = AutonomousShadowEngine(config, audit)
        collector = MarketDataCollector(EtoroMCPClient())
        if args.command == "shadow-once":
            print(
                json.dumps(
                    engine.collect_and_tick(collector).__dict__, default=str, indent=2
                )
            )
        else:
            engine.run_forever(collector, args.interval)
    elif args.command == "backtest":
        result = run_backtest(MovingAverageStrategy(config.strategy), args.symbol.upper(), load_closes(args.csv), config.initial_cash_usd)
        audit.append("backtest", result.__dict__)
        print(json.dumps(result.__dict__, default=str, indent=2))
    elif args.command == "run-once":
        if (
            config.account_mode == "demo"
            and args.execute_demo
            and not config.etoro_demo_execution_enabled
        ):
            raise SystemExit("DEMO execution is disabled; proposal generation remains available")
        agent = TradingAgent(config, audit, MarketDataCollector(EtoroMCPClient()), runtime)
        result = agent.run_once(args.symbol)
        print(json.dumps(result, default=str, indent=2))
        if result.get("status") == "awaiting_operator_approval" and args.execute_demo:
            proposal_id = str(result["proposal_id"])
            envelope_hash = str(result["envelope_hash"])
            confirmation = input(
                f"Type APPROVE {proposal_id} {envelope_hash[-8:]} to execute this one DEMO order: "
            )
            if confirmation != f"APPROVE {proposal_id} {envelope_hash[-8:]}":
                raise SystemExit("not approved; nothing executed")
            audit.approve_once(proposal_id, envelope_hash, "cli-owner")
            print(json.dumps(agent.execute_pending_demo(proposal_id), default=str, indent=2))


if __name__ == "__main__":
    main()
