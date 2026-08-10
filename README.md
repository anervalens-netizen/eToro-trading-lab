# eToro DEMO Trading Lab v0.3.0

Platformă de cercetare și tranzacționare virtuală, fără suport pentru bani reali.

Include:

- date eToro pentru 9 instrumente, inclusiv `OIL` (17) și `NATGAS` (22), validate și auditate;
- 12 ipoteze core plus 30 experimente commodity (10 ipoteze × prudent/balanced/aggressive), fiecare cu ledger separat;
- un portofoliu master virtual de 1.000 USD, administrat autonom de Sol prin `OPEN/CLOSE/HOLD`; Sol poate selecta un candidat determinist sau genera direct un `TradeIntent` strict;
- backtest next-quote, costuri per instrument și walk-forward out-of-sample;
- risc determinist, limite fixe, kill switch persistent și ordine sigilate Ed25519;
- executor cu rutele oficiale eToro DEMO open/full-close și mandat permanent strict pentru propunerile Sol sigilate;
- adaptor read-only Agent Portfolio v2 și executor blocat fără un User Key eToro separat pentru Environment=Demo/Permission=Write, fără niciun scope REAL;
- P&L zilnic, audit hash-chain, stări durabile și dashboard Authentik owner-only;
- task Codex recurent cu `gpt-5.6-sol`, fără OpenAI Platform API/key;
- registru round-trip, interfață light responsive cu detalii per strategie/trade și telemetrie AI;
- review post-trade MiniMax-M3; agregatele pot produce propuneri Sol exclusiv `RESEARCH_ONLY`, fără modificare automată de cod/config;
- scanner de catalizatori EIA/OPEC/White House/Treasury/NOAA; numai headline-urile oficiale noi și relevante intră automat în packet-ul Sol, cu TTL 6 h;
- replay clock stdlib determinist, fără dependența Nautilus folosită anterior doar pentru ceas.

Shadow trading și configurația separată DEMO execution funcționează autonom: strategiile oferă semnale, Sol poate selecta ori crea intenția și deschide/închide, iar risk engine-ul verifică numai limite deterministe. Lipsa unei decizii Sol produce `HOLD`. Mandatul permanent DEMO acceptă exclusiv sursele imuabile `sol_master_open`/`sol_master_close`, după verificarea seal-ului, broker truth, costurilor și kill switch-ului. Orice alt write rămâne manual. Nu există rută REAL în executor și activarea viitoare REAL nu poate fi automată.

Runtime-ul folosește numai bare închise cu 60 s grace, deduplicare per strategie și primul quote broker ulterior pentru fill. P&L-ul short este marcat la ask. Ledgerul master se schimbă numai după ACK și reconciliere eToro; un ACK fără broker truth în 120 s activează kill. Statisticile v0.3 aparțin epoch-ului `commodity-risk-grid-v5-20260810`; packet-urile Sol și fingerprint-urile vechi sunt invalidate, iar pozițiile shadow anterioare sunt păstrate, marcate și excluse din promovare.

Vezi [ARCHITECTURE.md](ARCHITECTURE.md), [COMMODITY_RESEARCH.md](COMMODITY_RESEARCH.md) și [RUNBOOK.md](RUNBOOK.md).
