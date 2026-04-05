#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
models.py — All runtime dataclasses and data models.

CHANGE LOG:
  MOVED    : SymbolMeta (line 715)
  MOVED    : Regime dataclass + __post_init__ (lines 866–938)
  MOVED    : OrderRef, BotState dataclasses (lines 1243–1360)
  MOVED    : Market dataclass (lines 1939–1972)
  MOVED    : Snapshot dataclass (lines 1977–2031)
  MOVED    : Intent dataclass (lines 2759–2773)
  MOVED    : TradeResult dataclass (lines 6159–6170)
  PRESERVED: All field names, defaults, comments, and __post_init__ logic exactly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from utils import D0


# ----------------------------
# Symbol metadata
# ----------------------------
@dataclass
class SymbolMeta:
    symbol: str
    price_increment: Decimal
    base_increment: Decimal
    min_funds: Decimal
    base_min_size: Decimal


# ----------------------------
# Regime
# ----------------------------
@dataclass
class Regime:
    name: str
    p_trend: Decimal
    p_breakout: Decimal
    er: Optional[Decimal]
    bbw: Optional[Decimal]
    adx: Optional[Decimal]
    di_plus: Optional[Decimal]
    di_minus: Optional[Decimal]

    p_chop: Decimal = Decimal("0.50")
    direction_bias: int = 0  # +1 long bias, -1 short bias, 0 neutral
    indeterminate: bool = False
    z_scores: Optional[Dict[str, Decimal]] = None
    reason: str = ""

    def __post_init__(self) -> None:
        """Type-hardening: keep regime probabilities numeric (Decimal) and never allow string bleed."""
        for fld in ("p_trend", "p_breakout", "p_chop"):
            v = getattr(self, fld)
            if isinstance(v, Decimal):
                continue
            if isinstance(v, str):
                try:
                    setattr(self, fld, Decimal(v))
                    continue
                except Exception:
                    setattr(self, fld, Decimal("0.50"))
                    self.indeterminate = True
                    if not self.reason:
                        self.reason = f"coerce_fail_{fld}"
                    continue
            try:
                setattr(self, fld, Decimal(str(v)))
            except Exception:
                setattr(self, fld, Decimal("0.50"))
                self.indeterminate = True
                if not self.reason:
                    self.reason = f"coerce_fail_{fld}"

        for fld in ("er", "bbw", "adx", "di_plus", "di_minus"):
            v = getattr(self, fld)
            if v is None or isinstance(v, Decimal):
                continue
            if isinstance(v, str):
                try:
                    setattr(self, fld, Decimal(v))
                    continue
                except Exception:
                    setattr(self, fld, None)
                    self.indeterminate = True
                    if not self.reason:
                        self.reason = f"coerce_fail_{fld}"
                    continue
            try:
                setattr(self, fld, Decimal(str(v)))
            except Exception:
                setattr(self, fld, None)
                self.indeterminate = True
                if not self.reason:
                    self.reason = f"coerce_fail_{fld}"


# ----------------------------
# Order reference
# ----------------------------
@dataclass
class OrderRef:
    order_id: str
    client_oid: str
    side: str
    price: Decimal
    size: Decimal
    created_ts: float
    purpose: str


# ----------------------------
# Bot state
# ----------------------------
@dataclass
class BotState:
    mode: str = "FLAT"  # FLAT, ENTRY_PENDING, IN_POSITION, EXIT_PENDING, HALTED
    position_side: Optional[str] = None  # LONG|SHORT|None
    position_dir: int = 0                # +1 for LONG, -1 for SHORT, 0 for FLAT
    position_qty: Decimal = D0
    avg_cost: Optional[Decimal] = None
    pos_open_ts: float = 0.0
    peak_price: Optional[Decimal] = None
    cooldown_until: float = 0.0

    entry_order: Optional[OrderRef] = None
    tp1_order: Optional[OrderRef] = None
    tp2_order: Optional[OrderRef] = None
    exit_order: Optional[OrderRef] = None
    exit_inflight: bool = False
    exit_attempts: int = 0
    last_exit_ts: float = 0.0

    last_entry_fill_ts: float = 0.0

    hold_until_ts: float = 0.0
    last_tp_reprice_ts: float = 0.0

    entry_tp1_eff: Optional[Decimal] = None
    entry_tp2_eff: Optional[Decimal] = None
    last_tp_modify_ts: float = 0.0
    last_tp_place_fail_ts: float = 0.0
    last_tp_placed_ts: float = 0.0
    last_tp_eval_bucket: int = 0
    tp_zero_open_confirm_count: int = 0
    last_margin_truth_log_ts: float = 0.0

    last_avg_recover_ts: float = 0.0

    last_trade_event_ts: float = 0.0
    last_activity_warn_ts: float = 0.0

    last_entry_attempt_ts: float = 0.0
    entry_price_hint: Optional[Decimal] = None
    entry_qty_hint: Decimal = D0
    entry_side_hint: str = ""

    # Adverse-selection monitor state
    pending_markout_ts: float = 0.0
    pending_markout_px: Optional[Decimal] = None
    pending_markout_side: str = ""
    adverse_sel_ema_bps: Decimal = D0
    adverse_sel_samples: int = 0

    # Opportunity-cost decay + hysteresis state
    opp_decay: Decimal = D0
    last_regime_name: str = ""

    # Balance cache
    last_bal_refresh_ts: float = 0.0
    force_bal_refresh: bool = True
    bal_q_free: Decimal = D0
    bal_q_total: Decimal = D0
    bal_b_free: Decimal = D0
    bal_b_total: Decimal = D0
    bal_q_liab: Decimal = D0
    bal_b_liab: Decimal = D0

    # Entry maintenance / decay
    entry_last_replace_ts: float = 0.0
    entry_replace_count: int = 0
    entry_intent_tag: str = ""
    entry_intent_urg: int = 0

    # Order-ops containment
    cancel_fail_streak: int = 0
    cancel_fail_window_start_ts: float = 0.0
    order_ops_degraded_until: float = 0.0

    ghost_exit_order_id: str = ""
    ghost_exit_side: str = ""
    ghost_exit_guard_until: float = 0.0
    last_ghost_exit_poll_ts: float = 0.0

    last_pause_log_ts: float = 0.0
    pause_reason: str = ""

    last_fatal_sig: str = ""
    last_fatal_ts: float = 0.0

    # Fixed-at-entry TP (v1)
    trade_tp1_eff: Optional[Decimal] = None
    trade_tp2_eff: Optional[Decimal] = None
    trade_tp_mode: str = ""
    trade_vol_metric: Optional[Decimal] = None
    trade_vol_min: Optional[Decimal] = None
    trade_vol_max: Optional[Decimal] = None
    trade_vol_norm: Optional[Decimal] = None
    trade_tp_base: Optional[Decimal] = None

    # Error budget / pause
    err_ts: List[float] = dataclasses.field(default_factory=list)
    err_ts_fatal: List[float] = dataclasses.field(default_factory=list)  # [AUDIT FIX RC-7] was runtime-only attr
    pause_until: float = 0.0
    halt_reason: str = ""


# ----------------------------
# Market data cache
# ----------------------------
@dataclass
class Market:
    px: Decimal = D0
    bid: Decimal = D0
    ask: Decimal = D0
    ws_age_s: int = 9999
    last_ws_ts: float = 0.0
    last_book_rest_ts: float = 0.0

    bid_sz: Decimal = D0
    ask_sz: Decimal = D0
    obi: Decimal = D0
    last_obi_ts: float = 0.0

    highs_1m: List[Decimal] = dataclasses.field(default_factory=list)
    lows_1m: List[Decimal] = dataclasses.field(default_factory=list)
    closes_1m: List[Decimal] = dataclasses.field(default_factory=list)
    vols_1m: List[Decimal] = dataclasses.field(default_factory=list)

    highs_5m: List[Decimal] = dataclasses.field(default_factory=list)
    lows_5m: List[Decimal] = dataclasses.field(default_factory=list)
    closes_5m: List[Decimal] = dataclasses.field(default_factory=list)
    vols_5m: List[Decimal] = dataclasses.field(default_factory=list)

    last_candle_refresh_ts_1m: float = 0.0
    last_candle_refresh_ts_5m: float = 0.0

    # Cached regimes computed on candle refresh
    regime_1m: Any = None
    regime_5m: Any = None


# Module-level singleton — PRESERVED (all modules share this one instance)
MKT = Market()


# ----------------------------
# Snapshot (decision input)
# ----------------------------
@dataclass
class Snapshot:
    ts: float
    px: Decimal
    bid: Decimal
    ask: Decimal
    spread_pct: Decimal
    rsi: Optional[Decimal]
    ema_f: Optional[Decimal]
    ema_s: Optional[Decimal]
    atrp: Optional[Decimal]
    vwap: Optional[Decimal]
    ret3_1m: Optional[Decimal]
    ret5_1m: Optional[Decimal]
    reg: Regime
    candle_age_1m_s: int
    candle_age_5m_s: int
    candles_stale: bool
    book_degraded: bool
    tp_req: Decimal
    tp1_eff: Decimal
    tp2_eff: Decimal
    cooldown_left: int
    pos_qty: Decimal
    pos_usd: Decimal
    avg: Optional[Decimal]
    upnl_pct: Optional[Decimal]
    pos_age_min: Optional[int]
    pos_side: Optional[str]
    q_liab: Decimal
    b_liab: Decimal
    q_free: Decimal
    q_total: Decimal
    b_free: Decimal
    b_total: Decimal
    open_orders: int
    open_orders_fetch_failed: bool
    margin_symbol_active: bool
    tracked_orders_active: int
    tracked_orders_query_failed: bool

    tp_mode: str

    bid_sz: Optional[Decimal]
    ask_sz: Optional[Decimal]
    obi: Optional[Decimal]

    tp_base_dyn: Optional[Decimal]
    vol_t: Optional[Decimal]
    vol_min: Optional[Decimal]
    vol_max: Optional[Decimal]
    vol_norm: Optional[Decimal]


# ----------------------------
# Strategy intent
# ----------------------------
@dataclass
class Intent:
    side: str          # "buy" or "sell"
    strategy_id: str   # "DIP", "TPB", "MOMO", "VBRK", "SQMR"
    score: float       # higher = better
    urgency: int       # 0=passive, 1=moderate, 2=taker-eligible
    size_mult: Decimal = Decimal("1.0")
    reason: str = ""


# ----------------------------
# Backtest trade result
# ----------------------------
@dataclass
class TradeResult:
    entry_ts: int
    exit_ts: int
    entry_px: Decimal
    exit_px: Decimal
    ret_pct: Decimal
    reason: str
    bars: int
    tp1_eff: Decimal
    tp2_eff: Decimal
    vol_norm: Optional[Decimal]
