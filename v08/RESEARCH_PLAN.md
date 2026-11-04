# Pre-registrazione Atlas Adaptive Risk v0.8 — 4 novembre 2026

Strategia *shadow* congelata PRIMA dell'ingresso forward del 5 novembre 2026.

**Ipotesi:** aggiungere target volatility 16% con 20 chiusure quotidiane IUSQ e conferma MA3 al trend MA6/MA12 può ridurre drawdown e perdita di rendimento corretta per rischio, senza garanzia di CAGR migliore.

Parametri: MA6 e MA12 mensili, banda ±1%; MA3 conferma; vol 20 sedute annualizzata sqrt(252), budget vol 16%, floor expo 25% solo se trend ON, altrimenti zero; expo ridotta del 50% quando MA3 non conferma. Prezzi eseguiti all'apertura della seduta successiva al segnale e ricontrollo esposizione solo al cambio del mese. Tassazione e costi identici tra i tre shadow.

**Metriche principali pre-registrate:** CAGR netto stimato annualizzato, MaxDD e durata recupero, Sharpe, Sortino, volatilità, frazione in cash, commissioni, imposte realizzate, numero ordini, esposizione media, concentrazione primi tre titoli, differenza in punti percentuali e win-rate mensile vs IUSQ. Test di stress con slippage ±0,3% e aumento delle commissioni (se e solo se codice simulativo implementato).

**Validazione:** i periodi 2016–2025 sono già stati esaminati nel progetto e non costituiscono holdout nuovo. Il forward successivo al 5 novembre 2026 resta la validazione prospettica; non cambiare parametri dopo aver visto i risultati, non usare ritorni di domani nel segnale oggi. Un eventuale backtest retrospettivo servirebbe solo da audit e richiede ranking universale PIT verificato.

**Caveat:** usa IUSQ per la stima di vol, non la vera volatilità Top10; si tratta di un proxy che può sottostimare il rischio concentrato. Il modello fiscale è un'approssimazione italiana, e il ranking annuale non si aggiorna automaticamente senza fonti contemporanee archiviate.