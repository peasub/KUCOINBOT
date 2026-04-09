#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py — KuCoin Bot configuration.

CHANGE LOG:
  MOVED    : Config dataclass + CFG singleton moved from kucoin_bot_V7_3_3.PY (lines 141–578)
  MOVED    : API keys (lines 134–136) — ⚠️ FLAGGED FOR REVIEW: hardcoded credentials.
             Consider loading from environment variables or a secrets file instead.
  PRESERVED: All field names, defaults, and comments exactly as in the original.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

# ----------------------------
# ⚠️  API KEYS — FLAGGED FOR REVIEW
# ----------------------------
# These credentials are hardcoded as they were in the original script ("as requested").
# KEEP THIS FILE PRIVATE. Do not commit to version control.
# For production hardening, load from environment variables or an encrypted secrets store.
KC_API_KEY        = os.getenv("KC_API_KEY",        "699186e797d7ce0001540ec5")
KC_API_SECRET     = os.getenv("KC_API_SECRET",     "79383155-0e75-4172-b3dc-2e2b86a905a8")
KC_API_PASSPHRASE = os.getenv("KC_API_PASSPHRASE", "P@uria922")


@dataclass
class Config:
    # Symbol
    symbol: str = os.getenv("BOT_SYMBOL", "ETH-USDT")

    # Account mode (spot vs margin). For bidirectional trading, use margin.
    account_mode: str = os.getenv("BOT_ACCOUNT_MODE", "margin")  # spot|margin
    enable_longs: bool = True
    enable_shorts: bool = True

    # Margin automation flags (used only when account_mode="margin")
    margin_autoborrow_long: bool = False     # typically False (longs funded by USDT)
    margin_autoborrow_short: bool = True     # True to borrow base on short entry
    margin_autorepay_on_exit: bool = True    # True to auto-repay on short exit/TP

    margin_isolated: bool = True              # True = isolated margin per symbol (recommended when you fund isolated)
    # [V7.2.5 FIX] Reverted from ISOLATED_MARGIN_TRADE: KuCoin returned 400400 "Only Support margin
    # trade order" — ISOLATED_MARGIN_TRADE is not a valid tradeType for /api/v3/hf/margin/orders/active.
    margin_trade_type: str = os.getenv("BOT_MARGIN_TRADE_TYPE", "MARGIN_TRADE")
    margin_trade_type_fallback: str = os.getenv("BOT_MARGIN_TRADE_TYPE_FALLBACK", "MARGIN_TRADE")
    margin_truth_symbol_probe: bool = True
    margin_truth_check_on_boot: bool = True
    tp_reseed_zero_open_confirm_cycles: int = 2

    # Capital
    usd_per_trade: Decimal = Decimal("50")  # adaptive multiplier applied by regime
    max_pos_usd: Decimal = Decimal("110")   # hard cap exposure

    # Dust
    dust_notional_usd: Decimal = Decimal("11")
    position_close_notional_usd: Decimal = Decimal("3")
    tp_balance_buffer_pct: Decimal = Decimal("0.998")

    # Exchange balance lag guard (seconds). Only apply local balance protection inside this window after ENTRY fill.
    balance_lag_grace_sec: int = 10

    # Master TP knob (single source of truth)
    tp_pct_base: Decimal = Decimal("0.014")  # 1.4%
    tp2_mult: Decimal = Decimal("1.75")      # TP2 = TP1 * tp2_mult
    tp_split_1: Decimal = Decimal("0.62")

    # Adaptive TP (v1): scale TP base between floor/ceiling using 5m volatility over lookback N.
    tp_mode: str = "vol"  # regime|static|vol
    tp_static_pct: Decimal = Decimal("0.0125")
    tp_fix_on_entry: bool = False

    tp_vol_metric: str = "atrp"  # atrp|bbw
    tp_vol_lookback_n: int = 100
    tp_vol_atr_len: int = 14
    tp_vol_bbw_len: int = 60
    tp_vol_floor_pct: Decimal = Decimal("0.0080")   # [AUDIT FIX RC-8] was 0.0060, now above cost floor
    tp_vol_ceiling_pct: Decimal = Decimal("0.020")
    tp_vol_gamma: Decimal = Decimal("1.0")

    # Floating TP (v3): smart order maintenance
    tp_float_expansion_threshold_pct: Decimal = Decimal("0.0030")
    tp_float_contraction_threshold_pct: Decimal = Decimal("0.0040")
    tp_hwm_trail_pct: Decimal = Decimal("0.0030")
    tp_modify_min_interval_sec: int = 60

    # TP realism / maintenance
    tp_atr_cap_mult: Decimal = Decimal("12")
    tp_reprice_min_age_sec: int = 180

    # Time-exit & breakout hold behavior
    hold_extend_minutes: int = 60
    hold_extend_p_break_min: Decimal = Decimal("0.50")
    hold_extend_bbw_max: Decimal = Decimal("0.0045")
    time_exit_only_losers: bool = True
    time_exit_min_edge_mult: Decimal = Decimal("0.80")
    time_exit_maker_sec: int = 45
    time_exit_maker_emergency_sec: int = 12
    time_exit_abort_hold_sec: int = 120

    # Anti-chase filters
    trend_pullback_allow_above_ema_fast: Decimal = Decimal("0.0003")
    trend_pullback_max_premium_vwap: Decimal = Decimal("0.0035")

    # Entry regime thresholds
    bear_bias_gate_p_trend_max: Decimal = Decimal("0.55")
    trend_pullback_p_trend_min: Decimal = Decimal("0.60")
    trend_pullback_adx_min: Decimal = Decimal("23")
    trend_pullback_di_gap_min: Decimal = Decimal("3")
    dip_p_trend_min: Decimal = Decimal("0.32")
    dip_p_trend_min_chop: Decimal = Decimal("0.34")
    dip_falling_knife_ret3_max: Decimal = Decimal("0.0030")
    long_quality_enable: bool = True
    long_quality_score_min: Decimal = Decimal("0.60")
    long_quality_edge_buffer_bps: Decimal = Decimal("8")
    long_quality_ret5_floor: Decimal = Decimal("0.0045")
    short_quality_enable: bool = True
    short_quality_score_min: Decimal = Decimal("0.62")
    short_quality_edge_buffer_bps: Decimal = Decimal("8")
    short_quality_rally_ret5_max: Decimal = Decimal("0.0040")
    entry_edge_model: str = "weighted_tp"
    entry_edge_runner_credit: Decimal = Decimal("1.00")
    direction_bias_di_gap_min: Decimal = Decimal("2.0")
    direction_bias_adx_min: Decimal = Decimal("18")
    direction_bias_p_trend_min: Decimal = Decimal("0.35")
    squeeze_break_p_trend_min: Decimal = Decimal("0.60")

    # Orchestrator routing thresholds (v5.1)
    orch_trend_on: Decimal = Decimal("0.60")
    orch_chop_on: Decimal = Decimal("0.60")
    orch_vol_exp_bbw_min: Decimal = Decimal("0.012")
    orch_vol_low_bbw_max: Decimal = Decimal("0.009")
    orch_breakout_route_on: Decimal = Decimal("0.50")
    orch_neutral_enable: bool = True
    orch_neutral_min_score: Decimal = Decimal("0.78")
    orch_neutral_conflict_gap: Decimal = Decimal("0.18")
    orch_neutral_breakout_min: Decimal = Decimal("0.58")
    orch_neutral_trend_min: Decimal = Decimal("0.62")
    brain_route_soft_enable: bool = True
    brain_route_on_weight: Decimal = Decimal("1.12")
    brain_route_off_weight: Decimal = Decimal("0.82")
    short_followthrough_enable: bool = True
    short_followthrough_p_trend_min: Decimal = Decimal("0.36")
    short_followthrough_p_break_min: Decimal = Decimal("0.60")
    short_followthrough_di_gap_min: Decimal = Decimal("10")
    short_followthrough_atrp_min: Decimal = Decimal("0.00035")
    short_followthrough_vwap_ext_max: Decimal = Decimal("0.020")
    short_followthrough_rsi_max: Decimal = Decimal("42")
    short_followthrough_rsi_min: Decimal = Decimal("24")
    # [V7.2.4 FIX] Regime-adaptive RSI floor for strong TREND breakdowns.
    short_followthrough_rsi_min_strong_trend: Decimal = Decimal("16")
    short_followthrough_p_trend_strong: Decimal = Decimal("0.80")
    short_followthrough_ret3_max: Decimal = Decimal("-0.0008")
    short_followthrough_score_boost: Decimal = Decimal("0.18")
    short_followthrough_rsi_min_chop: Decimal = Decimal("24")
    short_followthrough_p_trend_min_chop: Decimal = Decimal("0.52")
    short_followthrough_p_break_min_chop: Decimal = Decimal("0.60")
    continuation_giveback_enable: bool = True
    continuation_giveback_arm_pct: Decimal = Decimal("0.0045")
    continuation_giveback_floor_pct: Decimal = Decimal("0.0015")
    continuation_giveback_p_trend_max: Decimal = Decimal("0.60")
    continuation_giveback_p_break_max: Decimal = Decimal("0.60")
    vbrk_tp_compress_enable: bool = True
    vbrk_tp_compress_short_mult: Decimal = Decimal("0.78")
    vbrk_tp_compress_short_p_trend_max: Decimal = Decimal("0.62")

    # Neutral-state side inference knobs
    neutral_meanrev_enable: bool = True
    neutral_meanrev_buy_rsi_max: Decimal = Decimal("46")
    neutral_meanrev_sell_rsi_min: Decimal = Decimal("54")
    neutral_breakout_enable: bool = True
    neutral_breakout_long_rsi_min: Decimal = Decimal("58")
    neutral_breakout_short_rsi_max: Decimal = Decimal("42")
    neutral_breakout_ret3_min: Decimal = Decimal("0.0006")
    neutral_breakout_ret3_max_short: Decimal = Decimal("-0.0006")
    neutral_continuation_adx_min: Decimal = Decimal("20")
    neutral_continuation_breakout_min: Decimal = Decimal("0.50")
    neutral_continuation_rsi_max_long: Decimal = Decimal("68")
    neutral_continuation_rsi_min_short: Decimal = Decimal("26")
    long_continuation_rsi_max_momo: Decimal = Decimal("69")
    long_continuation_rsi_max_vbrk: Decimal = Decimal("76")
    long_continuation_tp_compress_enable: bool = True
    long_continuation_tp_compress_mult: Decimal = Decimal("0.72")
    long_continuation_tp_compress_atrp_max: Decimal = Decimal("0.0014")
    long_continuation_tp_compress_p_trend_max: Decimal = Decimal("0.72")
    continuation_giveback_fee_floor_mult: Decimal = Decimal("0.75")
    continuation_dead_age_min: int = 25
    continuation_dead_best_upnl_min: Decimal = Decimal("0.0025")
    continuation_dead_cur_upnl_max: Decimal = Decimal("0.0015")
    continuation_dead_p_trend_max: Decimal = Decimal("0.55")
    continuation_dead_p_break_max: Decimal = Decimal("0.55")

    # Dip worker knobs
    dip_disc_mixed: Decimal = Decimal("0.0018")
    dip_disc_chop: Decimal = Decimal("0.0016")
    dip_rsi_max_mixed: Decimal = Decimal("46")
    dip_rsi_max_chop: Decimal = Decimal("50")
    dip_bear_adx_min: Decimal = Decimal("28")
    dip_bear_dmi_gap: Decimal = Decimal("12")
    dip_bear_relax_gap: Decimal = Decimal("6")

    # Global long-side bearish pressure veto
    long_bear_adx_min: Decimal = Decimal("25")
    long_bear_dmi_gap: Decimal = Decimal("10")
    bear_allow_breakout_min: Decimal = Decimal("0.65")

    # Symmetric short-side bullish pressure veto
    short_bull_adx_min: Decimal = Decimal("25")
    short_bull_dmi_gap: Decimal = Decimal("10")
    bull_allow_breakdown_min: Decimal = Decimal("0.65")
    bear_breakout_override_enable: bool = True

    # Post-entry balance lag grace
    post_entry_balance_grace_sec: float = 8.0

    # Trend pullback worker knobs
    trend_pullback_rsi_lo: Decimal = Decimal("40")
    trend_pullback_rsi_hi: Decimal = Decimal("60")
    trend_pullback_max_dist: Decimal = Decimal("0.0035")
    trend_pullback_require_fast_reclaim: bool = True
    trend_pullback_block_in_squeeze: bool = True

    # Momentum breakout worker knobs
    momo_p_trend_min: Decimal = Decimal("0.61")
    momo_atrp_min: Decimal = Decimal("0.0012")
    momo_rsi_min: Decimal = Decimal("66")
    momo_max_premium_vwap: Decimal = Decimal("0.0040")
    momo_vwap_premium_rsi_norm_threshold: Decimal = Decimal("72")
    momo_vwap_premium_rsi_norm_relax: Decimal = Decimal("0.0070")
    momo_size_mult: Decimal = Decimal("0.70")
    momo_use_taker: bool = True
    momo_thesis_break_min_age_min: int = 15
    momo_thesis_break_max_age_min: int = 45
    momo_thesis_break_p_trend_max: Decimal = Decimal("0.45")
    momo_thesis_break_p_break_max: Decimal = Decimal("0.35")
    momo_thesis_break_loss_pct: Decimal = Decimal("0.0055")
    momo_thesis_break_allow_neutral: bool = True
    momo_thesis_break_stale_age_min: int = 20
    momo_thesis_break_stale_loss_pct: Decimal = Decimal("0.0030")
    momo_thesis_break_stale_p_trend_max: Decimal = Decimal("0.35")
    momo_thesis_break_stale_p_break_max: Decimal = Decimal("0.40")
    momo_mixed_p_break_min: Decimal = Decimal("0.72")
    momo_mixed_rsi_min: Decimal = Decimal("70")

    # Vol breakout worker knobs
    vbrk_p_break_min: Decimal = Decimal("0.55")
    vbrk_p_trend_min: Decimal = Decimal("0.55")
    vbrk_size_mult: Decimal = Decimal("0.75")
    vbrk_use_taker: bool = True
    momo_squeeze_rsi_max: Decimal = Decimal("80")
    vbrk_squeeze_rsi_max: Decimal = Decimal("85")
    vbrk_squeeze_p_break_min: Decimal = Decimal("0.45")

    # Squeeze mean-reversion worker knobs
    sqmr_p_chop_min: Decimal = Decimal("0.65")
    sqmr_disc: Decimal = Decimal("0.0010")
    sqmr_rsi_max: Decimal = Decimal("48")
    sqmr_size_mult: Decimal = Decimal("0.85")

    # Edge floor model
    fee_buy: Decimal = Decimal("0.0010")
    fee_sell: Decimal = Decimal("0.0010")
    adverse_select_pct: Decimal = Decimal("0.0010")
    min_net_edge_pct: Decimal = Decimal("0.0042")
    max_spread_pct: Decimal = Decimal("0.0012")

    # Candle freshness gate
    candles_stale_sec_1m: int = 180
    candles_stale_sec_5m: int = 240

    # Order-ops containment
    cancel_fail_limit: int = 3
    cancel_fail_window_sec: int = 30
    order_ops_degraded_sec: int = 120

    # Cooldown
    cooldown_base_sec: int = 90
    cooldown_max_sec: int = 900

    # Holding / exit discipline
    max_hold_minutes: int = 240
    emergency_dd_floor: Decimal = Decimal("0.0105")
    emergency_dd_atrp_mult: Decimal = Decimal("5.0")

    # Indicators
    rsi_len: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    ema_fast_len: int = 20   # backwards-compatible alias
    ema_slow_len: int = 50   # backwards-compatible alias
    atr_len: int = 14
    adx_len: int = 14
    adx_trend_lo: Decimal = Decimal("14")
    adx_trend_hi: Decimal = Decimal("28")
    bbw_len: int = 60
    er_len: int = 60

    # Probabilistic Regime Matrix (v5.0)
    regime_model: str = "prob_z"
    regime_z_window: int = 100
    regime_sigmoid_k: float = 1.6
    regime_sigmoid_theta: float = 0.35
    regime_w_adx: float = 0.34
    regime_w_bbw: float = 0.28
    regime_w_er: float = 0.22
    regime_w_dmi: float = 0.16

    # Breakout/Squeeze probability
    breakout_sigmoid_k: float = 1.8
    breakout_theta_z_bbw: float = -0.40

    # Data refresh
    candles_1m_limit: int = 220
    candles_5m_limit: int = 220
    candles_refresh_sec: int = 30
    book_refresh_sec: int = 5

    # Logging cadence
    hb_sec: int = 15
    decision_sec: int = 60
    decision_sec_flat: int = 60
    decision_sec_active: int = 20

    # Activity monitor
    activity_warn_sec: int = 1800
    activity_move_thresh_pct: Decimal = Decimal("0.0065")
    activity_warn_min_gap_sec: int = 900

    # Execution
    post_only: bool = True
    entry_improve_ticks: int = 1
    entry_ttl_sec: int = 45
    entry_nofill_grace_sec: float = 10.0
    recover_entry_hint_sec: float = 45.0
    tp_cancel_sec: int = 0
    # [V7.2.4 FIX] Grace window after TP placement before TP_REFS_STALE_CLEAR can fire.
    tp_stale_clear_grace_sec: float = 90.0

    # Resilience
    rest_timeout: int = 10
    rest_max_retries: int = 3
    rest_backoff_base: float = 0.7
    ws_reconnect_base: float = 1.0
    ws_reconnect_max: float = 30.0
    error_budget_window_sec: int = 300
    error_budget_max: int = 8
    pause_after_errors_sec: int = 180
    halt_cooldown_sec: int = 60

    # Balance + book caching
    balance_refresh_sec_flat: int = 6
    balance_refresh_sec_active: int = 2

    # OBI refresh cadence
    obi_refresh_sec_flat: int = 6
    obi_refresh_sec_active: int = 2

    # Maker→taker decay / queue-loss handling
    entry_decay_start_sec: int = 6
    entry_decay_step_sec: int = 4
    entry_max_replaces: int = 4
    entry_to_taker_after_sec: int = 18
    entry_queue_lost_ticks: int = 1
    entry_repeg_max_ticks: int = 8
    entry_taker_max_spread_pct: Decimal = Decimal("0.0008")

    # Active Opportunity Cost model
    opp_idle_threshold_sec: int = 1800
    opp_decay_step: Decimal = Decimal("0.02")
    opp_decay_max: Decimal = Decimal("0.25")
    opp_decay_relax: Decimal = Decimal("0.03")
    opp_market_move5_min_pct: Decimal = Decimal("0.006")

    # Regime hysteresis
    reg_enter_trend: Decimal = Decimal("0.65")
    reg_exit_trend: Decimal = Decimal("0.60")
    reg_enter_chop: Decimal = Decimal("0.65")
    reg_exit_chop: Decimal = Decimal("0.60")
    reg_enter_break: Decimal = Decimal("0.70")
    reg_exit_break: Decimal = Decimal("0.62")
    reg_enter_squeeze_zbbw: float = -1.00
    reg_exit_squeeze_zbbw: float = -0.60

    # Dynamic orthogonalization
    regime_dyn_orthogonalize: bool = True
    regime_corr_floor: float = 0.20

    exit_cancel_verify_sec: int = 3
    exit_cancel_verify_polls: int = 6
    ghost_exit_guard_sec: int = 2400
    ghost_exit_poll_sec: int = 15

    # TP float throttles
    tp_modify_min_interval_expand_sec: int = 60
    tp_modify_min_interval_contract_sec: int = 10
    tp_queue_guard_frac: Decimal = Decimal("0.15")

    # Probability report
    enable_probability_report: bool = False
    probability_report_every_n: int = 3
    probability_report_sims: int = 200
    probability_report_max_horizon: int = 240

    # Pause behavior
    pause_entries_only: bool = True

    # Toxic-flow / adverse-selection monitor
    adverse_sel_enable: bool = True
    adverse_sel_markout_sec: int = 30
    adverse_sel_ema_alpha: Decimal = Decimal("0.35")
    adverse_sel_stop_bps: Decimal = Decimal("4.0")
    adverse_sel_min_samples: int = 3
    adverse_sel_cooldown_sec: int = 900

    # Inventory skew
    inventory_skew_enable: bool = True
    inventory_skew_ticks_max: int = 2
    inventory_skew_size_penalty: Decimal = Decimal("0.35")

    # Event-loop latency watchdog
    latency_watchdog_enable: bool = True
    latency_watchdog_sample_ms: int = 50
    latency_watchdog_window_n: int = 240
    latency_watchdog_quantile: Decimal = Decimal("0.99")
    latency_watchdog_warmup_sec: int = 120
    latency_watchdog_p999_ms: Decimal = Decimal("85")
    latency_watchdog_worst_ms: Decimal = Decimal("120")
    latency_watchdog_trip_consec: int = 3
    latency_watchdog_pause_sec: int = 90
    latency_watchdog_require_data_degrade: bool = True
    latency_watchdog_ws_age_max_sec: int = 4
    pause_log_sec: int = 15

    # ----------------------------
    # Phase A: Protections framework
    # ----------------------------
    # [PROVEN] Low conviction block — prevents entries like April 7 trade #6 (p=0.17 → EMERGENCY)
    min_p_trend_for_entry: Decimal = Decimal("0.20")

    # [INFERRED] Emergency exit directional cooldown (minutes)
    emergency_cooldown_same_dir_minutes: int = 30

    # [PROPOSED] Giveback guard — cooldown after profitable-then-loss exit
    giveback_cooldown_minutes: int = 20

    # [PROPOSED] Consecutive loss halt
    max_consecutive_losses: int = 3
    consecutive_loss_pause_minutes: int = 45

    # [PROPOSED] Daily loss halt (bps, negative)
    max_daily_loss_bps: Decimal = Decimal("-200")

    # [INFERRED] Continuation maturity — enable penalty for same-worker same-direction streaks
    maturity_penalty_enable: bool = True

    # ----------------------------
    # V7.4.1: April 8 audit fixes
    # ----------------------------
    # [PROVEN RC-4] SFOL CHOP penalty — SFOL lost -178bps across 6 CHOP trades on Apr 8
    sfol_chop_score_penalty: Decimal = Decimal("0.20")

    # [PROVEN RC-4] Steeper maturity — 0.05/step was too gentle, 4 consecutive SFOLs still entered
    maturity_penalty_per_step: Decimal = Decimal("0.10")
    maturity_max_streak: int = 3   # hard block after this many consecutive same-worker same-direction

    # [PROVEN RC-2] Faster THESIS_DEAD in CHOP — 45min too slow, damage done by then
    continuation_dead_age_min_chop: int = 25

    # [PROVEN RC-3] Dynamic GIVEBACK — trade #8 had best=91bps, exited at 50bps (gave back 45%)
    giveback_dynamic_trail_pct: Decimal = Decimal("0.65")  # floor = max(static_floor, best * this)
    giveback_dynamic_enable: bool = True

    # [PROVEN RC-1] SQMR exhaustion guard — bought into $2270 top, should have been blocked
    sqmr_exhaustion_guard_enable: bool = True
    sqmr_exhaustion_lookback: int = 30          # candles to check for recent high
    sqmr_exhaustion_proximity_bps: Decimal = Decimal("50")  # within N bps of recent high
    sqmr_exhaustion_ret5_max: Decimal = Decimal("-0.0015")   # falling from high (negative ret5)

    # [PROVEN RC-5] Max daily loss tiered recovery — blocked 87 profitable signals for 3+ hours
    daily_loss_recovery_enable: bool = True
    daily_loss_recovery_after_minutes: int = 120   # allow test trade after N minutes of halt
    daily_loss_recovery_size_mult: Decimal = Decimal("0.50")  # at reduced size


# ---------------------------------------------------------------------------
# Global singleton — PRESERVED: all modules share this one instance.
# ---------------------------------------------------------------------------
CFG = Config()
