import os
from datetime import datetime, timezone

# TREND SCANNER II

HL_API_URL = "https://api.hyperliquid.xyz/info"

# ── Supabase persistence ───────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

PAIRS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "SUI", "NEAR", "OP", "APT", "LINK"]

SCAN_INTERVAL_SECONDS  = 30
PRICE_INTERVAL_SECONDS = 8
PAPER_MODE             = True

# ── Live trading safety ────────────────────────────────────────────────────────
LIVE_MANUAL_ENTRY_ONLY = True

TC_SCORE_THRESHOLD = 5

ATR_SL_MULTIPLIER = 1.0

TP1_R = 2.0
TP2_R = 3.0

LEVERAGE_HIGH = 10
LEVERAGE_MID  = 7
LEVERAGE_LOW  = 5

COOLDOWN_SECONDS      = 5400   # 90 minutes
CONSECUTIVE_LOSS_STOP = 3
DAILY_LOSS_LIMIT      = -500.0

MARGIN_PER_TRADE = 2000.0
MARGIN_HARD_CAP  = 25000.0

SESSION_FILTER_ENABLED = False
PLACE_EXCHANGE_SL      = True

PAIR_ADX_OVERRIDES: dict = {
    "SUI":  40,
    "NEAR": 42,
    "APT":  45,
    "LINK": 38,
}

MIN_SL_PCT: dict = {
    "BTC":  0.008,
    "ETH":  0.006,
    "SOL":  0.008,
    "XRP":  0.007,
    "DOGE": 0.007,
    "SUI":  0.010,
    "NEAR": 0.010,
    "LINK": 0.008,
    "OP":   0.012,
    "APT":  0.012,
}
MIN_SL_PCT_DEFAULT = 0.010
