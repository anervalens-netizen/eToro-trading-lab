# Architecture

## Scope

Sistemul acceptă numai `paper` și eToro `demo`. Configurația livrată rulează `paper`, cu date din contul DEMO și `etoro_demo_execution_enabled=false`. Executorul nu conține și nu acceptă rute REAL.

## Runtime flow

1. `MarketDataCollector` citește rate/candles prin MCP-ul oficial eToro, numai prin allowlist read-only.
2. Validatorul respinge simboluri necunoscute, mapping greșit, timestamp-uri ne-UTC, duplicate, gap-uri, OHLC invalid sau date stale.
3. Cele 12 strategii emit exclusiv `TradeIntent`; `strategy_01..12` sunt ledgere de ipoteză, nu capital cumulabil.
4. La fiecare bară închisă se construiește un packet sanitizat. Runnerul Dell folosește loginul ChatGPT existent și pornește `gpt-5.6-sol` stateless într-o unitate systemd read-only unde cheile SSH sunt `InaccessiblePaths`. Sol decide `OPEN`, `CLOSE` sau `HOLD` pentru un singur portofoliu master de 1.000 USD. Decizia este hash-bound, expiră și se consumă o singură dată.
5. `DeterministicRiskEngine` aplică limitele fixe și poate semna numai rutele DEMO open/full-close cu Ed25519. Sol nu poate mări suma, schimba requestul sau semna.
6. Semnalele și deciziile se execută în simulator numai la primul quote proaspăt ulterior. Costurile sunt versionate per instrument; OHLC permite stop/target conservator, cu stop prioritar când ordinea intrabar este necunoscută.
7. Pentru un write eToro DEMO, executorul verifică seal-ul public, aprobarea exactă one-time, tokenul delegat Agent Portfolio fără scope REAL, broker truth și eligibility/cost/quote live. Kill blochează opens, dar permite numai close reduce-only sigilat. După ACK, reconcilierea eșuată activează kill.
8. SQLite WAL/FULL folosește lock cross-process pentru writer-ul hash-chain. Dashboard-ul verifică lanțul complet read-only și tratează kill file + state prin OR fail-closed.
9. Dashboard-ul arată separat master NAV/P&L, coada Sol și cele 12 ledgere de cercetare. Authentik și verificarea exactă a ownerului rămân obligatorii.

## Security boundary

- Sol și strategiile nu dețin credentiale și nu pot invoca MCP write.
- MCP gateway-ul nu expune un `call_tool` generic și acceptă doar `POST /api/v2/trading/execution/demo/orders` și full-close `POST /api/v1/trading/execution/demo/market-close-orders/positions/{positionId}`.
- Risk signer-ul deține cheia privată; executorul primește doar cheia publică.
- Seal-ul leagă account, route, method, body, request ID, intent hash, risk snapshot/config, quote time și TTL.
- Proposal ID și `xRequestId` sunt unice. O propunere este imuabilă; nu poate fi re-legată înainte sau după aprobare.
- Aprobarea compară hash-ul complet, este atomic consumată înainte de write și poate produce cel mult un singur apel. Timeout/5xx devine `UNKNOWN`; nu există retry automat.
- Kill switch-ul pornește fail-closed și este verificat în risk, shadow și executor; numai un close reduce-only sigilat poate trece când kill este activ.
- Credentialele eToro sunt furnizate exclusiv prin `systemd LoadCredential`; nu intră în repo, DB, dashboard sau loguri.
- Cheia largă a ownerului nu este acceptată de executor. Acesta cere un token Agent Portfolio separat cu exact DEMO read+write și refuză orice token care expune scope REAL.
- `ProtectSystem=strict`, user fără shell, Unix socket montat read-only în Caddy, secret de boundary Caddy→aplicație și Authentik forward-auth reduc suprafața runtime.

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
