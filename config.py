import os

HL_API_URL = "https://api.hyperliquid.xyz/info"

# ── Supabase persistence ───────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

# ── HL research pairs for trend-continuation scanning ─────────────────────────
PAIRS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "SUI", "NEAR", "OP", "APT", "LINK"]

SCAN_INTERVAL_SECONDS  = 30
PRICE_INTERVAL_SECONDS = 8
PAPER_MODE             = True

# ── Live trading safety ────────────────────────────────────────────────────────
# When PAPER_MODE is False and LIVE_MANUAL_ENTRY_ONLY is True, the scanner will
# never automatically open a live exchange position. Alerts fire and the overlay
# updates normally but all live trade entry requires deliberate human action via
# the symbol overlay Open HL button. SL and TP exits continue to execute
# automatically once a trade is open. Only set LIVE_MANUAL_ENTRY_ONLY to False
# if you explicitly want fully automated live entry on every signal.
LIVE_MANUAL_ENTRY_ONLY = True

# ── TC scoring ────────────────────────────────────────────────────────────────
# Score threshold out of 7 points (P1–P7) for a TC alert to fire.
# P1 (trend gate) + P2 (ADX gate) are always 2 free points when hard gates pass.
# A threshold of 5 requires 3 additional scoring points (P3–P7).
TC_SCORE_THRESHOLD = 5

# ── SL / TP ───────────────────────────────────────────────────────────────────
ATR_SL_MULTIPLIER = 1.0

TP1_R = 2.0   # TC uses wider targets than bounce: 2R TP1
TP2_R = 3.0   #                                    3R TP2

# ── Leverage tiers ────────────────────────────────────────────────────────────
LEVERAGE_HIGH = 10
LEVERAGE_MID  = 7
LEVERAGE_LOW  = 5

# ── Risk controls ─────────────────────────────────────────────────────────────
COOLDOWN_SECONDS      = 1800
CONSECUTIVE_LOSS_STOP = 3
DAILY_LOSS_LIMIT      = -500.0

MARGIN_PER_TRADE = 2000.0
MARGIN_HARD_CAP  = 25000.0

# ── Session filter (disabled by default) ─────────────────────────────────────
SESSION_FILTER_ENABLED = False
PLACE_EXCHANGE_SL      = True
