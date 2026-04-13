#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
execution.py — Order placement, maintenance, reconciliation, and exit ladder.

CHANGE LOG:
  MOVED    : client_oid, _order_tag (lines 4068–4085)
  MOVED    : _record_cancel_success/failure, _safe_cancel (lines 4087–4109)
  MOVED    : _guard_stale_exit_order (lines 4111–4161)
  MOVED    : maker_entry_price, maker_exit_price, maker_limit_price (lines 4163–4189)
  MOVED    : maker_entry_price_smart (lines 4193–4248)
  MOVED    : place_entry (lines 4251–4340)
  MOVED    : place_tp_orders (lines 4343–4468)
  MOVED    : maybe_update_tp_orders_float (lines 4470–4688)
  MOVED    : maintain_entry_order (lines ~4640–4787)
  MOVED    : cancel_if_stale (lines 4789–4842)
  MOVED    : reconcile_orders (lines 4845–5086)
  MOVED    : _fresh_exit_remaining_qty (lines 5226–5268)
  MOVED    : execute_exit_ladder (lines 5271–5541)
  PRESERVED: All execution logic, autoRepay flags, cancel-fail circuit breakers,
             partial-fill guards, queue-loss escalation, and state transitions exactly.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from config import CFG
from logger import LOG, now_ts
from models import BotState, OrderRef, Snapshot, SymbolMeta
from client import KuCoinClient
from tp import adjust_tp_for_strategy
from utils import (
    D0, D1, _is_balance_insufficient, _is_insufficient_balance,
    _inventory_skew_ticks, _entry_size_after_inventory_skew,
    _safe_spread, add_error, q_down, q_up, rest_to_thread, to_str_q,
)
from tp import regime_sizing_mult

# [PHASE A] Ledger and protections — graceful degradation
try:
    from protections import update_protection_state_on_exit, update_maturity_on_entry
    from trade_quality_ledger import record_trade_quality, build_entry_taken_record, build_entry_rejected_record, build_exit_record, _dec_str
    _PHASE_A_OK = True
except ImportError:
    _PHASE_A_OK = False


def _record_exit_to_ledger(st: BotState, s: Snapshot, exit_reason: str) -> None:
    """[PHASE A] Record exit to trade quality ledger and update protections state. Best-effort."""
    if not _PHASE_A_OK:
        return
    try:
        _side = st.position_side or getattr(s, "pos_side", None) or ""
        _entry_side = "buy" if _side == "LONG" else ("sell" if _side == "SHORT" else "")
        _avg = st.avg_cost
        _best = getattr(st, "best_excursion_bps", None)
        _worst = getattr(st, "worst_excursion_bps", None)
        _hold_s = int(now_ts() - st.pos_open_ts) if st.pos_open_ts > 0 else 0
        _pnl_bps = None
        if _avg is not None and _avg > 0 and s.px > 0:
            if _side == "LONG":
                _pnl_bps = ((s.px - _avg) / _avg) * Decimal("10000")
            elif _side == "SHORT":
                _pnl_bps = ((_avg - s.px) / _avg) * Decimal("10000")

        _tag = str(getattr(st, "entry_intent_tag", "") or "")
        record_trade_quality(build_exit_record(
            ts=now_ts(),
            regime_name=s.reg.name if hasattr(s, "reg") else "",
            p_trend=getattr(s.reg, "p_trend", None) if hasattr(s, "reg") else None,
            p_chop=getattr(s.reg, "p_chop", None) if hasattr(s, "reg") else None,
            p_breakout=getattr(s.reg, "p_breakout", None) if hasattr(s, "reg") else None,
            direction_bias=int(getattr(s.reg, "direction_bias", 0) or 0) if hasattr(s, "reg") else 0,
            worker_tag=_tag, side=_entry_side,
            entry_px=_avg, exit_px=s.px,
            best_excursion_bps=_best, worst_excursion_bps=_worst,
            hold_seconds=_hold_s, realized_pnl_bps=_pnl_bps,
            exit_reason=exit_reason,
            notes=f"avg={_avg} qty={st.position_qty}",
        ))

        update_protection_state_on_exit(st, exit_reason, _entry_side, _pnl_bps, _best)
    except Exception:
        pass  # never crash


# ----------------------------
# Client order ID helpers
# ----------------------------
def client_oid(tag: str = "BOT") -> str:
    """Generate a KuCoin-compatible clientOid with strategy tag prefix."""
    tag = re.sub(r"[^A-Z0-9]", "", str(tag).upper())[:4] or "BOT"
    return (tag + uuid.uuid4().hex)[:32]


def _order_tag(st: Optional[BotState], purpose: str) -> str:
    """Encode strategy_id into KuCoin clientOid (4 chars max), including order purpose.
    Examples: MOM1 (MOMO TP1), VBR2 (VBRK TP2), DIPX (exit), BOT1 (fallback).
    """
    base = "BOT"
    if st is not None and getattr(st, "entry_intent_tag", ""):
        base = re.sub(r"[^A-Z0-9]", "", str(st.entry_intent_tag).upper())[:3] or "BOT"
    p = re.sub(r"[^A-Z0-9]", "", str(purpose).upper())[:1] or "O"
    return (base + p)[:4]


# ----------------------------
# Cancel-fail circuit breaker
# ----------------------------
def _record_cancel_success(st: BotState) -> None:
    st.cancel_fail_streak = 0
    st.cancel_fail_window_start_ts = 0.0


def _record_cancel_failure(st: BotState, purpose: str, err: Exception) -> None:
    ts = now_ts()
    if st.cancel_fail_window_start_ts <= 0.0 or (ts - st.cancel_fail_window_start_ts) > float(CFG.cancel_fail_window_sec):
        st.cancel_fail_window_start_ts = ts
        st.cancel_fail_streak = 1
    else:
        st.cancel_fail_streak += 1
    if st.cancel_fail_streak >= int(CFG.cancel_fail_limit):
        st.order_ops_degraded_until = max(
            st.order_ops_degraded_until, ts + float(CFG.order_ops_degraded_sec)
        )


async def _safe_cancel(cli: KuCoinClient, order_id: str, st: BotState, purpose: str) -> bool:
    try:
        await rest_to_thread(cli.cancel_any, CFG.symbol, order_id)
        _record_cancel_success(st)
        return True
    except Exception as e:
        _record_cancel_failure(st, purpose, e)
        return False


# ----------------------------
# Ghost-exit / zombie-exit guard
# ----------------------------
async def _guard_stale_exit_order(cli: KuCoinClient, st: BotState, s: Snapshot) -> bool:
    """[DAILY AUDIT FIX] Exact-order zombie-exit guard. Returns True while guard should block entries."""
    oid = str(getattr(st, "ghost_exit_order_id", "") or "")
    if not oid:
        return False
    now = now_ts()
    guard_until = float(getattr(st, "ghost_exit_guard_until", 0.0) or 0.0)
    if guard_until > 0 and now > guard_until:
        await LOG.log("WARN", f"GHOST_EXIT_GUARD_EXPIRED id={oid}")
        st.ghost_exit_order_id = ""
        st.ghost_exit_side = ""
        st.ghost_exit_guard_until = 0.0
        st.last_ghost_exit_poll_ts = 0.0
        return False
    if (now - float(getattr(st, "last_ghost_exit_poll_ts", 0.0) or 0.0)) < float(getattr(CFG, "ghost_exit_poll_sec", 15)):
        return True
    st.last_ghost_exit_poll_ts = now
    try:
        od = await rest_to_thread(cli.get_order_any, CFG.symbol, oid)
        is_active = bool(od.get("isActive", od.get("active", False)))
        deal_size = Decimal(str(od.get("dealSize", "0") or "0"))
        if is_active:
            try:
                await rest_to_thread(cli.cancel_any, CFG.symbol, oid)
                await LOG.log("WARN", f"GHOST_EXIT_CANCEL_RETRY id={oid}")
            except Exception as e:
                await LOG.log("WARN", f"GHOST_EXIT_CANCEL_RETRY_FAIL id={oid} err={e}")
            return True
        if deal_size > 0:
            side = (getattr(st, "ghost_exit_side", "") or "").lower()
            await LOG.log("WARN", f"GHOST_EXIT_LATE_FILL id={oid} side={side} deal={deal_size}")
            st.mode = "IN_POSITION_RECOVER"
            st.exit_inflight = False
        else:
            await LOG.log("INFO", f"GHOST_EXIT_CLEARED id={oid}")
        st.order_ops_degraded_until = min(
            float(getattr(st, "order_ops_degraded_until", 0.0) or 0.0), now + 5.0
        )
        st.ghost_exit_order_id = ""
        st.ghost_exit_side = ""
        st.ghost_exit_guard_until = 0.0
        st.last_ghost_exit_poll_ts = 0.0
        return False
    except Exception as e:
        await LOG.log("WARN", f"GHOST_EXIT_CHECK_FAIL id={oid} err={e}")
        return True


# ----------------------------
# Maker pricing
# ----------------------------
def maker_entry_price(meta: SymbolMeta, bid: Decimal, ask: Decimal) -> Decimal:
    return maker_limit_price(meta, bid, ask, "buy")


def maker_exit_price(meta: SymbolMeta, bid: Decimal, ask: Decimal) -> Decimal:
    return maker_limit_price(meta, bid, ask, "sell")


def maker_limit_price(meta: SymbolMeta, bid: Decimal, ask: Decimal, side: str) -> Decimal:
    """Side-aware maker limit price (postOnly-safe)."""
    bid, ask = _safe_spread(meta, bid, ask)
    if side == "buy":
        px = ask - meta.price_increment * Decimal(CFG.entry_improve_ticks)
        if px >= ask:
            px = ask - meta.price_increment
        if px <= 0:
            px = bid
        return q_down(px, meta.price_increment)
    px = bid + meta.price_increment * Decimal(CFG.entry_improve_ticks)
    if px <= bid:
        px = bid + meta.price_increment
    return q_down(px, meta.price_increment)


def maker_entry_price_smart(
    meta: SymbolMeta, bid: Decimal, ask: Decimal, obi: Optional[Decimal],
    intent: "Intent", st: BotState, side: str = "buy"  # type: ignore[name-defined]
) -> Tuple[Decimal, bool]:
    """[v5.2.0 AUDIT] OBI-aware maker pegging + controlled taker escalation."""
    bid, ask = _safe_spread(meta, bid, ask)
    spread = ask - bid
    if spread <= 0:
        base_px = bid if side == "buy" else ask
        return q_down(base_px, meta.price_increment), True

    o = obi if obi is not None else D0
    try:
        one = meta.price_increment
        if side == "buy":
            px = bid
            if spread >= (one * 2):
                px = bid + one
            if o <= Decimal("-0.15"):
                px = bid
            if o >= Decimal("0.15") and spread >= (one * 2):
                px = bid + one
            if intent.strategy_id in ("DIP", "SQMR"):
                px = bid
            if px >= ask:
                px = ask - one
            if px <= 0:
                px = bid
        else:
            px = ask
            if spread >= (one * 2):
                px = ask - one
            if o >= Decimal("0.15"):
                px = ask
            if o <= Decimal("-0.15") and spread >= (one * 2):
                px = ask - one
            if intent.strategy_id in ("DIP", "SQMR"):
                px = ask
            if px <= bid:
                px = bid + one
        return q_down(px, meta.price_increment), True
    except Exception:
        px = bid if side == "buy" else ask
        return q_down(px, meta.price_increment), True


# ----------------------------
# Entry placement
# ----------------------------
async def place_entry(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot, intent: "Intent"  # type: ignore[name-defined]
) -> None:
    if s.pos_usd >= CFG.position_close_notional_usd:
        await LOG.log("INFO", f"ENTRY_SKIP already_in_position pos_usd={s.pos_usd:.2f}")
        return
    if st.mode in ("ENTRY_PENDING", "EXIT_PENDING"):
        return

    from strategy import assess_entry_quality
    ok_q, score_q, reason_q = assess_entry_quality(s, intent)
    if not ok_q:
        await LOG.log("WARN", f"ENTRY_QUALITY_REJECT tag={intent.strategy_id} side={intent.side} {reason_q}")
        # [PHASE A] Record rejection to ledger
        if _PHASE_A_OK:
            try:
                import re as _re
                _bf_match = _re.search(r"blocker_family=(\S+)", reason_q)
                _bf = _bf_match.group(1).split("|")[0] if _bf_match else "quality_reject_score"
                _edge_match = _re.search(r"edge_bps=([\d.]+)", reason_q)
                _edge = Decimal(_edge_match.group(1)) if _edge_match else D0
                record_trade_quality(build_entry_rejected_record(
                    ts=now_ts(), regime_name=s.reg.name,
                    p_trend=s.reg.p_trend, p_chop=s.reg.p_chop, p_breakout=s.reg.p_breakout,
                    direction_bias=int(getattr(s.reg, "direction_bias", 0) or 0),
                    worker_tag=intent.strategy_id, raw_score=Decimal(str(intent.score)),
                    orch_adjusted_score=score_q,
                    blocker_family=_bf, edge_bps=_edge,
                    side=intent.side, notes=reason_q,
                ))
            except Exception:
                pass
        return

    mult = regime_sizing_mult(s.reg)
    usd_target = min(CFG.max_pos_usd, CFG.usd_per_trade * mult)
    usd_target *= intent.size_mult

    side = intent.side
    usd_target = _entry_size_after_inventory_skew(usd_target, st, s, side)

    if side == "buy":
        usd_target = min(usd_target, s.q_free * Decimal("0.995"))
        if s.q_free < meta.min_funds:
            await LOG.log("WARN", f"ENTRY_SKIP buy_funds q_free={s.q_free:.2f} < minFunds={meta.min_funds}")
            return
    else:
        usd_target = min(usd_target, s.q_total * Decimal("0.95"))
        if s.q_total < meta.min_funds:
            await LOG.log("WARN", f"ENTRY_SKIP short_collateral q_total={s.q_total:.2f} < minFunds={meta.min_funds}")
            return
    if usd_target < meta.min_funds:
        await LOG.log("WARN", f"ENTRY_SKIP usd_target={usd_target:.2f} < minFunds={meta.min_funds}")
        return

    st.entry_intent_tag = intent.strategy_id
    st.entry_intent_urg = int(intent.urgency)
    if side == "sell" and not getattr(CFG, "enable_shorts", True):
        return
    if side == "buy" and not getattr(CFG, "enable_longs", True):
        return

    if side == "buy":
        price, post_only = maker_entry_price_smart(meta, s.bid, s.ask, s.obi, intent, st, "buy")
        skew_ticks = _inventory_skew_ticks(side, st, s, "entry")
        if skew_ticks > 0:
            price = q_down(max(meta.price_increment, price - (meta.price_increment * Decimal(skew_ticks))), meta.price_increment)
    else:
        price, post_only = maker_entry_price_smart(meta, s.bid, s.ask, s.obi, intent, st, "sell")
        skew_ticks = _inventory_skew_ticks(side, st, s, "entry")
        if skew_ticks > 0:
            price = q_down(price + (meta.price_increment * Decimal(skew_ticks)), meta.price_increment)
    post_only = bool(getattr(CFG, "post_only", True)) and bool(post_only)

    qty = q_down(usd_target / price, meta.base_increment)
    if qty < meta.base_min_size:
        await LOG.log("WARN", f"ENTRY_SKIP qty={qty} < baseMin={meta.base_min_size}")
        return

    oid = client_oid(intent.strategy_id)
    price_s = to_str_q(price, meta.price_increment)
    size_s = to_str_q(qty, meta.base_increment)

    try:
        auto_borrow = (side == "sell" and bool(getattr(CFG, "margin_autoborrow_short", True))) or (
            side == "buy" and bool(getattr(CFG, "margin_autoborrow_long", False))
        )
        order_id = await rest_to_thread(
            cli.place_limit_any, CFG.symbol, side, price_s, size_s, oid, post_only, auto_borrow, False
        )
        st.entry_order = OrderRef(order_id, oid, side, price, qty, now_ts(), "ENTRY")
        st.mode = "ENTRY_PENDING"
        st.entry_replace_count = 0
        st.entry_last_replace_ts = now_ts()
        st.last_entry_attempt_ts = now_ts()
        st.entry_price_hint = price
        st.entry_qty_hint = qty
        st.entry_side_hint = side
        await LOG.log(
            "INFO",
            f"ENTRY_SENT {intent.strategy_id} {intent.reason} px={price_s} qty={size_s} "
            f"id={order_id} usd≈{(price*qty):.2f} reg={s.reg.name} p={s.reg.p_trend:.2f} "
            f"chop={s.reg.p_chop:.2f} tp1={s.tp1_eff:.2%} tp2={s.tp2_eff:.2%} "
            f"tp_mode={CFG.tp_mode} vol={s.vol_t if s.vol_t is not None else '-'} "
            f"norm={s.vol_norm if s.vol_norm is not None else '-'}",
        )
    except Exception as e:
        await LOG.log("WARN", f"ENTRY_FAIL {e} ({intent.reason})")
        if _is_insufficient_balance(e):
            st.force_bal_refresh = True
            return
        add_error(st, e)


# ----------------------------
# TP order placement
# ----------------------------
async def place_tp_orders(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot
) -> None:
    """Place resting TP1/TP2 as maker postOnly, side-aware for LONG/SHORT margin."""
    if st.avg_cost is None or st.position_qty <= 0:
        return
    if st.tp1_order and st.tp2_order:
        return
    if (st.tp1_order is None and st.tp2_order is None) and (
        getattr(s, "open_orders", 0) > 0
        or bool(getattr(s, "margin_symbol_active", False))
        or int(getattr(s, "tracked_orders_active", 0) or 0) > 0
    ):
        await LOG.log(
            "WARN",
            f"TP_PLACE_BLOCKED_OPEN_TRUTH open_orders={getattr(s,'open_orders',0)} "
            f"sym_open={int(bool(getattr(s,'margin_symbol_active',False)))} "
            f"tracked={int(getattr(s,'tracked_orders_active',0) or 0)}",
        )
        return

    side_pos = st.position_side or s.pos_side
    if side_pos not in ("LONG", "SHORT"):
        return

    pos_qty = q_down(st.position_qty, meta.base_increment)
    if pos_qty < meta.base_min_size:
        return

    existing_tp_qty = D0
    if st.tp1_order is not None:
        existing_tp_qty += st.tp1_order.size or D0
    if st.tp2_order is not None:
        existing_tp_qty += st.tp2_order.size or D0
    remaining_qty = q_down(max(D0, pos_qty - existing_tp_qty), meta.base_increment)
    if (st.tp1_order is None) ^ (st.tp2_order is None):
        if remaining_qty < meta.base_min_size:
            return

    tp1_eff = st.trade_tp1_eff if st.trade_tp1_eff is not None else s.tp1_eff
    tp2_eff = st.trade_tp2_eff if st.trade_tp2_eff is not None else s.tp2_eff
    tp1_eff, tp2_eff = adjust_tp_for_strategy(st, s, tp1_eff, tp2_eff)

    if st.tp1_order is None and st.tp2_order is None:
        pos_qty_eff = q_down(pos_qty * getattr(CFG, "tp_balance_buffer_pct", D1), meta.base_increment)
        tp1_qty = q_down(pos_qty_eff * CFG.tp_split_1, meta.base_increment)
        tp2_qty = q_down(max(D0, pos_qty_eff - tp1_qty), meta.base_increment)
        if tp1_qty < meta.base_min_size and tp2_qty >= meta.base_min_size:
            tp2_qty = q_down(pos_qty, meta.base_increment); tp1_qty = D0
        if tp2_qty < meta.base_min_size and tp1_qty >= meta.base_min_size:
            tp1_qty = q_down(pos_qty, meta.base_increment); tp2_qty = D0
    else:
        tp1_qty = remaining_qty if st.tp1_order is None else D0
        tp2_qty = remaining_qty if st.tp2_order is None else D0

    auto_repay = bool(getattr(CFG, "margin_autorepay_on_exit", True))
    if side_pos == "LONG":
        side = "sell"
        tp1_px = q_down(st.avg_cost * (D1 + tp1_eff), meta.price_increment)
        tp2_px = q_down(st.avg_cost * (D1 + tp2_eff), meta.price_increment)
    else:
        side = "buy"
        tp1_px = q_down(st.avg_cost * (D1 - tp1_eff), meta.price_increment)
        tp2_px = q_down(st.avg_cost * (D1 - tp2_eff), meta.price_increment)

    try:
        if side == "sell" and tp1_px <= s.bid:
            await execute_exit_ladder(cli, meta, st, s, kind="TP", why="tp_cross_market")
            return
        if side == "buy" and tp1_px >= s.ask:
            await execute_exit_ladder(cli, meta, st, s, kind="TP", why="tp_cross_market")
            return
    except Exception:
        pass

    async def _place_one(purpose: str, px: Decimal, qty: Decimal):
        if qty <= 0:
            return None
        oid = client_oid(_order_tag(st, purpose[-1]))
        order_id = await rest_to_thread(
            cli.place_limit_any, CFG.symbol, side,
            to_str_q(px, meta.price_increment), to_str_q(qty, meta.base_increment),
            oid, bool(getattr(CFG, "post_only", True)), False, auto_repay,
        )
        return OrderRef(order_id, oid, side, px, qty, now_ts(), purpose)

    try:
        if tp1_qty >= meta.base_min_size:
            st.tp1_order = await _place_one("TP1", tp1_px, tp1_qty)
            await LOG.log("INFO", f"TP1_SENT side={side} px={tp1_px} qty={tp1_qty} tp={(tp1_eff*100):.2f}%")
        if tp2_qty >= meta.base_min_size:
            st.tp2_order = await _place_one("TP2", tp2_px, tp2_qty)
            await LOG.log("INFO", f"TP2_SENT side={side} px={tp2_px} qty={tp2_qty} tp={(tp2_eff*100):.2f}%")
        if st.tp1_order is not None or st.tp2_order is not None:
            st.last_tp_placed_ts = now_ts()
            st.tp_zero_open_confirm_count = 0
    except Exception as e:
        now = now_ts()
        if _is_balance_insufficient(e):
            if (now - st.last_tp_place_fail_ts) >= 15:
                st.last_tp_place_fail_ts = now
                consec = int(getattr(st, "_tp_fail_consec", 0)) + 1
                st._tp_fail_consec = consec  # type: ignore[attr-defined]
                if consec >= 5:
                    await LOG.log("WARN", f"TP_PLACE_FAIL_PERSISTENT count={consec} {e}")
                else:
                    await LOG.log("WARN", f"TP_PLACE_FAIL {e}")
            return
        try:
            st._tp_fail_consec = 0  # type: ignore[attr-defined]
        except Exception:
            pass
        await LOG.log("WARN", f"TP_PLACE_FAIL {e}")
        add_error(st, e)


# ----------------------------
# Floating TP maintenance
# ----------------------------
async def maybe_update_tp_orders_float(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot
) -> None:
    """Floating TP (v3) SOM — asymmetric thresholds, throttled, partial-fill safe."""
    if st.exit_order is not None or st.mode == "EXIT_PENDING" or st.exit_inflight:
        return
    if st.mode not in ("IN_POSITION", "IN_POSITION_RECOVER"):
        return
    if st.mode == "IN_POSITION_RECOVER" and st.avg_cost is not None and st.entry_order is None:
        st.mode = "IN_POSITION"
    if st.avg_cost is None or st.position_qty <= 0:
        return

    side_pos = st.position_side or s.pos_side
    if side_pos not in ("LONG", "SHORT"):
        return

    now = now_ts()
    if st.tp1_order is None and st.tp2_order is None and not st.exit_inflight:
        if (
            getattr(s, "open_orders", 0) > 0
            or bool(getattr(s, "margin_symbol_active", False))
            or int(getattr(s, "tracked_orders_active", 0) or 0) > 0
        ):
            if (now - float(getattr(st, "last_margin_truth_log_ts", 0.0) or 0.0)) >= 60.0:
                st.last_margin_truth_log_ts = now
                await LOG.log("WARN", f"TP_SEED_BLOCKED_OPEN_TRUTH open={getattr(s,'open_orders',0)}")
            return
        await place_tp_orders(cli, meta, st, s)
        return

    global_min_interval = min(
        float(getattr(CFG, "tp_modify_min_interval_sec", 60)),
        float(getattr(CFG, "tp_modify_min_interval_expand_sec", 60)),
        float(getattr(CFG, "tp_modify_min_interval_contract_sec", 10)),
    )
    if (now - float(getattr(st, "last_tp_modify_ts", 0.0) or 0.0)) < global_min_interval:
        return

    tp1_eff = st.trade_tp1_eff if st.trade_tp1_eff is not None else s.tp1_eff
    tp2_eff = st.trade_tp2_eff if st.trade_tp2_eff is not None else s.tp2_eff

    if side_pos == "LONG":
        side = "sell"
        new_tp1_px = q_down(st.avg_cost * (D1 + tp1_eff), meta.price_increment)
        new_tp2_px = q_down(st.avg_cost * (D1 + tp2_eff), meta.price_increment)
        _tp_floor = q_down(st.avg_cost * (D1 + s.tp_req), meta.price_increment)
        try:
            if st.peak_price is not None and st.avg_cost is not None and st.peak_price > st.avg_cost:
                _hwm_trail = Decimal(str(getattr(CFG, "tp_hwm_trail_pct", Decimal("0.0030"))))
                _tp_hwm_floor = q_down(st.peak_price * (D1 - _hwm_trail), meta.price_increment)
                if _tp_hwm_floor > _tp_floor:
                    _tp_floor = _tp_hwm_floor
        except Exception:
            pass
        new_tp1_px = max(new_tp1_px, _tp_floor)
        new_tp2_px = max(new_tp2_px, _tp_floor)
    else:
        side = "buy"
        new_tp1_px = q_down(st.avg_cost * (D1 - tp1_eff), meta.price_increment)
        new_tp2_px = q_down(st.avg_cost * (D1 - tp2_eff), meta.price_increment)
        _tp_ceil = q_down(st.avg_cost * (D1 - s.tp_req), meta.price_increment)
        try:
            if st.peak_price is not None and st.avg_cost is not None and st.peak_price < st.avg_cost:
                _hwm_trail = Decimal(str(getattr(CFG, "tp_hwm_trail_pct", Decimal("0.0030"))))
                _tp_hwm_ceil = q_up(st.peak_price * (D1 + _hwm_trail), meta.price_increment)
                if _tp_hwm_ceil < _tp_ceil:
                    _tp_ceil = _tp_hwm_ceil
        except Exception:
            pass
        new_tp1_px = min(new_tp1_px, _tp_ceil)
        new_tp2_px = min(new_tp2_px, _tp_ceil)
        _tp_ceil_static = q_up(st.avg_cost * (D1 - s.tp_req), meta.price_increment)
        new_tp1_px = min(new_tp1_px, _tp_ceil_static)
        new_tp2_px = min(new_tp2_px, _tp_ceil_static)

    try:
        if side == "sell" and new_tp1_px <= s.bid:
            await execute_exit_ladder(cli, meta, st, s, kind="TP", why="tp_cross_market_float")
            return
        if side == "buy" and new_tp1_px >= s.ask:
            await execute_exit_ladder(cli, meta, st, s, kind="TP", why="tp_cross_market_float")
            return
    except Exception:
        pass

    auto_repay = bool(getattr(CFG, "margin_autorepay_on_exit", True))

    async def _maybe_move(ref_name: str, desired_px: Decimal, purpose: str):
        ref: Optional[OrderRef] = getattr(st, ref_name)
        if ref is None:
            return
        age = now - float(ref.created_ts or 0.0)
        if age < float(CFG.tp_reprice_min_age_sec):
            return
        try:
            od = await rest_to_thread(cli.get_order_any, CFG.symbol, ref.order_id)
            is_active = bool(od.get("isActive", od.get("active", False)))
            deal_sz = Decimal(str(od.get("dealSize", "0")))
            if is_active and deal_sz > 0:
                return
        except Exception:
            return

        old_px = ref.price if ref.price else desired_px
        if old_px <= 0:
            old_px = desired_px

        expansion = (desired_px > old_px) if side_pos == "LONG" else (desired_px < old_px)
        thr = CFG.tp_float_expansion_threshold_pct if expansion else CFG.tp_float_contraction_threshold_pct
        min_interval = (CFG.tp_modify_min_interval_expand_sec if expansion else CFG.tp_modify_min_interval_contract_sec)
        if (now - float(getattr(st, "last_tp_modify_ts", 0.0) or 0.0)) < float(min_interval):
            return

        move_pct = abs(desired_px - old_px) / max(old_px, meta.price_increment)
        if move_pct < thr:
            return

        # Queue guard: do not expand TP when price is already within queue-guard fraction
        try:
            tp_dist = abs(desired_px - s.px)
            total_dist = abs(old_px - s.px)
            if expansion and total_dist > 0 and (tp_dist / total_dist) < float(getattr(CFG, "tp_queue_guard_frac", Decimal("0.15"))):
                return
        except Exception:
            pass

        ok = await _safe_cancel(cli, ref.order_id, st, purpose)
        if not ok:
            return
        oid = client_oid(_order_tag(st, purpose[-1]))
        try:
            order_id = await rest_to_thread(
                cli.place_limit_any, CFG.symbol, side,
                to_str_q(desired_px, meta.price_increment),
                to_str_q(ref.size, meta.base_increment),
                oid, bool(getattr(CFG, "post_only", True)), False, auto_repay,
            )
            new_ref = OrderRef(order_id, oid, side, desired_px, ref.size, now_ts(), purpose)
            setattr(st, ref_name, new_ref)
            st.last_tp_modify_ts = now_ts()
            st.last_tp_placed_ts = now_ts()
            st.tp_zero_open_confirm_count = 0
            await LOG.log(
                "INFO",
                f"TP_FLOAT {purpose} old={old_px:.2f} new={desired_px:.2f} "
                f"{'expand' if expansion else 'contract'} move={move_pct:.4f}",
            )
        except Exception as e:
            await LOG.log("WARN", f"TP_FLOAT_FAIL {purpose} {e}")
            add_error(st, e)

    await _maybe_move("tp1_order", new_tp1_px, "TP1")
    await _maybe_move("tp2_order", new_tp2_px, "TP2")


# ----------------------------
# Entry maintenance (queue-loss decay + taker escalation)
# ----------------------------
async def maintain_entry_order(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot
) -> None:
    if st.mode != "ENTRY_PENDING" or st.entry_order is None:
        return
    ref = st.entry_order
    side = ref.side
    bid, ask = s.bid, s.ask
    now = now_ts()
    age = now - ref.created_ts

    if age < float(CFG.entry_decay_start_sec):
        return
    if (now - float(getattr(st, "entry_last_replace_ts", 0.0) or 0.0)) < float(CFG.entry_decay_step_sec):
        return

    # TTL check
    if age >= float(CFG.entry_ttl_sec):
        ok = await _safe_cancel(cli, ref.order_id, st, "ENTRY")
        if ok:
            st.entry_order = None
            st.mode = "FLAT"
            await LOG.log("INFO", f"ENTRY_TTL_CANCEL age={int(age)}s -> FLAT")
        return

    high_alpha = st.entry_intent_urg >= 2 and st.entry_intent_tag in ("MOMO", "VBRK", "SFOL")  # [V7.3.5] added SFOL
    queue_lost = False
    try:
        if side == "buy":
            queue_lost = bid >= (ref.price + meta.price_increment * Decimal(CFG.entry_queue_lost_ticks))
        else:
            queue_lost = ask <= (ref.price - meta.price_increment * Decimal(CFG.entry_queue_lost_ticks))
    except Exception:
        pass

    if not queue_lost and not high_alpha:
        return

    try:
        if st.entry_replace_count >= CFG.entry_max_replaces:
            return

        auto_borrow = (side == "sell" and bool(getattr(CFG, "margin_autoborrow_short", True))) or (
            side == "buy" and bool(getattr(CFG, "margin_autoborrow_long", False))
        )

        # Taker escalation for high-alpha intents
        if high_alpha and age >= CFG.entry_to_taker_after_sec and s.spread_pct <= CFG.entry_taker_max_spread_pct:
            ok = await _safe_cancel(cli, ref.order_id, st, "ENTRY")
            if not ok:
                await LOG.log("WARN", f"ENTRY_ESCALATE_SKIP cancel_failed id={ref.order_id}")
                return
            px = q_up(ask, meta.price_increment) if side == "buy" else q_down(bid, meta.price_increment)
            oid = client_oid(st.entry_intent_tag or "ENT")
            order_id = await rest_to_thread(
                cli.place_limit_any, CFG.symbol, side,
                to_str_q(px, meta.price_increment), to_str_q(ref.size, meta.base_increment),
                oid, False, auto_borrow, False,
            )
            st.entry_order = OrderRef(order_id, oid, side, px, ref.size, now_ts(), "ENTRY")
            st.entry_last_replace_ts = s.ts
            st.entry_replace_count += 1
            st.last_entry_attempt_ts = now_ts()
            st.entry_price_hint = px
            st.entry_qty_hint = ref.size
            st.entry_side_hint = side
            await LOG.log("INFO", f"ENTRY_ESCALATE_TAKER side={side} tag={st.entry_intent_tag} age={int(age)}s px={px}")
            return

        # Re-peg maker
        ok = await _safe_cancel(cli, ref.order_id, st, "ENTRY")
        if not ok:
            await LOG.log("WARN", f"ENTRY_REPEG_SKIP cancel_failed id={ref.order_id}")
            return

        if side == "buy":
            px = q_down(bid, meta.price_increment)
        else:
            px = q_down(ask, meta.price_increment)
            if px <= bid:
                px = q_down(bid + meta.price_increment, meta.price_increment)

        if px <= 0:
            return

        if st.entry_intent_tag in ("DIP", "SQMR"):
            try:
                if side == "buy":
                    delta_ticks = int((px - ref.price) / meta.price_increment)
                else:
                    delta_ticks = int((ref.price - px) / meta.price_increment)
                if delta_ticks > int(CFG.entry_repeg_max_ticks):
                    st.entry_order = None
                    st.mode = "FLAT"
                    st.entry_replace_count = 0
                    st.entry_last_replace_ts = s.ts
                    await LOG.log(
                        "INFO",
                        f"ENTRY_ABORT_CHASE side={side} tag={st.entry_intent_tag} "
                        f"age={int(age)}s delta_ticks={delta_ticks}",
                    )
                    return
            except Exception:
                pass

        oid = client_oid(st.entry_intent_tag or "ENT")
        order_id = await rest_to_thread(
            cli.place_limit_any, CFG.symbol, side,
            to_str_q(px, meta.price_increment), to_str_q(ref.size, meta.base_increment),
            oid, True, auto_borrow, False,
        )
        st.entry_order = OrderRef(order_id, oid, side, px, ref.size, now_ts(), "ENTRY")
        st.entry_last_replace_ts = s.ts
        st.entry_replace_count += 1
        await LOG.log(
            "INFO", f"ENTRY_REPEG side={side} age={int(age)}s queue_lost={queue_lost} new_px={px} cnt={st.entry_replace_count}"
        )
    except Exception as e:
        await LOG.log("WARN", f"ENTRY_MAINT_FAIL {e}")


# ----------------------------
# Stale-order TTL cancellation
# ----------------------------
async def cancel_if_stale(cli: KuCoinClient, st: BotState) -> None:
    """Cancel stale orders with purpose-aware TTL."""
    ts = now_ts()
    for ref_name in ("entry_order", "tp1_order", "tp2_order"):
        ref: Optional[OrderRef] = getattr(st, ref_name)
        if not ref:
            continue
        if ref.purpose == "ENTRY":
            ttl = int(getattr(CFG, "entry_ttl_sec", 45))
        elif ref.purpose in ("TP1", "TP2"):
            ttl = int(getattr(CFG, "tp_cancel_sec", 0))
        else:
            ttl = int(getattr(CFG, "entry_ttl_sec", 45))
        if ttl <= 0:
            continue
        age = ts - ref.created_ts
        if age <= ttl:
            continue
        ok = await _safe_cancel(cli, ref.order_id, st, ref.purpose)
        if ok:
            await LOG.log("INFO", f"ORDER_CANCEL {ref.purpose} age={int(age)}s")
            setattr(st, ref_name, None)
        else:
            await LOG.log("WARN", f"ORDER_CANCEL_FAIL {ref.purpose} age={int(age)}s -> KEEP_TRACKING")
            continue
        if ref.purpose == "ENTRY":
            if st.mode == "ENTRY_PENDING":
                st.mode = "FLAT"
            if st.cooldown_until < ts:
                st.cooldown_until = ts + 10
            await LOG.log("INFO", "ENTRY_TIMEOUT_CANCEL -> FLAT")
            st.trade_tp1_eff = None; st.trade_tp2_eff = None; st.trade_tp_mode = ""
            st.entry_tp1_eff = None; st.entry_tp2_eff = None
            st.trade_tp_base = None; st.trade_vol_metric = None
            st.trade_vol_min = None; st.trade_vol_max = None; st.trade_vol_norm = None


# ----------------------------
# Order reconciliation
# ----------------------------
async def reconcile_orders(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot
) -> None:
    """Reconcile tracked orders and state transitions. Never assume fill without confirmation."""
    # Entry reconciliation
    if st.entry_order:
        try:
            od = await rest_to_thread(cli.get_order_any, CFG.symbol, st.entry_order.order_id)
            is_active = bool(od.get("isActive", od.get("active", False)))
            deal_size = Decimal(str(od.get("dealSize", "0")))
            deal_funds = Decimal(str(od.get("dealFunds", "0")))
            if not is_active and deal_size > 0:
                st.position_qty = deal_size
                st.avg_cost = (deal_funds / deal_size) if deal_size > 0 else st.avg_cost
                ent_side = st.entry_order.side if st.entry_order else None
                if ent_side == "sell":
                    st.position_side = "SHORT"; st.position_dir = -1
                else:
                    st.position_side = "LONG"; st.position_dir = 1
                st.pos_open_ts = st.pos_open_ts or now_ts()
                st.hold_until_ts = 0.0; st.last_tp_reprice_ts = 0.0
                st.peak_price = s.px; st.mode = "IN_POSITION"
                st.entry_order = None; st.last_entry_fill_ts = now_ts()
                st.last_trade_event_ts = now_ts()
                st.pending_markout_ts = now_ts() + float(getattr(CFG, "adverse_sel_markout_sec", 30))
                st.pending_markout_px = st.avg_cost
                st.pending_markout_side = st.position_side or ""
                st.entry_tp1_eff = s.tp1_eff; st.entry_tp2_eff = s.tp2_eff
                if CFG.tp_fix_on_entry:
                    st.trade_tp1_eff = s.tp1_eff; st.trade_tp2_eff = s.tp2_eff
                    st.trade_tp_mode = s.tp_mode; st.trade_tp_base = s.tp_base_dyn
                    st.trade_vol_metric = s.vol_t; st.trade_vol_min = s.vol_min
                    st.trade_vol_max = s.vol_max; st.trade_vol_norm = s.vol_norm
                    await LOG.log("INFO", f"TP_FREEZE_ENTRY tp1={(s.tp1_eff*100):.2f}% tp2={(s.tp2_eff*100):.2f}%")
                await LOG.log("INFO", f"ENTRY_FILLED qty={deal_size} avg={st.avg_cost:.2f} tp1={(s.tp1_eff*100):.2f}%")
                # [PHASE A] Record entry to ledger, update maturity, reset excursion
                if _PHASE_A_OK:
                    try:
                        _tag = str(getattr(st, "entry_intent_tag", "") or "")
                        _side = "buy" if st.position_side == "LONG" else "sell"
                        from tp import entry_expected_edge_bps as _eeb
                        from models import Intent as _Intent
                        _dummy_intent = _Intent(side=_side, strategy_id=_tag, score=0.0, urgency=0)
                        _edge = _eeb(s, _dummy_intent)
                        record_trade_quality(build_entry_taken_record(
                            ts=now_ts(), regime_name=s.reg.name,
                            p_trend=s.reg.p_trend, p_chop=s.reg.p_chop, p_breakout=s.reg.p_breakout,
                            direction_bias=int(getattr(s.reg, "direction_bias", 0) or 0),
                            worker_tag=_tag, raw_score=D0, orch_adjusted_score=D0,
                            edge_bps=_edge, side=_side, entry_px=st.avg_cost,
                            notes=f"qty={deal_size}",
                        ))
                        update_maturity_on_entry(st, _tag, _side)
                        st.best_excursion_bps = None
                        st.worst_excursion_bps = None
                    except Exception:
                        pass
            elif not is_active and deal_size == 0:
                recovered = False
                try:
                    fills = await rest_to_thread(cli.list_fills, CFG.symbol, None, 20, st.entry_order.order_id)
                    f_sz = sum(Decimal(str(f.get("size") or "0")) for f in fills)
                    f_funds = sum(Decimal(str(f.get("funds") or "0")) for f in fills)
                    if f_sz > 0:
                        st.position_qty = f_sz
                        st.avg_cost = (f_funds / f_sz) if f_sz > 0 else st.avg_cost
                        ent_side = st.entry_order.side if st.entry_order else None
                        if ent_side == "sell":
                            st.position_side = "SHORT"; st.position_dir = -1
                        else:
                            st.position_side = "LONG"; st.position_dir = 1
                        st.pos_open_ts = st.pos_open_ts or now_ts()
                        st.hold_until_ts = 0.0; st.peak_price = s.px; st.mode = "IN_POSITION"
                        st.entry_order = None; st.last_entry_fill_ts = now_ts()
                        st.last_trade_event_ts = now_ts()
                        st.pending_markout_ts = now_ts() + float(getattr(CFG, "adverse_sel_markout_sec", 30))
                        st.pending_markout_px = st.avg_cost; st.pending_markout_side = st.position_side or ""
                        st.entry_tp1_eff = s.tp1_eff; st.entry_tp2_eff = s.tp2_eff
                        await LOG.log("INFO", f"ENTRY_FILLED_RECOVER qty={f_sz} avg={st.avg_cost:.2f}")
                        # [PHASE A] Record recovered entry to ledger, reset excursion
                        if _PHASE_A_OK:
                            try:
                                _tag = str(getattr(st, "entry_intent_tag", "") or "")
                                _side = "buy" if st.position_side == "LONG" else "sell"
                                update_maturity_on_entry(st, _tag, _side)
                                st.best_excursion_bps = None
                                st.worst_excursion_bps = None
                            except Exception:
                                pass
                        recovered = True
                except Exception:
                    recovered = False
                if not recovered:
                    _entry_age = (now_ts() - st.entry_order.created_ts) if st.entry_order else 0.0
                    if _entry_age >= float(getattr(CFG, "entry_nofill_grace_sec", 4.0)):
                        _no_fill_id = st.entry_order.order_id if st.entry_order else "-"
                        st.entry_order = None; st.mode = "FLAT"
                        await LOG.log("INFO", f"ENTRY_DONE_NOFILL id={_no_fill_id}")
                    st.trade_tp1_eff = None; st.trade_tp2_eff = None; st.trade_tp_mode = ""
                    st.trade_tp_base = None; st.trade_vol_metric = None
                    st.trade_vol_min = None; st.trade_vol_max = None; st.trade_vol_norm = None
        except Exception as e:
            await LOG.log("WARN", f"ENTRY_RECON_FAIL {e}")
            add_error(st, e)

    if st.mode == "ENTRY_PENDING" and st.entry_order is None:
        try:
            q_res = s.q_total - s.q_free
        except Exception:
            q_res = Decimal("0")
        if s.open_orders == 0 and s.pos_usd < CFG.dust_notional_usd and q_res < Decimal("0.50"):
            st.mode = "FLAT"
            await LOG.log("WARN", "RECOVER_FLAT_FROM_ENTRY_PENDING")

    # Exit reconciliation
    ref = st.exit_order
    if ref is not None:
        try:
            od = await rest_to_thread(cli.get_order_any, CFG.symbol, ref.order_id)
            is_active = bool(od.get("isActive", od.get("active", False)))
            deal_size = Decimal(str(od.get("dealSize", "0")))
            if is_active:
                st.mode = "EXIT_PENDING"
            else:
                st.exit_order = None; st.exit_inflight = False; st.last_trade_event_ts = now_ts()
                if deal_size > 0:
                    st.position_qty = max(D0, st.position_qty - deal_size)
        except Exception as e:
            await LOG.log("WARN", f"EXIT_RECON_FAIL {e}")
            add_error(st, e)

    # TP reconciliation
    tp_delta_consumed = False
    for ref_name in ("tp1_order", "tp2_order"):
        ref2: Optional[OrderRef] = getattr(st, ref_name)
        if not ref2:
            continue
        try:
            od = await rest_to_thread(cli.get_order_any, CFG.symbol, ref2.order_id)
            is_active = bool(od.get("isActive", od.get("active", False)))
            deal_size = Decimal(str(od.get("dealSize", "0")))
            local_qty_before = st.position_qty
            if not is_active:
                if deal_size <= 0:
                    try:
                        fills = await rest_to_thread(cli.list_fills, CFG.symbol, None, 20, ref2.order_id)
                        deal_size = sum(Decimal(str(f.get("size") or "0")) for f in fills)
                    except Exception:
                        pass
                if deal_size <= 0 and not tp_delta_consumed:
                    try:
                        exch_qty = max(D0, Decimal(str(getattr(s, "pos_qty", D0) or D0)))
                        inferred = max(D0, local_qty_before - exch_qty)
                        if inferred >= meta.base_increment:
                            deal_size = q_down(inferred, meta.base_increment)
                            tp_delta_consumed = True
                    except Exception:
                        pass
                setattr(st, ref_name, None)
                if deal_size > 0:
                    st.position_qty = max(D0, local_qty_before - deal_size)
                st.last_trade_event_ts = now_ts()
                await LOG.log("INFO", f"{ref2.purpose}_DONE deal={deal_size} rem={st.position_qty}")
        except Exception as e:
            await LOG.log("WARN", f"{ref2.purpose}_RECON_FAIL {e}")
            add_error(st, e)

    # Flatten if TP orders cleared the position
    if st.mode.startswith("IN_POSITION") and s.open_orders == 0:
        try:
            _skip = bool(st.last_entry_fill_ts and (now_ts() - st.last_entry_fill_ts) < CFG.post_entry_balance_grace_sec)
        except Exception:
            _skip = False
        local_notional = st.position_qty * s.px
        exch_notional = s.pos_usd
        if (
            not _skip and st.exit_order is None and st.tp1_order is None and st.tp2_order is None
            and max(local_notional, exch_notional) < CFG.position_close_notional_usd
            and s.pos_qty <= (meta.base_min_size * Decimal("2"))
        ):
            _flatten_state(st)
            await LOG.log("INFO", "EXIT_FLAT_AFTER_TP")

    # Mode correction from exchange truth
    if s.pos_usd >= CFG.position_close_notional_usd:
        if (st.mode == "FLAT" and not st.entry_order) or (
            st.mode == "EXIT_PENDING" and st.exit_order is None and s.open_orders == 0
        ):
            st.mode = "IN_POSITION" if st.avg_cost is not None else "IN_POSITION_RECOVER"
            st.position_side = s.pos_side
            st.position_dir = 1 if s.pos_side == "LONG" else (-1 if s.pos_side == "SHORT" else 0)
            st.position_qty = s.pos_qty
            st.pos_open_ts = st.pos_open_ts or now_ts()
            st.peak_price = st.peak_price or s.px
            try:
                _hint_age = (now_ts() - st.last_entry_attempt_ts) if st.last_entry_attempt_ts > 0 else 999999.0
                _hint_side_ok = (st.entry_side_hint == "buy" and s.pos_side == "LONG") or (st.entry_side_hint == "sell" and s.pos_side == "SHORT")
                _qty_slop = meta.base_increment * Decimal("5")
                _qty_ok = (st.entry_qty_hint > 0) and (abs(s.pos_qty - st.entry_qty_hint) <= _qty_slop)
                if (
                    st.avg_cost is None and st.entry_price_hint is not None
                    and _hint_side_ok and _qty_ok
                    and _hint_age <= float(getattr(CFG, "recover_entry_hint_sec", 45.0))
                ):
                    st.avg_cost = st.entry_price_hint
                    st.last_trade_event_ts = now_ts()
                    st.last_entry_fill_ts = now_ts()
                    st.entry_tp1_eff = s.tp1_eff; st.entry_tp2_eff = s.tp2_eff
                    st.pending_markout_ts = now_ts() + float(getattr(CFG, "adverse_sel_markout_sec", 30))
                    st.pending_markout_px = st.avg_cost; st.pending_markout_side = s.pos_side or ""
                    await LOG.log("WARN", f"AVG_HINT_RECOVER avg={st.avg_cost:.2f} age={int(_hint_age)}s")
            except Exception:
                pass
            if st.mode == "IN_POSITION_RECOVER":
                await LOG.log("WARN", f"RECOVER_IN_POSITION pos_usd={s.pos_usd:.2f}")
            else:
                await LOG.log("WARN", f"RECOVER_FROM_EXIT_PENDING pos_usd={s.pos_usd:.2f}")

    # TP stale-clear with grace window
    try:
        _stale_clear_grace = float(getattr(CFG, "tp_stale_clear_grace_sec", 30.0))
        _since_placed = now_ts() - float(getattr(st, "last_tp_placed_ts", 0.0) or 0.0)
        _open_orders_fetch_failed = bool(getattr(s, "open_orders_fetch_failed", False))
        _tracked_active = False
        _tracked_query_failed = False
        if (
            s.pos_usd >= CFG.position_close_notional_usd and s.open_orders == 0
            and (st.tp1_order is not None or st.tp2_order is not None or st.exit_order is not None)
            and st.mode in ("IN_POSITION", "IN_POSITION_RECOVER", "EXIT_PENDING")
        ):
            for _ref_name in ("tp1_order", "tp2_order", "exit_order"):
                _ref = getattr(st, _ref_name, None)
                if _ref is None:
                    continue
                try:
                    _od = await rest_to_thread(cli.get_order_any, CFG.symbol, _ref.order_id)
                    _active = bool(_od.get("isActive", _od.get("active", False))) or bool(_od.get("inOrderBook", False))
                    _remain = Decimal(str(_od.get("remainSize", "0") or "0"))
                    _deal = Decimal(str(_od.get("dealSize", "0") or "0"))
                    _size = Decimal(str(_od.get("size", "0") or "0"))
                    if _active or _remain > 0 or (_size > 0 and _deal < _size):
                        _tracked_active = True
                        break
                except Exception:
                    _tracked_query_failed = True
                    break
        _zero_open_ok = (
            s.pos_usd >= CFG.position_close_notional_usd and s.open_orders == 0
            and st.exit_order is None and st.mode in ("IN_POSITION", "IN_POSITION_RECOVER", "EXIT_PENDING")
            and _since_placed > _stale_clear_grace and not _open_orders_fetch_failed
            and not _tracked_query_failed and not _tracked_active
            and not bool(getattr(s, "margin_symbol_active", False))
        )
        if _zero_open_ok:
            st.tp_zero_open_confirm_count = int(getattr(st, "tp_zero_open_confirm_count", 0) or 0) + 1
        else:
            st.tp_zero_open_confirm_count = 0
        if _zero_open_ok and st.tp_zero_open_confirm_count >= int(getattr(CFG, "tp_reseed_zero_open_confirm_cycles", 2)):
            if st.tp1_order is not None or st.tp2_order is not None:
                await LOG.log("WARN", f"TP_REFS_STALE_CLEAR pos_usd={s.pos_usd:.2f} since_placed={_since_placed:.0f}s confirm={st.tp_zero_open_confirm_count}")
            st.tp1_order = None; st.tp2_order = None; st.tp_zero_open_confirm_count = 0
        elif (_tracked_active or bool(getattr(s, "margin_symbol_active", False))) and (st.tp1_order is not None or st.tp2_order is not None):
            st.tp_zero_open_confirm_count = 0
    except Exception:
        pass

    # Recover FLAT when exchange confirms position gone
    if (
        s.pos_usd < CFG.position_close_notional_usd
        and st.mode in ("IN_POSITION", "EXIT_PENDING")
        and not (st.entry_order or st.tp1_order or st.tp2_order or st.exit_order)
    ):
        try:
            if st.last_entry_fill_ts and (now_ts() - st.last_entry_fill_ts) < CFG.post_entry_balance_grace_sec:
                return
        except Exception:
            pass
        _flatten_state(st)
        await LOG.log("INFO", "RECOVER_FLAT")


def _flatten_state(st: BotState) -> None:
    """Zero out all position/order state to FLAT. Called from multiple reconciliation paths."""
    st.mode = "FLAT"
    st.avg_cost = None
    st.position_qty = D0
    st.position_side = None
    st.position_dir = 0
    st.pos_open_ts = 0.0
    st.entry_order = None
    st.tp1_order = None
    st.tp2_order = None
    st.exit_order = None          # [DAILY AUDIT FIX] was missing — stale exit refs persisted
    st.exit_inflight = False
    st.exit_attempts = 0          # [DAILY AUDIT FIX] was missing — counter grew across trades
    st.peak_price = None
    st.entry_tp1_eff = None; st.entry_tp2_eff = None
    st.trade_tp1_eff = None; st.trade_tp2_eff = None; st.trade_tp_mode = ""
    st.trade_tp_base = None; st.trade_vol_metric = None
    st.trade_vol_min = None; st.trade_vol_max = None; st.trade_vol_norm = None


# ----------------------------
# Fresh exit qty from exchange truth
# ----------------------------
async def _fresh_exit_remaining_qty(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot,
    side_pos: str, exit_side: str,
) -> Tuple[Decimal, Dict[str, Decimal]]:
    """[REBUILD FIX] Refresh remaining close size from exchange truth."""
    base, quote = CFG.symbol.split("-")
    b_free, b_total, b_liab = await rest_to_thread(cli.accounts_any, base)
    q_free, q_total, q_liab = await rest_to_thread(cli.accounts_any, quote)
    net_base = b_total - b_liab
    ask_now = s.ask if (s.ask is not None and s.ask > 0) else max(s.px, meta.price_increment)
    max_buy = q_down((q_free / max(ask_now, meta.price_increment)), meta.base_increment) if ask_now > 0 else D0

    if side_pos == "LONG":
        rem = q_down(max(D0, min(max(D0, net_base), max(D0, b_free))), meta.base_increment)
    else:
        short_rem = max(D0, -net_base)
        if bool(getattr(CFG, "margin_autorepay_on_exit", True)) and short_rem > D0:
            rem = q_down(short_rem, meta.base_increment)
        else:
            rem = q_down(min(short_rem, max(D0, max_buy)), meta.base_increment)

    info = {
        "b_free": b_free, "b_total": b_total, "b_liab": b_liab,
        "q_free": q_free, "q_total": q_total, "q_liab": q_liab,
        "net_base": net_base, "max_buy": max_buy,
    }
    return rem, info


# ----------------------------
# Exit ladder
# ----------------------------
async def execute_exit_ladder(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot, kind: str, why: str
) -> None:
    """Phase 4: maker-first ladder then market — LONG/SHORT margin."""
    if s.pos_usd < CFG.dust_notional_usd:
        return
    if st.exit_inflight or st.mode == "EXIT_PENDING":
        return
    st.exit_inflight = True
    st.exit_attempts += 1
    st.last_exit_ts = now_ts()

    side_pos = st.position_side or s.pos_side
    if side_pos not in ("LONG", "SHORT"):
        st.exit_inflight = False
        return

    exit_side = "sell" if side_pos == "LONG" else "buy"
    auto_repay = bool(getattr(CFG, "margin_autorepay_on_exit", True))

    # Cancel TP orders first
    tp_cancel_ok = True
    for ref_name in ("tp1_order", "tp2_order"):
        ref: Optional[OrderRef] = getattr(st, ref_name)
        if not ref:
            continue
        ok = await _safe_cancel(cli, ref.order_id, st, ref.purpose)
        if ok:
            await LOG.log("INFO", f"CANCEL_{ref.purpose} for_exit")
            setattr(st, ref_name, None)
        else:
            tp_cancel_ok = False
            await LOG.log("WARN", f"CANCEL_{ref.purpose}_FAIL keep_tracking id={ref.order_id}")

    if not tp_cancel_ok:
        st.exit_inflight = False
        st.cooldown_until = now_ts() + min(120, int(CFG.cooldown_max_sec))
        await LOG.log("WARN", "EXIT_ABORT cancel_fail -> order_ops_degraded")
        return

    qty_info: Dict[str, Decimal] = {}
    try:
        qty, qty_info = await _fresh_exit_remaining_qty(cli, meta, st, s, side_pos, exit_side)
    except Exception:
        qty_info = {}
        truth_qty = s.pos_qty if (s.pos_qty is not None and s.pos_qty > 0) else st.position_qty
        qty = q_down(truth_qty, meta.base_increment)

    if qty < meta.base_min_size:
        # [V7.4.2 CRITICAL FIX] Was silent return → caused infinite cancel-TP loop (3846 iterations, 30+ hours stuck).
        # After TP1 partial fill, remaining qty was below base_min_size. The bot cancelled TPs,
        # hit this check, returned silently, re-placed TPs, cancelled again — forever.
        # Now: check notional, flatten if dust, escape after too many attempts.
        st.exit_inflight = False
        net_base = qty_info.get("net_base", D0)
        rem_notional = abs(net_base) * s.px if net_base != D0 else abs(st.position_qty * s.px)
        if rem_notional < CFG.position_close_notional_usd:
            _record_exit_to_ledger(st, s, kind)
            _flatten_state(st)
            st.cooldown_until = now_ts() + min(CFG.cooldown_max_sec, CFG.cooldown_base_sec)
            await LOG.log("INFO", f"EXIT_DONE_DUST_EARLY qty={qty} rem={rem_notional:.2f}")
            st.last_trade_event_ts = now_ts()
            return
        # Hard escape: if we've been trying to exit for > 50 attempts, force flatten
        if st.exit_attempts >= int(getattr(CFG, "exit_stuck_max_attempts", 50)):
            _record_exit_to_ledger(st, s, kind)
            _flatten_state(st)
            st.cooldown_until = now_ts() + min(CFG.cooldown_max_sec, CFG.cooldown_base_sec)
            await LOG.log("WARN", f"EXIT_FORCE_FLATTEN exit_attempts={st.exit_attempts} qty={qty} rem={rem_notional:.2f} — stuck loop escape")
            st.last_trade_event_ts = now_ts()
            return
        await LOG.log("WARN", f"EXIT_QTY_TOO_SMALL qty={qty} min={meta.base_min_size} rem={rem_notional:.2f} attempts={st.exit_attempts}")
        return

    # Maker attempt
    try:
        px = maker_limit_price(meta, s.bid, s.ask, exit_side)
        skew_ticks = _inventory_skew_ticks(exit_side, st, s, "exit")
        if skew_ticks < 0:
            if exit_side == "sell":
                px = q_down(max(s.bid, px + (meta.price_increment * Decimal(skew_ticks))), meta.price_increment)
            else:
                px = q_down(max(meta.price_increment, px - (meta.price_increment * Decimal(abs(skew_ticks)))), meta.price_increment)
        oid = client_oid(_order_tag(st, "X"))
        order_id = await rest_to_thread(
            cli.place_limit_any, CFG.symbol, exit_side,
            to_str_q(px, meta.price_increment), to_str_q(qty, meta.base_increment),
            oid, bool(getattr(CFG, "post_only", True)), False, auto_repay,
        )
        st.exit_order = OrderRef(order_id, oid, exit_side, px, qty, now_ts(), "EXIT")
        st.mode = "EXIT_PENDING"
        st.entry_order = None
        await LOG.log("INFO", f"EXIT_MAKER_SENT side={exit_side} {kind} {why} px={px} qty={qty} id={order_id}")
        await asyncio.sleep(CFG.time_exit_maker_emergency_sec if kind == "EMERGENCY" else CFG.time_exit_maker_sec)
    except Exception as e:
        await LOG.log("WARN", f"EXIT_MAKER_FAIL {e}")
        add_error(st, e)

    # Check if maker filled
    try:
        _b_free, _b_total, _b_liab = await rest_to_thread(cli.accounts_any, CFG.symbol.split("-")[0])
        net_base = _b_total - _b_liab
        if abs(net_base) * s.px < CFG.dust_notional_usd:
            _record_exit_to_ledger(st, s, kind)  # [PHASE A] record before flatten
            _flatten_state(st)
            st.cooldown_until = now_ts() + min(CFG.cooldown_max_sec, CFG.cooldown_base_sec)
            st.ghost_exit_order_id = ""; st.ghost_exit_side = ""
            st.ghost_exit_guard_until = 0.0; st.last_ghost_exit_poll_ts = 0.0
            await LOG.log("INFO", "EXIT_DONE_POST_MAKER")
            st.last_trade_event_ts = now_ts()
            return
    except Exception:
        pass

    # TIME exit: do not cross spread for sub-edge winners
    if kind == "TIME":
        up = s.upnl_pct if s.upnl_pct is not None else Decimal("0")
        if up < s.tp_req:
            try:
                opens = await rest_to_thread(cli.list_open_orders_any, CFG.symbol)
                for o in opens:
                    if o.get("side") == exit_side:
                        try:
                            await rest_to_thread(cli.cancel_any, CFG.symbol, o["id"])
                        except Exception:
                            pass
            except Exception:
                pass
            st.mode = "IN_POSITION"
            st.exit_inflight = False
            await LOG.log("INFO", f"TIME_EXIT_ABORT upnl={(up*100):.2f}% < tp_req={(s.tp_req*100):.2f}% -> HOLD")
            if st.exit_order is not None:
                try:
                    await rest_to_thread(cli.cancel_any, CFG.symbol, st.exit_order.order_id)
                    await LOG.log("INFO", f"EXIT_CANCEL_ON_ABORT id={st.exit_order.order_id}")
                    st.exit_order = None
                except Exception as ce:
                    await LOG.log("WARN", f"EXIT_CANCEL_ON_ABORT_FAIL {ce}")
            if st.exit_order is not None:
                return
            if st.tp1_order is None and st.tp2_order is None:
                await place_tp_orders(cli, meta, st, s)
            st.hold_until_ts = max(st.hold_until_ts, now_ts() + float(getattr(CFG, "time_exit_abort_hold_sec", 120)))
            return

    # Last-resort market order
    # [V7.4.3 FIX] Track the maker exit order as ghost BEFORE market fallback.
    # Without this, if the maker fills late after market exit, it creates a phantom position
    # that nothing tracks or cancels. Root cause of Apr-11 "wrong buy" incident.
    if st.exit_order is not None:
        st.ghost_exit_order_id = st.exit_order.order_id
        st.ghost_exit_side = st.exit_order.side
        st.ghost_exit_guard_until = now_ts() + float(getattr(CFG, "ghost_exit_guard_sec", 2400))
        st.last_ghost_exit_poll_ts = 0.0
    try:
        cancel_ok = True
        opens = await rest_to_thread(cli.list_open_orders_any, CFG.symbol)
        for o in opens:
            if o.get("side") == exit_side:
                ok = await _safe_cancel(cli, o["id"], st, "EXIT")
                cancel_ok = cancel_ok and ok

        if not cancel_ok:
            st.exit_inflight = False
            st.mode = "EXIT_PENDING"
            st.order_ops_degraded_until = max(st.order_ops_degraded_until, now_ts() + float(CFG.order_ops_degraded_sec))
            await LOG.log("WARN", "EXIT_MARKET_ABORT cancel_fail_prevent_reversal")
            return

        live_exit = []
        for _ in range(int(CFG.exit_cancel_verify_polls)):
            opens = await rest_to_thread(cli.list_open_orders_any, CFG.symbol)
            live_exit = [o for o in opens if o.get("side") == exit_side]
            if not live_exit:
                break
            await asyncio.sleep(float(CFG.exit_cancel_verify_sec) / max(1, int(CFG.exit_cancel_verify_polls)))

        if live_exit:
            st.exit_inflight = False
            st.mode = "EXIT_PENDING"
            st.order_ops_degraded_until = max(st.order_ops_degraded_until, now_ts() + float(CFG.order_ops_degraded_sec))
            await LOG.log("WARN", f"EXIT_MARKET_ABORT live_exit_orders={len(live_exit)}")
            return

        qty_mkt, qty_info2 = await _fresh_exit_remaining_qty(cli, meta, st, s, side_pos, exit_side)
        net_base_now = qty_info2.get("net_base", D0)
        if qty_mkt < meta.base_min_size:
            rem_notional = abs(net_base_now) * s.px
            if rem_notional < CFG.position_close_notional_usd:
                _record_exit_to_ledger(st, s, kind)  # [PHASE A]
                _flatten_state(st)
                await LOG.log("INFO", f"EXIT_DONE_DUST_AFTER_PARTIAL rem={rem_notional:.2f}")
                return
            # [V7.3.1 FIX] EXIT_MARKET_SKIP state race
            if st.exit_order is not None:
                await LOG.log("WARN", f"EXIT_MARKET_SKIP rem_notional={rem_notional:.2f} exit_order={st.exit_order.order_id} -> hold EXIT_PENDING")
                st.exit_inflight = False
                st.mode = "EXIT_PENDING"
            else:
                # [V7.4.2] Also add stuck escape here
                if st.exit_attempts >= int(getattr(CFG, "exit_stuck_max_attempts", 50)):
                    _record_exit_to_ledger(st, s, kind)
                    _flatten_state(st)
                    st.cooldown_until = now_ts() + min(CFG.cooldown_max_sec, CFG.cooldown_base_sec)
                    await LOG.log("WARN", f"EXIT_FORCE_FLATTEN_MARKET exit_attempts={st.exit_attempts} rem={rem_notional:.2f}")
                    st.last_trade_event_ts = now_ts()
                    return
                await LOG.log("WARN", f"EXIT_MARKET_SKIP rem_notional={rem_notional:.2f} no_exit_order -> IN_POSITION")
                st.exit_inflight = False
                st.mode = "IN_POSITION"
            return

        oid = client_oid(_order_tag(st, "MX"))
        order_id = await rest_to_thread(
            cli.place_market_any, CFG.symbol, exit_side,
            to_str_q(qty_mkt, meta.base_increment), oid, False, auto_repay,
        )
        if st.exit_order is None:
            st.ghost_exit_order_id = order_id
            st.ghost_exit_side = exit_side
            st.ghost_exit_guard_until = now_ts() + float(getattr(CFG, "ghost_exit_guard_sec", 2400))
        await LOG.log("INFO", f"EXIT_MARKET_SENT side={exit_side} {kind} {why} qty={qty_mkt} id={order_id}")
        st.exit_inflight = False
        st.exit_order = None
        _record_exit_to_ledger(st, s, kind)  # [PHASE A] record before flatten
        _flatten_state(st)
        st.cooldown_until = now_ts() + min(CFG.cooldown_max_sec, CFG.cooldown_base_sec)
        await LOG.log("INFO", "EXIT_DONE_MARKET")
        st.last_trade_event_ts = now_ts()
    except Exception as e:
        await LOG.log("WARN", f"EXIT_MARKET_FAIL {e}")
        st.exit_inflight = False
        add_error(st, e)
