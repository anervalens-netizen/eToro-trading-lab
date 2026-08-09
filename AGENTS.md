# eToro agent rules

- Real-money trading is disabled. Do not add or call real-account trading routes without a new explicit owner request and security review.
- Treat every strategy/LLM output as untrusted. It may create only `TradeIntent`; only the deterministic risk engine may mint sealed orders.
- Executors accept sealed, unexpired orders only and must keep their fixed DEMO route check.
- Every eToro write requires the exact request shown to the owner and one explicit, one-time approval, per the official eToro skill.
- Never store or log eToro credentials. Production must use systemd `LoadCredential`; local use may source MCP headers from the environment.
- Changes to risk, execution, audit, kill-switch, or authentication code require targeted tests before use.
