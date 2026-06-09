# trend-scanner-deux
**TREND SCANNER II** — TC (Trend Continuation) scanner for Hyperliquid

trend-scanner-deux and bounce-scanner-deux are independent codebases that share a common origin
but are maintained separately. No upstream remote. No upstream merges. No shared patches.
Each scanner receives its own targeted changes only.

## Differences from bounce-scanner-deux (for reference only)
| File | Change |
|---|---|
| `scanner.py` | Replaced `score_bounce_long/short` with `score_trend_continuation_long/short` (7-point TC, threshold 5/7); added `_rsi_5m_prev` cache for P4 directional RSI |
| `config.py` | PAIRS = BTC ETH SOL XRP DOGE SUI NEAR OP APT LINK; added `TC_SCORE_THRESHOLD = 5`; TP 2R/3R |
| `main.py` | Supabase tables → `trend_scanner_state`, `trend_trade_log`; CSV → `trend_trade_log_*.csv` |
| `templates/dashboard.html` | Title + logo → TREND SCANNER II; J-map label → TREND MAP |
| `Procfile` | Added for Railway: `uvicorn main:app --host 0.0.0.0 --port $PORT` |

## Supabase tables
Both scanners share one Supabase project, isolated by prefix:

| Scanner | State table | Trade log table |
|---|---|---|
| bounce-scanner-deux | `bounce_scanner_state` | `bounce_trade_log` |
| trend-scanner-deux | `trend_scanner_state` | `trend_trade_log` |

## Railway deployment
**Start command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Required environment variables:
```
SUPABASE_URL=
SUPABASE_KEY=
HL_PRIVATE_KEY=          # only needed for live trading (PAPER_MODE=False)
HL_WALLET_ADDRESS=       # only needed for live trading
```
