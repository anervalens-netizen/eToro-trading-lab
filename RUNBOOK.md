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

## DEMO execution gate

Configurația livrată nu execută ordine eToro. Pentru etapa următoare:

1. soak shadow și fault drills;
2. verificare scopes DEMO, reconciliation și request exact;
3. configurație separată untracked cu `account_mode=demo` și `etoro_demo_execution_enabled=true`;
4. aprobare owner exactă, one-time, pentru fiecare request eToro write.

Executorul nu acceptă cheia largă a contului. În eToro: Settings → Trading →
API Key Management → Create New Key, alege `Environment=Demo`,
`Permission=Write`, IP-ul serverului și expirare. User Key-ul separat se pune în
`/etc/etoro-agent/etoro-demo-user-key` și se încarcă prin `LoadCredential`.
Executorul cere DEMO read+write și refuză orice scope REAL. eToro poate adăuga
cheii DEMO scope-uri auxiliare ale platformei; acestea
nu lărgesc allowlist-ul runtime, care conține exclusiv read-urile necesare și
rutele DEMO open/close. Managementul Agent Portfolio folosește numai rutele v2;
codul runtime expune doar listarea/scopes, nu provisioning generic.

După activare, `etoro-demo-executor.service` consumă numai propuneri deja sigilate și aprobate. Deschiderea folosește `/api/v2/trading/execution/demo/orders`; închiderea completă rezolvă `positionId` din broker truth și folosește ruta oficială DEMO market-close. Serviciul nu se pornește cât configurația livrată este `paper`/execution disabled.

`init-security` generează `risk-signing.key` (privată, numai shadow/risk) și
`risk-verifying.pub` (publică, singura cheie încărcată în executor). Executorul
nu trebuie să primească niciodată `ETORO_RISK_SIGNING_KEY_FILE`.

Nu adăuga rute REAL, real scopes sau un auto-approver. Banii reali necesită o cerere owner separată și review de securitate.
