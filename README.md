# eToro DEMO Trading Lab v0.5.9

Runtime canonic v2 pentru cercetare și execuție exclusiv eToro DEMO. Nu există rută, configurație, credential sau promovare automată pentru capital REAL.

## Contract critic

- Modelele și strategiile emit numai `TradeIntent`; numai kernelul determinist poate crea ordine sigilate.
- Flux unic: `Intent -> Risk -> Order -> ACK -> Fill -> Position -> Exit -> P&L -> Reconciliation`.
- OPEN este legat de hash-ul intentului. CLOSE/PARTIAL_CLOSE este legat separat de poziția locală completă, `broker_position_id`, cantitate, motiv, snapshot broker și configurația de risc.
- Gate-ul DEMO este verificat în fiecare etapă din procesul care deține credentialul write. Eliminarea lui blochează starea, invalidează ordinele netrimise și oprește writer-ele prin systemd.
- Fiecare packet AI este legat de `SHADOW` sau de versiunea durabilă a unui epoch `ACTIVE`; packet-urile vechi sunt expirate atomic înainte de inference/budget claim și nu pot crea comenzi după activare.
- `etoro-v2-exit-manager.service` aplică stop, take-profit, time-stop și invalidări independent de AI. `HOLD` nu poate suspenda un exit obligatoriu.
- Reconcilierea read-only urmărește ordinele și toate pozițiile broker-backed, inclusiv close/partial close și SL/TP executat server-side.
- Un singur writer broker este permis. Unitatea și comenzile CLI ale executorului v1 au fost eliminate; provisioning-ul maschează și orice copie instalată anterior.

## Operare

Configurația implicită este shadow/read-only și pornește `LOCKED`; fișierul `/etc/etoro-v2-control/ENABLE_DEMO_EXECUTION` lipsește. O instalare reușită nu reprezintă aprobare pentru execuție autonomă: rămân obligatorii calibrarea costurilor, fault drills, 30–60 zile de soak DEMO/shadow, acoperirea de regim și holdout-ul final.

PostgreSQL v2 este sursa canonică multi-proces; SQLite v2 este doar referință/replay. Release-urile sunt instalate pe SHA exact din bundle GitHub atestat, cu lock hashed, wheelhouse offline, suită completă înainte de schimbarea symlinkului, backup verificat și restore smoke-test.

Documentație canonică:

- [V2_ARCHITECTURE.md](V2_ARCHITECTURE.md)
- [V2_DEPLOYMENT.md](V2_DEPLOYMENT.md)
- [V2_SECURITY.md](V2_SECURITY.md)
- [V2_RESEARCH_PROTOCOL.md](V2_RESEARCH_PROTOCOL.md)
- [V2_STATUS.md](V2_STATUS.md)
- [KEY_RECOVERY.md](KEY_RECOVERY.md)

`ARCHITECTURE.md` și `RUNBOOK.md` descriu runtime-ul v1 pensionat și sunt păstrate numai ca istoric/forensic.
