# Atlas Quant Research v1.0 — Backtest esplorativo

**Data studio:** 4 novembre 2026  
**Periodo storico:** 1 febbraio 2016 – 10 dicembre 2025, 2.550 sedute e circa 9,86 anni.  
**Capitale di partenza:** €10.000 virtuali per strategia. Nessun ordine reale. **Il motore forward su GitHub non è stato toccato.**

## Risultati principali

| Strategia | CAGR dopo imposte stimate | Max drawdown | Sharpe | Ordini | Capitale finale liquidabile |
|---|---:|---:|---:|---:|---:|
| Top10 eleggibili USD/EUR, buy & hold con ricambio annuale | **15,80%** | −32,54% | 0,80 | 46 | €42.459 |
| Top10 eleggibili + MA6/MA12 | **13,61%** | −28,70% | 0,78 | 82 | €35.171 |
| **Quant Top20 — Momentum + Low Vol** | **8,51%** | −30,64% | 0,60 | **970** | €22.358 |
| **Quant Top20 — Trend + Target Vol** | **6,28%** | −31,36% | 0,52 | **961** | €18.229 |
| Proxy globale 60/30/10, acquisto e mantenimento | 9,86% | −27,47% | 0,72 | 1 | €25.267 |

**Esito: in questo universo storico la sofisticazione non migliora il rendimento.** Il Top20 momentum perde ~7,3 punti CAGR contro la Top10 eleggibile buy & hold e circa 5,1 punti contro la Top10 MA6/MA12. Il Top20 a volatilità target non migliora nemmeno il drawdown complessivo; l'ottimizzatore di rischio, nella versione provata, non giustifica l'operatività aggiuntiva. Non promuovere nessuno dei due modelli a strategia live.

## Costi e rotazione

- Top10 buy & hold: 46 ordini, €46 commissioni, €60,82 slippage simulato; imposte sulle plusvalenze effettivamente realizzate €838,30 (esclusa la liquidazione teorica giornaliera), €410,74 sui dividendi stimati.
- Top10 MA6/MA12: 82 ordini, €82 commissioni, €143,46 slippage, €3.558 imposte su plusvalenze realizzate e €284 sui dividendi stimati.
- Quant20 Momentum: **970 ordini**, €970 commissioni, €697,53 slippage, €3.133,63 imposte su plusvalenze realizzate e €790,72 sui dividendi stimati.
- Quant20 Trend/Vol: **961 ordini**, €961 commissioni, €530,17 slippage e €2.174,97 imposte sulle plusvalenze realizzate. Un ordine rimasto ineseguito è stato annullato dopo un segnale nuovo (audit 2025-03-03), **senza inventarne il prezzo**.

L'alta rotazione è una parte del risultato, non un vincolo imposto artificialmente. Il backtest registra un ordine per ogni singolo acquisto/vendita.

## Stress test dei costi

Scenario più severo: **€2 per ordine e 0,30% slippage per lato**, stessi segnali e prezzi.

| Strategia | CAGR standard | CAGR costi elevati | Differenza |
|---|---:|---:|---:|
| Top10 buy & hold | 15,80% | 15,69% | −0,11 pp |
| Top10 MA6/MA12 | 13,61% | 13,40% | −0,21 pp |
| Quant20 Momentum | 8,51% | 7,09% | −1,42 pp |
| Quant20 Trend/Vol | 6,28% | 4,93% | −1,35 pp |

## Robustezza statistica descrittiva

Differenze di log-rendimento mensile, annualizzate; bootstrap con blocchi di 12 mesi, 3.000 repliche:

- Quant20 Momentum vs Top10 buy & hold: −6,44 pp/anno; IC95% [−12,76, +0,71] pp.
- Quant20 Trend/Vol vs Top10 MA6/MA12: −6,68 pp/anno; IC95% [−12,60, −1,13] pp.
- Quant20 Momentum vs proxy globale: −1,12 pp/anno; IC95% [−5,02, +2,44] pp.

**Non sono intervalli corretti per le numerose strategie già esplorate**, né provano causalità o prestazioni future. Le date 2021–2025 sono già state discusse in Atlas: non costituiscono un test veramente blind.

## Limiti che impediscono di chiamarlo un quant fund istituzionale

1. Il **Top200 è storicamente ricostruito, non PIT certificato**; alcuni prezzi potrebbero non includere aziende delistate, con rischio di survivorship bias.
2. Gli input consentono di utilizzare **solo USD ed EUR**, restringendo a 218 emittenti con storia disponibile e ~150 eleggibili in un dato anno. Non rappresenta il mondo intero.
3. Non abbiamo bilanci globali point-in-time affidabili e completi: il motore **non ha testato Quality né Value**, ma solo segnali su prezzi. Una componente di qualità disponibile per titoli SEC ha copertura limitata e farebbe nascere bias geografico.
4. OHLC Yahoo scaricati e rettificati ex-post; aperture e cambi sono proxy di esecuzione, non prezzi certificati di broker; distribuzioni da *adj_close* stimate.
5. Il benchmark è un composito sintetico (SPY/EFA/EEM) e il trattamento fiscale è approssimato, non identico all'ETF IUSQ né al regime amministrato.
6. Molti test già effettuati su questi anni, anche nei progetti Atlas precedenti; non esiste un *out-of-sample* davvero indipendente in questo studio.

**Decisione consigliata:** non modificare il forward. La fase successiva scientificamente utile sarebbe acquisire un universo PIT globale certificato con dati fondamentali e delisting, e usare un backtest con dati mai utilizzati prima; non aggiungere altri parametri per 'salvare' il CAGR delle varianti perdenti.

## Materiali inclusi

`quant_research.py` (motore), `analytics.py` (bootstrap e stress), `tests/test_quant.py` (12 test), `data/` (OHLC, ranking, FX e benchmark originali), `reports/` (NAV, ordini, segnali, audit fattori, curve storiche, stress, subperiodi, manifest). Tutto funziona offline. Il forward ufficiale v0.7 e quello shadow v0.8 rimangono autonomi e immutati.