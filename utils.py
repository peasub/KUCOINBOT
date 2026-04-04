#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
utils.py — Shared constants, math helpers, error tracking, and async threading bridge.

CHANGE LOG:
  MOVED    : D0/D1/D10 precision constants (lines 126–128)
  MOVED    : q_down, q_up, to_str_q (lines 723–742)
  MOVED    : _safe_spread, _obi_from_sizes, _clamp_dec, _clamp01, _safe_div (lines 2283–2299)
  MOVED    : _inventory_frac, _inventory_skew_ticks, _entry_size_after_inventory_skew (lines 2302–2340)
  MOVED    : _entry_markout_bps, update_adverse_selection_monitor, update_opportunity_cost (lines 2342–2407)
  MOVED    : add_error, is_transient_net_error (lines 1622–1659)
  MOVED    : rest_to_thread (line 3699)
  MOVED    : _is_balance_insufficient (lines 597–599), _is_insufficient_balance alias
  PRESERVED: All logic exactly as in the original script.
"""

from __future__ import annotations

import asyncio
import math
import time
from decimal import Decimal, ROUND_DOWN
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from models import BotState, SymbolMeta, Snapshot

# ----------------------------
# Precision singletons
# ----------------------------
D0 = Decimal("0")
D1 = Decimal("1")
D10 = Decimal("10")


# ----------------------------
# Exchange error helpers
# ----------------------------
def _is_balance_insufficient(e: Exception) -> bool:
    s = str(e)
    return ("126013" in s) or ("Balance insufficient" in s)


# backwards-compatible alias (used in some call sites in execution.py)
_is_insufficient_balance = _is_balance_insufficient


# ----------------------------
# Decimal quantization helpers
# ----------------------------
def q_down(x: Decimal, inc: Decimal) -> Decimal:
    """Floor-quantize x to the nearest multiple of inc."""
    if inc <= 0:
        return x
    return (x / inc).to_integral_value(rounding=ROUND_DOWN) * inc


def q_up(x: Decimal, inc: Decimal) -> Decimal:
    """Ceiling-quantize x to the nearest multiple of inc."""
    q = q_down(x, inc)
    if inc > 0 and q < x:
        q = q + inc
    return q


def to_str_q(x: Decimal, inc: Decimal) -> str:
    """Quantize and return a clean decimal string (no trailing zeros)."""
    q = q_down(x, inc)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


# ----------------------------
# Spread / OBI helpers
# ----------------------------
def _safe_spread(meta: "SymbolMeta", bid: Decimal, ask: Decimal) -> Tuple[Decimal, Decimal]:
    """Guard against ask<=bid anomalies. Returns (bid, ask) sanitised."""
    if bid <= 0 or ask <= 0:
        return bid, ask
    if ask <= bid:
        ask = bid + meta.price_increment
    return bid, ask


def _obi_from_sizes(bid_sz: Decimal, ask_sz: Decimal) -> Decimal:
    """Compute order-book imbalance proxy from level-1 sizes. Range: -1..+1."""
    denom = bid_sz + ask_sz
    if denom <= 0:
        return D0
    return (bid_sz - ask_sz) / denom


# ----------------------------
# Clamp helpers
# ----------------------------
def _clamp_dec(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, x))


def _clamp01(x: Decimal) -> Decimal:
    return max(D0, min(D1, x))


def _safe_div(a: Decimal, b: Decimal) -> Decimal:
    return a / b if b != 0 else D0


# ----------------------------
# Inventory skew helpers
# ----------------------------
def _inventory_frac(st: "BotState", s: "Snapshot") -> Decimal:
    """Signed inventory fraction of max_pos_usd: +1 = full long, -1 = full short, 0 = flat."""
    try:
        from config import CFG
        if CFG.max_pos_usd <= 0:
            return D0
        sign = D0
        side = s.pos_side or getattr(st, "position_side", None)
        if side == "LONG":
            sign = D1
        elif side == "SHORT":
            sign = Decimal("-1")
        notional = max(
            Decimal(str(getattr(s, "pos_usd", D0) or D0)),
            abs(getattr(st, "position_qty", D0) or D0) * max(getattr(s, "px", D0) or D0, D0),
        )
        frac = min(D1, max(D0, notional / CFG.max_pos_usd))
        return sign * frac
    except Exception:
        return D0


def _inventory_skew_ticks(side: str, st: "BotState", s: "Snapshot", purpose: str = "entry") -> int:
    """Return tick offset (+= more passive, -= more aggressive) based on current inventory."""
    from config import CFG
    if not getattr(CFG, "inventory_skew_enable", True):
        return 0
    inv = _inventory_frac(st, s)
    mag = int(
        (abs(inv) * Decimal(str(getattr(CFG, "inventory_skew_ticks_max", 2)))).to_integral_value(
            rounding=ROUND_DOWN
        )
    )
    if mag <= 0:
        return 0
    if purpose == "entry":
        if (inv > 0 and side == "buy") or (inv < 0 and side == "sell"):
            return mag
        if (inv > 0 and side == "sell") or (inv < 0 and side == "buy"):
            return -mag
    return 0


def _entry_size_after_inventory_skew(
    usd_target: Decimal, st: "BotState", s: "Snapshot", side: str
) -> Decimal:
    """Scale down entry size when already leaning the same direction as the new trade."""
    from config import CFG
    if not getattr(CFG, "inventory_skew_enable", True):
        return usd_target
    inv = _inventory_frac(st, s)
    penalty = getattr(CFG, "inventory_skew_size_penalty", Decimal("0.35"))
    if (inv > 0 and side == "buy") or (inv < 0 and side == "sell"):
        mult = max(Decimal("0.50"), D1 - (abs(inv) * penalty))
        return usd_target * mult
    return usd_target


# ----------------------------
# Adverse-selection monitor helpers
# ----------------------------
def _entry_markout_bps(side: str, fill_px: Decimal, px_now: Decimal) -> Decimal:
    """Signed markout in basis points (positive = favourable)."""
    if fill_px <= 0 or px_now <= 0:
        return D0
    if side == "SHORT":
        return ((fill_px - px_now) / fill_px) * Decimal("10000")
    return ((px_now - fill_px) / fill_px) * Decimal("10000")


def update_adverse_selection_monitor(st: "BotState", s: "Snapshot") -> Optional[str]:
    """Update EMA of entry markout. Returns a log message string or None."""
    from config import CFG
    if not getattr(CFG, "adverse_sel_enable", True):
        return None
    ts = time.time()
    if getattr(st, "pending_markout_ts", 0.0) <= 0 or ts < st.pending_markout_ts:
        return None
    fill_px = getattr(st, "pending_markout_px", None)
    side = getattr(st, "pending_markout_side", "") or ""
    if fill_px is None or not side:
        st.pending_markout_ts = 0.0
        st.pending_markout_px = None
        st.pending_markout_side = ""
        return None
    mark_bps = _entry_markout_bps(side, fill_px, s.px)
    alpha = getattr(CFG, "adverse_sel_ema_alpha", Decimal("0.35"))
    old = getattr(st, "adverse_sel_ema_bps", D0)
    st.adverse_sel_ema_bps = (alpha * mark_bps) + ((D1 - alpha) * old)
    st.adverse_sel_samples = int(getattr(st, "adverse_sel_samples", 0) or 0) + 1
    st.pending_markout_ts = 0.0
    st.pending_markout_px = None
    st.pending_markout_side = ""
    stop_bps = getattr(CFG, "adverse_sel_stop_bps", Decimal("4.0"))
    if (
        st.adverse_sel_samples >= int(getattr(CFG, "adverse_sel_min_samples", 3))
        and st.adverse_sel_ema_bps <= -abs(stop_bps)
    ):
        return f"ADVERSE_SELECTION_TRIP markout={mark_bps:.2f}bps ema={st.adverse_sel_ema_bps:.2f}bps"
    return f"ADVERSE_SELECTION markout={mark_bps:.2f}bps ema={st.adverse_sel_ema_bps:.2f}bps"


def update_opportunity_cost(st: "BotState", s: "Snapshot", move5: Optional[Decimal]) -> None:
    """[v5.2.0 AUDIT] Active Opportunity Cost (Type II error reduction)."""
    from config import CFG
    try:
        if st.mode != "FLAT":
            st.opp_decay = D0
            return
        idle = (s.ts - st.last_trade_event_ts) if st.last_trade_event_ts > 0 else 0
        market_moving = False
        if move5 is not None and move5 >= CFG.opp_market_move5_min_pct:
            market_moving = True
        if s.reg.p_breakout >= Decimal("0.60") or s.reg.p_trend >= Decimal("0.70"):
            market_moving = market_moving or True

        # OPP_BEAR_GUARD: do not relax thresholds into extreme bearish directional pressure.
        try:
            if s.reg.adx is not None and s.reg.di_plus is not None and s.reg.di_minus is not None:
                dmi_gap = Decimal(str(s.reg.di_minus)) - Decimal(str(s.reg.di_plus))
                if Decimal(str(s.reg.adx)) >= CFG.long_bear_adx_min and dmi_gap >= CFG.long_bear_dmi_gap:
                    st.opp_decay = D0
                    return
        except Exception:
            pass

        if idle >= CFG.opp_idle_threshold_sec and market_moving:
            st.opp_decay = _clamp_dec(st.opp_decay + CFG.opp_decay_step, D0, CFG.opp_decay_max)
        else:
            st.opp_decay = _clamp_dec(st.opp_decay - CFG.opp_decay_relax, D0, CFG.opp_decay_max)
    except Exception:
        st.opp_decay = D0


# ----------------------------
# Error budget / error tracking
# ----------------------------
def add_error(st: "BotState", e: Exception) -> None:
    """Record a non-fatal engine error. Never crashes the process."""
    from config import CFG
    try:
        ts = time.time()
        budget = int(getattr(CFG, "error_budget_max", 50))
        window = float(getattr(CFG, "error_budget_window_sec", 300))
        pause = float(getattr(CFG, "pause_after_errors_sec", 180))
        max_keep = max(50, budget * 6)
        if not hasattr(st, "err_ts") or st.err_ts is None:
            st.err_ts = []
        st.err_ts.append(ts)
        st.err_ts = [x for x in st.err_ts if (ts - x) <= window][-max_keep:]
        if not hasattr(st, "err_ts_fatal") or st.err_ts_fatal is None:
            st.err_ts_fatal = []
        if not is_transient_net_error(e):
            st.err_ts_fatal.append(ts)
            st.err_ts_fatal = [x for x in st.err_ts_fatal if (ts - x) <= window][-max_keep:]
        if len(st.err_ts) >= budget:
            st.pause_until = max(float(getattr(st, "pause_until", 0.0) or 0.0), ts + pause)
            st.pause_reason = "error_budget"
    except Exception:
        return


def is_transient_net_error(e: Exception) -> bool:
    """Best-effort classifier for connectivity / transient REST issues."""
    s = str(e)
    needles = [
        "HTTPSConnectionPool", "Max retries exceeded", "NameResolutionError",
        "Temporary failure in name resolution", "Connection aborted", "Connection reset",
        "ConnectionError", "ReadTimeout", "ConnectTimeout", "timed out",
        "Network is unreachable", "No route to host", "RemoteDisconnected",
        "429", "Too Many Requests",
    ]
    return any(n in s for n in needles)


# ----------------------------
# Async threading bridge
# ----------------------------
async def rest_to_thread(fn, *args, **kwargs):
    """Run a synchronous REST call in a thread pool to avoid blocking the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)
