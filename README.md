# eToro DEMO Trading Lab v0.6.1

Un singur runtime canonic, exclusiv eToro DEMO. Nu există rută, configurație,
credential sau serviciu pentru bani REALI.

## Contract

- Flux unic: `Intent -> Risk -> sealed Order -> ACK -> Fill -> Position -> Exit -> P&L -> Reconciliation`.
- AI poate selecta sau respinge numai candidați determinați local; nu poate inventa simbol, direcție, sumă, SL/TP ori slippage.
- Numai kernelul determinist și signer-ul izolat Ed25519 pot autoriza o comandă.
- PostgreSQL este singura autoritate operațională. SQLite este limitat la catalog raw, research/replay și teste; nu există CLI, serviciu sau credential broker pentru un writer SQLite.
- `etoro-v2` este inspection-only: `validate-config` și `release-info`. Singurul writer broker este `etoro-v2-executor-postgres.service`.
- OPEN cere simultan gate DEMO, stare `ACTIVE`, epoch curent, release de strategie semnat și dovezi OOS/promotion/soak/cost/calendar valide.
- `LOCKED` blochează risc nou. Cu gate prezent permite numai CLOSE reduce-only strict legat de poziția broker; fără gate este freeze absolut al writerelor.
- Ordinele ambigue devin `UNKNOWN`; payload-urile pre-submit otrăvite ajung `QUARANTINED`; niciuna nu este retrimisă orb.

## Stare sigură implicită

Installerul lasă gate-ul `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION` absent,
starea `LOCKED` și writer-ele oprite. Repo-ul nu livrează un manifest de strategie
promovat: până există dovezi empirice reale, OPEN este blocat chiar dacă un
operator pornește accidental serviciile de execuție.

Runtime-ul v1 și comanda `etoro-agent` au fost eliminate din sursă, wheel,
provisioning și systemd. Referința forensic este numai în istoricul Git, descrisă
în [archive/legacy-v1/README.md](archive/legacy-v1/README.md).

Documentație canonică: [arhitectură](V2_ARCHITECTURE.md),
[deploy](V2_DEPLOYMENT.md), [securitate](V2_SECURITY.md),
[research](V2_RESEARCH_PROTOCOL.md), [status](V2_STATUS.md) și
[recuperare chei](KEY_RECOVERY.md).
