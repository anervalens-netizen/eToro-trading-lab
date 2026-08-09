# eToro DEMO Trading Lab

Platformă de cercetare și tranzacționare virtuală, fără suport pentru bani reali.

Include:

- date eToro pentru 7 instrumente, validate și auditate;
- 12 ipoteze deterministe, fiecare cu ledger de cercetare separat;
- un portofoliu master virtual de 1.000 USD, administrat de Sol prin `OPEN/CLOSE/HOLD`;
- backtest next-quote, costuri per instrument și walk-forward out-of-sample;
- risc determinist, limite fixe, kill switch persistent și ordine sigilate Ed25519;
- executor cu rutele oficiale eToro DEMO open/full-close și aprobare owner exactă, one-time;
- P&L zilnic, audit hash-chain, stări durabile și dashboard Authentik owner-only;
- task Codex recurent cu `gpt-5.6-sol`, fără OpenAI Platform API/key;
- replay clock stdlib determinist, fără dependența Nautilus folosită anterior doar pentru ceas.

Shadow trading este complet autonom: strategiile propun, Sol selectează/deschide/închide, iar risk engine-ul poate doar restrânge acțiunea. Lipsa unei decizii Sol produce `HOLD`. Scrierile eToro DEMO rămân dezactivate implicit și necesită aprobarea individuală a requestului exact. Nu există rută REAL în executor.

Vezi [ARCHITECTURE.md](ARCHITECTURE.md) și [RUNBOOK.md](RUNBOOK.md).
