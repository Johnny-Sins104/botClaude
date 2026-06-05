# Scalping Bot - Paper-Only Binance Dashboard

Bot FastAPI per paper trading su dati pubblici Binance, con dashboard locale.

Questa build e' paper-only: non puo' inviare ordini reali. Se `PAPER_MODE=false`, il processo si blocca all'avvio con:

```text
LIVE TRADING DISABLED: this build is paper-only
```

## Installazione

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
```

Compila `.env` solo in locale. Non condividere mai `.env`, screenshot del file, API key o secret. Lo ZIP pulito deve contenere solo `.env.example` con placeholder.

## Avvio

```powershell
python bot.py
```

Oppure:

```powershell
python -m uvicorn bot:app --host 127.0.0.1 --port 8000
```

Dashboard:

```text
http://127.0.0.1:8000
```

Il server usa `127.0.0.1` come host di default. CORS e' limitato a:

```text
http://127.0.0.1:8000
http://localhost:8000
```

## Configurazione Minima

`.env.example` contiene solo placeholder:

```dotenv
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
PAPER_MODE=true
HOST=127.0.0.1
PORT=8000
SYMBOL=BTCUSDT
CAPITAL=1000
STRATEGY=ema
RISK_PCT=1.0
```

Non impostare `PAPER_MODE=false`: questa build lo rifiuta sempre.

## Strategie

| Codice | Strategia | Sintesi |
|---|---|---|
| `ema` | EMA + RSI + VWAP | Confluenza trend, momentum e prezzo medio volume |
| `bb` | Bollinger Bands | Mean reversion su chiusura fuori banda |
| `macd` | MACD | Cross MACD/signal calcolato con `signal_prev` e `signal_cur` |
| `ichi` | Ichimoku | Close vs Kumo, Tenkan/Kijun e struttura cloud |

Tutte le strategie calcolano nuovi segnali solo su candele chiuse. Il prezzo live non genera ingressi intrabar.

## Prezzo Live e Dashboard

- Live BTC price: aggiornato tick-by-tick dal WebSocket Binance.
- PnL, stop loss, take profit e trailing stop: aggiornati usando il prezzo live.
- Strategy signal: aggiornato solo quando una candela 1m e' chiusa.
- La dashboard legge `/api/state` ogni 500 ms e aggiorna solo campi dinamici.

## Sicurezza e Robustezza

- Reconnect WebSocket automatico con backoff configurabile.
- Daily loss limit paper: quando il P&L giornaliero totale raggiunge il limite, il bot blocca nuove entry fino al giorno successivo.
- Kill switch manuale via dashboard o API: blocca nuove entry immediatamente; puo' anche chiudere la posizione paper aperta al prezzo live.
- SL, TP e trailing stop restano gestiti sul prezzo live anche quando nuove entry sono bloccate.

Parametri configurabili via `/api/config`:

| Parametro | Default | Descrizione |
|---|---:|---|
| `daily_loss_limit_pct` | `3.0` | Perdita giornaliera massima in percentuale della baseline del giorno |
| `daily_loss_limit_enabled` | `true` | Abilita/disabilita il blocco giornaliero |
| `allow_short` | `false` | Abilita gli ingressi SHORT simulati in paper |
| `min_entry_score` | `75` | Score minimo per aprire una nuova posizione |
| `reconnect_initial_delay_sec` | `2` | Primo ritardo prima del reconnect |
| `reconnect_max_delay_sec` | `60` | Backoff massimo tra reconnect |

## Paper Trading Realistico

Il paper trading applica fee e slippage configurabili nel backend:

- `fee_pct` default `0.001`
- `slippage_pct` default `0.0002`
- `max_notional_pct` default `0.95`
- `min_entry_score` default `75`
- `allow_short` default `false`

Il capitale viene aggiornato con `pnl_net`, cioe' PnL lordo meno fee di ingresso e uscita. Ogni trade salva prezzi signal/fill, fee, slippage, sizing e PnL lordo/netto.

Gli SHORT sono simulati solo in paper. Binance Spot non apre short reali. Per evitare aperture short involontarie, gli ingressi short sono disabilitati di default; si abilitano solo con `allow_short=true` via `/api/config`.

## API Locale

| Endpoint | Metodo | Descrizione |
|---|---|---|
| `/` | GET | Dashboard |
| `/api/state` | GET | Stato completo paper-only |
| `/api/config` | POST | Aggiorna configurazione validata |
| `/api/kill-switch` | POST | Attiva/disattiva kill switch paper |
| `/api/trades` | GET | Storico trade JSON |
| `/api/trades/download` | GET | Download `logs/trades.json` se esiste |

Esempio:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/config -ContentType 'application/json' -Body '{"strategy":"macd","risk_pct":1.0}'
```

`/api/config` rifiuta valori fuori range e parametri sconosciuti con HTTP 400. In caso di errore non modifica parzialmente la configurazione.

Kill switch:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":true,"reason":"manual","close_position":false}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":false}'
```

Per chiudere anche la posizione paper aperta:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/kill-switch -ContentType 'application/json' -Body '{"enabled":true,"reason":"manual","close_position":true}'
```

## Persistenza Paper

Il bot salva lo stato paper in:

```text
logs/state.json
```

Al riavvio ripristina eventuale posizione aperta. Se il file e' corrotto, il bot avvisa e riparte flat.

`logs/` non deve essere incluso nello ZIP finale.

## Test Minimi

```powershell
python -m py_compile bot.py
$env:PAPER_MODE='true'; python -c "import bot; print('paper import ok')"
$env:PAPER_MODE='false'; python -c "import bot"
```

Test API config senza avviare il WebSocket:

```powershell
$env:PAPER_MODE='true'; python -c "from fastapi.testclient import TestClient; import bot; c=TestClient(bot.app); print(c.post('/api/config', json={'risk_pct':999}).status_code); print(c.post('/api/config', json={'strategy':'bb','risk_pct':1.0}).status_code)"
$env:PAPER_MODE='true'; python -c "from fastapi.testclient import TestClient; import bot; c=TestClient(bot.app); print(c.post('/api/kill-switch', json={'enabled':True}).status_code); print(c.post('/api/kill-switch', json={'enabled':False}).status_code)"
```

## ZIP Pulito

Comando PowerShell consigliato:

```powershell
Compress-Archive -LiteralPath bot.py,index.html,requirements.txt,README.md,.env.example -DestinationPath ..\botClaude-paper-only.zip -Force
```

Questo include solo i file sorgente necessari ed esclude `.env`, `logs/`, `__pycache__/`, `*.pyc`, `.git/` e cache temporanee.
