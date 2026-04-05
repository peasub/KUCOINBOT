#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snapshot.py — Market snapshot assembly and order/position reconciliation.

CHANGE LOG:
  MOVED    : calc_upnl_pct (lines 3681–3697)
  MOVED    : rest_to_thread wrapper reference — actual fn lives in utils.py
  MOVED    : _order_record_is_live (lines 3703–3719)
  MOVED    : _tracked_order_truth (lines 3722–3736)
  MOVED    : _margin_open_order_truth (lines 3739–3800)
  MOVED    : build_snapshot (lines 3803–4063)
  PRESERVED: All reconciliation logic, balance caching, OBI refresh, stale-candle gates,
             TP mode resolution, and balance-lag grace logic exactly as in the original.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from config import CFG
from indicators import atr, ema, rsi
from logger import LOG, now_ts
from client import KuCoinClient
from models import BotState, MKT, Regime, Snapshot, SymbolMeta
from regime import apply_regime_hysteresis, classify_regime_C, classify_regime_prob, combine_regimes
from tp import (
    compute_tp_base_from_vol,
    compute_vwap,
    effective_tp,
    required_move_pct,
)
from utils import D0, D1, _obi_from_sizes, _safe_spread, rest_to_thread


# ----------------------------
# uPnL helper
# ----------------------------
def calc_upnl_pct(px: Decimal, avg: Optional[Decimal], position_dir: int = 1) -> Optional[Decimal]:
    """Unrealized PnL percent. LONG (+1): (px-avg)/avg. SHORT (-1): (avg-px)/avg."""
    if avg is None or avg <= 0:
        return None
    if int(position_dir) < 0:
        return (avg - px) / avg
    return (px - avg) / avg


def _to_int(x: Any) -> int:
    try:
        return int(x)
    except Exception:
        return 0


# ----------------------------
# Order truth helpers
# ----------------------------
def _order_record_is_live(od: Dict[str, Any]) -> bool:
    """Desk truth for an order record across spot/margin response variants."""
    try:
        active = bool(od.get("isActive", od.get("active", False))) or bool(od.get("inOrderBook", False))
        if active:
            return True
        remain = Decimal(str(od.get("remainSize", od.get("remainFunds", "0")) or "0"))
        if remain > 0:
            return True
        size = Decimal(str(od.get("size", od.get("funds", "0")) or "0"))
        deal = Decimal(str(od.get("dealSize", od.get("dealFunds", "0")) or "0"))
        status = str(od.get("status", "") or "").lower()
        if size > 0 and deal < size and status not in {"done", "cancelled", "canceled"}:
            return True
    except Exception:
        return False
    return False


async def _tracked_order_truth(cli: KuCoinClient, st: BotState) -> Dict[str, Any]:
    live_names: List[str] = []
    checked: List[str] = []
    for ref_name in ("entry_order", "tp1_order", "tp2_order", "exit_order"):
        ref = getattr(st, ref_name, None)
        if ref is None:
            continue
        checked.append(ref_name)
        try:
            od = await rest_to_thread(cli.get_order_any, CFG.symbol, ref.order_id)
        except Exception as e:
            return {"failed": True, "live_names": live_names, "checked": checked, "error": str(e)}
        if _order_record_is_live(od):
            live_names.append(ref_name)
    return {"failed": False, "live_names": live_names, "checked": checked, "error": ""}


async def _margin_open_order_truth(
    cli: KuCoinClient, st: BotState, need_open_scan: bool
) -> Dict[str, Any]:
    """Unified open-order truth for isolated margin. Intentionally redundant for safety."""
    result: Dict[str, Any] = {
        "open_orders": [],
        "open_n": 0,
        "effective_open_n": 0,
        "fetch_failed": False,
        "fetch_error": "",
        "symbol_active": False,
        "symbol_probe_failed": False,
        "symbol_probe_error": "",
        "tracked_active_count": 0,
        "tracked_live_names": [],
        "tracked_query_failed": False,
        "tracked_query_error": "",
        "source_trade_type": str(
            getattr(cli, "_margin_trade_type_working",
                    getattr(CFG, "margin_trade_type", "MARGIN_TRADE"))
        ),
    }
    has_tracked_refs = bool(st.entry_order or st.tp1_order or st.tp2_order or st.exit_order)
    if not need_open_scan:
        if has_tracked_refs:
            tracked = await _tracked_order_truth(cli, st)
            result["tracked_query_failed"] = bool(tracked.get("failed", False))
            result["tracked_query_error"] = str(tracked.get("error", "") or "")
            result["tracked_live_names"] = list(tracked.get("live_names", []) or [])
            result["tracked_active_count"] = len(result["tracked_live_names"])
            result["effective_open_n"] = result["tracked_active_count"]
        return result

    try:
        opens = await rest_to_thread(cli.list_open_orders_any, CFG.symbol)
        result["open_orders"] = opens
        result["open_n"] = len(opens)
    except Exception as e:
        result["fetch_failed"] = True
        result["fetch_error"] = str(e)

    if (str(getattr(CFG, "account_mode", "spot")).lower() == "margin"
            and bool(getattr(CFG, "margin_truth_symbol_probe", True))):
        try:
            symbols = await rest_to_thread(cli.list_open_margin_order_symbols_any)
            result["symbol_active"] = CFG.symbol in set(symbols or [])
        except Exception as e:
            result["symbol_probe_failed"] = True
            result["symbol_probe_error"] = str(e)

    if (has_tracked_refs or result["fetch_failed"]
            or result["open_n"] == 0 or result["symbol_active"]):
        tracked = await _tracked_order_truth(cli, st)
        result["tracked_query_failed"] = bool(tracked.get("failed", False))
        result["tracked_query_error"] = str(tracked.get("error", "") or "")
        result["tracked_live_names"] = list(tracked.get("live_names", []) or [])
        result["tracked_active_count"] = len(result["tracked_live_names"])

    eff = result["open_n"]
    if result["symbol_active"]:
        eff = max(eff, 1)
    eff = max(eff, int(result["tracked_active_count"]))
    result["effective_open_n"] = eff
    result["source_trade_type"] = str(
        getattr(cli, "_margin_trade_type_working", result["source_trade_type"])
    )
    return result


# ----------------------------
# Snapshot builder
# ----------------------------
async def build_snapshot(cli: KuCoinClient, meta: SymbolMeta, st: BotState) -> Snapshot:
    ts = now_ts()

    # --- Level 1 book (WS cache first, REST fallback) ---
    px = MKT.px
    bid = MKT.bid
    ask = MKT.ask
    book_degraded = False
    s_ws_age = int(now_ts() - MKT.last_ws_ts) if MKT.last_ws_ts > 0 else 9999
    if px <= 0 or (now_ts() - MKT.last_ws_ts) > 8:
        try:
            if (ts - getattr(MKT, "last_book_rest_ts", 0.0)) >= float(
                getattr(CFG, "book_refresh_sec", 5)
            ):
                px, bid, ask = await rest_to_thread(cli.level1, CFG.symbol)
                MKT.px, MKT.bid, MKT.ask = px, bid, ask
                MKT.last_book_rest_ts = ts
        except Exception as e:
            if px > 0:
                book_degraded = True
                if bid <= 0:
                    bid = max(meta.price_increment, px - meta.price_increment)
                if ask <= 0 or ask <= bid:
                    ask = bid + max(meta.price_increment, meta.price_increment)
                if (ts - float(getattr(st, "last_book_degraded_log_ts", 0.0) or 0.0)) > 30:
                    await LOG.log(
                        "WARN",
                        f"BOOK_DEGRADED reuse_cache ws_age={s_ws_age}s err={e}",
                    )
                    st.last_book_degraded_log_ts = ts  # type: ignore[attr-defined]
            else:
                raise

    # --- OBI refresh (throttled) ---
    try:
        obi_sec = (
            CFG.obi_refresh_sec_active
            if st.mode in ("ENTRY_PENDING", "IN_POSITION", "IN_POSITION_RECOVER", "EXIT_PENDING")
            else CFG.obi_refresh_sec_flat
        )
        if (ts - getattr(MKT, "last_obi_ts", 0.0)) >= obi_sec:
            _px2, _b2, _a2, bsz, asz = await rest_to_thread(cli.level1_full, CFG.symbol)
            if bsz is not None and asz is not None:
                MKT.bid_sz, MKT.ask_sz = bsz, asz
                MKT.obi = _obi_from_sizes(bsz, asz)
                MKT.last_obi_ts = ts
    except Exception:
        pass

    bid, ask = _safe_spread(meta, bid, ask)
    spread_pct = (ask - bid) / px if px > 0 else Decimal("0")

    # --- Balance cache ([v5.2.0] reduced REST load) ---
    base, quote = CFG.symbol.split("-")
    bal_sec = (
        CFG.balance_refresh_sec_active
        if st.mode in ("ENTRY_PENDING", "IN_POSITION", "IN_POSITION_RECOVER", "EXIT_PENDING")
        else CFG.balance_refresh_sec_flat
    )
    if st.force_bal_refresh or (ts - st.last_bal_refresh_ts) >= bal_sec:
        try:
            q_free, q_total, q_liab = await rest_to_thread(cli.accounts_any, quote)
            b_free, b_total, b_liab = await rest_to_thread(cli.accounts_any, base)
            st.bal_q_free = q_free
            st.bal_q_total = q_total
            st.bal_b_free = b_free
            st.bal_b_total = b_total
            st.bal_q_liab = q_liab
            st.bal_b_liab = b_liab
            st.last_bal_refresh_ts = ts
            st.force_bal_refresh = False
        except Exception:
            pass  # Use cached values

    q_free = st.bal_q_free
    q_total = st.bal_q_total
    b_free = st.bal_b_free
    b_total = st.bal_b_total
    q_liab = st.bal_q_liab
    b_liab = st.bal_b_liab

    # --- Open-order truth ---
    _open_orders_fetch_failed = False
    _margin_symbol_active = False
    _tracked_orders_active = 0
    _tracked_orders_query_failed = False
    need_open_scan = st.mode in (
        "IN_POSITION", "IN_POSITION_RECOVER", "EXIT_PENDING", "ENTRY_PENDING"
    )
    try:
        truth = await _margin_open_order_truth(cli, st, need_open_scan)
        open_n = int(truth["effective_open_n"])
        _open_orders_fetch_failed = bool(truth["fetch_failed"])
        _margin_symbol_active = bool(truth["symbol_active"])
        _tracked_orders_active = int(truth["tracked_active_count"])
        _tracked_orders_query_failed = bool(truth["tracked_query_failed"])
        # Log margin truth periodically
        if (
            str(getattr(CFG, "account_mode", "spot")).lower() == "margin"
            and (ts - float(getattr(st, "last_margin_truth_log_ts", 0.0) or 0.0)) >= 300
        ):
            st.last_margin_truth_log_ts = ts
            await LOG.log(
                "INFO",
                f"MARGIN_TRUTH open_n={open_n} eff={truth['effective_open_n']} "
                f"sym_active={int(_margin_symbol_active)} fetch_fail={int(_open_orders_fetch_failed)} "
                f"tracked={_tracked_orders_active} query_fail={int(_tracked_orders_query_failed)} "
                f"tradeType={truth.get('source_trade_type','-')}",
            )
    except Exception:
        open_n = 0
        _open_orders_fetch_failed = True

    # --- Position truth from balances ---
    # For margin: net = total - liability
    net_base = b_total - b_liab
    pos_qty = D0
    pos_side: Optional[str] = None
    pos_usd = D0
    if abs(net_base) * px >= CFG.dust_notional_usd:
        if net_base > 0:
            pos_qty = net_base
            pos_side = "LONG"
            pos_usd = pos_qty * px
        elif net_base < 0:
            pos_qty = abs(net_base)
            pos_side = "SHORT"
            pos_usd = pos_qty * px

    avg = st.avg_cost
    upnl_pct: Optional[Decimal] = None
    if avg is not None and pos_qty > 0:
        upnl_pct = calc_upnl_pct(px, avg, st.position_dir if st.position_dir != 0 else 1)

    pos_age_min: Optional[int] = None
    if st.pos_open_ts > 0 and pos_qty > 0:
        pos_age_min = int((ts - st.pos_open_ts) / 60)

    # --- Cooldown ---
    cd_left = max(0, int(st.cooldown_until - ts))

    # --- Candle indicators ---
    highs_1m = MKT.highs_1m
    lows_1m = MKT.lows_1m
    closes_1m = MKT.closes_1m
    vols_1m = MKT.vols_1m
    highs_5m = MKT.highs_5m
    lows_5m = MKT.lows_5m
    closes_5m = MKT.closes_5m

    rsi_v: Optional[Decimal] = None
    ema_f: Optional[Decimal] = None
    ema_s: Optional[Decimal] = None
    atrp: Optional[Decimal] = None
    vwap: Optional[Decimal] = None
    ret3_1m: Optional[Decimal] = None
    ret5_1m: Optional[Decimal] = None

    if len(closes_1m) >= CFG.rsi_len + 1:
        rsi_v = rsi(closes_1m, CFG.rsi_len)
    if len(closes_1m) >= CFG.ema_fast:
        ema_f = ema(closes_1m, CFG.ema_fast)
    if len(closes_1m) >= CFG.ema_slow:
        ema_s = ema(closes_1m, CFG.ema_slow)
    if len(closes_1m) >= CFG.atr_len + 1:
        atr_v = atr(highs_1m, lows_1m, closes_1m, CFG.atr_len)
        if atr_v is not None and closes_1m[-1] > 0:
            atrp = atr_v / closes_1m[-1]
    if len(closes_1m) >= 4:
        ret3_1m = (closes_1m[-1] / closes_1m[-4] - D1) if closes_1m[-4] > 0 else None
    if len(closes_1m) >= 6:
        ret5_1m = (closes_1m[-1] / closes_1m[-6] - D1) if closes_1m[-6] > 0 else None
    if len(closes_1m) >= 60 and len(vols_1m) >= 60:
        vwap = compute_vwap(closes_1m, vols_1m, 60)

    # --- Regime (use cached if available) ---
    reg_1m: Optional[Regime] = getattr(MKT, "regime_1m", None)
    reg_5m: Optional[Regime] = getattr(MKT, "regime_5m", None)
    if reg_1m is None:
        if CFG.regime_model == "prob_z" and len(closes_1m) >= 80:
            reg_1m = classify_regime_prob(highs_1m, lows_1m, closes_1m)
        elif len(closes_1m) >= 70:
            reg_1m = classify_regime_C(highs_1m, lows_1m, closes_1m)
    if reg_5m is None:
        if CFG.regime_model == "prob_z" and len(closes_5m) >= 80:
            reg_5m = classify_regime_prob(highs_5m, lows_5m, closes_5m)
        elif len(closes_5m) >= 70:
            reg_5m = classify_regime_C(highs_5m, lows_5m, closes_5m)

    if reg_1m is not None and reg_5m is not None:
        reg = combine_regimes(reg_1m, reg_5m)
    elif reg_1m is not None:
        reg = reg_1m
    elif reg_5m is not None:
        reg = reg_5m
    else:
        reg = Regime(
            name="UNKNOWN", p_trend=Decimal("0.50"), p_breakout=Decimal("0.50"),
            er=None, bbw=None, adx=None, di_plus=None, di_minus=None,
            p_chop=Decimal("0.50"), indeterminate=True, reason="no_data",
        )

    reg = apply_regime_hysteresis(reg, st)

    # --- TP computation ---
    tp_req = required_move_pct(spread_pct)
    tp_mode_live = CFG.tp_mode
    tp_base_dyn = vol_t = vol_min = vol_max = vol_norm = None

    if tp_mode_live == "static":
        tp1_eff, tp2_eff = effective_tp(CFG.tp_static_pct, tp_req, reg, atrp)
    elif tp_mode_live == "vol":
        tp_base_dyn, vol_t, vol_min, vol_max, vol_norm = compute_tp_base_from_vol(
            highs_5m, lows_5m, closes_5m
        )
        if tp_base_dyn is not None:
            # [AUDIT FIX RC-4] Route through effective_tp for regime scaling
            tp1_eff, tp2_eff = effective_tp(tp_base_dyn, tp_req, reg, atrp)
        else:
            tp1_eff, tp2_eff = effective_tp(CFG.tp_pct_base, tp_req, reg, atrp)
    else:  # regime
        tp1_eff, tp2_eff = effective_tp(CFG.tp_pct_base, tp_req, reg, atrp)

    # --- Candle freshness ---
    last_c1 = MKT.last_candle_refresh_ts_1m
    last_c5 = MKT.last_candle_refresh_ts_5m
    c1_age = int(ts - last_c1) if last_c1 > 0 else 10 ** 9
    c5_age = int(ts - last_c5) if last_c5 > 0 else 10 ** 9
    candles_stale = (
        (last_c1 <= 0) or (last_c5 <= 0)
        or (c1_age > int(CFG.candles_stale_sec_1m))
        or (c5_age > int(CFG.candles_stale_sec_5m))
        or (c1_age < -5) or (c5_age < -5)
    )
    if candles_stale:
        try:
            import dataclasses as _dc
            reg = _dc.replace(reg, indeterminate=True,
                              reason=f"stale_candles age1={c1_age}s age5={c5_age}s")
        except Exception:
            pass

    return Snapshot(
        ts=ts, px=px, bid=bid, ask=ask, spread_pct=spread_pct,
        rsi=rsi_v, ema_f=ema_f, ema_s=ema_s, atrp=atrp, vwap=vwap,
        ret3_1m=ret3_1m, ret5_1m=ret5_1m,
        reg=reg, candle_age_1m_s=c1_age, candle_age_5m_s=c5_age,
        candles_stale=candles_stale, book_degraded=book_degraded,
        tp_req=tp_req, tp1_eff=tp1_eff, tp2_eff=tp2_eff,
        cooldown_left=cd_left,
        pos_qty=pos_qty, pos_usd=pos_usd, avg=avg, upnl_pct=upnl_pct,
        pos_age_min=pos_age_min, pos_side=pos_side,
        q_liab=q_liab, b_liab=b_liab,
        q_free=q_free, q_total=q_total, b_free=b_free, b_total=b_total,
        open_orders=open_n,
        open_orders_fetch_failed=_open_orders_fetch_failed,
        margin_symbol_active=_margin_symbol_active,
        tracked_orders_active=_tracked_orders_active,
        tracked_orders_query_failed=_tracked_orders_query_failed,
        bid_sz=(MKT.bid_sz if getattr(MKT, "bid_sz", D0) > 0 else None),
        ask_sz=(MKT.ask_sz if getattr(MKT, "ask_sz", D0) > 0 else None),
        obi=(MKT.obi if getattr(MKT, "obi", D0) != 0 else None),
        tp_mode=tp_mode_live,
        tp_base_dyn=tp_base_dyn, vol_t=vol_t, vol_min=vol_min,
        vol_max=vol_max, vol_norm=vol_norm,
    )
