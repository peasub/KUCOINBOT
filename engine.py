#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py — Async runtime: engine loop, WebSocket feed, candle refresh, latency watchdog.

CHANGE LOG:
  MOVED    : latency_watchdog_loop (lines 5535–5602)
  MOVED    : ws_loop (lines 5606–5665)
  MOVED    : candle_refresh_loop (lines 5670–5706)
  MOVED    : probability_report (lines 5708–5744)
  MOVED    : recover_avg_cost_if_needed (lines 5751–5797)
  MOVED    : engine_loop (lines 5800–6084)
  MOVED    : self_test (lines 6106–6134)
  MOVED    : _install_asyncio_exception_handler (lines 6136–6153)
  PRESERVED: All async task wiring, reconciliation order, heartbeat logic, latency trip,
             WS subscription format, candle rotation, and halt/recovery flow exactly.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import ssl
import time
import traceback
from decimal import Decimal
from typing import List, Optional

import certifi
import websockets

from client import KuCoinClient, API_BASE
from config import CFG
from execution import (
    cancel_if_stale,
    execute_exit_ladder,
    maintain_entry_order,
    maybe_update_tp_orders_float,
    place_entry,
    place_tp_orders,
    reconcile_orders,
)
from logger import GLOBAL_STATE_REF, LAT_WATCH, LOG, fmt_ts, now_ts
from models import BotState, MKT, Snapshot, SymbolMeta
from regime import apply_regime_hysteresis, classify_regime_C, classify_regime_prob
from snapshot import build_snapshot
from state import state_load, state_save
from strategy import (
    collect_intents,
    diagnose_no_intent,
    exit_signal,
    orchestrate,
)
from utils import D0, D1, add_error, rest_to_thread, update_adverse_selection_monitor, update_opportunity_cost

import logger as _logger_module  # needed to set GLOBAL_STATE_REF


# ----------------------------
# Latency watchdog
# ----------------------------
async def latency_watchdog_loop(cli: KuCoinClient) -> None:
    if not bool(getattr(CFG, "latency_watchdog_enable", True)):
        return
    sample_s = float(getattr(CFG, "latency_watchdog_sample_ms", 50)) / 1000.0
    q = float(getattr(CFG, "latency_watchdog_quantile", Decimal("0.99")))
    q = min(0.999, max(0.90, q))
    warmup_sec = float(getattr(CFG, "latency_watchdog_warmup_sec", 90))
    LAT_WATCH.setdefault("breach_count", 0)
    LAT_WATCH.setdefault("started_ts", now_ts())

    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(sample_s)
        lag_ms = max(0.0, ((time.perf_counter() - t0) - sample_s) * 1000.0)
        LAT_WATCH["samples"].append(lag_ms)

        if (now_ts() - float(LAT_WATCH.get("started_ts", 0.0) or 0.0)) < warmup_sec:
            continue
        if len(LAT_WATCH["samples"]) < max(25, int(getattr(CFG, "latency_watchdog_window_n", 240)) // 4):
            continue

        arr = sorted(LAT_WATCH["samples"])
        idx = min(len(arr) - 1, max(0, int(math.ceil(q * len(arr))) - 1))
        qlag = arr[idx]
        worst = arr[-1]
        thr = float(getattr(CFG, "latency_watchdog_p999_ms", Decimal("85")))
        worst_thr = float(getattr(CFG, "latency_watchdog_worst_ms", Decimal("120")))

        if qlag <= thr or worst <= worst_thr:
            LAT_WATCH["breach_count"] = 0
            continue

        now = now_ts()
        ws_age = (now - float(getattr(MKT, "last_ws_ts", 0.0) or 0.0)) if float(getattr(MKT, "last_ws_ts", 0.0) or 0.0) > 0 else 10 ** 9
        c1_age = (now - float(getattr(MKT, "last_candle_refresh_ts_1m", 0.0) or 0.0)) if float(getattr(MKT, "last_candle_refresh_ts_1m", 0.0) or 0.0) > 0 else 10 ** 9
        c5_age = (now - float(getattr(MKT, "last_candle_refresh_ts_5m", 0.0) or 0.0)) if float(getattr(MKT, "last_candle_refresh_ts_5m", 0.0) or 0.0) > 0 else 10 ** 9
        data_degraded = (
            ws_age > float(getattr(CFG, "latency_watchdog_ws_age_max_sec", 4))
            or c1_age > float(getattr(CFG, "candles_stale_sec_1m", 180))
            or c5_age > float(getattr(CFG, "candles_stale_sec_5m", 240))
        )

        st = _logger_module.GLOBAL_STATE_REF
        if bool(getattr(CFG, "latency_watchdog_require_data_degrade", True)) and (st is None or st.mode == "FLAT") and not data_degraded:
            LAT_WATCH["breach_count"] = 0
            continue

        LAT_WATCH["breach_count"] = int(LAT_WATCH.get("breach_count", 0) or 0) + 1
        if LAT_WATCH["breach_count"] < int(getattr(CFG, "latency_watchdog_trip_consec", 3)):
            continue
        if now < float(LAT_WATCH.get("tripped_until", 0.0) or 0.0):
            continue

        LAT_WATCH["tripped_until"] = now + float(getattr(CFG, "latency_watchdog_pause_sec", 90))
        LAT_WATCH["breach_count"] = 0
        try:
            if st is not None:
                st.pause_until = max(float(getattr(st, "pause_until", 0.0) or 0.0), now + float(getattr(CFG, "latency_watchdog_pause_sec", 90)))
                st.pause_reason = "latency_watchdog"
            canceled = 0
            if data_degraded and (st is None or st.mode in ("FLAT", "ENTRY_PENDING")):
                opens = await rest_to_thread(cli.list_open_orders_any, CFG.symbol)
                for o in opens:
                    try:
                        await rest_to_thread(cli.cancel_any, CFG.symbol, o["id"])
                        canceled += 1
                    except Exception:
                        pass
            await LOG.log("ERROR", f"LATENCY_KILL_SWITCH q={q:.3f} lag={qlag:.2f}ms worst={worst:.2f}ms data_degraded={int(data_degraded)} canceled={canceled}")
        except Exception as e:
            await LOG.log("ERROR", f"LATENCY_KILL_SWITCH_FAIL {e}")


# ----------------------------
# WebSocket loop
# ----------------------------
async def ws_loop(cli: KuCoinClient) -> None:
    backoff = CFG.ws_reconnect_base
    while True:
        try:
            j = await rest_to_thread(cli._request, "POST", "/api/v1/bullet-public")
            inst = j["data"]["instanceServers"][0]
            token = j["data"]["token"]
            endpoint = inst["endpoint"]
            ws_url = f"{endpoint}?token={token}"
            sslctx = ssl.create_default_context(cafile=certifi.where())
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, close_timeout=5, ssl=sslctx) as ws:
                await LOG.log("INFO", f"WS_UP endpoint={endpoint}")
                backoff = CFG.ws_reconnect_base

                sub = {
                    "id": str(__import__("uuid").uuid4()),
                    "type": "subscribe",
                    "topic": f"/market/ticker:{CFG.symbol}",
                    "privateChannel": False,
                    "response": True,
                }
                await ws.send(json.dumps(sub))

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    if data.get("type") == "message" and data.get("topic", "").startswith("/market/ticker:"):
                        d = data.get("data", {})
                        bb = d.get("bestBid")
                        ba = d.get("bestAsk")
                        lp = d.get("price")
                        if bb is not None and ba is not None:
                            bid = Decimal(str(bb))
                            ask = Decimal(str(ba))
                            MKT.bid = bid
                            MKT.ask = ask
                            if lp is not None:
                                MKT.px = Decimal(str(lp))
                            else:
                                MKT.px = (bid + ask) / Decimal("2")
                            MKT.last_ws_ts = now_ts()
        except Exception as e:
            await LOG.log("WARN", f"WS_DOWN {e} reconnect_in={backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(CFG.ws_reconnect_max, backoff * 1.6)


# ----------------------------
# Candle refresh loop
# ----------------------------
async def candle_refresh_loop(cli: KuCoinClient) -> None:
    while True:
        try:
            h, l, c, v = await rest_to_thread(cli.klines, CFG.symbol, "1min", CFG.candles_1m_limit)
            MKT.highs_1m, MKT.lows_1m, MKT.closes_1m, MKT.vols_1m = h, l, c, v
            MKT.last_candle_refresh_ts_1m = now_ts()

            h5, l5, c5, v5 = await rest_to_thread(cli.klines, CFG.symbol, "5min", CFG.candles_5m_limit)
            MKT.highs_5m, MKT.lows_5m, MKT.closes_5m, MKT.vols_5m = h5, l5, c5, v5
            MKT.last_candle_refresh_ts_5m = now_ts()

            # Regime cache (computed once per candle refresh; used by engine heartbeat)
            try:
                if CFG.regime_model == "prob_z":
                    MKT.regime_1m = classify_regime_prob(MKT.highs_1m, MKT.lows_1m, MKT.closes_1m)
                    MKT.regime_5m = classify_regime_prob(MKT.highs_5m, MKT.lows_5m, MKT.closes_5m)
                else:
                    from models import Regime
                    _unknown = Regime(name="UNKNOWN", p_trend=Decimal("0.50"), p_breakout=Decimal("0.50"),
                                      er=None, bbw=None, adx=None, di_plus=None, di_minus=None,
                                      p_chop=Decimal("0.50"), indeterminate=True, reason="insufficient_data")
                    MKT.regime_1m = classify_regime_C(MKT.highs_1m, MKT.lows_1m, MKT.closes_1m) if len(MKT.closes_1m) >= 70 else _unknown
                    MKT.regime_5m = classify_regime_C(MKT.highs_5m, MKT.lows_5m, MKT.closes_5m) if len(MKT.closes_5m) >= 70 else _unknown
            except Exception:
                MKT.regime_1m = None
                MKT.regime_5m = None

            await LOG.log("INFO", f"CANDLES_REFRESH 1m={len(c)} 5m={len(c5)} close={c[-1]:.2f}")

            if CFG.enable_probability_report:
                try:
                    stp = int(getattr(candle_refresh_loop, "_n", 0)) + 1
                    setattr(candle_refresh_loop, "_n", stp)
                    if stp % max(1, CFG.probability_report_every_n) == 0:
                        await probability_report(c, CFG.max_hold_minutes, CFG.tp_pct_base)
                except Exception:
                    pass
        except Exception as e:
            await LOG.log("WARN", f"CANDLES_REFRESH_FAIL {e}")
        await asyncio.sleep(CFG.candles_refresh_sec)


async def probability_report(closes: List[Decimal], max_hold_min: int, tp_base: Decimal) -> None:
    """[v5.2.0 AUDIT] Quick bootstrap probability — moved off the event loop (observability only)."""
    if not getattr(CFG, "enable_probability_report", False):
        return

    def _cpu(closes_: List[Decimal], max_hold_min_: int, tp_base_: Decimal):
        try:
            if len(closes_) < 60:
                return
            n = min(len(closes_) - 1, 360)
            rets = [float((closes_[-i] / closes_[-i - 1]) - D1) for i in range(n, 0, -1)]
            if not rets:
                return
            sims = int(getattr(CFG, "probability_report_sims", 200))
            horizon = min(int(getattr(CFG, "probability_report_max_horizon", 240)), min(max_hold_min_, 360))
            hit = 0
            for _ in range(sims):
                px = 1.0
                target = 1.0 + float(tp_base_)
                for _t in range(horizon):
                    px *= 1.0 + random.choice(rets)
                    if px >= target:
                        hit += 1
                        break
            p = hit / max(1, sims)
            try:
                print(f"[{fmt_ts()}] INFO PROB_SIM p_hit≈{p:.2f} sims={sims} horizon={horizon} tp_base={float(tp_base_)*100:.2f}%", flush=True)
            except Exception:
                pass
        except Exception:
            return

    await asyncio.to_thread(_cpu, closes, max_hold_min, tp_base)


# ----------------------------
# Avg-cost recovery
# ----------------------------
async def recover_avg_cost_if_needed(
    cli: KuCoinClient, meta: SymbolMeta, st: BotState, s: Snapshot
) -> None:
    """V7 avg-cost recovery using recent fills reconstructed against current net position."""
    try:
        if st.avg_cost is not None:
            return
        if st.mode not in ("IN_POSITION", "IN_POSITION_RECOVER"):
            return
        if s.pos_usd < CFG.dust_notional_usd or s.pos_side not in ("LONG", "SHORT"):
            return
        ts = now_ts()
        if ts - st.last_avg_recover_ts < 30:
            return
        st.last_avg_recover_ts = ts

        fills = await rest_to_thread(cli.list_fills, CFG.symbol, None, 200, None)
        if not fills:
            return

        target_qty = s.pos_qty
        current_side = s.pos_side
        remaining = target_qty
        cost = D0
        for f in fills:
            side = str(f.get("side", "")).lower()
            sz = Decimal(str(f.get("size") or "0"))
            px = Decimal(str(f.get("price") or "0"))
            if sz <= 0 or px <= 0:
                continue
            if current_side == "LONG" and side != "buy":
                continue
            if current_side == "SHORT" and side != "sell":
                continue
            take = min(remaining, sz)
            cost += take * px
            remaining -= take
            if remaining <= 0:
                break

        if target_qty > 0 and (target_qty - remaining) > 0:
            from utils import q_down
            from models import SymbolMeta as _SM
            st.avg_cost = q_down(cost / (target_qty - remaining), meta.price_increment)
            st.mode = "IN_POSITION"
            await LOG.log("WARN", f"AVG_RECOVER side={current_side} avg={st.avg_cost:.2f} qty≈{(target_qty-remaining)}")
    except Exception as e:
        await LOG.log("WARN", f"AVG_RECOVER_FAIL {e}")


# ----------------------------
# Main engine loop
# ----------------------------
async def engine_loop(cli: KuCoinClient, meta: SymbolMeta) -> None:
    """[v5.2.1 HOTFIX] Restored engine loop — the single trading decision dispatcher."""
    global GLOBAL_STATE_REF
    st = state_load()
    _logger_module.GLOBAL_STATE_REF = st
    if st.last_trade_event_ts <= 0:
        st.last_trade_event_ts = now_ts()
    await LOG.log("INFO", f"STATE_LOAD mode={st.mode} avg={st.avg_cost} qty={st.position_qty} cd={int(max(0, st.cooldown_until-now_ts()))}s")

    last_hb = 0.0
    last_dec = 0.0

    while True:
        try:
            ts = now_ts()

            # --- Pause handling ---
            entries_blocked = False
            if st.pause_until > ts:
                rem = max(1, int(math.ceil(st.pause_until - ts)))
                pause_reason = getattr(st, "pause_reason", "") or "error_budget"
                if CFG.pause_entries_only:
                    entries_blocked = True
                    if ts - getattr(st, "last_pause_log_ts", 0.0) >= float(getattr(CFG, "pause_log_sec", 15)):
                        st.last_pause_log_ts = ts
                        await LOG.log("WARN", f"PAUSED_ENTRIES {rem}s due to {pause_reason}")
                else:
                    await LOG.log("WARN", f"PAUSED {rem}s due to {pause_reason}")
                    await asyncio.sleep(5)
                    continue
            elif getattr(st, "pause_reason", ""):
                st.pause_reason = ""

            # --- Halt handling ---
            if st.mode == "HALTED":
                halt_until = float(getattr(st, "halt_until", 0.0) or 0.0)
                if halt_until == 0.0:
                    st.halt_until = ts + float(getattr(CFG, "halt_cooldown_sec", 60.0))  # type: ignore[attr-defined]
                    await LOG.log("ERROR", f"HALTED reason={st.halt_reason}")
                elif ts >= halt_until:
                    if len(getattr(st, "err_ts_fatal", []) or []) < CFG.error_budget_max:
                        st.mode = "FLAT"
                        st.halt_reason = ""
                        st.halt_until = 0.0  # type: ignore[attr-defined]
                        await LOG.log("WARN", "HALT_CLEARED auto_recover")
                    else:
                        st.halt_until = ts + float(getattr(CFG, "halt_cooldown_sec", 60.0))  # type: ignore[attr-defined]
                        await LOG.log("ERROR", f"HALT_EXTEND budget_not_recovered err_fatal={len(getattr(st,'err_ts_fatal',[]))}")
                await asyncio.sleep(5)
                continue

            # --- Heartbeat ---
            if (ts - last_hb) >= CFG.hb_sec:
                last_hb = ts
                s_hb = await build_snapshot(cli, meta, st)
                up_s = f"{(s_hb.upnl_pct*100):.2f}%" if s_hb.upnl_pct is not None else "-"
                sprd = float(s_hb.ask - s_hb.bid)
                _ws_age = int(ts - MKT.last_ws_ts) if MKT.last_ws_ts > 0 else 9999
                _c1_age = int(s_hb.candle_age_1m_s) if getattr(s_hb, "candle_age_1m_s", None) is not None else -1
                _c5_age = int(s_hb.candle_age_5m_s) if getattr(s_hb, "candle_age_5m_s", None) is not None else -1
                _avg_s = f"{st.avg_cost:.2f}" if st.avg_cost is not None else "-"
                _hold_s = int(ts - st.last_trade_event_ts) if st.mode in ("IN_POSITION", "IN_POSITION_RECOVER") else 0
                _errs = len(getattr(st, "err_ts_fatal", []) or [])
                _vol_s = f"{float(s_hb.vol_t):.2f}" if s_hb.vol_t is not None else "-"
                _norm_s = f"{float(s_hb.vol_norm):.2f}" if s_hb.vol_norm is not None else "-"
                await LOG.log(
                    "INFO",
                    f"HB mode={st.mode} px={s_hb.px:.2f} bid={s_hb.bid:.2f} ask={s_hb.ask:.2f} "
                    f"sprd={sprd:.2f} "
                    f"reg={s_hb.reg.name} p={s_hb.reg.p_trend:.2f} chop={s_hb.reg.p_chop:.2f} "
                    f"pb={s_hb.reg.p_breakout:.2f} dir={getattr(s_hb.reg,'direction_bias',0)} "
                    f"pos={s_hb.pos_usd:.2f} side={s_hb.pos_side or '-'} upnl={up_s} "
                    f"avg={_avg_s} hold={_hold_s}s "
                    f"tp1={(s_hb.tp1_eff*100):.2f}% tp2={(s_hb.tp2_eff*100):.2f}% "
                    f"tp_mode={s_hb.tp_mode} vol={_vol_s} "
                    f"norm={_norm_s} "
                    f"rsi={f'{s_hb.rsi:.2f}' if s_hb.rsi is not None else '-'} "
                    f"cd={s_hb.cooldown_left}s obi={f'{s_hb.obi:.2f}' if s_hb.obi is not None else '-'} "
                    f"q_free={s_hb.q_free:.2f} b_free={s_hb.b_free:.2f} open={s_hb.open_orders} "
                    f"ws_age={_ws_age}s c1={_c1_age}s c5={_c5_age}s errs={_errs}",
                )

            # --- Decision cadence ---
            dec_sec = CFG.decision_sec_active if st.mode not in ("FLAT",) else CFG.decision_sec_flat
            if (ts - last_dec) < dec_sec:
                await asyncio.sleep(1)
                continue
            last_dec = ts

            s = await build_snapshot(cli, meta, st)
            s_ws_age = int(ts - MKT.last_ws_ts) if MKT.last_ws_ts > 0 else 9999

            # --- Adverse selection monitor ---
            try:
                as_msg = update_adverse_selection_monitor(st, s)
                if as_msg:
                    if "TRIP" in as_msg:
                        st.pause_until = max(st.pause_until, now_ts() + float(getattr(CFG, "adverse_sel_cooldown_sec", 900)))
                        st.pause_reason = "adverse_selection"
                        await LOG.log("WARN", as_msg)
                    else:
                        await LOG.log("INFO", as_msg)
            except Exception:
                pass

            # --- Opportunity cost decay ---
            try:
                move5: Optional[Decimal] = None
                if len(MKT.closes_1m) >= 6:
                    c0 = MKT.closes_1m[-6]
                    c1 = MKT.closes_1m[-1]
                    move5 = abs((c1 / c0) - D1) if c0 > 0 else None
                update_opportunity_cost(st, s, move5)
            except Exception:
                pass

            # --- Activity warn ---
            try:
                if st.mode == "FLAT" and not entries_blocked and getattr(s, "open_orders", 0) == 0:
                    closes = getattr(MKT, "closes_1m", None)
                    if closes and len(closes) >= 31:
                        last = Decimal(str(closes[-1]))
                        prev = Decimal(str(closes[-31]))
                        move30 = abs((last / prev) - D1) if prev > 0 else D0
                        if move30 >= CFG.activity_move_thresh_pct and (ts - st.last_trade_event_ts) >= CFG.activity_warn_sec:
                            if (ts - st.last_activity_warn_ts) >= CFG.activity_warn_min_gap_sec:
                                st.last_activity_warn_ts = ts
                                await LOG.log("WARN", f"ACTIVITY_WARN idle={int(ts-st.last_trade_event_ts)}s move30={(move30*100):.2f}% reg={s.reg.name} p={s.reg.p_trend:.2f}")
            except Exception:
                pass

            # --- Book degraded: skip trading orders ---
            if getattr(s, "book_degraded", False):
                if (ts - float(getattr(st, "last_md_skip_log_ts", 0.0) or 0.0)) > 30:  # type: ignore[attr-defined]
                    await LOG.log("WARN", f"TRADE_SKIP book_degraded ws_age={s_ws_age}s")
                    st.last_md_skip_log_ts = ts  # type: ignore[attr-defined]
                await asyncio.sleep(1)
                continue

            # --- Ghost exit guard ---
            from execution import _guard_stale_exit_order
            ghost_exit_block = await _guard_stale_exit_order(cli, st, s)
            if ghost_exit_block and st.mode == "FLAT":
                if (ts - float(getattr(st, "last_ghost_skip_log_ts", 0.0) or 0.0)) > 30:  # type: ignore[attr-defined]
                    rem = int(max(0.0, float(getattr(st, "ghost_exit_guard_until", 0.0) or 0.0) - ts))
                    await LOG.log("WARN", f"TRADE_SKIP ghost_exit_guard id={getattr(st,'ghost_exit_order_id','')} rem={rem}s")
                    st.last_ghost_skip_log_ts = ts  # type: ignore[attr-defined]
                await asyncio.sleep(1)
                continue

            # --- Reconcile + maintenance ---
            await reconcile_orders(cli, meta, st, s)
            await cancel_if_stale(cli, st)
            if st.mode == "ENTRY_PENDING":
                await maintain_entry_order(cli, meta, st, s)

            # --- Trading decisions ---
            if st.mode == "FLAT":
                if s.cooldown_left > 0:
                    pass
                elif entries_blocked:
                    if ts - float(getattr(st, "last_entry_block_log_ts", 0.0) or 0.0) >= float(getattr(CFG, "pause_log_sec", 15)):  # type: ignore[attr-defined]
                        st.last_entry_block_log_ts = ts  # type: ignore[attr-defined]
                        await LOG.log("WARN", "ENTRY_BLOCKED pause_entries_only=1")
                else:
                    if getattr(s, "candles_stale", False):
                        await LOG.log("WARN", f"ENTRY_BLOCKED stale_candles age1={s.candle_age_1m_s}s age5={s.candle_age_5m_s}s")
                        await asyncio.sleep(1)
                        continue
                    if getattr(st, "order_ops_degraded_until", 0.0) > ts:
                        rem = int(st.order_ops_degraded_until - ts)
                        await LOG.log("WARN", f"ENTRY_BLOCKED order_ops_degraded {rem}s (cancel_fail_streak={st.cancel_fail_streak})")
                        await asyncio.sleep(1)
                        continue
                    if st.exit_inflight or st.mode == "EXIT_PENDING":
                        await LOG.log("WARN", "ENTRY_BLOCKED exit_inflight")
                        await asyncio.sleep(1)
                        continue

                    intents = collect_intents(s, st)
                    intent = orchestrate(intents, s.reg, st)

                    if not intents:
                        if (
                            (s.reg.p_trend >= Decimal("0.35") or s.reg.p_chop >= Decimal("0.60") or s.reg.p_breakout >= Decimal("0.40"))
                            and (ts - float(getattr(st, "last_no_intent_log_ts", 0.0) or 0.0)) >= 60.0  # type: ignore[attr-defined]
                        ):
                            st.last_no_intent_log_ts = ts  # type: ignore[attr-defined]
                            await LOG.log("INFO", f"ORCH_NO_INTENT reg={s.reg.name} dir={getattr(s.reg,'direction_bias',0)} p={s.reg.p_trend:.2f} pb={s.reg.p_breakout:.2f} why={diagnose_no_intent(s)}")
                    elif intent is None:
                        if (ts - float(getattr(st, "last_orch_standdown_log_ts", 0.0) or 0.0)) >= 60.0:  # type: ignore[attr-defined]
                            st.last_orch_standdown_log_ts = ts  # type: ignore[attr-defined]
                            try:
                                _leader = sorted(intents, key=lambda x: (x.score, x.urgency), reverse=True)[0]
                                _lead_txt = f"{_leader.strategy_id}:{_leader.side}:{_leader.score:.2f}:u{_leader.urgency}"
                            except Exception:
                                _lead_txt = "-"
                            await LOG.log("INFO", f"ORCH_STANDDOWN reg={s.reg.name} intents={len(intents)} top={_lead_txt}")

                    if len(intents) >= 2:
                        try:
                            ranked = sorted(intents, key=lambda x: (x.score, x.urgency), reverse=True)
                            top = ranked[0]; nxt = ranked[1]
                            if abs(float(top.score) - float(nxt.score)) <= 0.20:
                                await LOG.log("INFO", f"INTENT_CLASH top={top.strategy_id}:{top.side}:{top.score:.2f} next={nxt.strategy_id}:{nxt.side}:{nxt.score:.2f} chosen={(intent.strategy_id if intent else '-')}")
                        except Exception:
                            pass

                    if intent is not None:
                        # [AUDIT FIX RC-5] Quality gate runs inside place_entry; no double-call
                        await LOG.log("INFO", f"ENTRY_PREFLIGHT tag={intent.strategy_id} side={intent.side} score={intent.score:.2f} urg={intent.urgency}")
                        await place_entry(cli, meta, st, s, intent)

            elif st.mode in ("IN_POSITION", "IN_POSITION_RECOVER"):
                if st.avg_cost is None:
                    st.mode = "IN_POSITION_RECOVER"
                    await recover_avg_cost_if_needed(cli, meta, st, s)
                elif st.mode == "IN_POSITION_RECOVER" and st.entry_order is None:
                    st.mode = "IN_POSITION"
                    await LOG.log("INFO", f"RECOVER_MODE_PROMOTE avg={st.avg_cost:.2f}")

                if st.avg_cost is not None and (st.tp1_order is None and st.tp2_order is None):
                    await place_tp_orders(cli, meta, st, s)

                await maybe_update_tp_orders_float(cli, meta, st, s)

                action, why = exit_signal(s, st)
                if action is not None:
                    await execute_exit_ladder(cli, meta, st, s, action, why)

            state_save(st)
            await asyncio.sleep(1)

        except Exception as e:
            tb = traceback.format_exc(limit=8)
            await LOG.log("ERROR", f"ENGINE_ERR {e} | {tb}")
            add_error(st, e)
            fatal_n = len(getattr(st, "err_ts_fatal", []) or [])
            if fatal_n >= (CFG.error_budget_max * 2):
                st.mode = "HALTED"
                st.halt_reason = f"too_many_errors: {e}"
                await LOG.log("ERROR", f"HALT {st.halt_reason}")
            await asyncio.sleep(2)


# ----------------------------
# Bootstrap / self-test
# ----------------------------
async def self_test(cli: KuCoinClient) -> SymbolMeta:
    """[DAILY AUDIT FIX] Apr-01 RCA-1: Boot function-validation guard."""
    _required_fns = [
        "compute_vwap", "build_snapshot", "orchestrate", "execute_exit_ladder",
        "maybe_update_tp_orders_float", "place_tp_orders", "maintain_entry_order",
    ]
    from tp import compute_vwap  # noqa: F401
    from snapshot import build_snapshot as _bs  # noqa: F401
    from strategy import orchestrate as _orch  # noqa: F401
    for _fn_name in _required_fns:
        # Verify each function is importable (catches partial/wrong-version deploys)
        pass  # imports above already validate — if they fail, exception propagates

    await rest_to_thread(cli.time_sync)
    await LOG.log("INFO", f"TIME_SYNC delta_ms={cli._server_delta_ms}")
    meta = await rest_to_thread(cli.get_symbol_meta, CFG.symbol)
    px, bid, ask = await rest_to_thread(cli.level1, CFG.symbol)
    await LOG.log("INFO", f"SELF_TEST level1 bid={bid} ask={ask} symbol={CFG.symbol} inc_p={meta.price_increment} inc_b={meta.base_increment} minFunds={meta.min_funds}")

    if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
        await LOG.log("INFO", f"MARGIN_CFG isolated={int(bool(getattr(CFG,'margin_isolated',False)))} tradeType={getattr(CFG,'margin_trade_type','MARGIN_TRADE')}")
        if bool(getattr(CFG, "margin_truth_check_on_boot", True)):
            try:
                opens = await rest_to_thread(cli.list_open_margin_orders, CFG.symbol)
                syms = await rest_to_thread(cli.list_open_margin_order_symbols_any)
                working = str(getattr(cli, "_margin_trade_type_working", getattr(CFG, "margin_trade_type", "MARGIN_TRADE")))
                await LOG.log("INFO", f"MARGIN_TRUTH_BOOT open_n={len(opens)} sym_active={int(CFG.symbol in set(syms or []))} tradeType={working}")
            except Exception as e:
                await LOG.log("WARN", f"MARGIN_TRUTH_BOOT_FAIL {e}")
    return meta


def _install_asyncio_exception_handler(loop) -> None:
    """Install an asyncio exception handler on the running loop."""
    def handler(loop, context):
        msg = context.get("exception") or context.get("message")
        try:
            print(f"[{fmt_ts()}] ASYNCIO_ERR {msg}", flush=True)
        except Exception:
            pass
    try:
        loop.set_exception_handler(handler)
    except Exception:
        pass
