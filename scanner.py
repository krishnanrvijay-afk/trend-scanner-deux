import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from config import (
    PAIRS, TC_SCORE_THRESHOLD, ATR_SL_MULTIPLIER,
    TP1_R, TP2_R, LEVERAGE_HIGH, LEVERAGE_MID, LEVERAGE_LOW,
    COOLDOWN_SECONDS, PAPER_MODE, CONSECUTIVE_LOSS_STOP,
    MIN_SL_PCT, MIN_SL_PCT_DEFAULT, MARGIN_PER_TRADE,
    PAIR_ADX_OVERRIDES,
)

log = logging.getLogger("scanner")

# ── Module-level state ────────────────────────────────────────────────────────

_last_scores: dict[str, int]   = {}   # keyed "BTCSHORT" / "BTCLONG"
_cooldowns:   dict[str, float] = {}   # keyed "BTCSHORT" / "BTCLONG" → expiry ts
_scan_count:  int              = 0
_pending:     dict[str, dict]  = {}   # first-scan confirmed, awaiting 2nd
_rsi_5m_prev: dict[str, float] = {}   # previous-scan RSI5m for directional delta


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _compute_kdj(candles: list[dict], period: int = 9) -> tuple[float, float, float]:
    if len(candles) < period:
        return 50.0, 50.0, 50.0
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    K, D = 50.0, 50.0
    for i in range(len(closes)):
        if i < period - 1:
            continue
        h_n = max(highs[i - period + 1 : i + 1])
        l_n = min(lows[i  - period + 1 : i + 1])
        rsv = (closes[i] - l_n) / (h_n - l_n) * 100 if h_n != l_n else 50.0
        K   = 2 / 3 * K + 1 / 3 * rsv
        D   = 2 / 3 * D + 1 / 3 * K
    J = 3 * K - 2 * D
    return K, D, J


def _compute_rsi(candles: list[dict], period: int = 14) -> float:
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _compute_atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    atr = sum(trs[:period]) / min(period, len(trs))
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _compute_adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        up   = candles[i]["high"]  - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dms.append(max(0.0, up)   if up   > down else 0.0)
        minus_dms.append(max(0.0, down) if down > up   else 0.0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    atr_s  = sum(trs[:period])
    pdm_s  = sum(plus_dms[:period])
    mdm_s  = sum(minus_dms[:period])
    dxs    = []
    for i in range(period, len(trs)):
        atr_s  = atr_s  - atr_s  / period + trs[i]
        pdm_s  = pdm_s  - pdm_s  / period + plus_dms[i]
        mdm_s  = mdm_s  - mdm_s  / period + minus_dms[i]
        if atr_s == 0:
            continue
        pdi = pdm_s / atr_s * 100
        mdi = mdm_s / atr_s * 100
        dxs.append(abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi else 0.0)
    if not dxs:
        return 0.0
    adx = sum(dxs[:period]) / min(period, len(dxs))
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _compute_ma(candles: list[dict], period: int) -> Optional[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period



def _compute_ema(candles: list[dict], period: int) -> Optional[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _compute_vol_ma(candles: list[dict], period: int) -> Optional[float]:
    vols = [c.get("volume") or c.get("vol", 0) for c in candles]
    if len(vols) < period:
        return None
    return sum(vols[-period:]) / period

def _trend_from_ma(price: float, ma10: Optional[float], ma30: Optional[float], ma60: Optional[float]) -> str:
    if ma10 and ma30 and ma60:
        if price > ma10 > ma30 > ma60:
            return "Strong Bull"
        if price < ma10 < ma30 < ma60:
            return "Strong Bear"
    return "Neutral"


def _depth_pcts(book: dict) -> tuple[float, float]:
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_vol = sum(b["sz"] for b in bids)
    ask_vol = sum(a["sz"] for a in asks)
    total   = bid_vol + ask_vol
    if total == 0:
        return 50.0, 50.0
    return round(bid_vol / total * 100, 1), round(ask_vol / total * 100, 1)


# ── Scoring ───────────────────────────────────────────────────────────────────

def _leverage_tier(adx: float) -> tuple[str, int]:
    if adx >= 50:
        return "HIGH_PROB", LEVERAGE_HIGH
    if adx >= 30:
        return "STRONG", LEVERAGE_MID
    return "REGULAR", LEVERAGE_LOW


def score_trend_continuation_long(price, ind5m, ind1h, ticker, depth,
                                   j15=None, rsi5m_prev=None):
    """TC LONG scoring. Returns (score, details). Adapted for HL data format."""
    score = 0
    details = []
    change_pct  = ticker.get("change_pct", 0.0)
    candles5m   = ind5m["candles"]
    vol_ma5     = ind5m["vol_ma5"]
    vol_ma10    = ind5m["vol_ma10"]

    # C1: Price > 1h MA60 and 24h change > -3%
    ma60_1h = ind1h["ma60"]
    if ma60_1h and price > ma60_1h and change_pct > -3:
        score += 1
        details.append(f"[+] Price > 1h MA60 ({ma60_1h:.4f}), 24h chg {change_pct:.2f}%")
    else:
        details.append(f"[-] Price vs 1h MA60 / 24h chg")

    # C2: 5m MA structure bullish (stacked or converging) + price > EMA20
    ma5, ma10, ma30, ema20 = ind5m["ma5"], ind5m["ma10"], ind5m["ma30"], ind5m["ema20"]
    if ma5 and ma10 and ma30 and ema20:
        stacked    = ma5 > ma10 > ma30
        converging = (abs(ma5 - ma10) / price < 0.003 and abs(ma10 - ma30) / price < 0.003)
        if (stacked or converging) and price > ema20:
            score += 1
            details.append(f"[+] 5m MAs bullish stacked/converging, price > EMA20")
        else:
            details.append(f"[-] 5m MA structure not bullish")
    else:
        details.append(f"[-] 5m MAs insufficient data")

    # C3: Price within 0.5% of 5m MA cluster (pullback entry)
    if ma10 and ma30 and ema20:
        closest = min([ma10, ma30, ema20], key=lambda x: abs(x - price))
        if abs(price - closest) / price < 0.005:
            score += 1
            details.append(f"[+] Price within 0.5% of 5m MA cluster (pullback)")
        else:
            details.append(f"[-] Price not near 5m MA cluster")
    else:
        details.append(f"[-] 5m cluster data insufficient")

    # C4: 5m KDJ J < 15 (oversold on 5m)
    _, _, j5 = ind5m["kdj"]
    if j5 is not None and j5 < 15:
        score += 1
        details.append(f"[+] 5m KDJ J={j5:.1f} < 15 (oversold)")
    else:
        details.append(f"[-] 5m KDJ J={f'{j5:.1f}' if j5 is not None else 'N/A'} not < 15")

    # C5: 1h KDJ J < 50 (room to rally)
    _, _, j1h = ind1h["kdj"]
    if j1h is not None and j1h < 50:
        score += 1
        details.append(f"[+] 1h KDJ J={j1h:.1f} < 50 (not overbought)")
    else:
        details.append(f"[-] 1h KDJ J={f'{j1h:.1f}' if j1h is not None else 'N/A'} not < 50")

    # C6: Buy depth >= 60%
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    bid_vol   = sum(b.get("sz", 0) for b in bids)
    ask_vol   = sum(a.get("sz", 0) for a in asks)
    total_vol = bid_vol + ask_vol
    if total_vol > 0 and bid_vol / total_vol >= 0.6:
        score += 1
        details.append(f"[+] Buy depth {bid_vol/total_vol*100:.1f}% >= 60%")
    else:
        pct = bid_vol / total_vol * 100 if total_vol > 0 else 0
        details.append(f"[-] Buy depth {pct:.1f}% < 60%")

    # C7: Large bid wall within 0.2% of price
    if len(bids) >= 2:
        bid_sizes = [b.get("sz", 0) for b in bids]
        avg_bid   = sum(bid_sizes) / len(bid_sizes)
        max_bid   = max(bid_sizes)
        max_bid_p = bids[bid_sizes.index(max_bid)].get("px", 0)
        if avg_bid > 0 and max_bid >= 3 * avg_bid and price and abs(price - max_bid_p) / price < 0.002:
            score += 1
            details.append(f"[+] Large bid wall {max_bid:.1f} >= 3x avg, within 0.2%")
        else:
            details.append(f"[-] No significant bid wall near price")
    else:
        details.append(f"[-] Insufficient bid data")

    # C8: Spread penalty (> 0.1%)
    if bids and asks:
        bp, ap = bids[0].get("px", 0), asks[0].get("px", 0)
        if bp > 0:
            sp = (ap - bp) / bp * 100
            if sp > 0.1:
                score -= 1
                details.append(f"[-] Wide spread penalty > 0.1% ({sp:.3f}%)")
            else:
                details.append(f"[~] Spread OK ({sp:.3f}%)")

    # C9: Volume surge + mostly green candles
    if vol_ma5 and len(candles5m) >= 10:
        vols10  = [c.get("volume") or c.get("vol", 0) for c in candles5m[-10:]]
        last3   = candles5m[-3:]
        green3  = sum(1 for c in last3 if c["close"] > c["open"])
        if max(vols10) >= 1.5 * vol_ma5 and green3 >= 2:
            score += 1
            details.append(f"[+] Vol surge (max10={max(vols10):.0f} >= 1.5x MA5), last 3 mostly green")
        else:
            details.append(f"[-] No vol surge with green candles")
    else:
        details.append(f"[-] Insufficient 5m volume data")

    # C10: Volume trend increasing + all 3 green
    if len(candles5m) >= 3:
        t3    = candles5m[-3:]
        v3    = [c.get("volume") or c.get("vol", 0) for c in t3]
        if v3[0] < v3[1] < v3[2] and all(c["close"] > c["open"] for c in t3):
            score += 1
            details.append(f"[+] Volume trend increasing with green candles")
        else:
            details.append(f"[-] Volume trend not increasing with green candles")
    else:
        details.append(f"[-] Insufficient candles for volume trend check")

    # C11: Last candle vol >= MA10 and green
    if candles5m and vol_ma10:
        last = candles5m[-1]
        lv   = last.get("volume") or last.get("vol", 0)
        if lv >= vol_ma10 and last["close"] > last["open"]:
            score += 1
            details.append(f"[+] Last candle vol >= MA10 and green")
        else:
            details.append(f"[-] Last candle vol/color not bullish")
    else:
        details.append(f"[-] Insufficient last candle data")

    # C12: Large move penalty (> 5% in last 24 candles)
    recent24 = candles5m[-24:] if len(candles5m) >= 24 else candles5m
    if any(c["open"] > 0 and abs(c["close"] - c["open"]) / c["open"] > 0.05 for c in recent24):
        score -= 1
        details.append(f"[-] Recent large move > 5%")
    else:
        details.append(f"[~] No large move > 5% in last 24 candles")

    # C13: Funding rate neutral
    fp = ticker.get("funding_pct", 0.0)
    if -0.01 <= fp <= 0.01:
        score += 1
        details.append(f"[+] Funding rate {fp:.4f}% in neutral range")
    else:
        details.append(f"[-] Funding rate {fp:.4f}% outside neutral range")

    # C14: 15m KDJ J < 30 (momentum not overbought)
    if j15 is not None:
        if j15 < 30:
            score += 1
            details.append(f"[+] 15m KDJ J={j15:.1f} < 30")
        else:
            details.append(f"[-] 15m KDJ J={j15:.1f} not < 30")

    # C15: RSI 5m rising (directional momentum)
    if rsi5m_prev is not None:
        rsi5m_cur = _compute_rsi(candles5m)
        if rsi5m_cur > rsi5m_prev:
            score += 1
            details.append(f"[+] RSI 5m rising ({rsi5m_prev:.1f} → {rsi5m_cur:.1f})")
        else:
            details.append(f"[-] RSI 5m not rising ({rsi5m_prev:.1f} → {rsi5m_cur:.1f})")

    return score, details  # max ~15


def score_trend_continuation_short(price, ind5m, ind1h, ticker, depth,
                                    j15=None, rsi5m_prev=None):
    """TC SHORT scoring. Returns (score, details). Adapted for HL data format."""
    score = 0
    details = []
    change_pct = ticker.get("change_pct", 0.0)
    candles5m  = ind5m["candles"]
    vol_ma5    = ind5m["vol_ma5"]
    vol_ma10   = ind5m["vol_ma10"]

    # C1: Price < 1h MA60 and 24h change < 3%
    ma60_1h = ind1h["ma60"]
    if ma60_1h and price < ma60_1h and change_pct < 3:
        score += 1
        details.append(f"[+] Price < 1h MA60 ({ma60_1h:.4f}), 24h chg {change_pct:.2f}%")
    else:
        details.append(f"[-] Price vs 1h MA60 / 24h chg")

    # C2: 5m MA structure bearish + price < EMA20
    ma5, ma10, ma30, ema20 = ind5m["ma5"], ind5m["ma10"], ind5m["ma30"], ind5m["ema20"]
    if ma5 and ma10 and ma30 and ema20:
        if ma5 < ma10 < ma30 and price < ema20:
            score += 1
            details.append(f"[+] 5m MAs stacked bearish, price < EMA20")
        else:
            details.append(f"[-] 5m MA structure not bearish")
    else:
        details.append(f"[-] 5m MAs insufficient data")

    # C3: Near 20-candle/24h high with upper wicks
    high24h = ticker.get("high24h", price)
    if len(candles5m) >= 20:
        recent20 = candles5m[-20:]
        ref_high = max(max(c["high"] for c in recent20), high24h)
        last3    = candles5m[-3:]
        upper_wick = any(
            (c["high"] - max(c["open"], c["close"])) > 0.5 * abs(c["close"] - c["open"])
            for c in last3 if abs(c["close"] - c["open"]) > 0
        )
        if price and abs(price - ref_high) / price < 0.005 and upper_wick:
            score += 1
            details.append(f"[+] Price near 20c/24h high with upper wicks")
        else:
            details.append(f"[-] Not near high with upper wicks")
    else:
        details.append(f"[-] Insufficient candle data for high check")

    # C4: 5m KDJ J > 85 (overbought)
    _, _, j5 = ind5m["kdj"]
    if j5 is not None and j5 > 85:
        score += 1
        details.append(f"[+] 5m KDJ J={j5:.1f} > 85 (overbought)")
    else:
        details.append(f"[-] 5m KDJ J={f'{j5:.1f}' if j5 is not None else 'N/A'} not > 85")

    # C5: 1h KDJ J > 50 (elevated)
    _, _, j1h = ind1h["kdj"]
    if j1h is not None and j1h > 50:
        score += 1
        details.append(f"[+] 1h KDJ J={j1h:.1f} > 50 (elevated)")
    else:
        details.append(f"[-] 1h KDJ J={f'{j1h:.1f}' if j1h is not None else 'N/A'} not > 50")

    # C6: Sell depth >= 60%
    bids      = depth.get("bids", [])
    asks      = depth.get("asks", [])
    bid_vol   = sum(b.get("sz", 0) for b in bids)
    ask_vol   = sum(a.get("sz", 0) for a in asks)
    total_vol = bid_vol + ask_vol
    if total_vol > 0 and ask_vol / total_vol >= 0.6:
        score += 1
        details.append(f"[+] Sell depth {ask_vol/total_vol*100:.1f}% >= 60%")
    else:
        pct = ask_vol / total_vol * 100 if total_vol > 0 else 0
        details.append(f"[-] Sell depth {pct:.1f}% < 60%")

    # C7: Large ask wall within 0.2% of price
    if len(asks) >= 2:
        ask_sizes = [a.get("sz", 0) for a in asks]
        avg_ask   = sum(ask_sizes) / len(ask_sizes)
        max_ask   = max(ask_sizes)
        max_ask_p = asks[ask_sizes.index(max_ask)].get("px", 0)
        if avg_ask > 0 and max_ask >= 3 * avg_ask and price and abs(price - max_ask_p) / price < 0.002:
            score += 1
            details.append(f"[+] Large ask wall {max_ask:.1f} >= 3x avg, within 0.2%")
        else:
            details.append(f"[-] No significant ask wall near price")
    else:
        details.append(f"[-] Insufficient ask data")

    # C8: Spread penalty (> 0.1%)
    if bids and asks:
        bp, ap = bids[0].get("px", 0), asks[0].get("px", 0)
        if bp > 0:
            sp = (ap - bp) / bp * 100
            if sp > 0.1:
                score -= 1
                details.append(f"[-] Wide spread penalty > 0.1% ({sp:.3f}%)")
            else:
                details.append(f"[~] Spread OK ({sp:.3f}%)")

    # C9: Weak green vol or large red rejection candle
    if vol_ma5 and len(candles5m) >= 5:
        last   = candles5m[-1]
        lv     = last.get("volume") or last.get("vol", 0)
        recent5 = candles5m[-5:]
        weak_greens = [c for c in recent5
                       if c["close"] > c["open"] and (c.get("volume") or c.get("vol", 0)) < vol_ma5]
        large_red   = last["close"] < last["open"] and lv >= 1.5 * vol_ma5
        if len(weak_greens) >= 2 or large_red:
            score += 1
            details.append(f"[+] Weak green vol or large red rejection candle")
        else:
            details.append(f"[-] No weak green / large red signal")
    else:
        details.append(f"[-] Insufficient volume data")

    # C10: Last candle vol >= MA10 and red
    if candles5m and vol_ma10:
        last = candles5m[-1]
        lv   = last.get("volume") or last.get("vol", 0)
        if lv >= vol_ma10 and last["close"] < last["open"]:
            score += 1
            details.append(f"[+] Last candle vol >= MA10 and red")
        else:
            details.append(f"[-] Last candle vol/color not bearish")
    else:
        details.append(f"[-] Insufficient last candle data")

    # C11: Large move penalty
    recent24 = candles5m[-24:] if len(candles5m) >= 24 else candles5m
    if any(c["open"] > 0 and abs(c["close"] - c["open"]) / c["open"] > 0.05 for c in recent24):
        score -= 1
        details.append(f"[-] Recent large move > 5%")
    else:
        details.append(f"[~] No large move > 5% in last 24 candles")

    # C12: Funding rate short-friendly
    fp = ticker.get("funding_pct", 0.0)
    if -0.005 <= fp <= 0.02:
        score += 1
        details.append(f"[+] Funding rate {fp:.4f}% in short-friendly range")
    else:
        details.append(f"[-] Funding rate {fp:.4f}% outside short-friendly range")

    # C13: 15m KDJ J > 70 (momentum not oversold)
    if j15 is not None:
        if j15 > 70:
            score += 1
            details.append(f"[+] 15m KDJ J={j15:.1f} > 70")
        else:
            details.append(f"[-] 15m KDJ J={j15:.1f} not > 70")

    # C14: RSI 5m falling (bearish momentum)
    if rsi5m_prev is not None:
        rsi5m_cur = _compute_rsi(candles5m)
        if rsi5m_cur < rsi5m_prev:
            score += 1
            details.append(f"[+] RSI 5m falling ({rsi5m_prev:.1f} → {rsi5m_cur:.1f})")
        else:
            details.append(f"[-] RSI 5m not falling ({rsi5m_prev:.1f} → {rsi5m_cur:.1f})")

    return score, details  # max ~14


# ── Cooldown helpers ──────────────────────────────────────────────────────────

def set_close_cooldown(symbol: str, direction: str):
    _cooldowns[f"{symbol}{direction}"] = time.time() + COOLDOWN_SECONDS


def get_cooldown_remaining(symbol: str, direction: str) -> int:
    exp = _cooldowns.get(f"{symbol}{direction}", 0)
    return max(0, int(exp - time.time()))


def clear_cooldown(symbol: str, direction: str):
    _cooldowns.pop(f"{symbol}{direction}", None)


def get_pending() -> dict:
    return dict(_pending)


def get_scan_count() -> int:
    return _scan_count


def clear_all_scanner_state():
    global _scan_count
    _last_scores.clear()
    _cooldowns.clear()
    _pending.clear()
    _rsi_5m_prev.clear()
    _scan_count = 0


def get_session_name() -> str:
    """Return current trading session name based on UTC hour."""
    h = datetime.now(timezone.utc).hour
    if h >= 17 or h < 8:  return "ASIA"
    if 8  <= h < 12:       return "EU"
    if 12 <= h < 17:       return "US"
    return "OFF"


def get_session_sl_buffer() -> float:
    """Additional SL buffer (fraction of price) added by session."""
    s = get_session_name()
    if s == "ASIA": return 0.003
    if s == "EU":   return 0.001
    return 0.0


def compute_market_health(pair_states: list[dict], recent_trades: list[dict]) -> dict:
    """Aggregate market-wide health; returns RUN/CAUTION/HALT per direction."""
    total = len(pair_states)
    if total == 0:
        return {
            "short_status": "CAUTION", "long_status": "CAUTION",
            "bear_count": 0, "bull_count": 0, "total": 0,
            "bear_ratio": 0.0, "bull_ratio": 0.0,
            "avg_adx": 0.0, "avg_j5": 50.0, "sl_rate": 0.0,
        }
    bear_count = sum(1 for s in pair_states if s.get("trend") in ("Bearish", "Strong Bear"))
    bull_count = sum(1 for s in pair_states if s.get("trend") in ("Bullish", "Strong Bull"))
    bear_ratio = bear_count / total
    bull_ratio = bull_count / total
    adx_vals   = [s["adx1h"] for s in pair_states if s.get("adx1h") is not None]
    j5_vals    = [s["j5m"]   for s in pair_states if s.get("j5m")  is not None]
    avg_adx    = sum(adx_vals) / len(adx_vals) if adx_vals else 0.0
    avg_j5     = sum(j5_vals)  / len(j5_vals)  if j5_vals  else 50.0
    recent6    = [t for t in recent_trades
                  if (t.get("close_reason") or t.get("exit_reason"))][-6:]
    sl_rate    = (
        sum(1 for t in recent6
            if (t.get("close_reason") or t.get("exit_reason") or "").upper().startswith("SL"))
        / len(recent6)
    ) if recent6 else 0.0
    if bear_ratio >= 0.6 and avg_adx >= 35 and avg_j5 <= 70 and sl_rate < 0.4:
        short_status = "RUN"
    elif bear_ratio < 0.3 or sl_rate >= 0.6 or (avg_j5 >= 85 and bear_ratio < 0.5):
        short_status = "HALT"
    else:
        short_status = "CAUTION"
    if bull_ratio >= 0.6 and avg_adx >= 35 and avg_j5 >= 30 and sl_rate < 0.4:
        long_status = "RUN"
    elif bull_ratio < 0.3 or sl_rate >= 0.6 or (avg_j5 <= 15 and bull_ratio < 0.5):
        long_status = "HALT"
    else:
        long_status = "CAUTION"
    return {
        "short_status": short_status,
        "long_status":  long_status,
        "bear_count":   bear_count,
        "bull_count":   bull_count,
        "total":        total,
        "bear_ratio":   round(bear_ratio, 3),
        "bull_ratio":   round(bull_ratio, 3),
        "avg_adx":      round(avg_adx, 1),
        "avg_j5":       round(avg_j5, 1),
        "sl_rate":      round(sl_rate, 3),
    }


# ── Main scan ─────────────────────────────────────────────────────────────────

async def run_full_scan(hl_client, market_health: Optional[dict] = None) -> list[dict]:
    global _scan_count

    _scan_count += 1
    new_alerts: list[dict] = []

    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing — 12 pairs × 0.5s = 6s minimum spread
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)

            if not price or price == 0:
                log.warning(f"[SCAN] {symbol} — no price, skipping")
                continue

            # ── Indicators ────────────────────────────────────────────────────
            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            atr5m      = _compute_atr(candles_5m)
            atr15m     = _compute_atr(candles_15m)
            atr1h      = _compute_atr(candles_1h)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            vol_15m    = candles_15m[-1]["volume"] if candles_15m else 0
            vol_ma15m  = (sum(c["volume"] for c in candles_15m[-10:]) / min(10, len(candles_15m))
                          if candles_15m else 0)

            # ── SL distance (ATR base, floored by MIN_SL_PCT + session buffer) ──────
            _sl_atr      = atr15m * ATR_SL_MULTIPLIER
            _min_sl_pct  = MIN_SL_PCT.get(symbol, MIN_SL_PCT_DEFAULT)
            _sess_buf    = get_session_sl_buffer()
            _min_sl_dist = price * (_min_sl_pct + _sess_buf)
            sl_dist      = max(_sl_atr, _min_sl_dist)

            # ── Build indicator dicts for TC scoring ──────────────────────────
            ma5_5m  = _compute_ma(candles_5m, 5)
            ma10_5m = _compute_ma(candles_5m, 10)
            ma30_5m = _compute_ma(candles_5m, 30)
            ema20_5m = _compute_ema(candles_5m, 20)
            k5m, d5m, j5m_kdj = _compute_kdj(candles_5m)
            k1h_v, d1h_v, j1h_v = _compute_kdj(candles_1h)
            vol_ma5_v  = _compute_vol_ma(candles_5m, 5)
            vol_ma10_v = _compute_vol_ma(candles_5m, 10)
            ind5m = {
                "ma5": ma5_5m, "ma10": ma10_5m, "ma30": ma30_5m, "ema20": ema20_5m,
                "kdj": (k5m, d5m, j5m_kdj),
                "candles": candles_5m,
                "vol_ma5": vol_ma5_v, "vol_ma10": vol_ma10_v,
            }
            ind1h = {
                "ma60": ma60, "kdj": (k1h_v, d1h_v, j1h_v), "candles": candles_1h,
            }
            ticker = {"change_pct": 0.0, "high24h": price, "funding_pct": 0.0}

            rsi5m = _compute_rsi(candles_5m)

            # ── Score both directions ─────────────────────────────────────────
            for direction in ("SHORT", "LONG"):
                key = f"{symbol}{direction}"

                if get_cooldown_remaining(symbol, direction) > 0:
                    continue

                # ── TC ADX hard gate ──────────────────────────────────────────
                _adx_min_tc = 25 if direction == "SHORT" else 30
                if adx1h < _adx_min_tc:
                    log.info(f"[SKIP] {symbol} {direction} — ADX {adx1h:.1f} < {_adx_min_tc} (TC gate)")
                    _pending.pop(key, None)
                    _last_scores[key] = 0
                    continue

                # ── Pair ADX override gate ────────────────────────────────────
                if symbol in PAIR_ADX_OVERRIDES:
                    _adx_min = PAIR_ADX_OVERRIDES[symbol]
                    if adx1h < _adx_min:
                        log.info(f"[SKIP] {symbol} {direction} — "
                                 f"ADX {adx1h:.1f} below pair minimum {_adx_min}")
                        _pending.pop(key, None)
                        _last_scores.pop(key, None)
                        continue

                _rsi_prev = _rsi_5m_prev.get(symbol)
                if direction == "SHORT":
                    score, details = score_trend_continuation_short(
                        price, ind5m, ind1h, ticker, book, j15=j15m, rsi5m_prev=_rsi_prev)
                else:
                    score, details = score_trend_continuation_long(
                        price, ind5m, ind1h, ticker, book, j15=j15m, rsi5m_prev=_rsi_prev)

                tier, lev = _leverage_tier(adx1h)
                log_gates = (f"adx={adx1h:.1f} j15m={j15m:.1f} j1h={j1h:.1f} "
                             f"rsi15m={rsi15m:.1f} bid={bid_pct:.1f}% ask={ask_pct:.1f}%")

                if score >= TC_SCORE_THRESHOLD:
                    log.info(f"[SCORE] {symbol} {direction} score={score}/{len(details)} {log_gates}")
                else:
                    if _last_scores.get(key, 0) >= TC_SCORE_THRESHOLD:
                        log.info(f"[SCORE] {symbol} {direction} score={score} below threshold {log_gates}")
                    _last_scores[key] = 0
                    _pending.pop(key, None)
                    continue

                # Consecutive scan confirmation (2 scans)
                if _last_scores.get(key, 0) < TC_SCORE_THRESHOLD:
                    _last_scores[key] = score
                    _pending[key] = {
                        "symbol": symbol, "direction": direction,
                        "score": score, "tier": tier,
                    }
                    log.info(f"[SCORE] {symbol} {direction} first-scan confirmed (score={score}) — awaiting 2nd")
                    continue

                # Second consecutive scan — emit alert
                _last_scores[key] = score

                # Compute SL / TP prices
                is_hc = False  # TC does not use HIGH_CONVICTION tier
                partial_price = None
                if direction == "SHORT":
                    sl_price  = round(price + sl_dist, 6)
                    tp1_price = round(price - sl_dist * TP1_R, 6)
                    tp2_price = round(price - sl_dist * TP2_R, 6)
                else:
                    sl_price  = round(price - sl_dist, 6)
                    tp1_price = round(price + sl_dist * TP1_R, 6)
                    tp2_price = round(price + sl_dist * TP2_R, 6)

                dollar_risk = round(
                    2000.0 * lev * (sl_dist / price) if price else 0, 2
                )

                alert = {
                    "symbol":       symbol,
                    "direction":    direction,
                    "score":        score,
                    "tier":         tier,
                    "leverage":     lev,
                    "entry_price":  price,
                    "sl_price":     sl_price,
                    "sl_dist":      round(sl_dist, 6),
                    "tp1_price":    tp1_price,
                    "tp2_price":    tp2_price,
                    "dollar_risk":  dollar_risk,
                    "j15m":         round(j15m, 2),
                    "j1h":          round(j1h, 2),
                    "j5m":          round(j5m, 2),
                    "rsi15m":       round(rsi15m, 2),
                    "rsi1h":        round(rsi1h, 2),
                    "atr15m":       round(atr15m, 6),
                    "adx1h":        round(adx1h, 2),
                    "bid_pct":      bid_pct,
                    "ask_pct":      ask_pct,
                    "trend":        trend,
                    "ma10":         round(ma10, 6) if ma10 else None,
                    "ma30":         round(ma30, 6) if ma30 else None,
                    "ma60":         round(ma60, 6) if ma60 else None,
                    "fired_at":     int(time.time()),
                    "is_in_trade":   False,
                    "is_score10":    is_hc,
                    "margin":        MARGIN_PER_TRADE * 2 if is_hc else MARGIN_PER_TRADE,
                    "partial_price": partial_price,
                    "session":       get_session_name(),
                }
                # ── Market HALT gate ──────────────────────────────────────
                _mh = market_health or {}
                if direction == "SHORT" and _mh.get("short_status") == "HALT":
                    log.info(f"[BLOCKED] {symbol} SHORT — MARKET HALT")
                    continue
                if direction == "LONG" and _mh.get("long_status") == "HALT":
                    log.info(f"[BLOCKED] {symbol} LONG — MARKET HALT")
                    continue

                new_alerts.append(alert)
                _pending.pop(key, None)
                log.info(f"[ALERT] {symbol} {direction} tier={tier} lev={lev}x entry={price} "
                         f"sl={sl_price} tp1={tp1_price} adx={adx1h:.1f}")

            # Update RSI directional cache after scoring this symbol
            _rsi_5m_prev[symbol] = rsi5m

        except Exception as e:
            log.error(f"[SCAN] {symbol} error: {e}", exc_info=True)

    log.info(f"[SCAN] #{_scan_count} complete — {len(new_alerts)} new alerts")
    return new_alerts


async def scan_pair_state(hl_client) -> list[dict]:
    """Return lightweight per-pair indicator state for the dashboard grid."""
    states = []
    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing between pairs
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)
            if not price:
                states.append({"symbol": symbol, "price": 0})
                continue

            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            atr15m     = _compute_atr(candles_15m)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            _ps_ind5m = {
                "ma5": _compute_ma(candles_5m, 5), "ma10": _compute_ma(candles_5m, 10),
                "ma30": _compute_ma(candles_5m, 30), "ema20": _compute_ema(candles_5m, 20),
                "kdj": _compute_kdj(candles_5m), "candles": candles_5m,
                "vol_ma5": _compute_vol_ma(candles_5m, 5),
                "vol_ma10": _compute_vol_ma(candles_5m, 10),
            }
            _ps_ind1h = {"ma60": ma60, "kdj": _compute_kdj(candles_1h), "candles": candles_1h}
            _ps_ticker = {"change_pct": 0.0, "high24h": price, "funding_pct": 0.0}
            _ps_rsi_prev = _rsi_5m_prev.get(symbol)
            short_sc, _ = score_trend_continuation_short(price, _ps_ind5m, _ps_ind1h, _ps_ticker, book, j15=j15m, rsi5m_prev=_ps_rsi_prev)
            long_sc, _  = score_trend_continuation_long( price, _ps_ind5m, _ps_ind1h, _ps_ticker, book, j15=j15m, rsi5m_prev=_ps_rsi_prev)
            short_score = short_sc
            long_score  = long_sc
            short_tier, _ = _leverage_tier(adx1h), None
            long_tier,  _ = _leverage_tier(adx1h), None
            short_tier  = short_tier[0]
            long_tier   = long_tier[0]

            states.append({
                "symbol":      symbol,
                "price":       price,
                "j5m":         round(j5m, 2),
                "j15m":        round(j15m, 2),
                "j1h":         round(j1h, 2),
                "rsi15m":      round(rsi15m, 2),
                "rsi1h":       round(rsi1h, 2),
                "atr15m":      round(atr15m, 6),
                "adx1h":       round(adx1h, 2),
                "bid_pct":     bid_pct,
                "ask_pct":     ask_pct,
                "trend":       trend,
                "ma10":        round(ma10, 6) if ma10 else None,
                "ma30":        round(ma30, 6) if ma30 else None,
                "ma60":        round(ma60, 6) if ma60 else None,
                "short_score": short_score,
                "short_tier":  short_tier,
                "long_score":  long_score,
                "long_tier":   long_tier,
                "cooldown_short": get_cooldown_remaining(symbol, "SHORT"),
                "cooldown_long":  get_cooldown_remaining(symbol, "LONG"),
            })
        except Exception as e:
            log.error(f"[STATE] {symbol} error: {e}")
            states.append({"symbol": symbol, "price": 0})
    return states


async def _fetch_pair_data(hl_client, symbol: str):
    candles_5m, candles_15m, candles_1h, book, price = await asyncio.gather(
        hl_client.get_candles(symbol, "5m",  100),
        hl_client.get_candles(symbol, "15m", 100),
        hl_client.get_candles(symbol, "1h",  100),
        hl_client.get_orderbook(symbol, 20),
        hl_client.get_price(symbol),
    )
    return candles_5m, candles_15m, candles_1h, book, price


def log_startup_config():
    log.info(
        f"[CONFIG] PAIRS={len(PAIRS)} "
        f"TC_SCORE_THRESHOLD={TC_SCORE_THRESHOLD} ATR_SL={ATR_SL_MULTIPLIER}x "
        f"ADX_GATE_LONG=30 ADX_GATE_SHORT=25 "
        f"TP1={TP1_R}R TP2={TP2_R}R "
        f"COOLDOWN={COOLDOWN_SECONDS//60}min "
        f"CIRCUIT_BREAKER={CONSECUTIVE_LOSS_STOP} PAPER={PAPER_MODE}"
    )
