# Runbook

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
etoro-agent --config config/demo.json --runtime runtime ai-pending --limit 5
etoro-agent --config config/demo.json --runtime runtime kill
etoro-agent --config config/demo.json --runtime runtime resume --confirm RESUME_DEMO
etoro-agent --config config/demo.json --runtime runtime dashboard --host 127.0.0.1 --port 8765
etoro-agent --config config/demo.json --runtime runtime agent-portfolio-status
```

`kill` este intenționat imediat. `resume` cere confirmare exactă. La boot nou, starea este fail-closed până la resume explicit.

## Production service

```bash
sudo systemctl status etoro-shadow etoro-dashboard
sudo journalctl -u etoro-shadow -u etoro-dashboard --since today
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

`OPEN` cere `--candidate-id` exact. `CLOSE` este valid numai pentru packet de poziție. Packet expirat/hash greșit/decizie repetată este respins. Task-ul nu are credentiale eToro și nu poate executa ordine.

Pe Dell, `etoro-sol-runner.service` folosește `/usr/bin/codex` autentificat prin ChatGPT, modelul exact `gpt-5.6-sol`, fără cheie OpenAI Platform. Wrapperul SSH este determinist; procesul model rulează separat, read-only, fără acces la cheile SSH. Orice eroare/quota produce zero decizii noi și implicit `HOLD`.

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

`etoro-demo-executor.service` consumă numai propuneri sigilate și autorizate. Deschiderea folosește `/api/v2/trading/execution/demo/orders`; închiderea completă rezolvă `positionId` din broker truth și folosește ruta oficială DEMO market-close. Serviciul nu se pornește cu configurația `paper`/execution disabled.

Shadow worker folosește numai bare finalizate plus `candle_close_grace_seconds` (60 s în configurațiile livrate), deduplică separat fiecare strategie și așteaptă un timestamp de quote broker strict mai nou înainte de fill-ul simulat. După un write DEMO, `master_pending_execution` rămâne durabil până când broker truth confirmă poziția deschisă/închisă; ledgerul local nu anticipează brokerul. ACK nereconciliat în 120 s trece kill în `LOCKED` și cere investigație, nu retry.

Calendarul SPX500/NSDQ100 urmează orele eToro publicate: deschidere duminică 22:00 UTC, închidere vineri 20:30 UTC și pauză zilnică 21:00–22:00 UTC. Sursa canonică: `https://www.etoro.com/trading/market-hours-and-events/`. Calendarul conservator poate bloca opens suplimentar, niciodată să extindă sesiunea.

Un `trade_intent` cu `accepted_for_execution=false` este numai observație de research din sesiune închisă. Nu îl promova și nu îl reintroduce la următoarea deschidere; runtime-ul expiră și pending-urile care traversează închiderea.

Înainte să înregistreze o propunere master, shadow worker cere eligibility și cost preview DEMO. Minimul de expunere, settlement/direcție/leverage și suma minimă sunt verificate aici și din nou în executor. O incompatibilitate oprește candidatul fără write și fără kill. `settlementType=real` este permis numai determinist pentru buy nelevierat AAPL/TSLA/BTC/ETH pe ruta DEMO; nu schimbă account mode și nu permite vreo rută REAL.

Epoch-ul curent este `broker-compatible-v3-20260810`. La schimbarea epoch-ului, packet-urile Sol pending/decided vechi sunt invalidate atomic. Dashboard-ul exclude de la promovare orice poziție `carried pre-epoch`; aceasta rămâne vizibilă și este gestionată normal până la închidere. Nu șterge auditul sau fill-urile vechi pentru a cosmetiza statisticile.

`init-security` generează `risk-signing.key` (privată, numai shadow/risk) și
`risk-verifying.pub` (publică, singura cheie încărcată în executor). Executorul
nu trebuie să primească niciodată `ETORO_RISK_SIGNING_KEY_FILE`.

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
