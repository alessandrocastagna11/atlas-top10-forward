# Ranking point-in-time annuali

L'aggiornamento annuale non è autorizzato senza uno snapshot verificato.
Esempio di *schema*; NON creare questa classifica finché i dati non sono reali:

```json
{
  "snapshot_asof": "2026-12-31",
  "published_at": "2027-01-05",
  "source_url": "https://fonte-verificata.example/ranking-2026",
  "symbols": ["AAPL", "NVDA", "MSFT", "GOOG", "AMZN", "TSM", "SPCX", "2222.SR", "TSLA", "META"],
  "securities": {}
}
```

Un nuovo simbolo richiede un profilo dell'emittente completo. La fonte `.example` è un placeholder, non una fonte.