# eToro DEMO Trading Lab

Platformă de cercetare și tranzacționare virtuală, fără suport pentru bani reali.

Include:

- date eToro pentru 7 instrumente, validate și auditate;
- 12 strategii deterministe, fiecare cu ledger shadow separat de 1.000 USD;
- backtest cu costuri și walk-forward out-of-sample;
- risc determinist, limite fixe, kill switch persistent și ordine sigilate Ed25519;
- executor cu o singură rută eToro DEMO și aprobare owner exactă, one-time;
- P&L zilnic, audit hash-chain, stări durabile și dashboard Authentik owner-only;
- NautilusTrader 1.231.0 folosit numai ca runtime offline/replay, nu ca adaptor eToro.

Shadow trading este complet autonom. Scrierile eToro DEMO rămân dezactivate implicit și, conform skill-ului oficial eToro, necesită aprobarea individuală a requestului exact. Nu există rută REAL în executor.

Vezi [ARCHITECTURE.md](ARCHITECTURE.md) și [RUNBOOK.md](RUNBOOK.md).
