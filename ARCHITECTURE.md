# Architecture

## Scope

Sistemul acceptă numai `paper` și eToro `demo`. Configurația livrată rulează `paper`, cu date din contul DEMO și `etoro_demo_execution_enabled=false`. Executorul nu conține și nu acceptă rute REAL.

## Runtime flow

1. `MarketDataCollector` citește rate/candles prin MCP-ul oficial eToro, numai prin allowlist read-only.
2. Validatorul respinge simboluri necunoscute, mapping greșit, timestamp-uri ne-UTC, duplicate, gap-uri, OHLC invalid sau date stale.
3. Cele 12 strategii emit exclusiv `TradeIntent`; fiecare folosește `strategy_01..12`, NAV inițial 1.000 USD și stare izolată.
4. `DeterministicRiskEngine` aplică limitele fixe din `config/demo.json` și poate semna doar ruta DEMO cu Ed25519.
5. Shadow fills sunt simulate local. Pentru un write eToro DEMO, executorul verifică public seal-ul, kill state/file, aprobarea exactă one-time, scope-ul DEMO, eligibility și costs chiar înainte de rețea.
6. Evenimentele, P&L-ul, propunerile și stările sunt persistate în SQLite WAL/FULL, cu hash-chain. O schemă append-only PostgreSQL și store-ul aferent sunt incluse pentru etapa de migrare operațională.
7. Dashboard-ul citește baza read-only; doar endpointurile locale kill/resume/approve pot scrie în control audit. Authentik și verificarea exactă a username-ului owner sunt ambele obligatorii.

## Security boundary

- LLM-ul și strategiile nu dețin credentiale și nu pot invoca MCP write.
- MCP gateway-ul nu expune un `call_tool` generic și acceptă o singură rută write: `POST /api/v2/trading/execution/demo/orders`.
- Risk signer-ul deține cheia privată; executorul primește doar cheia publică.
- Seal-ul leagă account, route, method, body, request ID, intent hash, risk snapshot/config, quote time și TTL.
- Proposal ID și `xRequestId` sunt unice. O propunere este imuabilă; nu poate fi re-legată înainte sau după aprobare.
- Aprobarea compară hash-ul complet, este atomic consumată înainte de write și poate produce cel mult un singur apel. Timeout/5xx devine `UNKNOWN`; nu există retry automat.
- Kill switch-ul pornește fail-closed și este verificat în risk, shadow și executor, inclusiv imediat înainte de write.
- Credentialele eToro sunt furnizate exclusiv prin `systemd LoadCredential`; nu intră în repo, DB, dashboard sau loguri.
- `ProtectSystem=strict`, user fără shell, bind numai pe Docker bridge-ul Caddy și Authentik forward-auth reduc suprafața runtime.

## Strategies

1. ORB 15m immediate
2. ORB 15m retest
3. first/last 30m momentum
4. Donchian ATR breakout
5. EMA 9/21 + ADX
6. Bollinger squeeze breakout
7. Bollinger/RSI mean reversion
8. ATR shock fade
9. London breakout EURUSD
10. NY/London overlap momentum EURUSD
11. SPX/Nasdaq pairs mean reversion
12. EURUSD 4h time-series momentum

Toate sunt ipoteze de cercetare, nu strategii validate sau promisiuni de profit. Promovarea nu este automată.
