# Atlas V4 Confidence70 | Live PAPER (separato da Atlas Top 10)

**Stato:** infrastruttura e portafoglio virtuale pubblicati; feed EOD di benchmark confermato; **generatore automatico dei segnali V4 non ancora distribuito** su GitHub. Il progetto dichiara `PENDING_SIGNAL` fino a quando riceve una nuova decisione calcolata da un vero motore Confidence70 e dai suoi input aggiornati.

- **Branch:** `atlas-v4-confidence70-live-paper` (il ramo `main` e gli altri forward restano invariati).
- **Motore di riferimento:** `ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07`.
- **Modello storico riprodotto:** 2012–2025, CAGR 10.87785242%, MaxDD −13.50881516%, 720 ordini. Differisce leggermente dall'originale congelato (10.90209%).
- **Capitale:** 10.000 EUR **virtuali**, stato iniziale 100% cash. Nessun collegamento a broker, token o ordini reali.
- **Dati di mercato:** Yahoo Chart non ufficiale, caching nel singolo job e tracciamento SHA-256. Fuso del mercato per la sessione del titolo, cambio in EUR con ultima osservazione precedente all'apertura, stop su barre mancanti.
- **Esecuzione:** ordini *simulati* all'apertura della seduta successiva alla decisione, registrati soltanto quando l'osservazione open è disponibile; non equivalgono a un ordine reale inviato alle 9:30.
- **Gestione rischio:** dieci posizioni max, 15% per emittente, 30% per settore, 99% esposizione, 1% riserva liquidità, 35 giorni minimi di detenzione prima di vendere, commissioni 1 EUR/trade, slippage 10 bps per lato, imposta stimata 26% sulle plusvalenze. La contabilizzazione fiscale è un modello semplificato, non una dichiarazione fiscale.
- **Bilanciamento:** l'overlay live usa una banda di ribilanciamento del 5% del NAV, per evitare micro-ordini. È un'approssimazione operativa; il backtest congelato non viene riscritto.

## Stato corrente
- [Dashboard leggibile](reports/latest.md)
- [Stato JSON](reports/latest_status.json)
- [Portafoglio virtuale](state/portfolio.json)
- [Registro ordini CSV](reports/orders.csv)
- [Workflow GitHub Actions](../../.github/workflows/atlas-v4-confidence70-paper.yml)

## Architettura

1. `provider.py` recupera barre OHLC con timestamp/sessione locale e cambio in EUR. La chiusura verificata di ACWI funge da *heartbeat* del feed.
2. Un **generatore esterno** del vero modello Confidence70 calcola ranking, punteggi, regime e pesi da input PIT/FX aggiornati. Questo componente **non è attualmente autonomo nel repository**: il replay storico da 161 MB arriva al 10/12/2025; non va estrapolato al 2026.
3. `inbox/latest_signal.json` è la decisione del generatore: le quattro sorgenti che ha usato devono essere allegate come `inbox/sources/prices.csv`, `fx.csv`, `universe.csv`, `scores.csv`. I quattro hash dichiarati devono corrispondere esattamente ai file allegati.
4. `run.py` controlla identificativo motore, beta, prove storiche, sessioni, vintage, pesi, mapping `ticker → market_symbol`, quotazioni del provider e differenze di prezzo oltre 5%. **Nessuna decisione nuova senza tutti i controlli.**
5. `paper.py` contabilizza SELL/BUY atomici, tasse proxy e saldo virtuale; blocca duplicati, vendite prima dei 35 giorni e saldi incoerenti. Non può inviare denaro.

## Formato del segnale (SOLO PRODUTTORE DEL MODELLO)

Il file `inbox/latest_signal.json` deve rispettare il formato 3.0; la struttura completa degli attributi richiesti è nel codice `run.validate_signal`. Esempio **non reale, non utilizzare come segnale**:

```json
{
  "schema_version":"3.0",
  "engine_id":"ATLAS_UNIFIED_REGION_ROTATE_V4_CONFIDENCE_BETA070_2026-10-07",
  "mode":"SHADOW",
  "decision_session":"YYYY-MM-DD",
  "execution_session":"YYYY-MM-DD",
  "targets":[{"ticker":"AAPL","market_symbol":"AAPL","region":"USA","sector":"XLK","weight":0.10,"price_eur":123.45}],
  "authorization":{"real_money_authorized":false},
  "data_freshness":{"prices_as_of_session":"YYYY-MM-DD","scores_as_of_session":"YYYY-MM-DD","membership_as_of_session":"YYYY-MM-DD","fx_as_of_session":"YYYY-MM-DD"},
  "provenance":{
    "model_name":"Confidence70","confidence_beta":0.70,
    "replay_equity_sha256":"188e39bf2196296653c5985ddaed5d9eb548e3bc891972f549dcd4326fdfc43c",
    "replay_orders_sha256":"57859b33f601d93566ea14dee70a69ebf80bdfacd650282f991e78071c2b7168",
    "canonical_usa_bundle_sha256":"fb1321e534d21059fc4f151e8027071106cc1899ef8b830a249ab61d6d8be2d3",
    "parity_status":"REPLAY_DIFFERS_FROM_ORIGINAL",
    "lookahead_audit":"RESEARCH_ONLY",
    "produced_by_engine_run":true,
    "live_source_sha256":{"prices":"sha256_64_hex","fx":"sha256_64_hex","universe":"sha256_64_hex","scores":"sha256_64_hex"}
  }
}
```

Gli hash e il flag sul motore dichiarano provenienza e coerenza, **non sono una firma crittografica di una terza parte né certificano da soli l'assenza di lookahead**. L'engine deve produrre davvero il segnale e i quattro file sorgenti; una persona non dovrebbe compilare manualmente una decisione inventata.

## Esecuzione e automazione

```bash
python -m unittest discover -s strict_shadow/live_v4/tests -v
python strict_shadow/live_v4/run.py --check
python strict_shadow/live_v4/run.py
```

Il workflow esegue i test, acquisisce il feed, produce il report e salva *solo* `strict_shadow/live_v4/state|reports|market_data` sul ramo dedicato. Un workflow concluso con `success` **non** prova l'esecuzione di un ordine: controllare sempre `status` e `total_paper_orders`.

**Scheduling GitHub:** i workflow pianificati con `schedule` partono dal branch predefinito, non da questo branch. Qui sono attivi i trigger `push`; per il report quotidiano autonomo è necessaria l'approvazione di un piccolo dispatcher su `main` che faccia esclusivamente checkout/push sul ramo V4, oppure un repository GitHub dedicato. Fino a quel momento non dichiarare attivi aggiornamenti giornalieri automatici.

## Qualità storica
Il replay storico viene da un archivio ricostruito (284 serie globali, USA canonico, Global200 annuale) e non ha parità numerica esatta; la conversione FX storica originale era interpolata/mediata su base annuale. Queste limitazioni sono **documentate**, non occultate. L'uso di quote live con FX puntuali non sana retroattivamente il backtest né prova che i ranking 2026 siano corretti.

## Requisiti mancanti per autonomia completa
- Caricare il motore V4 generatore di segnali forward e gli input storici canonici in un archivio durevole leggibile da GitHub Actions.
- Collegare feed PIT per aggiornare realmente classifiche/ranking USA+RoW e tutti i segnali senza hindsight; non usare Top500 corrente retroattivamente.
- Verificare end-to-end un segnale fresco, i dati di apertura e almeno una simulazione reale con i controlli superati.
- Abilitare il trigger periodico senza toccare il forward ufficiale.
