# trend-scanner-deux
**TREND SCANNER II** — TC (Trend Continuation) scanner for Hyperliquid

Derived from [bounce-scanner-deux](https://github.com/krishnanrvijay-afk/bounce-scanner-deux).
Fork relationship preserved via upstream remote:

```bash
git remote add upstream https://github.com/krishnanrvijay-afk/bounce-scanner-deux
git fetch upstream
git merge upstream/main --allow-unrelated-histories
```

## What changed vs bounce-scanner-deux
- **Scoring**: `score_bounce_long/short` → `score_trend_continuation_long/short` (7-point TC scoring, threshold 5/7)
- **Pairs**: BTC ETH SOL XRP DOGE SUI NEAR OP APT LINK (HL research pairs)
- **Dashboard**: TREND SCANNER II title
- **Supabase tables**: `trend_scanner_state`, `trend_trade_log` (isolated from bounce_ tables)
- **TP targets**: 2R TP1, 3R TP2 (wider than bounce 1R/1.5R)
- **rsi_5m_prev cache**: P4 (RSI directional) requires two consecutive scans to populate

## Supabase tables
Both scanners share the same Supabase project but use distinct table prefixes:

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
HL_PRIVATE_KEY=          # only needed for live trading
HL_WALLET_ADDRESS=       # only needed for live trading
```
