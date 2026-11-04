# Atlas Quant Research v1.0 — esperimento storico *non live*

Prototipo indipendente **preparato il 4 novembre 2026**. Nessun ordine reale, nessun aggiornamento della strategia ufficiale e nessuna scrittura verso `strategies/`, `reports/` o `v08/` del progetto forward. Il backtest fa uso esclusivamente di dati congelati e locali, senza API live.

## Cosa è stato realmente costruito

Quattro conti storici con €10.000 ciascuno, **2016-02-01–2025-12-10**:

1. `Top10_Eligible_BH`: prime dieci società **eleggibili USD/EUR** della Top200 ricostruita; febbraio = aggiornamento annuale, senza vendite dei vincitori per riequilibrare.
2. `Top10_Eligible_MA6_12`: stesso paniere, regime globale basato su SMA6/SMA12 mensili ±1% (0%/100% investito).
3. `Quant20_Momentum`: selezione trimestrale dei 20 titoli migliori per 12-1 momentum (45%), 6-1 momentum (25%), bassa volatilità a 126 giorni (20%), basso rischio negativo (10%). Massimo 5 selezionati per settore, senza ribilanciare automaticamente i sopravvissuti.
4. `Quant20_TrendVol`: stessa selezione Top20, segnale MA6/MA12 globale 0/1; in risk-on, esposizione da 50% a 100% in funzione di un budget volatilità annua **14%**, con pesi inversamente proporzionali alla volatilità e proiezione su limiti **9% per nome** e **30% per settore** all'aggiornamento trimestrale. Nei mesi intermedi adatta l'esposizione senza ricostruire arbitrariamente i pesi relativi.

I modelli sono stati **fissati prima del confronto** e non ottimizzati sulla metrica CAGR finale. Non c'è modello di intelligenza artificiale, stima comprovata di alpha, previsione quality/value o ottimizzazione garantita.

## Provenienza e limiti

- Origine: archivio utente `H4_GLOBAL_DATASET_FROZEN_2026-09-12.zip`, contenente `global_top200_annual_pit.csv` e prezzi Yahoo OHLC, con subset originale in `data/Top200_USD_EUR_Frozen_Prices.zip` (218 società). Impronta origine in `reports/manifest.json` e `data/provenance.json`.
- **Classifiche PIT-style, NON PIT certificate**: raccolte storiche ricostruite, non copie indipendentemente verificate delle pubblicazioni del periodo. Rimangono possibili survivorship, errori di mapping, modifiche retroattive.
- **Solo titoli denominati USD o EUR** presenti nel dataset: non rappresenta il completo universo globale e introduce una distorsione verso Stati Uniti ed Europa. Non è l'esatta Top 10 mondiale usata nel motore live.
- Segnale sempre su dati disponibili fino all'**ultimo giorno del mese precedente**; ordine alla prima **OPEN** storica successiva per ciascuna sede, con cambio EUR/USD dell'ultima data disponibile **precedente** all'apertura. Il benchmark sintetico del regime è `60% SPY / 30% EFA / 10% EEM` convertito in EUR, NON IUSQ certificato.
- Dati Yahoo OHLC già rettificati ex-post per gli split; i dividendi sono **stimati** dal rapporto `adj_close/close` e applicati convenzionalmente all'ex-date, non alla pay-date. I titoli non sono accompagnati da dati fondamentali globali completi per un modello Quality/Value PIT serio.
- Stima italiana: 26% su plusvalenze realizzate, minusvalenze compensabili per 4 anni, proxy 26% dividendi, 0,2% annuale bollo, 1 € per ordine, 0,1% slippage per lato. NAV come **liquidazione ipotetica tassata**, senza anticipare imposte nei flussi reali. Modello semplificato; le tasse ETF non sono equivalenti alle azioni e il benchmark ad accumulazione è favorito dall'assenza del drag sui dividendi intermedi.
- La selezione Top20 genera un numero elevato di ordini, imposte e spread; il test non prova che un modello quantistico, un ensemble o un ottimizzatore più complesso batterebbero il mercato.

## Risultati e verifica

Leggere `REPORT.md`, `reports/comparison.csv`, `reports/uncertainty.csv`, `reports/stress_sensitivity.csv`, grafici e registri `*_orders.csv` / `*_signals.csv`.

Esecuzione locale con Python 3.11+:

```bash
pip install -r requirements.txt
python quant_research.py --output reports
python analytics.py
python -m unittest discover -s tests -p 'test_*.py' -v
```

Gli archivi dei prezzi e dei cambi sono inclusi nella ZIP di ricerca. I file pubblicati sul ramo GitHub della ricerca contengono codice e report ma non il database binario completo. Nessun dato viene scaricato dalla rete dal motore di ricerca.