#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — Offline backtest, Monte Carlo analytics, TP optimiser, and CLI test tools.

CHANGE LOG:
  MOVED    : _pct, _dd_curve (lines 6172–6183)
  MOVED    : compute_trade_metrics (lines 6185–6224)
  MOVED    : bootstrap_monte_carlo (lines 6226–6260)
  MOVED    : fetch_public_candles (lines 6262–6273)
  MOVED    : load_1m_series (lines 6275–6304)
  MOVED    : build_5m_from_1m (lines 6306–6317)
  MOVED    : run_backtest_tp_variant (lines 6344–~6500)
  MOVED    : run_backtest_compare, optimize_tp_vol_params (lines ~6500–6636)
  MOVED    : tp_float_quick_test (lines 6087–6101)
  PRESERVED: All backtest logic, fee assumptions, TP fill simulation, Monte Carlo sampling exactly.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests

from client import API_BASE
from config import CFG
from indicators import atr, rsi, ema
from logger import LOG
from models import Regime, TradeResult
from regime import classify_regime_C, classify_regime_prob, combine_regimes
from tp import _tp_eff_from_mode, compute_tp_base_from_vol, compute_vwap, effective_tp, required_move_pct
from utils import D0, D1


# ----------------------------
# Metric helpers
# ----------------------------
def _pct(x: Decimal) -> str:
    return f"{(x*100):.2f}%"


def _dd_curve(equity: List[float]) -> float:
    peak = -1e18
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def compute_trade_metrics(trades: List[TradeResult]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0, "win_rate": 0.0, "avg_ret": 0.0, "std_ret": 0.0,
            "expectancy": 0.0, "profit_factor": 0.0, "sharpe_trade": 0.0, "max_dd": 0.0,
        }
    rets = [float(t.ret_pct) for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    win_rate = len(wins) / len(rets)
    avg_ret = float(sum(Decimal(str(r)) for r in rets) / Decimal(len(rets)))
    std_ret = float(statistics.pstdev(rets)) if len(rets) > 1 else 0.0

    avg_win = float(sum(wins) / len(wins)) if wins else 0.0
    avg_loss = float(sum(losses) / len(losses)) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    gross_profit = sum(wins) if wins else 0.0
    gross_loss = -sum(losses) if losses else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    sharpe_trade = (avg_ret / std_ret) if std_ret > 0 else 0.0

    eq = [1.0]
    for r in rets:
        eq.append(eq[-1] * (1.0 + r))
    max_dd = _dd_curve(eq)

    return {
        "trades": len(rets),
        "win_rate": win_rate,
        "avg_ret": avg_ret,
        "std_ret": std_ret,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "sharpe_trade": sharpe_trade,
        "max_dd": max_dd,
        "final_mult": eq[-1],
    }


def bootstrap_monte_carlo(
    trade_returns: List[float],
    sims: int = 3000,
    horizon: int = 1000,
    seed: int = 7,
) -> Dict[str, Any]:
    if not trade_returns:
        return {"sims": 0}
    rng = random.Random(seed)
    finals = []
    max_dds = []
    for _ in range(sims):
        eq = 1.0; peak = 1.0; max_dd = 0.0
        for _i in range(horizon):
            r = rng.choice(trade_returns)
            eq *= (1.0 + r)
            peak = max(peak, eq)
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
        finals.append(eq)
        max_dds.append(max_dd)
    finals.sort(); max_dds.sort()

    def q(arr, p):
        if not arr:
            return 0.0
        k = int((len(arr) - 1) * p)
        return arr[k]

    return {
        "sims": sims, "horizon": horizon,
        "final_p10": q(finals, 0.10), "final_p50": q(finals, 0.50), "final_p90": q(finals, 0.90),
        "p_ruin_50dd": sum(1 for d in max_dds if d >= 0.50) / len(max_dds),
        "dd_p50": q(max_dds, 0.50), "dd_p90": q(max_dds, 0.90),
    }


# ----------------------------
# Public candle fetching (unsigned)
# ----------------------------
def fetch_public_candles(symbol: str, typ: str, start_at: int, end_at: int) -> List[List[str]]:
    """Fetch public candles from KuCoin (unsigned). Returns raw rows (strings)."""
    url = API_BASE + "/api/v1/market/candles"
    params = {"symbol": symbol, "type": typ, "startAt": start_at, "endAt": end_at}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    j = r.json()
    rows = j.get("data") or []
    rows.sort(key=lambda x: int(x[0]))
    return rows


def load_1m_series(
    symbol: str, days: int = 45, limit_calls: int = 300
) -> Tuple[List[int], List[Decimal], List[Decimal], List[Decimal], List[Decimal], List[Decimal]]:
    """Load ~days of 1m candles via public endpoint, paginating backwards."""
    now_s = int(time.time())
    start_s = now_s - int(days * 86400)
    end = now_s
    step = 1500 * 60
    all_rows: List[List[str]] = []
    calls = 0
    while end > start_s and calls < limit_calls:
        calls += 1
        chunk_start = max(start_s, end - step)
        rows = fetch_public_candles(symbol, "1min", chunk_start, end)
        if not rows:
            break
        if all_rows and rows and rows[-1][0] == all_rows[0][0]:
            rows = rows[:-1]
        all_rows = rows + all_rows
        end = chunk_start - 60
        time.sleep(0.12)

    ts = [int(r[0]) for r in all_rows]
    o = [Decimal(r[1]) for r in all_rows]
    c = [Decimal(r[2]) for r in all_rows]
    h = [Decimal(r[3]) for r in all_rows]
    lo = [Decimal(r[4]) for r in all_rows]
    v = [Decimal(r[5]) for r in all_rows]
    return ts, o, h, lo, c, v


def build_5m_from_1m(
    ts: List[int], o: List[Decimal], h: List[Decimal],
    l: List[Decimal], c: List[Decimal], v: List[Decimal],
) -> Tuple[List[int], List[Decimal], List[Decimal], List[Decimal], List[Decimal], List[Decimal]]:
    """Aggregate 1m candles into 5m candles aligned by index groups of 5."""
    ts5 = []; o5 = []; h5 = []; l5 = []; c5 = []; v5 = []
    n = len(ts)
    for i in range(0, n - (n % 5), 5):
        ts5.append(ts[i + 4])
        o5.append(o[i])
        h5.append(max(h[i:i + 5]))
        l5.append(min(l[i:i + 5]))
        c5.append(c[i + 4])
        v5.append(sum(v[i:i + 5]))
    return ts5, o5, h5, l5, c5, v5


# ----------------------------
# TP variant backtest
# ----------------------------
def run_backtest_tp_variant(
    ts: List[int], o: List[Decimal], h: List[Decimal], l: List[Decimal],
    c: List[Decimal], v: List[Decimal],
    tp_mode: str,
    target_trades: int = 1000,
    spread_pct_assumed: Decimal = Decimal("0.0004"),
) -> List[TradeResult]:
    """Deterministic backtest focused on TP behavior."""
    ts5, o5, h5, l5, c5, v5 = build_5m_from_1m(ts, o, h, l, c, v)
    w1 = max(CFG.candles_1m_limit, 300)
    w5 = max(CFG.candles_5m_limit, 300)

    trades: List[TradeResult] = []
    in_pos = False
    entry_px = D0; entry_i = -1; peak = D0; hold_until = 0
    rem = Decimal("0")
    tp1_hit = False
    tp1_px = tp2_px = D0
    tp1_eff = tp2_eff = D0
    vol_norm_entry: Optional[Decimal] = None
    cooldown_until = 0

    for i in range(0, len(c) - 2):
        if len(trades) >= target_trades:
            break

        start1 = max(0, i - w1 + 1)
        highs = h[start1:i + 1]
        lows = l[start1:i + 1]
        closes = c[start1:i + 1]
        vols_1m = v[start1:i + 1]

        i5 = i // 5
        start5 = max(0, i5 - w5 + 1)
        highs5 = h5[start5:i5 + 1]
        lows5 = l5[start5:i5 + 1]
        closes5 = c5[start5:i5 + 1]

        if len(closes) < 80 or len(closes5) < 80:
            continue
        if ts[i] < cooldown_until:
            continue

        if CFG.regime_model == "prob_z":
            try:
                r1 = classify_regime_prob(highs, lows, closes)
                r5 = classify_regime_prob(highs5, lows5, closes5)
                reg = combine_regimes(r1, r5)
            except Exception:
                continue
        else:
            try:
                reg = classify_regime_C(highs, lows, closes)
            except Exception:
                continue

        tp_req = required_move_pct(spread_pct_assumed)
        atrp_v: Optional[Decimal] = None
        if len(closes) >= CFG.atr_len + 1:
            atr_v = atr(highs, lows, closes, CFG.atr_len)
            if atr_v is not None and closes[-1] > 0:
                atrp_v = atr_v / closes[-1]

        tp1_eff_c, tp2_eff_c, tp_base_dyn, vol_t, vol_min, vol_max, vol_norm_c = _tp_eff_from_mode(
            tp_mode, tp_req, reg, atrp_v, highs5, lows5, closes5
        )

        if not in_pos:
            # Simple entry signal: EMA cross + regime not indeterminate
            if reg.indeterminate:
                continue
            if reg.p_trend < Decimal("0.55") and reg.p_chop > Decimal("0.65"):
                continue
            if len(closes) < CFG.ema_fast + 1:
                continue
            ema_f = ema(closes, CFG.ema_fast)
            ema_s = ema(closes, CFG.ema_slow) if len(closes) >= CFG.ema_slow else None
            if ema_f is None or ema_s is None:
                continue
            if closes[-1] <= ema_f:
                continue
            entry_px = o[i + 1]
            if entry_px <= 0:
                continue
            entry_i = i + 1
            in_pos = True; peak = entry_px; hold_until = 0
            rem = Decimal("1")
            tp1_hit = False
            tp1_eff = tp1_eff_c; tp2_eff = tp2_eff_c
            vol_norm_entry = vol_norm_c
            tp1_px = entry_px * (D1 + tp1_eff)
            tp2_px = entry_px * (D1 + tp2_eff)
            continue

        bars_held = i - entry_i
        px_high = h[i]
        px_low = l[i]

        # Peak tracking
        peak = max(peak, px_high)

        # TP1 fill
        if not tp1_hit and px_high >= tp1_px:
            tp1_hit = True
            rem = D1 - CFG.tp_split_1

        # TP2 fill
        if tp1_hit and px_high >= tp2_px:
            exit_px = tp2_px
            ret = float(((exit_px - entry_px) / entry_px * CFG.tp_split_1)
                        + ((exit_px - entry_px) / entry_px * rem))
            ret_dec = Decimal(str(ret)) - (CFG.fee_buy + CFG.fee_sell + spread_pct_assumed)
            trades.append(TradeResult(
                entry_ts=ts[entry_i], exit_ts=ts[i], entry_px=entry_px, exit_px=exit_px,
                ret_pct=ret_dec, reason="TP2", bars=bars_held,
                tp1_eff=tp1_eff, tp2_eff=tp2_eff, vol_norm=vol_norm_entry,
            ))
            in_pos = False
            cooldown_until = ts[i] + CFG.cooldown_base_sec
            continue

        # Emergency exit
        if peak > 0:
            dd = (peak - px_low) / peak
            dd_th = CFG.emergency_dd_floor
            if atrp_v is not None:
                dd_th = max(dd_th, atrp_v * CFG.emergency_dd_atrp_mult)
            if dd >= dd_th and bars_held >= 5:
                exit_px = o[i + 1]
                partial_ret = float(tp1_eff * CFG.tp_split_1) if tp1_hit else 0.0
                runner_ret = float(((exit_px - entry_px) / entry_px) * float(rem if tp1_hit else D1))
                ret_dec = Decimal(str(partial_ret + runner_ret)) - (CFG.fee_buy + CFG.fee_sell + spread_pct_assumed)
                trades.append(TradeResult(
                    entry_ts=ts[entry_i], exit_ts=ts[i], entry_px=entry_px, exit_px=exit_px,
                    ret_pct=ret_dec, reason="EMERGENCY", bars=bars_held,
                    tp1_eff=tp1_eff, tp2_eff=tp2_eff, vol_norm=vol_norm_entry,
                ))
                in_pos = False
                cooldown_until = ts[i] + CFG.cooldown_base_sec
                continue

        # Time exit
        if bars_held >= CFG.max_hold_minutes:
            exit_px = o[i + 1]
            partial_ret = float(tp1_eff * CFG.tp_split_1) if tp1_hit else 0.0
            runner_ret = float(((exit_px - entry_px) / entry_px) * float(rem if tp1_hit else D1))
            ret_dec = Decimal(str(partial_ret + runner_ret)) - (CFG.fee_buy + CFG.fee_sell + spread_pct_assumed)
            trades.append(TradeResult(
                entry_ts=ts[entry_i], exit_ts=ts[i], entry_px=entry_px, exit_px=exit_px,
                ret_pct=ret_dec, reason="TIME", bars=bars_held,
                tp1_eff=tp1_eff, tp2_eff=tp2_eff, vol_norm=vol_norm_entry,
            ))
            in_pos = False
            cooldown_until = ts[i] + CFG.cooldown_base_sec

    return trades


# ----------------------------
# Backtest compare + optimise
# ----------------------------
async def run_backtest_compare(
    symbol: str, days: int = 45, target_trades: int = 1000,
    mc_sims: int = 3000, mc_seed: int = 7, optimize: bool = False,
) -> None:
    await LOG.log("INFO", f"BACKTEST_START symbol={symbol} days={days} target={target_trades}")
    try:
        ts, o, h, l, c, v = load_1m_series(symbol, days)
    except Exception as e:
        await LOG.log("ERROR", f"BACKTEST_LOAD_FAIL {e}")
        return

    await LOG.log("INFO", f"BACKTEST_DATA rows={len(c)} bars")

    for mode in ("static", "vol", "regime"):
        try:
            trades = run_backtest_tp_variant(ts, o, h, l, c, v, tp_mode=mode, target_trades=target_trades)
            m = compute_trade_metrics(trades)
            mc = bootstrap_monte_carlo([float(t.ret_pct) for t in trades], sims=mc_sims, horizon=min(1000, len(trades) * 2), seed=mc_seed)
            await LOG.log(
                "INFO",
                f"BACKTEST_RESULT mode={mode} trades={m['trades']} win={m['win_rate']*100:.1f}% "
                f"exp={m['expectancy']*100:.2f}% Sharpe={m['sharpe_trade']:.2f} MaxDD={m['max_dd']*100:.1f}% "
                f"Final×={m.get('final_mult',1.0):.2f} MC_p50={mc.get('final_p50',0):.2f} "
                f"MC_dd_p90={mc.get('dd_p90',0)*100:.1f}%",
            )
        except Exception as e:
            await LOG.log("WARN", f"BACKTEST_MODE_FAIL mode={mode} err={e}")

    if optimize:
        await optimize_tp_vol_params(ts, o, h, l, c, v, target_trades=target_trades)


async def optimize_tp_vol_params(
    ts: List[int], o: List[Decimal], h: List[Decimal], l: List[Decimal],
    c: List[Decimal], v: List[Decimal],
    target_trades: int = 500, top_k: int = 10,
) -> None:
    """Grid-search TP vol params on the fetched data. Caution: overfit risk."""
    orig = (CFG.tp_vol_floor_pct, CFG.tp_vol_ceiling_pct, CFG.tp_vol_gamma, CFG.tp_vol_lookback_n)
    best = []

    for fl in (Decimal("0.005"), Decimal("0.006"), Decimal("0.008"), Decimal("0.010")):
        for ce in (Decimal("0.016"), Decimal("0.020"), Decimal("0.025")):
            for g in (Decimal("0.8"), Decimal("1.0"), Decimal("1.2")):
                for n in (60, 100, 150):
                    if fl >= ce:
                        continue
                    CFG.tp_vol_floor_pct = fl
                    CFG.tp_vol_ceiling_pct = ce
                    CFG.tp_vol_gamma = g
                    CFG.tp_vol_lookback_n = n
                    try:
                        trades = run_backtest_tp_variant(ts, o, h, l, c, v, tp_mode="vol", target_trades=target_trades)
                        m = compute_trade_metrics(trades)
                        score = (
                            m.get("expectancy", 0.0) * 5
                            + m.get("sharpe_trade", 0.0) * 2
                            - m.get("max_dd", 0.0) * 3
                        )
                        best.append((score, {
                            "score": score, "floor": str(fl), "ceil": str(ce),
                            "gamma": str(g), "n": n, **m,
                        }))
                    except Exception:
                        pass

    CFG.tp_vol_floor_pct, CFG.tp_vol_ceiling_pct, CFG.tp_vol_gamma, CFG.tp_vol_lookback_n = orig

    best.sort(key=lambda x: x[0], reverse=True)
    best = best[:top_k]
    await LOG.log("INFO", f"OPT_TP_DONE candidates={len(best)}")
    for i, (_s, row) in enumerate(best, 1):
        await LOG.log(
            "INFO",
            f"OPT_TP#{i} score={row['score']:.3f} floor={row['floor']} ceil={row['ceil']} "
            f"gamma={row['gamma']} n={row['n']} trades={row['trades']} "
            f"win={row['win_rate']*100:.1f}% exp={row['expectancy']*100:.2f}% "
            f"Sharpe={row['sharpe_trade']:.2f} MaxDD={row['max_dd']*100:.1f}%",
        )


# ----------------------------
# TP float quick test (offline visibility check)
# ----------------------------
async def tp_float_quick_test() -> None:
    """Quick visibility test: prints TP breathing with different vol_norm levels."""
    fake_entry = Decimal("0.0110")
    upnl = Decimal("0.0040")
    for norm in [Decimal("0.15"), Decimal("0.55"), Decimal("0.85"), Decimal("0.35")]:
        base = CFG.tp_vol_floor_pct + (norm ** CFG.tp_vol_gamma) * (CFG.tp_vol_ceiling_pct - CFG.tp_vol_floor_pct)
        tp_live = max(CFG.tp_vol_floor_pct, min(CFG.tp_vol_ceiling_pct, base))
        vol_lbl = "Low" if norm < Decimal("0.33") else ("Med" if norm < Decimal("0.66") else "High")
        tag = "Expanded" if tp_live > fake_entry else ("Contracted" if tp_live < fake_entry else "Same")
        await LOG.log(
            "INFO",
            f"TEST_HB IN_POSITION | PnL:+{(upnl*100):.2f}% | "
            f"Vol:{vol_lbl}(norm={norm:.2f}) | "
            f"TP_Live:{(tp_live*100):.2f}% ({tag} from Entry:{(fake_entry*100):.2f}%)",
        )
    await LOG.log("INFO", "TEST_DONE")
