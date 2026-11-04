# Atlas Top10 — Audit Backtest v0.8 (4 novembre 2026)

## Esito sintetico

**I risultati sono stati calcolati su un archivio storico realmente disponibile, non inventati.** Ma si tratta di un **BACKTEST-PROXY esplorativo**, non del backtest perfettamente equivalente alla v0.8 in forward: i prezzi di esecuzione sono i primi **CLOSE** giornalieri disponibili dopo il segnale (non gli OPEN); il segnale usa il proxy globale SPY/EFA/EEM e non IUSQ; ranking ricostruito e non certificato PIT. Non sostituire o promuovere questi risultati a performance attese.

**Periodo:** 2016-02-01 – 2025-12-10, 9.86 anni. Capitale iniziale 10.000 €. Costi e tassa su liquidazione ipotetica finale inclusi nella NAV liquidabile (*). Benchmark sintetico globale 60% SPY/30% EFA/10% EEM convertito EUR: **9.86% CAGR netto**, capitale finale 25,266.83 € (a fronte della stessa fiscalità approssimata).

## Confronto netto

| Strategia       | CAGR netto *   | MaxDD *   |   Sharpe |   Ordini | Valore finale *   |
|:----------------|:---------------|:----------|---------:|---------:|:------------------|
| BuyHold_Top10   | 16.16%         | -32.19%   |     0.84 |       44 | 43,779 €          |
| MA10            | 9.55%          | -25.27%   |     0.68 |      156 | 24,576 €          |
| MA6_MA12        | 13.59%         | -28.71%   |     0.8  |       78 | 35,110 €          |
| MA8_MA15        | 13.77%         | -28.57%   |     0.81 |       78 | 35,672 €          |
| Hybrid_MA3_MA10 | 10.28%         | -20.96%   |     0.74 |      116 | 26,235 €          |
| Adaptive_Risk   | 9.37%          | -22.86%   |     0.76 |      514 | 24,183 €          |

*La NAV finale incorpora un'imposta su vendita teorica di tutte le posizioni ancora aperte. Plusvalenze realizzate tassate al 26%, minusvalenze con scadenza 4 anni, slippage 0,1% lato, commissione 1€ per ordine, bollo 0,2% annuo, dividendi 26% proxy, liquidità non remunerata, tassa di negoziazione HKD stimata.*

**Interpretazione:** MA6/MA12 supera Adaptive Risk di **4.22 punti CAGR**, ma Adaptive Risk ha un MaxDD migliore di **5.85 punti percentuali**. Adaptive Risk effettua **514 ordini contro 78**, quindi richiede molta più operatività e produce un maggiore rischio di modellizzazione dei costi. La versione B&H cresce più rapidamente ma subisce un drawdown del 32.19%.

**Correzione audit eseguita:** la vecchia interpretazione del motore v0.8 poteva vendere titoli vincenti per ricostruire pesi nominali anche a segnale invariato. Lo studio mantiene invariati gli incumbent quando esposizione e membership non cambiano; al cambio del ranking ricicla esclusivamente i proventi dei titoli usciti. Questo porta il B&H a 44 ordini (27 BUY e 17 SELL), coerenti con l'obiettivo di non ribilanciare i vincitori.

## Analisi cronologica (NON out-of-sample indipendente)

| Strategia     | 2016–2021 CAGR   | 2022–2025 CAGR   | 2022 MaxDD   |
|:--------------|:-----------------|:-----------------|:-------------|
| BuyHold_Top10 | 19.04%           | 11.59%           | -32.02%      |
| MA10          | 10.47%           | 7.58%            | -24.54%      |
| MA6_MA12      | 14.34%           | 11.87%           | -23.59%      |
| Adaptive_Risk | 9.77%            | 8.23%            | -15.35%      |

Il periodo 2022–2025 era già stato esaminato in Atlas e non costituisce un vero holdout nuovo. Scegliere strategie sulla base di questo periodo può generare sovraottimizzazione.

## Sensibilità commissioni e slippage

| Scenario                | Strategia     | CAGR netto *   | MaxDD *   |   Ordini |
|:------------------------|:--------------|:---------------|:----------|---------:|
| Base 0,1% slippage / 1€ | MA6_MA12      | 13.59%         | -28.71%   |       78 |
| Base 0,1% slippage / 1€ | Adaptive_Risk | 9.37%          | -22.86%   |      514 |
| Slippage 0,3% / 1€      | MA6_MA12      | 13.43%         | -28.71%   |       78 |
| Slippage 0,3% / 1€      | Adaptive_Risk | 9.02%          | -22.90%   |      514 |
| Slippage 0,1% / 2€      | MA6_MA12      | 13.55%         | -28.72%   |       78 |
| Slippage 0,1% / 2€      | Adaptive_Risk | 9.07%          | -22.92%   |      514 |

La maggiore operatività dell'Adaptive Risk lo rende particolarmente sensibile ai costi. Sono scenari simulati, non prezzi di mercato certificati.

## Indicatore di robustezza esplorativo

Nel calcolo block-bootstrap di differenze mensili accoppiate tra MA6/MA12 e Adaptive Risk, la differenza di log-rendimento annualizzato è **+3,79 pp** in favore di MA6/MA12, intervallo percentile 95% **[+1,45, +6,20] pp**, usando 2.500 bootstrap in blocchi di 12 mesi. Non è una prova statistica conclusiva: dati, soglie e varianti sono stati esaminati molte volte; il bootstrap non corregge tutti questi fattori e non certifica i prezzi e i ranking.

## Limiti non risolti e miglioramenti ancora necessari

1. **Prezzi OPEN mancanti:** per esecuzione fedele alla v0.8 occorrono OHLC originali per ogni asset, con corporate action, orari e festività delle singole borse. Questo test impiega close successivi a segnale precedente.
2. **Indice di regime non identico:** il proxy 60/30/10 in EUR non è un ETF IUSQ storico, e può attivare segnali in date diverse.
3. **Universe survivorship/PIT:** ranking da snapshot ricostruiti; necessarie fonti contemporanee registrate con data pubblicazione, gestione dei delisting e corporate action.
4. **FX/dividendi/bollo:** conversioni dei prezzi storici derivano da serie EUR già preelaborate; HKD usa un rapporto di cambio indicativo, non fixing negoziabili; dividendi e regime fiscale italiano sono stime. Non è un calcolo fiscale certificato.
5. **Calendari:** gli archivi dei prezzi sono allineati alle sedute del benchmark USA; l'esecuzione multi-borsa vera non può essere ricostruita completamente da questo snapshot.
6. **Rischio statistico:** rendimenti già selezionati fra numerose strategie e asset ad alta capitalizzazione; nessuna garanzia predittiva.

## Materiali riproducibili

- `backtest_v08_proxy.py`: motore e generazione CSV su dati archived (senza rete).
- `diagnostics.py`: sensitività, sottoperiodi, bootstrap, grafici.
- `test_backtest.py`: test automatici (4).
- `historical_source/`: dataset v0.5 e codice fiscale originale per riprodurre lo stesso universo. Hash in `INPUT_SHA256.json`.
- `COMPARISON.csv`, `SUBPERIODS.csv`, `SENSITIVITY.csv`, `MONTHLY_WINS.csv`, `EQUITY.csv`: tabelle numeriche.
- `*_ORDERS.csv`, `*_SIGNALS.csv`, `*_NAV.csv`: audit riga per riga delle operazioni e segnali.

**I registri ufficiali forward v0.7 non sono stati modificati.** La correzione anti-turnover nel motore v0.8 shadow è tecnica e pre-prima-operazione, preservando l'intento dichiarato di non ribilanciare i titoli vincenti con segnale invariato.