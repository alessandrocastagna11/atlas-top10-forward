# Atlas ETF V2.3 — Motore originale recuperato

Il ramo di ricerca `atlas-etf-v23-original` contiene il motore **esatto** della release congelata V2.3 (1° ottobre 2026) e gli input storici originari in `pinned_reference.zip`. Il motore viene estratto, verificato con SHA-256 `aef2809b8a3bfe01e3b0fa6b3f0d564b8ad0db90340a04a7480d7266d9141c7a` e importato senza modificarne la macchina a stati.

La prima fase ricontrolla tutti i nove indicatori congelati 1926–2025, inclusi 9,360402% CAGR, −36,536837% MaxDD e 1.154 operazioni. **Questa fase deve passare prima dell'estensione.**

La seconda fase estende il periodo al **11 novembre 2026**. Conserva immutato il CSV Fama/French per tutto il periodo congelato fino al 31 dicembre 2025 e incorpora i dati 2026 già compresi nel pacchetto (fino al 29 maggio 2026). Per i giorni successivi prova la fonte ufficiale Kenneth French e integra soltanto quelli ancora mancanti con le variazioni giornaliere di SPY aggiustato per dividendi, tramite Yahoo Chart. Anche il tasso privo di rischio nell'ultima tratta approssimata viene mantenuto all'ultimo valore disponibile: non è un dato storico pienamente osservato.

I punteggi di stress mensili originali arrivano al **novembre 2025**. Per i mesi successivi e solo nell'estensione, il report esegue tre scenari: `neutral` (score 0), `carry` (ultimo score osservato), `stressed` (score 2). Se differiscono, manca un singolo risultato 2026 determinato finché non sono recuperate le serie credito, breadth, tassi originali. Il modello e i suoi indicatori di uscita/rientro restano congelati.

Il benchmark passivo usa **lo stesso identico proxy azionario Fama/French**, con gli stessi versamenti (1.000 € e 250 € al mese), slippage, bollo e tassazione in liquidazione. Esso non corrisponde al paniere passivo 65/15/10/10 e non ha lo storico di un reale MSCI World 1926.

## Modalità di esecuzione

```bash
python atlas_etf_pac/v23_original/run_extended.py --end 2025-12-31 --no-online --scenario carry
python atlas_etf_pac/v23_original/run_extended.py --end 2026-11-11 --scenario all
```

I report sono in `atlas_etf_pac/v23_original/reports/`. Questo ramo rimane **PAPER / RESEARCH** e non crea ordini reali. Il conto forward esistente 4-ETF 65/15/10/10 rimane invariato sul ramo `atlas-etf-pac-paper`. Non simuliamo una migrazione contabile retroattiva né attribuiamo ai nuovi dati 2026 la certificazione frozen del 2025.

## Punto critico per il passaggio in forward

La V2.3 originale è una strategia proxy azionaria globale con core/satellite 50/50 e difesa Treasury/cash, **non** una strategia 4-ETF. Per migrarne l'uso giornaliero serve un nuovo ledger virtuale prospettico che applichi la macchina a stati sul proxy negoziabile con dati EOD e regole sugli ordini; non si può riutilizzare in silenzio il ledger 4-sleeve né definire "originale" il monitor 4-ETF.
