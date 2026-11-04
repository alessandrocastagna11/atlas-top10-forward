# Atlas Top 10 Global v0.8 — Forward shadow research

**Solo simulazione.** Nessun ordine reale e nessun ricalcolo retroattivo del portafoglio ufficiale v0.7. Capitale iniziale **10.000 EUR per ognuna delle tre varianti**. Seed il **4 novembre 2026**, prima operazione virtuale ammissibile **5 novembre 2026** alla prima apertura idonea. I rendimenti cominciano soltanto dopo che esistono prezzi effettivamente osservati.

## Perché è separato dalla v0.7

La v0.7 usa la chiusura per simulare ordini. La v0.8 usa, come proxy non garantito, il prezzo **OPEN della prima seduta successiva alla decisione** e aggiunge controlli contro look-ahead. Per non contaminare un forward già avviato, i file `strategies/`, il capitale e il benchmark della v0.7 restano immutati. La comparazione delle curve va normalizzata alla data comune di partenza.

## Varianti (parametri congelati prima del forward)

* `MA10_Open`: segnale da **ultimo mese completo** di IUSQ.DE: prezzo sopra MA10+1% => exposure 100%, sotto MA10−1% => 0%, altrimenti mantiene stato.
* `MA6_MA12_Open`: MA6 sopra MA12+1% => 100%; sotto MA12−1% => 0%; zona neutra mantiene stato.
* `Adaptive_Risk`: trend MA6/MA12 sopra; se risk-on esposizione **min(100%, max(25%, 16% / volatilità annualizzata realizzata a 20 sedute))**. Se la chiusura dell'ultimo mese completo è sotto la MA3, **dimezza** l'esposizione. Se risk-off, 0%. Aggiornamento di esposizione **mensile**, non quotidiano. Il 16% non è stato ottimizzato: è un'ipotesi esplorativa pre-registrata.

Il paniere di partenza è la Top10 congelata il 3 novembre 2026, con strumenti/ticker `AAPL, NVDA, MSFT, GOOG, AMZN, TSM, SPCX, 2222.SR, TSLA, META` (tutti **virtuali**). Il ticker SPCX è verificato come Space Exploration Technologies Corp., Nasdaq, dal 12/06/2026: dati di precedenti strumenti omonimi non devono entrare nella serie. TSM è una ADR statunitense: non coincide con l'azione ordinaria su Taiwan. Non è garantita l'accessibilità broker di Saudi Aramco.

## Esecuzione e contabilità

* Il segnale usa solo prezzi **prima** della seduta; gli ordini vengono registrati all'**OPEN del titolo** quando il mercato pertinente è aperto; se un titolo è chiuso, l'ordine resta in attesa. La liquidità non viene allocata a titoli privi di quotazione.
* Il benchmark è il conto virtuale separato `IUSQ.DE`, acquistato alla prima apertura consentita: nessun titolo parte prima del benchmark.
* Per eseguire, EURUSD ed EURSAR usano l'**ultimo fixing precedente** la seduta; per valorizzare a fine giornata viene usato l'ultimo fixing disponibile fino alla stessa data.
* Commissione 1 € per acquisto o vendita, slippage ±0,10% rispetto all'OPEN (non è un prezzo negoziato verificato), commissione saudita stimata 0,155%, tasse sulle plusvalenze realizzate 26%, minusvalenze compensate nei quattro anni successivi, dividendi 26% proxy, bollo teorico 0,20% sui titoli.
* Le quote frazionarie e il costo fiscale medio aggregato sono approssimazioni: non replicano integralmente il broker. Dividendi accreditati convenzionalmente all'ex-date, non necessariamente alla pay-date. Ritenute estere, fiscalità particolare ETF e regole di esecuzione reali non certificate.
* `reports/latest.md`, `reports/latest_status.json`, `reports/*_nav.csv`, `reports/*_orders.csv` sono aggiornati dal runner; `state/*.json` è persistente, un conto separato per modello. `market_data/last_snapshot.json` contiene l'ultima fonte e il suo SHA256.

## Rotazione annuale PIT

Per febbraio 2027 e successivi è richiesta la classifica di fine dicembre dell'anno precedente in `v08/rosters/YYYY.json`, con `snapshot_asof`, `published_at`, `source_url`, `symbols` (10 elementi univoci). Per simboli nuovi richiede anche `securities` con `issuer_key`, `currency`, `venue` e, dove disponibile, `isin`. Non riempire con una classifica retrospettiva pubblicata dopo il segnale: la validazione rifiuta qualsiasi `published_at` futura. Se mancante, vengono **congelate le rotazioni**, non inventati gli emittenti; le posizioni esistenti vengono valorizzate.

## Convalida

Prima del commit il workflow esegue **15 test v0.7 + 25 test v0.8** (synthetic fixture; non dati di mercato reali). Una prima esecuzione CI riuscita dimostra installazione, non rendimento o correttezza assoluta della strategia. La validazione delle quotazioni reali avviene solo durante il primo run dopo il 5 novembre 2026.

## Comandi

```
python -m unittest discover -s v08/tests -p 'test_*.py' -v
python v08/run_shadow.py --seed
python v08/run_shadow.py
```

**Attenzione:** `--seed` rigenera solo report, non cancella stati o ordini; non va usato per retrodatare ordini. Non configurare acquisti reali. Il file GitHub `v08/reports/latest.md` è il riferimento per il forward shadow.