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

## Risultati verificati — 1926-07-01 / 2026-11-11

| Metrica | Atlas V2.3 originale (macchina a stati invariata) | PAC passivo sullo stesso proxy FF |
|---|---:|---:|
| CAGR TWR | 9,3808% | 10,1972% |
| Max drawdown | -36,5368% | -84,5247% |
| XIRR | 9,1866% | 10,5201% |
| Valore netto liquidabile finale | EUR 237.058.084,04 | EUR 706.818.529,23 |
| Capitale versato | EUR 302.000 | EUR 302.000 |
| Ordini Atlas | 1.167 | — |

Copertura: dati FF congelati fino al 31 dicembre 2025, FF aggiornati fino al 30 settembre 2026 (85 sedute aggiunte oltre maggio), SPY con prezzi aggiustati per 30 sedute dal 1° ottobre all'11 novembre. Le tre ipotesi macro 2026 (`neutral`, `carry`, `stressed`) restituiscono gli stessi valori perché nel 2026 non ci sono trigger di stress nella macchina a stati. Gli 11 mesi macro successivi a novembre 2025 non sono dati originali osservati; non utilizzare questa estensione come test esatto PIT.

Audit GitHub Actions: [ultimo workflow di ricerca](https://github.com/alessandrocastagna11/atlas-top10-forward/actions/workflows/atlas-v23-original-replay.yml). Risultati: [summary.json](reports/summary.json), originale leggibile: [atlas_v23_pac_stress.py](atlas_v23_pac_stress.py).

## Ulteriore confronto omogeneo: 2016–2026

| Metrica | Atlas V2.3 originale | PAC passivo sullo stesso proxy FF |
|---|---:|---:|
| CAGR TWR | 11,6876% | 14,7115% |
| Max drawdown | −21,7154% | −34,3602% |
| XIRR | 10,7984% | 12,1567% |
| Capitale finale netto liquidabile | EUR 62.548,94 | EUR 67.837,28 |
| Capitale versato | EUR 33.500 | EUR 33.500 |
| Ordini Atlas | 142 | — |

La macchina a stati V2.3 originale non usa la costruzione 4-ETF 65/15/10/10. Questo confronto isola la logica di timing e difesa originale sullo stesso proxy azionario; non è un confronto di asset allocation. Il report verificabile è in [reports_modern/summary.json](reports_modern/summary.json).


## Challenger: rientro satellite più rapido — verificato 12 novembre 2026

Esperimento isolato e replicabile in [reentry_experiment.py](reentry_experiment.py); [risultati JSON](reports/reentry_challengers_2026.json). **La versione originale V2.3 rimane immutata, con satellite_rebound=0.03.** Sono cambiati soltanto i valori nel config del replay challenger: 0.025, 0.02, 0.015. Uscite, stress gate, core, costi, versamenti e benchmark restano invariati. Stesso pannello di dati per le quattro varianti in ogni replay.

| Soglia | CAGR 2016–2026 | MaxDD 2016–2026 | YTD 2026 con avvio 2016 | CAGR 1926–2026 | MaxDD 1926–2026 | Ordini 1926–2026 |
|---|---:|---:|---:|---:|---:|---:|
| **3% congelato** | **11,6876%** | −21,7154% | 3,87196% | **9,3808%** | −36,5368% | 1.167 |
| 2,5% | 12,0168% | −21,6441% | 3,8097% | 9,1886% | −35,2756% | 1.221 |
| 2% | 12,8940% | −21,8267% | 4,0279% | 8,8093% | −42,1959% | 1.248 |
| 1,5% | 11,3001% | −24,6844% | 3,9611% | 8,4222% | −41,2900% | 1.296 |

Nel replay inizializzato nel 2016, la prima rientrata del 2026 è: originale 16/04; 2,5% 15/04; 2% o 1,5% 14/04. Le uscite 2026 restano 16/03 e 06/11 per tutte le varianti. Un rientro anticipato di due sedute recupera solo 0,156 punti percentuali di TWR YTD 2026 in un portafoglio nato nel 2016.

**Decisione di ricerca:** la soglia 2% non supera la validazione secolare (CAGR e DD peggiori). La soglia 2,5% ha miglior DD secolare ma rendimento inferiore; nessuna variante è un miglioramento inequivocabile della baseline. Non promuovere un challenger alla strategia ufficiale né mutare il conto forward.

Limiti: storico lungo su proxy Fama/French, estensione 2026 con dati ufficiali fino al 30 settembre e SPY da ottobre, macro 2026 sensibile a dati non osservati, nessun test di generalizzazione su un ETF direttamente negoziato, varianti scelte ex post. Inoltre il vecchio `ATLAS_V23_RECOVERY_ACCEL_TEST_2026-10-01.zip` e il `ATLAS_V23_DYNAMIC_RECOVERY_TEST_2026-10-01.zip` erano test *diversi*: acceleratori con ETF e selettori su motore di ricerca semplificato, **non** sostituiscono la validazione sul motore esatto.
