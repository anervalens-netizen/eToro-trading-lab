# Architecture

## Scope

Sistemul acceptă numai `paper` și eToro `demo`. Configurația livrată rulează `paper`, cu date din contul DEMO și `etoro_demo_execution_enabled=false`. Executorul nu conține și nu acceptă rute REAL.

## Runtime flow

1. `MarketDataCollector` citește rate/candles prin MCP-ul oficial eToro, numai prin allowlist read-only.
2. Collectorul cere o bară suplimentară, elimină bara încă în formare și aplică 60 s grace după close. Validatorul respinge simboluri necunoscute, mapping greșit, timestamp-uri ne-UTC, duplicate, gap-uri, OHLC invalid sau date stale. Fiecare strategie e evaluată o singură dată pentru fingerprint-ul propriei bare închise; actualizarea asincronă a altui simbol nu o retrigger-uiește.
3. Cele 42 de strategii emit exclusiv `TradeIntent`; `strategy_01..42` sunt ledgere de ipoteză, nu capital cumulabil. Primele 12 păstrează core-ul v0.2. Următoarele 30 sunt 10 ipoteze OIL/NATGAS, fiecare rulată independent în profilurile prudent/balanced/aggressive. Semnalele observate când sesiunea e închisă rămân în audit, dar nu devin pending, candidat Sol sau ordin; un pending care ajunge într-o sesiune închisă expiră.
4. La fiecare bară nouă eligibilă sau catalizator oficial nou se construiește un packet sanitizat. Scannerul permite numai șase surse HTTPS fixe, face bootstrap fără alerte istorice, deduplică headline-urile și păstrează evenimentele relevante șase ore. Runnerul Dell folosește loginul ChatGPT existent și pornește `gpt-5.6-sol` stateless într-o unitate systemd: numai auth-ul ChatGPT este montat read-only, home/SSH sunt mascate și execuția altor binare este interzisă. Sol decide `OPEN`, `CLOSE` sau `HOLD` pentru masterul de 1.000 USD. La `OPEN` poate selecta un candidat sau genera direct un intent în catalog și limitele transmise. Decizia este hash-bound, expiră și se consumă o singură dată.
5. `DeterministicRiskEngine` aplică limitele fixe și poate semna numai rutele DEMO open/full-close cu Ed25519. Procesul shadow/risk deține cheia privată; executorul primește exclusiv cheia publică. Sol nu poate mări suma, schimba requestul sau semna.
6. Semnalele shadow se execută în simulator numai la primul quote cu timestamp broker strict ulterior quote-ului care a produs semnalul. Quote age folosește timestamp-ul eToro, nu timpul local de colectare; short-urile sunt marcate la ask, long-urile la bid. Costurile sunt versionate per instrument; OHLC permite stop/target conservator, cu stop prioritar când ordinea intrabar este necunoscută.
7. Pentru un write eToro DEMO, executorul verifică seal-ul public, sursa imuabilă și autorizarea exactă, cheia separată creată în eToro pentru mediul DEMO fără scope REAL, broker truth și eligibility/cost/quote live. În modul unattended, mandatul permanent poate autoriza exclusiv `sol_master_open`/`sol_master_close`; propunerile manuale nu îl pot folosi. Scope-urile auxiliare adăugate automat de platformă nu lărgesc allowlist-ul fix al procesului. Kill blochează opens, dar permite numai close reduce-only sigilat. Propunerile expirate sunt respinse terminal înainte de rețea. Ledgerul master nu se modifică la decizia Sol și nici la trimitere: numai după ACK plus broker truth reconciliat. Lipsa reconcilierii în 120 s sau orice diferență între poziția master locală și broker truth blochează sistemul; `UNKNOWN` nu se reîncearcă.
8. SQLite WAL/FULL folosește lock cross-process pentru writer-ul hash-chain. Dashboard-ul verifică lanțul complet read-only și tratează kill file + state prin OR fail-closed.
9. Fiecare round-trip închis intră în registrul determinist. MiniMax-M3 îl analizează asincron, cu job durabil, lease și claim token. Agregatul zilnic poate fi analizat de Sol, dar rezultatul este numai `RESEARCH_ONLY`; nu modifică strategii sau risc.
10. Dashboard-ul light arată master NAV/P&L, cele 42 de ledgere, profil/simbol, catalizatori activi, istoric paginat, lifecycle, review-uri, propuneri și consum AI. Authentik și verificarea exactă a ownerului rămân obligatorii.

Epoch-ul `commodity-risk-grid-v5-20260810` invalidează atomic packet-urile Sol pending/decided și fingerprint-urile de evaluare din politici anterioare, astfel încât toate cele 42 de ledgere primesc exact un snapshot inițial. Pozițiile shadow vechi rămân gestionate până la închidere, dar sunt marcate `carried_position` și excluse de la promovare; auditul istoric nu este șters.

Masterul de 1.000 USD primește candidații strategiilor și intenții directe Sol pentru instrumentele deschise/valide din catalog. Eligibility și cost preview eToro decid compatibilitatea exactă înainte de propunere. Configurația DEMO permite maximum 1.000 USD per ordin/gross/simbol, 20 USD risc proiectat/trade, 25 USD pierdere zilnică și 50 USD lunară, fără leverage. OIL/NATGAS au minimum broker live de 1.000 USD, deci un intent commodity poate trece numai cu suma exactă disponibilă și stop de maximum 2%; executorul reverifică eligibility live. Pentru buy nelevierat pe acțiuni, `settlementType=real` înseamnă activ suport, nu cont REAL; ruta și credentialul rămân strict DEMO. Aceste limite nu se transferă automat la REAL.

## Security boundary

- Sol și strategiile nu dețin credentiale și nu pot invoca MCP write.
- MCP gateway-ul nu expune un `call_tool` generic și acceptă doar `POST /api/v2/trading/execution/demo/orders` și full-close `POST /api/v1/trading/execution/demo/market-close-orders/positions/{positionId}`.
- Risk signer-ul deține cheia privată; executorul primește doar cheia publică.
- Seal-ul leagă account, route, method, body, request ID, intent hash, risk snapshot/config, quote time și TTL.
- Proposal ID și `xRequestId` sunt unice. O propunere este imuabilă; nu poate fi re-legată înainte sau după aprobare.
- Autorizarea, manuală sau prin mandatul permanent DEMO, compară hash-ul complet, este atomic consumată înainte de write și poate produce cel mult un singur apel. Timeout/5xx devine `UNKNOWN`; nu există retry automat. Un eșec de preflight după autorizare devine terminal `REJECTED` fără write și activează kill în modul unattended.
- Kill switch-ul pornește fail-closed și este verificat în risk, shadow și executor; numai un close reduce-only sigilat poate trece când kill este activ.
- Credentialele eToro sunt furnizate exclusiv prin `systemd LoadCredential`; nu intră în repo, DB, dashboard sau loguri.
- Cheia largă a ownerului nu este acceptată de executor. Acesta cere un User Key separat, generat în eToro cu Environment=Demo și Permission=Write, și refuză orice scope REAL.
- Mandatul DEMO nu este o permisiune REAL. Ruta, credentialul, unitatea și configurația REAL sunt intenționat absente și necesită un release separat.
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

Commodity grid: adaptive range, Donchian breakout, EMA trend, shock/spike fade și volatility squeeze, separat pentru OIL și NATGAS. Fiecare are trei profiluri: prudent (50 USD, selectivitate ridicată), balanced (100 USD) și aggressive (150 USD, prag mai rapid). Stop/target/holding sunt proprii profilului; toate rămân sub risk engine-ul global, fără leverage și fără excepții.

Toate sunt ipoteze de cercetare, nu strategii validate sau promisiuni de profit. Promovarea nu este automată.
