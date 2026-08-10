# OIL și NATGAS — research v0.3

## Concluzie

`OIL` eToro este CFD-ul Non Expiry cu instrument ID `17`, urmărit separat de `OIL.24-7`. `NATGAS` Non Expiry este instrument ID `22`. Gridul v0.3 folosește exact aceste două instrumente.

Pragul mental 70–100 USD nu este un canal structural pentru WTI. În eșantionul de aproximativ 15 ani, numai 39,2% din observații au fost în interval; percentila 25/mediana/percentila 75 au fost aproximativ 53,28/69,46/87,86 USD. Pe ultimii aproximativ cinci ani, intervalul a cuprins 64,4% din observații, deci este un regim recent, nu o regulă permanentă. Șocurile pozitive WTI de cel puțin 8% au revenit complet în 60 sesiuni numai în aproximativ 37,5% din cazuri; nu se aplică automat fade.

La NATGAS, 2,79 USD nu este podea istorică. Pe aproximativ 15 ani, P10/P25/mediană/P75/P90 au fost aproximativ 2,01/2,52/2,92/3,65/4,59 USD/MMBtu. Volatilitatea anualizată a front-month a fost aproximativ 53,6%, iar drawdown-ul observat a depășit 80%. Șocurile pozitive de cel puțin 10% au arătat mean reversion mai des decât șocurile negative; de aceea gridul are `positive_spike_fade`, nu un fade simetric implicit.

## Experimente

Pentru fiecare instrument rulează cinci ipoteze: adaptive range pe cuantile rolling, Donchian breakout, EMA trend, shock/spike fade și volatility squeeze breakout. Fiecare este duplicată în trei profile independente:

| Profil | Notional | Confidence | Selectivitate | OIL stop/target | NATGAS stop/target |
|---|---:|---:|---:|---:|---:|
| prudent | 50 USD | 0,68 | 1,30× | 2% / 4% | 3,5% / 7% |
| balanced | 100 USD | 0,60 | 1,00× | 3% / 6% | 5,5% / 10% |
| aggressive | 150 USD | 0,55 | 0,75× | 5% / 9% | 8% / 14% |

Acestea sunt profile de experiment shadow, nu relaxări ale risk engine-ului. Limitele masterului DEMO, kill, max exposure, monthly loss și eligibility broker rămân superioare și fail-closed.

Eligibility DEMO verificat live la 10 august 2026 indică `minPositionExposure=1000` USD și `minStopLossPercentage=1` pentru ambele instrumente. Masterul unic poate folosi astfel numai 1.000 USD la leverage 1; limita deterministă de 20 USD risc/trade plafonează stop-ul la 2%. Profilele shadow de 50/100/150 USD nu sunt prezentate ca ordine broker; ele compară sensibilitatea semnalelor și costurilor.

## Catalizatori

- EIA WPSR: de regulă miercuri 10:30 ET; crude inventories și produse.
- EIA WNGSR: de regulă joi 10:30 ET; storage este driver primar pentru NATGAS.
- OPEC/OPEC+: producție și cote, relevante direct pentru OIL.
- White House/Treasury: sancțiuni, SPR, tarife și declarații de politică.
- NOAA/NHC: uragane care pot afecta producția, rafinarea, conductele și LNG.

Headline-ul nu este semnal suficient. Scannerul îl clasifică și îl trimite automat către Sol; Sol vede și confirmarea de preț/spread/data quality. Un eveniment ambiguu sau neconfirmat trebuie să producă `HOLD`.

Surse principale: [EIA WPSR](https://www.eia.gov/petroleum/supply/weekly/), [EIA WNGSR](https://ir.eia.gov/ngs/ngs.html), [EIA price data](https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm), [FRED WTI](https://fred.stlouisfed.org/series/DCOILWTICO), [FRED Henry Hub](https://fred.stlouisfed.org/series/DHHNGSP), [OPEC](https://www.opec.org/), [eToro instrument lookup](https://api-portal.etoro.com/guides/get-instrument-id).
