# Atlas Top 10 A — Forward Daily v0.7 (3 novembre 2026)

Questo progetto contiene un motore **eseguibile** per due simulazioni separate, ognuna con capitale virtuale iniziale di **10.000 €**:

- `MA10_Official`: regola A congelata, prezzo finale mensile IUSQ rispetto alla sua SMA10, banda ±1%.
- `MA6_MA12_Challenger`: regola alternativa da testare in **parallelo**, SMA6 vs SMA12 con banda ±1%, senza modificare la strategia ufficiale. Il suo primo segnale è `PENDING` finché non sono disponibili 12 chiusure mensili verificabili.

Il benchmark per **entrambe** è `IUSQ.DE` (ETF MSCI ACWI in EUR ad accumulazione), conto virtuale parallelo da 10.000 €. Tutte le date di ingresso sono legate a reali prezzi di chiusura, e **il primo ordine può avvenire non prima del 4 novembre 2026**. I prezzi inesistenti non vengono stimati; se una fonte manca, il portafoglio resta in attesa. L'algoritmo non invia ordini ad alcun broker.

## Come farlo funzionare

### Esecuzione locale

Con Python 3.11+ e Internet:

```bash
python -m pip install -r requirements.txt
python daily_runner.py --check
python daily_runner.py
```

Il programma scarica barre giornaliere di tutti i titoli, IUSQ, EUR/USD ed EUR/SAR. Aggiorna i file `strategies/*/state.json`, `daily_nav.csv`, `orders.csv`, `quotes_audit.csv`, `dashboard.html`; crea `reports/latest.md`, `reports/latest_status.json` e `market_data/latest_snapshot.json` (contenente data e impronta SHA-256 della fonte). Ripetendo il comando sulla stessa data **non si duplicano operazioni**. Il sistema tenta di recuperare i giorni saltati.

**Il codice non può aggiornarsi da solo a computer spento.** La cartella `.github/workflows/atlas-daily.yml` contiene la pianificazione GitHub Actions. Per abilitarla, creare un repository GitHub **privato**, copiare il contenuto di questa cartella nella root del repository, abilitare Actions e i permessi di scrittura dei workflow. La pagina dashboard **non richiede GitHub Pages**: si può aprire come file locale dalla cartella. GitHub Actions salva quotidianamente nel repository i ledger aggiornati; se il servizio quote è indisponibile, salva soltanto il rapporto `PENDING_DATA`, senza inventare esecuzioni. La schedulazione di GitHub può subire ritardi.

Il report automatico ChatGPT già attivo alle 23:30 ora italiana è **separato dall'esecuzione di questo codice**. Per fare in modo che riporti esattamente il patrimonio aggiornato da GitHub Actions bisogna rendere accessibili al processo del report i registri del repository o fornire periodicamente i CSV. L'automazione non esegue in autonomia il file ZIP allegato.

### Rapporto giornaliero

Il report include: patrimonio e rendimento cumulato in EUR, valore IUSQ, extra-rendimento, regime RISK_ON/CASH, acquisti, vendite, singoli ordini, bollo maturato, imposte su plusvalenze realizzate, eventuali ritardi nei feed. Sono esportati **due conti distinti**, mai fusi. Le commissioni sono **1 € per BUY + 1 € per SELL**; lo slippage è 0,10% per lato; tassazione italiana stimata **26%** sulle plusvalenze realizzate e compensazione minusvalenze fino a 4 anni successivi; bollo 0,20% annuo sul capitale investito; dividendi 26% **proxy**. Il cash rende 0% per prudenza.

**Limiti fiscali:** le ritenute estere sui dividendi e alcuni dettagli del regime amministrato/strumenti esteri non sono riprodotti esattamente. Il confronto con l'ETF (ad accumulazione) è prima della liquidazione fiscale finale, che differisce per regime fiscale rispetto alle azioni dirette.

### Universo iniziale congelato

Dati originari del progetto del 3/11/2026: `AAPL, NVDA, MSFT, GOOG, AMZN, TSM, SPCX, 2222.SR, TSLA, META`, con quote frazionarie **solo virtuali**. SpaceX `SPCX` è effettivamente quotata dal 12/06/2026 secondo le fonti societarie. Saudi Aramco `2222.SR` è quotata in SAR: la serie EUR/SAR è necessaria per il NAV. Il portafoglio è equivalente a un paniere di singole azioni, non a un ETF. Le date del ranking sono congelo *ex ante* e un aggiornamento da febbraio 2027 richiede il file `strategies/<strategy>/annual_rosters/2026.json` con dieci ticker verificati dalla classifica del **dicembre 2026**. Il file `ESEMPIO.json` non viene mai letto come ranking reale.

### Dati verificati e test

Sono consentite solo osservazioni di mercato con data non futura e nessuna esecuzione su prezzi mancanti. I test sintetici utilizzano un percorso temporaneo separato e **non modificano** i registri ufficiali. Una data interamente mancante conserva l'ultimo NAV verificabile con stato `PENDING_DATA`. Il programma confronta l'impronta SHA-256 dello snapshot per audit, senza promettere una certificazione istituzionale dei prezzi Yahoo.

Per i file annuali, usare:

```json
{"symbols":["AAPL","NVDA","MSFT","GOOG","AMZN","TSM","SPCX","2222.SR","TSLA","META"]}
```

**Attenzione:** l'esempio serve solo a mostrare il formato. Non riusare un ranking 2026 corrente come fotografia valida di dicembre 2026.

### Corporate action e distribuzioni

Il sistema legge anche gli eventi di split del provider, adegua le quote senza generare compravendite imponibili e registra gli eventi in `corporate_actions.csv`. I dividendi vengono accreditati nel cash al netto della stima d'imposta e registrati in `dividend_history.csv`; il pagamento è simulato alla data ex-dividend per mancanza di una data di incasso affidabile. Questi limiti sono distinti dalle plusvalenze realizzate sulle vendite.
## v0.8 — Ricerca SHADOW con Adaptive Risk (4 novembre 2026)

La **strategia ufficiale v0.7 non è stata modificata**. Dal **5 novembre 2026** il workflow può aggiornare tre nuovi conti virtuali **indipendenti** da 10.000 € ciascuno, con prezzi di apertura della seduta successiva e benchmark IUSQ:

- MA10_Open (singola MA10 ±1%)
- MA6_MA12_Open (cross mensile MA6/MA12 ±1%)
- Adaptive_Risk (dual MA6/MA12 + MA3 + volatilità IUSQ a 20 sedute, budget vol 16%)

[Ultimo report v0.8](v08/reports/latest.md) · [Stato machine-readable v0.8](v08/reports/latest_status.json) · [Motore e avvertenze](v08/README.md) · [Ipotesi pre-registrate](v08/RESEARCH_PLAN.md)

Le quote di mercato sono fornite da Yahoo Chart con ritardo e possono essere incomplete. Non c'è broker né denaro reale. I risultati dei test sintetici non sono risultati finanziari. Una classifica annuale non verificata blocca le nuove rotazioni. La simulazione al prezzo OPEN più slippage non garantisce un'esecuzione reale. Il workflow è configurato per girare dopo la chiusura dei mercati USA, ma la disponibilità effettiva dei dati va verificata a ogni run.

## Backtest v0.8 (esplorativo, non ufficiale)

[Rapporto e confronto storico delle sei strategie](backtests/v08_close_proxy/FINAL_REPORT.md) · [Rendimenti netti in CSV](backtests/v08_close_proxy/COMPARISON.csv) · [Analisi di sensitività](backtests/v08_close_proxy/SENSITIVITY.csv).

**Avviso:** lo storico archiviato usa prezzi di chiusura (non OPEN) e proxy globale SPY/EFA/EEM (non ETF IUSQ), ranking ricostruiti non certificati point-in-time. Le tre strategie v0.8 shadow e i due conti ufficiali v0.7 restano distinti e NON sono stati retroattivamente modificati dai calcoli di backtest.
