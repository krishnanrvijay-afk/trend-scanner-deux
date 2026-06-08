import asyncio
import time
import logging
from typing import Optional

from config import (
    PAIRS, TC_SCORE_THRESHOLD, ATR_SL_MULTIPLIER,
    TP1_R, TP2_R, LEVERAGE_HIGH, LEVERAGE_MID, LEVERAGE_LOW,
    COOLDOWN_SECONDS, PAPER_MODE, CONSECUTIVE_LOSS_STOP,
)

log = logging.getLogger("scanner")

# ── Module-level state ────────────────────────────────────────────────────────

_last_scores:  dict[str, int]   = {}   # keyed "BTCSHORT" / "BTCLONG"
_cooldowns:    dict[str, float] = {}   # keyed "BTCSHORT" / "BTCLONG" → expiry ts
_scan_count:   int              = 0
_pending:      dict[str, dict]  = {}   # first-scan confirmed, awaiting 2nd
_rsi_5m_prev:  dict[str, float] = {}   # symbol → prior-scan 5m RSI (P4 cache)


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
    bid_vol = sum(float(b["sz"]) for b in bids)
    ask_vol = sum(float(a["sz"]) for a in asks)
    total   = bid_vol + ask_vol
    if total == 0:
        return 50.0, 50.0
    return round(bid_vol / total * 100, 1), round(ask_vol / total * 100, 1)


def sma(values: list, n: int) -> Optional[float]:
    """Simple moving average of the last n values in a list."""
    if not values or len(values) < n:
        return None
    return sum(values[-n:]) / n


# ── Scoring ───────────────────────────────────────────────────────────────────

def _leverage_tier(adx: float) -> tuple[str, int]:
    if adx >= 50:
        return "HIGH_PROB", LEVERAGE_HIGH
    if adx >= 25:
        return "STRONG", LEVERAGE_MID
    return "REGULAR", LEVERAGE_LOW


def score_trend_continuation_short(candles5m, candles1h, depth, adx_1h, trend, rsi_5m_prev=None):
    """7-point TC score for SHORT.
    Hard gates: trend == 'Strong Bear' AND adx_1h >= 25 AND ask_pct >= 55%
    (65% when trend opposes direction).
    Adapted for HL orderbook format (bids/asks as list of dicts with 'sz' field).
    rsi_5m_prev: prior scan cached 5m RSI (P4)."""
    if trend != "Strong Bear" or adx_1h is None or adx_1h < 25:
        return 0, []
    # ── Depth hard gate ──────────────────────────────────────────────────────
    _bids = depth.get("bids", [])
    _asks = depth.get("asks", [])
    _bv   = sum(float(b["sz"]) for b in _bids) if _bids else 0
    _av   = sum(float(a["sz"]) for a in _asks) if _asks else 0
    _tv   = _bv + _av
    _ap   = _av / _tv * 100 if _tv > 0 else 0
    _depth_thr = 65 if trend in ("Bullish", "Strong Bull") else 55
    if _ap < _depth_thr:
        return 0, [f"[-] TC DEPTH gate: S%={_ap:.1f}% < {_depth_thr}% required"]

    closes_1h = [c["close"] for c in candles1h]
    ma10_1h   = sma(closes_1h, 10)
    ma30_1h   = sma(closes_1h, 30)
    ma60_1h   = sma(closes_1h, 60)

    vols_5m  = [c["volume"] for c in candles5m]   # HL uses "volume"
    vol_ma10 = sma(vols_5m, 10)
    last_vol = candles5m[-1]["volume"] if candles5m else 0

    bids      = depth.get("bids", [])
    asks      = depth.get("asks", [])
    bid_vol   = sum(float(b["sz"]) for b in bids) if bids else 0
    ask_vol   = sum(float(a["sz"]) for a in asks) if asks else 0
    total_vol = bid_vol + ask_vol
    ask_pct   = ask_vol / total_vol * 100 if total_vol > 0 else 0

    rsi_5m = _compute_rsi(candles5m)
    rsi_1h = _compute_rsi(candles1h)

    score   = 0
    details = []

    # P1 — trend gate (already passed)
    score += 1
    details.append("[+] TC P1: trend == Strong Bear")
    # P2 — ADX gate (already passed)
    score += 1
    details.append(f"[+] TC P2: adx_1h {adx_1h:.1f} >= 25")
    # P3 — 1h MA bearish stack
    if ma10_1h and ma30_1h and ma60_1h and ma10_1h < ma30_1h < ma60_1h:
        score += 1
        details.append(f"[+] TC P3: 1h MA stack ({ma10_1h:.2f}<{ma30_1h:.2f}<{ma60_1h:.2f})")
    else:
        details.append("[-] TC P3: 1h MA stack not bearish")
    # P4 — 5m RSI declining from above 60 (bounce peaking)
    if rsi_5m > 60 and rsi_5m_prev is not None and rsi_5m < rsi_5m_prev:
        score += 1
        details.append(f"[+] TC P4: rsi_5m {rsi_5m:.1f} > 60 and declining (prev {rsi_5m_prev:.1f})")
    elif rsi_5m > 60 and rsi_5m_prev is None:
        details.append(f"[-] TC P4: rsi_5m {rsi_5m:.1f} > 60 but no prior cached value — not scored")
    else:
        _prev_s = f"{rsi_5m_prev:.1f}" if rsi_5m_prev is not None else "N/A"
        details.append(f"[-] TC P4: rsi_5m {rsi_5m:.1f} not > 60 or not declining (prev {_prev_s})")
    # P5 — 1h RSI < 50 (trend not exhausted)
    if rsi_1h < 50:
        score += 1
        details.append(f"[+] TC P5: rsi_1h {rsi_1h:.1f} < 50")
    else:
        details.append(f"[-] TC P5: rsi_1h {rsi_1h:.1f} not < 50")
    # P6 — last 5m candle vol > 1.5x MA10
    if vol_ma10 and last_vol > vol_ma10 * 1.5:
        score += 1
        details.append(f"[+] TC P6: vol {last_vol:.0f} > 1.5x MA10 {vol_ma10:.0f}")
    else:
        vol_ma10_s = f"{vol_ma10:.0f}" if vol_ma10 else "N/A"
        details.append(f"[-] TC P6: vol {last_vol:.0f} not > 1.5x MA10 {vol_ma10_s}")
    # P7 — depth scoring point
    if ask_pct >= 55:
        score += 1
        details.append(f"[+] TC P7: ask_pct {ask_pct:.1f}% >= 55%")
    else:
        details.append(f"[-] TC P7: ask_pct {ask_pct:.1f}% < 55%")

    return score, details


def score_trend_continuation_long(candles5m, candles1h, depth, adx_1h, trend, rsi_5m_prev=None):
    """7-point TC score for LONG.
    Hard gates: trend == 'Strong Bull' AND adx_1h >= 25.
    Adapted for HL orderbook format (bids/asks as list of dicts with 'sz' field).
    rsi_5m_prev: prior scan cached 5m RSI (P4)."""
    if trend != "Strong Bull" or adx_1h is None or adx_1h < 25:
        return 0, []
    closes_1h = [c["close"] for c in candles1h]
    ma10_1h   = sma(closes_1h, 10)
    ma30_1h   = sma(closes_1h, 30)
    ma60_1h   = sma(closes_1h, 60)

    vols_5m  = [c["volume"] for c in candles5m]   # HL uses "volume"
    vol_ma10 = sma(vols_5m, 10)
    last_vol = candles5m[-1]["volume"] if candles5m else 0

    bids      = depth.get("bids", [])
    asks      = depth.get("asks", [])
    bid_vol   = sum(float(b["sz"]) for b in bids) if bids else 0
    ask_vol   = sum(float(a["sz"]) for a in asks) if asks else 0
    total_vol = bid_vol + ask_vol
    bid_pct   = bid_vol / total_vol * 100 if total_vol > 0 else 0

    rsi_5m = _compute_rsi(candles5m)
    rsi_1h = _compute_rsi(candles1h)

    score   = 0
    details = []

    # P1 — trend gate (already passed)
    score += 1
    details.append("[+] TC P1: trend == Strong Bull")
    # P2 — ADX gate (already passed)
    score += 1
    details.append(f"[+] TC P2: adx_1h {adx_1h:.1f} >= 25")
    # P3 — 1h MA bullish stack
    if ma10_1h and ma30_1h and ma60_1h and ma10_1h > ma30_1h > ma60_1h:
        score += 1
        details.append(f"[+] TC P3: 1h MA stack ({ma10_1h:.2f}>{ma30_1h:.2f}>{ma60_1h:.2f})")
    else:
        details.append("[-] TC P3: 1h MA stack not bullish")
    # P4 — 5m RSI < 40 AND rising (pullback recovering into trend)
    if rsi_5m < 40 and rsi_5m_prev is not None and rsi_5m > rsi_5m_prev:
        score += 1
        details.append(f"[+] TC P4: rsi_5m {rsi_5m:.1f} < 40 and rising (prev {rsi_5m_prev:.1f})")
    elif rsi_5m < 40 and rsi_5m_prev is None:
        details.append(f"[-] TC P4: rsi_5m {rsi_5m:.1f} < 40 but no prev cached — skipped")
    else:
        _prev_s = f"{rsi_5m_prev:.1f}" if rsi_5m_prev is not None else "N/A"
        details.append(f"[-] TC P4: rsi_5m {rsi_5m:.1f} not < 40 or not rising (prev {_prev_s})")
    # P5 — 1h RSI > 50 (trend has energy)
    if rsi_1h > 50:
        score += 1
        details.append(f"[+] TC P5: rsi_1h {rsi_1h:.1f} > 50")
    else:
        details.append(f"[-] TC P5: rsi_1h {rsi_1h:.1f} not > 50")
    # P6 — vol spike
    if vol_ma10 and last_vol > vol_ma10 * 1.5:
        score += 1
        details.append(f"[+] TC P6: vol {last_vol:.0f} > 1.5x MA10 {vol_ma10:.0f}")
    else:
        vol_ma10_s = f"{vol_ma10:.0f}" if vol_ma10 else "N/A"
        details.append(f"[-] TC P6: vol {last_vol:.0f} not > 1.5x MA10 {vol_ma10_s}")
    # P7 — depth informational (not a gate or score point for LONG)
    details.append(f"[i] TC P7: bid_pct {bid_pct:.1f}% (informational only)")

    return score, details


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


# ── Main scan ─────────────────────────────────────────────────────────────────

async def run_full_scan(hl_client) -> list[dict]:
    global _scan_count

    _scan_count += 1
    new_alerts: list[dict] = []

    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)

            if not price or price == 0:
                log.warning(f"[SCAN] {symbol} — no price, skipping")
                continue

            # ── Indicators ────────────────────────────────────────────────────
            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi_5m     = _compute_rsi(candles_5m)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            atr15m     = _compute_atr(candles_15m)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            sl_dist = atr15m * ATR_SL_MULTIPLIER

            # ── Score both directions ─────────────────────────────────────────
            for direction in ("SHORT", "LONG"):
                key = f"{symbol}{direction}"

                if get_cooldown_remaining(symbol, direction) > 0:
                    continue

                prev_rsi = _rsi_5m_prev.get(symbol)

                if direction == "SHORT":
                    score, details = score_trend_continuation_short(
                        candles_5m, candles_1h, book, adx1h, trend, rsi_5m_prev=prev_rsi
                    )
                    log_gates = (
                        f"trend={trend} adx={adx1h:.1f} "
                        f"ask={ask_pct:.1f}% score={score}/{7}"
                    )
                else:
                    score, details = score_trend_continuation_long(
                        candles_5m, candles_1h, book, adx1h, trend, rsi_5m_prev=prev_rsi
                    )
                    log_gates = (
                        f"trend={trend} adx={adx1h:.1f} "
                        f"bid={bid_pct:.1f}% score={score}/{7}"
                    )

                if score >= TC_SCORE_THRESHOLD:
                    log.info(f"[SCORE] {symbol} {direction} score={score}/7 PASS  {log_gates}")
                else:
                    if _last_scores.get(key, 0) >= TC_SCORE_THRESHOLD:
                        log.info(f"[SCORE] {symbol} {direction} score={score}/7 FAIL  {log_gates}")
                    _last_scores[key] = score
                    _pending.pop(key, None)
                    continue

                # Consecutive scan confirmation
                if _last_scores.get(key, 0) < TC_SCORE_THRESHOLD:
                    _last_scores[key] = score
                    _pending[key] = {
                        "symbol": symbol, "direction": direction,
                        "score": score,
                    }
                    log.info(f"[SCORE] {symbol} {direction} first-scan confirmed (score={score}/7) — awaiting 2nd")
                    continue

                # Second consecutive scan
                _last_scores[key] = score
                tier, lev = _leverage_tier(adx1h)

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
                    "alert_type":   f"TC_{direction}",
                    "score":        score,
                    "score_max":    7,
                    "tier":         tier,
                    "leverage":     lev,
                    "entry_price":  price,
                    "sl_price":     sl_price,
                    "sl_dist":      round(sl_dist, 6),
                    "tp1_price":    tp1_price,
                    "tp2_price":    tp2_price,
                    "dollar_risk":  dollar_risk,
                    "tc_details":   details,
                    "j5m":          round(j5m, 2),
                    "j15m":         round(j15m, 2),
                    "j1h":          round(j1h, 2),
                    "rsi_5m":       round(rsi_5m, 2),
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
                    "is_in_trade":  False,
                }
                new_alerts.append(alert)
                _pending.pop(key, None)
                log.info(
                    f"[ALERT] {symbol} TC_{direction} score={score}/7 tier={tier} lev={lev}x "
                    f"entry={price} sl={sl_price} tp1={tp1_price} adx={adx1h:.1f}"
                )

            # ── Update rsi_5m_prev cache after both directions scored ─────────
            _rsi_5m_prev[symbol] = rsi_5m

        except Exception as e:
            log.error(f"[SCAN] {symbol} error: {e}", exc_info=True)

    log.info(f"[SCAN] #{_scan_count} complete — {len(new_alerts)} new alerts")
    return new_alerts


async def scan_pair_state(hl_client) -> list[dict]:
    """Return lightweight per-pair indicator state for the dashboard grid."""
    states = []
    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)
            if not price:
                states.append({"symbol": symbol, "price": 0})
                continue

            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi_5m     = _compute_rsi(candles_5m)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            atr15m     = _compute_atr(candles_15m)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            prev_rsi = _rsi_5m_prev.get(symbol)

            tc_short_score, _ = score_trend_continuation_short(
                candles_5m, candles_1h, book, adx1h, trend, rsi_5m_prev=prev_rsi
            )
            tc_long_score, _ = score_trend_continuation_long(
                candles_5m, candles_1h, book, adx1h, trend, rsi_5m_prev=prev_rsi
            )

            states.append({
                "symbol":         symbol,
                "price":          price,
                "j5m":            round(j5m, 2),
                "j15m":           round(j15m, 2),
                "j1h":            round(j1h, 2),
                "rsi_5m":         round(rsi_5m, 2),
                "rsi15m":         round(rsi15m, 2),
                "rsi1h":          round(rsi1h, 2),
                "atr15m":         round(atr15m, 6),
                "adx1h":          round(adx1h, 2),
                "bid_pct":        bid_pct,
                "ask_pct":        ask_pct,
                "trend":          trend,
                "ma10":           round(ma10, 6) if ma10 else None,
                "ma30":           round(ma30, 6) if ma30 else None,
                "ma60":           round(ma60, 6) if ma60 else None,
                "short_score":    tc_short_score,
                "long_score":     tc_long_score,
                "short_tier":     _leverage_tier(adx1h)[0],
                "long_tier":      _leverage_tier(adx1h)[0],
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
        f"[CONFIG] PAIRS={len(PAIRS)} ({', '.join(PAIRS)}) "
        f"TC_SCORE_THRESHOLD={TC_SCORE_THRESHOLD}/7 "
        f"ATR_SL={ATR_SL_MULTIPLIER}x "
        f"COOLDOWN={COOLDOWN_SECONDS//60}min "
        f"CIRCUIT_BREAKER={CONSECUTIVE_LOSS_STOP} PAPER={PAPER_MODE}"
    )
