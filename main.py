#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — Entry point for the KuCoin bot.

CHANGE LOG:
  MOVED    : parse_args (lines 6638–6648 of original)
  MOVED    : main async function (lines 6650–6685)
  MOVED    : if __name__ == "__main__" block (lines 7003–7009)
  PRESERVED: All task wiring, offline mode branches, and signal handling exactly.

HOW TO RUN:
  Normal trading mode:
    python3 main.py

  Offline tools:
    python3 main.py --tp-float-test
    python3 main.py --backtest-compare --symbol ETH-USDT --backtest-days 30
    python3 main.py --backtest-compare --optimize-tp
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List

# ── CRITICAL: client_patch must load BEFORE anything else touches KuCoinClient.
# This binds the improved V7.1.0 method implementations (tradeType resolution,
# cache, fill recovery) onto KuCoinClient via setattr, overwriting any stale
# versions that may exist in client.py from a partial deploy.
import client_patch  # noqa: F401 — side-effect import (setattr binding)

from config import CFG, KC_API_KEY, KC_API_SECRET, KC_API_PASSPHRASE
from logger import LOG
from client import KuCoinClient
from models import MKT
from engine import (
    engine_loop,
    ws_loop,
    candle_refresh_loop,
    latency_watchdog_loop,
    self_test,
    _install_asyncio_exception_handler,
)
from backtest import run_backtest_compare, tp_float_quick_test
from utils import rest_to_thread


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="KuCoin Margin Bot V7.3.5",
        add_help=True,
    )
    p.add_argument(
        "--tp-float-test",
        action="store_true",
        help="Run a quick TP breathing visibility test (no exchange).",
    )
    p.add_argument(
        "--backtest-compare",
        action="store_true",
        help="Fetch public candles and compare STATIC vs VOL TP.",
    )
    p.add_argument(
        "--symbol",
        type=str,
        default=CFG.symbol,
        help="Symbol like ETH-USDT",
    )
    p.add_argument(
        "--backtest-days",
        type=int,
        default=45,
        help="How many days of 1m candles to fetch (approx).",
    )
    p.add_argument(
        "--backtest-trades",
        type=int,
        default=1000,
        help="Target number of trades to simulate.",
    )
    p.add_argument(
        "--mc-sims",
        type=int,
        default=3000,
        help="Monte Carlo bootstrap simulations.",
    )
    p.add_argument(
        "--mc-seed",
        type=int,
        default=7,
        help="Monte Carlo random seed.",
    )
    p.add_argument(
        "--optimize-tp",
        action="store_true",
        help="Grid-search TP vol params on the fetched data (overfit risk).",
    )
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    # Install asyncio exception handler on the running loop (Python 3.14+ compatible)
    _install_asyncio_exception_handler(asyncio.get_running_loop())

    # --- Offline audit modes (no WS, no private endpoints) ---
    if getattr(args, "tp_float_test", False):
        await tp_float_quick_test()
        return

    if getattr(args, "backtest_compare", False):
        await run_backtest_compare(
            args.symbol,
            args.backtest_days,
            args.backtest_trades,
            args.mc_sims,
            args.mc_seed,
            optimize=getattr(args, "optimize_tp", False),
        )
        return

    # --- Live trading mode ---
    if "PUT_YOUR_KEY_HERE" in KC_API_KEY or "PUT_YOUR_SECRET_HERE" in KC_API_SECRET:
        await LOG.log("ERROR", "API_KEYS_NOT_SET - edit config.py and paste your keys.")
        return

    cli = KuCoinClient(KC_API_KEY, KC_API_SECRET, KC_API_PASSPHRASE)
    meta = await self_test(cli)

    # Pre-load candle caches before the engine starts
    try:
        h, l, c, v = await rest_to_thread(cli.klines, CFG.symbol, "1min", CFG.candles_1m_limit)
        MKT.highs_1m, MKT.lows_1m, MKT.closes_1m, MKT.vols_1m = h, l, c, v
        h5, l5, c5, v5 = await rest_to_thread(cli.klines, CFG.symbol, "5min", CFG.candles_5m_limit)
        MKT.highs_5m, MKT.lows_5m, MKT.closes_5m, MKT.vols_5m = h5, l5, c5, v5
        await LOG.log("INFO", f"CANDLES_LOADED 1m={len(c)} 5m={len(c5)} close={c[-1]:.2f}")
    except Exception as e:
        await LOG.log("WARN", f"CANDLES_LOAD_FAIL {e}")

    # --- Launch all async tasks ---
    tasks = [
        asyncio.create_task(ws_loop(cli)),
        asyncio.create_task(candle_refresh_loop(cli)),
        asyncio.create_task(engine_loop(cli, meta)),
        asyncio.create_task(latency_watchdog_loop(cli)),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        args = parse_args(sys.argv[1:])
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print("bye")
