# Legacy v1 runbook (read-only reference)

Do not use these commands to activate a broker writer. Operational procedures are in `V2_DEPLOYMENT.md`; v1 is retained only for replay and forensic interpretation.

## Local setup and verification

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
node --check src/etoro_agent/dashboard_static/dashboard.js
```

MCP oficial: `https://mcp.public-api.etoro.com`. Autentificarea locală acceptă `ETORO_USER_KEY` + `ETORO_API_KEY` sau fișierele `ETORO_USER_KEY_FILE` + `ETORO_API_KEY_FILE`. OAuth folosește exclusiv varianta bearer. Nu combina metodele și nu pune valori în repo/chat/log.

## Safe commands

```bash
etoro-agent --config config/demo.json --runtime runtime status
etoro-agent --config config/demo.json --runtime runtime shadow-once
etoro-agent --config config/demo.json --runtime runtime shadow-worker --interval 60
etoro-agent --config config/demo.json --runtime runtime news-once
etoro-agent --config config/demo.json --runtime runtime news-worker --interval 120
etoro-agent --config config/demo.json --runtime runtime ai-pending --limit 5
etoro-agent --config config/demo.json --runtime runtime kill
etoro-agent --config config/demo.json --runtime runtime resume --confirm RESUME_DEMO
etoro-agent --config config/demo.json --runtime runtime dashboard --host 127.0.0.1 --port 8765
etoro-agent --config config/demo.json --runtime runtime agent-portfolio-status
```

`kill` este intenționat imediat. `resume` cere confirmare exactă. La boot nou, starea este fail-closed până la resume explicit.

Pentru o poziție DEMO închisă server-side prin SL/TP, reconcilierea manuală de
recovery se rulează numai în `LOCKED`, după backup verificat și după confirmarea
absenței poziției curente la broker:

```bash
etoro-agent --config config/demo-execution.json --runtime runtime \
  reconcile-demo-close --symbol SYMBOL --position-id 123456 \
  --confirm RECONCILE_DEMO_CLOSE_123456
```

Comanda face exclusiv read din istoricul DEMO, validează identitatea, direcția,
unitățile, prețul de open și baza locală, apoi proiectează atomic `netProfit` și
fill-ul raportate de broker. Proiecția locală anterioară și hash-ul dovezii rămân
în `shadow_broker_close_reconciliations`; nu există write broker sau rută REAL.
Diferența de afișare dintre suma rotunjită la cenți și produsul unități×preț este
acceptată numai în limita fixă de 0,02 USD; profitul rămâne `netProfit` broker.
`resume` refuză audit invalid, drift, execuție master pending sau stare `UNKNOWN`.

Dacă un ACK de open a fost proiectat cu quote-ul următor în locul poziției
curente de la broker, recovery-ul este tot read-only față de broker:

```bash
etoro-agent --config config/demo-execution.json --runtime runtime \
  reconcile-demo-open --symbol SYMBOL --position-id 123456 \
  --replace-local-projection --confirm RECONCILE_DEMO_OPEN_123456
```

Fill-ul local greșit nu este șters: intră în `shadow_fill_quarantine`, iar
read-modelurile și contoarele îl exclud. Poziția și fill-ul activ sunt recreate
din `positionID`, `orderID`, direcție, unități, open rate, timestamp, sumă și
costurile DEMO returnate de broker, toate păstrate cu hash de evidență.

## Production service

```bash
sudo systemctl status etoro-shadow etoro-news-scanner etoro-dashboard
sudo journalctl -u etoro-shadow -u etoro-news-scanner -u etoro-dashboard --since today
sudo curl --fail --unix-socket /run/etoro-agent/dashboard.sock http://localhost/healthz
sudo systemctl list-timers etoro-backup.timer
```

Runtime: `/var/lib/etoro-agent`; dashboard Unix socket: `/run/etoro-agent/dashboard.sock`, montat read-only în Caddy și protejat suplimentar cu un secret de boundary root-only. Credentiale: `/etc/etoro-agent/*`, root-only, încărcate cu `LoadCredential`. Dashboard: `https://trading.astancu.eu`, prin Cloudflare Tunnel → Caddy → Authentik → FastAPI. Orice acces fără boundary-ul Caddy și headerul Authentik exact al ownerului este respins.

## Sol decision loop

Task-ul Codex recurent citește numai packet-uri sanitizate și scrie o decizie hash-bound:

```bash
etoro-agent --config config/demo.json --runtime runtime ai-pending --limit 5
etoro-agent --config config/demo.json --runtime runtime ai-decide \
  --packet-id ID --packet-hash HASH --action HOLD --confidence 0.70 \
  --reason-code insufficient_edge --rationale "No robust edge" --model gpt-5.6-sol
```

Runnerul automat acceptă la `OPEN` fie `candidate_id` exact, fie un intent direct strict: simbol catalogat, side, sumă, stop, target și holding. `CLOSE` este valid numai pentru packet de poziție. Packet expirat/hash greșit/decizie repetată este respins. Nu există plafon zilnic arbitrar Sol; deduplicarea event-driven și quota ChatGPT controlează apelurile. Modelul nu are credentiale eToro, dar comenzile sale validate ajung automat la executorul DEMO.

Runner-ele v1 `etoro-sol-runner.service` și `etoro-minimax-runner.service`,
checkout-ul detached `/opt/eToro-runtime` și entrypoint-urile lor instalabile sunt
pensionate. Nu le reactiva; această secțiune descrie numai formatul istoric al
datelor v1. Autoritatea AI live este exclusiv `etoro-v2-sol-runner.service` din
release-ul imuabil `/opt/etoro-v2/current`, împreună cu socketul/modelul v2
izolat.

## v0.3 commodity grid, news hook, AI review și dashboard

```bash
systemctl status etoro-v2-sol-runner etoro-v2-sol-model.socket
journalctl -u etoro-v2-sol-runner -u etoro-v2-sol-model@ --since today
etoro-agent --config config/demo.json --runtime runtime ai-review-pending --limit 5
```

MiniMax-M3 rulează prin OpenCode cu modelul exact `minimax-coding-plan/MiniMax-M3`. Joburile au lease, attempt și claim token; un rezultat întârziat nu poate câștiga peste un retry. Ambele runner-e publică heartbeat cu `last_success` și `consecutive_errors`. Endpointurile read-only sunt `/api/strategies`, `/api/trades`, `/api/reviews`, `/api/ai/usage`; dashboard-ul paginează istoricul și nu inventează costuri indisponibile.

`etoro-news-scanner.service` verifică la 120 s EIA petroleum/gas, OPEC, White House, U.S. Treasury și NOAA/NHC. Primul poll este numai baseline. Un headline ulterior trebuie să conțină un instrument și un catalizator; este deduplicat, auditat, expiră după 6 h și schimbă fingerprint-ul packet-ului Sol. `direction_hint` este numai clasificare lexicală; Sol trebuie să ceară confirmare în preț și poate răspunde `HOLD`. Scannerul nu primește credentiale eToro și nu face writes broker.

Cele 42 de ledgere însumează 42.000 USD capital shadow fictiv exclusiv pentru comparație. Nu reprezintă capital disponibil și nu se agregă în masterul unic de 1.000 USD. Comparațiile valide se fac în interiorul aceleiași familii și aceleiași ferestre de date, net de costuri.

Istoric v0.2: tabelele `AIReviewStore` pot rămâne pentru interpretare forensic;
serviciul MiniMax v1 nu mai este instalabil. Nu restaura DB automat deoarece ai
pierde evenimente.

Backup-ul verificat al auditului rulează la 02:45 în `/storage/backups/db/etoro` și `/opt/Mobiup/ops/backups/etoro`; sincronizarea generală de la 03:00 îl publică ulterior spre NAS.

## DEMO autonomous execution gate

`config/demo.json` rămâne paper/manual. Configurația separată de producție
`config/demo-execution.json` activează executorul unattended numai în DEMO:

1. `account_mode=demo` și `etoro_demo_execution_enabled=true`;
2. `demo_execution_authorization=standing_demo`;
3. sursa propunerii este imuabilă și exact `sol_master_open` sau `sol_master_close`;
4. seal Ed25519 valid/neexpirat, request hash exact și toate controalele deterministe trec;
5. scope DEMO read+write, broker truth, eligibility, cost și quote sunt reverificate imediat înainte de write;
6. autorizarea se consumă atomic o singură dată. `UNKNOWN` nu se reîncearcă; orice eșec unattended activează kill.

Mandatul permanent DEMO a fost acordat explicit de owner la 2026-08-09. Nu
autorizează un apel Codex/MCP interactiv și nu poate fi folosit de propuneri cu
sursa `manual`; acestea păstrează aprobarea exactă, individuală.

Executorul nu acceptă cheia largă a contului. În eToro: Settings → Trading →
API Key Management → Create New Key, alege `Environment=Demo`,
`Permission=Write`, IP-ul serverului și expirare. User Key-ul separat se pune în
`/etc/etoro-agent/etoro-demo-user-key` și se încarcă prin `LoadCredential`.
Executorul cere DEMO read+write și refuză orice scope REAL. eToro poate adăuga
cheii DEMO scope-uri auxiliare ale platformei; acestea
nu lărgesc allowlist-ul runtime, care conține exclusiv read-urile necesare și
rutele DEMO open/close. Managementul Agent Portfolio folosește numai rutele v2;
codul runtime expune doar listarea/scopes, nu provisioning generic.

Executorul v1 descris în această secțiune este arhivat istoric: unitatea și comenzile sale CLI au fost eliminate din repository. Provisioning-ul v2 maschează numele unității pentru a neutraliza eventuale copii instalate anterior.

Shadow worker folosește numai bare finalizate plus `candle_close_grace_seconds` (60 s în configurațiile livrate), deduplică separat fiecare strategie și așteaptă un timestamp de quote broker strict mai nou înainte de fill-ul simulat. După un write DEMO, `master_pending_execution` rămâne durabil până când broker truth confirmă poziția deschisă/închisă; ledgerul local nu anticipează brokerul. O propunere expirată este respinsă fără write. ACK nereconciliat în 120 s sau diferența local–broker trece kill în `LOCKED`, publică health ne-sănătos și cere investigație, nu retry.

Calendarul SPX500/NSDQ100 urmează orele eToro publicate: deschidere duminică 22:00 UTC, închidere vineri 20:30 UTC și pauză zilnică 21:00–22:00 UTC. Sursa canonică: `https://www.etoro.com/trading/market-hours-and-events/`. Calendarul conservator poate bloca opens suplimentar, niciodată să extindă sesiunea.

Un `trade_intent` cu `accepted_for_execution=false` este numai observație de research din sesiune închisă. Nu îl promova și nu îl reintroduce la următoarea deschidere; runtime-ul expiră și pending-urile care traversează închiderea.

Înainte să înregistreze o propunere master, shadow worker cere eligibility și cost preview DEMO. Minimul de expunere, settlement/direcție/leverage și suma minimă sunt verificate aici și din nou în executor. O incompatibilitate oprește candidatul fără write și fără kill. `settlementType=real` este permis numai determinist pentru buy nelevierat AAPL/TSLA/BTC/ETH pe ruta DEMO; nu schimbă account mode și nu permite vreo rută REAL.

Epoch-ul curent este `commodity-risk-grid-v5-20260810`. La schimbarea epoch-ului, packet-urile Sol pending/decided și fingerprint-urile de evaluare vechi sunt invalidate atomic; următorul poll produce exact un snapshot inițial pentru fiecare din cele 42 de strategii. Dashboard-ul exclude de la promovare orice poziție `carried pre-epoch`; aceasta rămâne vizibilă și este gestionată normal până la închidere. Nu șterge auditul sau fill-urile vechi pentru a cosmetiza statisticile.

`init-security` generează `risk-signing.key` (privată, numai shadow/risk) și
`risk-verifying.pub` (publică, singura cheie încărcată în executor). Executorul
nu trebuie să primească niciodată `ETORO_RISK_SIGNING_KEY_FILE`.

## v2 shadow și execution gate — PostgreSQL

Runtime-ul canonic v2 folosește PostgreSQL pentru executor, reconciliere, decision/role apply, dashboard și anchor. SQLite v2 este numai implementare de referință/replay. Release-ul se instalează exclusiv la SHA exact sub `/opt/etoro-v2/releases/<sha>`, cu manifest și lock de dependențe; `current` este un symlink atomic. `provision-v2-host.sh` aplică schema verificată și granturile distincte engine/executor/observer.

Signer-ul v2 rulează ca `etoro-signer`, fără rețea sau DB, și este singurul proces care încarcă `v2-risk-signing.key`. Validează peer UID și întregul mandat înainte de semnare. Execution applier-ul folosește socket-ul local și cheia publică; executorul rulează ca `etoro-executor`, nu poate accesa socket-ul/cheia privată și încarcă exclusiv `v2-risk-verifying.pub` plus cheia broker DEMO write. `verify-v2-boundaries.sh` probează efectiv permisiunile negative. Fiecare `OrderCommand` leagă payload-ul complet, configurația de risc, TTL-ul și sursa fixă. Un `SUBMITTING` sau ACK nu se retrimite și blochează risc nou până la reconciliere exactă.

Fără `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION`, numai `etoro-v2-decision-apply.service` este permis: nu are rețea, broker sau signer și înregistrează `broker_write=false`, fără `OrderCommand`. Fiecare packet este legat durabil de autoritatea `SHADOW` fără epoch. Gate-ul oprește consumul shadow, iar starea `ACTIVE` deschide un epoch `EXECUTION` egal cu versiunea stării; packet-urile `PENDING`/`ERROR` din autoritatea veche sunt expirate înainte de inference/budget claim, iar cele `DECIDED` sunt expirate atomic înainte de apply claim. `etoro-v2-decision-apply-execution.service` poate transforma numai selecția unui candidat determinist din epoch-ul curent în intent. Commit-ul comenzii verifică același epoch în tranzacția order/reservation/outbox, iar executorul nu trimite nici OPEN, nici CLOSE când starea este `LOCKED`. Modelul nu poate inventa symbol, side, amount, SL/TP, horizon sau slippage.

În lane-ul `D_sol_plus_critic`, coordinatorul nu pune deciderul în coadă simultan cu criticul. Numai output-ul critic al aceluiași packet/bar poate crea packet-ul decider; `VETO`, `DE_RISK` sau `INCONCLUSIVE` blochează un entry nou. P&L daily/weekly/monthly este derivat din evenimentele de realizare datate; câștigul istoric/lifetime nu poate masca o pierdere zilnică, iar pierderea nerealizată curentă este aplicată conservator tuturor porților.

Backup-ul v2 cere obligatoriu fișierul libpq service al observerului, `pg_dump` și `pg_restore`; lipsa oricăruia face jobul să eșueze. Conexiunea nu este expusă în argv. Mesajul `ETORO_V2_BACKUP_OK` apare numai după arhiva PostgreSQL validată și checksum-uită. `etoro-v2-restore-drill.timer` restaurează săptămânal într-o bază temporară, verifică event table/schema și o șterge; nu suprascrie baza canonică.

Înainte de orice activare: rulează suita completă inclusiv testul PostgreSQL cu `ETORO_TEST_POSTGRES_DSN`, verifică schema și backup/restore, pornește întâi market/shadow read-only, execută fault drill pentru crash după send și `UNKNOWN`, confirmă zero reconciliere ambiguă și zero drift broker/local, apoi verifică exact SHA/config și rollback. Un ordin încă pending nu devine fill final. Orice identitate broker sau preț/cantitate de close incompletă rămâne manual review și `LOCKED`.

Nu porni v2 dacă runtime-ul v1 are poziție locală fără corespondent broker, ordin nereconciliat sau kill activ. Activarea v2 nu este inclusă în simpla remediere/validare a PR-ului.

## Future REAL activation gate — neimplementat

Nu există rută, serviciu sau configurație REAL. Trecerea la bani reali se face
numai într-un release separat și niciodată prin schimbarea unei singure valori:

1. cerere explicită nouă a ownerului pentru activare REAL;
2. rezultate DEMO revizuite, criterii economice acceptate și buget/risc pentru contul REAL reconfirmate;
3. security review nou pentru data, Sol, risk, seal, audit, executor, reconciliation și kill; toate testele adversariale plus fault drill trecute;
4. User Key nou, exclusiv `Environment=Real`, stocat separat prin `LoadCredential`; cheia DEMO nu se reutilizează și niciun proces LLM nu primește credentiale;
5. config, unitate systemd, runtime/DB și allowlist REAL separate. Rutele REAL se adaugă explicit numai în acel release; testul care interzice azi rutele REAL trebuie schimbat și revizuit intenționat;
6. boot REAL în `LOCKED`, reconciliere broker completă, zero drift și verificare manuală a ordinului minim înainte de activare;
7. decizie separată privind autorizarea fiecărui write REAL. Mandatul permanent DEMO nu se copiază, nu se moștenește și nu se extinde automat;
8. deploy cu rollback, monitorizare și dovadă exactă de SHA/config/credential scope. Nicio promovare automată din DEMO.

Până când toate cele opt puncte sunt îndeplinite, orice scope sau rută REAL este
o încălcare fail-closed și executorul trebuie să refuze pornirea.
