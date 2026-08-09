# eToro agent rules

- Real-money trading is disabled. Do not add or call real-account trading routes without a new explicit owner request and security review.
- Treat every strategy/LLM output as untrusted. It may create only `TradeIntent`; only the deterministic risk engine may mint sealed orders.
- Executors accept sealed, unexpired orders only and must keep their fixed DEMO route check.
- Interactive Codex/MCP writes still require the exact request shown to the owner and one explicit, one-time approval.
- The owner explicitly granted an unattended standing mandate on 2026-08-09 for the deployed service, limited to sealed, unexpired `sol_master_open`/`sol_master_close` proposals, fixed DEMO routes, deterministic risk, audit, reconciliation and kill checks. This mandate never applies to REAL.
- REAL activation is a separate future release: explicit owner request, separate REAL credential and service/config, fresh security review and fault drill. Never inherit or copy the DEMO standing mandate and never auto-promote DEMO to REAL.
- Never store or log eToro credentials. Production must use systemd `LoadCredential`; local use may source MCP headers from the environment.
- Changes to risk, execution, audit, kill-switch, or authentication code require targeted tests before use.
