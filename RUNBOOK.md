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
etoro-agent --config config/demo.json --runtime runtime kill
etoro-agent --config config/demo.json --runtime runtime resume --confirm RESUME_DEMO
etoro-agent --config config/demo.json --runtime runtime dashboard --host 127.0.0.1 --port 8765
```

`kill` este intenționat imediat. `resume` cere confirmare exactă. La boot nou, starea este fail-closed până la resume explicit.

## Production service

```bash
sudo systemctl status etoro-shadow etoro-dashboard
sudo journalctl -u etoro-shadow -u etoro-dashboard --since today
curl --fail http://172.23.0.1:8765/healthz
```

Runtime: `/var/lib/etoro-agent`. Credentiale: `/etc/etoro-agent/*`, root-only, încărcate cu `LoadCredential`. Dashboard: `https://trading.astancu.eu`, prin Cloudflare Tunnel → Caddy → Authentik → FastAPI. Orice acces fără proxy-ul Caddy și headerul Authentik exact al ownerului este respins.

## DEMO execution gate

Configurația livrată nu execută ordine eToro. Pentru etapa următoare:

1. soak shadow și fault drills;
2. verificare scopes DEMO, reconciliation și request exact;
3. configurație separată untracked cu `account_mode=demo` și `etoro_demo_execution_enabled=true`;
4. aprobare owner exactă, one-time, pentru fiecare request eToro write.

Nu adăuga rute REAL, real scopes sau un auto-approver. Banii reali necesită o cerere owner separată și review de securitate.
