#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client.py — KuCoin REST API client.

CHANGE LOG:
  MOVED    : API_BASE, WS_BASE constants (lines 1463–1464)
  MOVED    : KuCoinClient class (lines 1466–1617) — correctly inside class
  FIXED    : margin_accounts, isolated_accounts, accounts_any (lines 1661–1767 in original) were
             accidentally placed at MODULE LEVEL (outside the class body) due to an indentation
             bug. They are now properly placed INSIDE the class as they were intended to be.
             The V7.1.0 patch block (client_patch.py) still binds improved versions via setattr
             as a live safety net — that is preserved and intentional.
  MOVED    : add_error, is_transient_net_error were between client methods (lines 1622–1659);
             those are now in utils.py.
  PRESERVED: All HTTP endpoints, signatures, retry logic, and request body construction exactly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import certifi
import requests

from config import CFG
from models import SymbolMeta
from utils import D0


# ----------------------------
# KuCoin API endpoints
# ----------------------------
API_BASE = "https://api.kucoin.com"
WS_BASE = "wss://ws-api-spot.kucoin.com"


class KuCoinClient:
    def __init__(self, key: str, secret: str, passphrase: str):
        self.key = key
        self.secret = secret
        self.passphrase = passphrase
        self.session = requests.Session()
        self.session.verify = certifi.where()
        self._server_delta_ms = 0
        # Working trade type cache (updated by _kc_margin_trade_type_candidates in client_patch.py)
        self._margin_trade_type_working = CFG.margin_trade_type

    # ----------------------------
    # Auth + request
    # ----------------------------
    def _sign(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        now_ms = int(time.time() * 1000) + self._server_delta_ms
        prehash = f"{now_ms}{method.upper()}{path}{body}"
        sig = base64.b64encode(
            hmac.new(self.secret.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        passphrase = base64.b64encode(
            hmac.new(self.secret.encode(), self.passphrase.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "KC-API-KEY": self.key,
            "KC-API-SIGN": sig,
            "KC-API-TIMESTAMP": str(now_ms),
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> dict:
        """Signed request wrapper with retry/backoff."""
        method_u = method.upper()
        body = json.dumps(data, separators=(",", ":")) if (data and method_u != "GET") else ""

        request_path = path
        request_url = API_BASE + path
        if params:
            items: List[Tuple[str, Any]] = sorted((str(k), v) for k, v in params.items())
            sign_parts: List[str] = []
            for k, v in items:
                if isinstance(v, (list, tuple)):
                    for vv in v:
                        sign_parts.append(f"{k}={vv}")
                else:
                    sign_parts.append(f"{k}={v}")
            qs_sign = "&".join(sign_parts)
            qs_url = urllib.parse.urlencode(items, doseq=True)
            request_path = f"{path}?{qs_sign}"
            request_url = f"{API_BASE}{path}?{qs_url}"

        for attempt in range(CFG.rest_max_retries):
            try:
                headers = self._sign(method_u, request_path, body)
                if method_u == "GET":
                    resp = self.session.get(request_url, headers=headers, timeout=CFG.rest_timeout)
                elif method_u == "DELETE":
                    resp = self.session.delete(request_url, headers=headers, timeout=CFG.rest_timeout)
                else:
                    resp = self.session.request(
                        method_u, request_url, data=body, headers=headers, timeout=CFG.rest_timeout
                    )
                try:
                    j = resp.json()
                except Exception:
                    raise RuntimeError(f"KuCoin HTTP {resp.status_code} non-JSON: {resp.text[:200]}")
                if j.get("code") != "200000":
                    raise RuntimeError(f"KuCoin API error code={j.get('code')} msg={j.get('msg')}")
                return j
            except Exception:
                if attempt == CFG.rest_max_retries - 1:
                    raise
                time.sleep(CFG.rest_backoff_base * (2 ** attempt))
        raise RuntimeError("unreachable")

    # ----------------------------
    # Time sync
    # ----------------------------
    def time_sync(self):
        r = self.session.get(f"{API_BASE}/api/v1/timestamp", timeout=CFG.rest_timeout)
        r.raise_for_status()
        server_ms = int(r.json()["data"])
        local_ms = int(time.time() * 1000)
        self._server_delta_ms = server_ms - local_ms

    # ----------------------------
    # Symbol metadata
    # ----------------------------
    def get_symbol_meta(self, symbol: str) -> SymbolMeta:
        try:
            j = self._request("GET", f"/api/v2/symbols/{symbol}")
            it = j["data"]
            return SymbolMeta(
                symbol=symbol,
                price_increment=Decimal(it["priceIncrement"]),
                base_increment=Decimal(it["baseIncrement"]),
                min_funds=Decimal(it["minFunds"]),
                base_min_size=Decimal(it["baseMinSize"]),
            )
        except Exception:
            j = self._request("GET", "/api/v1/symbols")
            for it in j["data"]:
                if it["symbol"] == symbol:
                    return SymbolMeta(
                        symbol=symbol,
                        price_increment=Decimal(it["priceIncrement"]),
                        base_increment=Decimal(it["baseIncrement"]),
                        min_funds=Decimal(it["minFunds"]),
                        base_min_size=Decimal(it["baseMinSize"]),
                    )
            raise RuntimeError(f"symbol meta not found for {symbol}")

    # ----------------------------
    # Level 1 book
    # ----------------------------
    def level1(self, symbol: str) -> Tuple[Decimal, Decimal, Decimal]:
        j = self._request("GET", "/api/v1/market/orderbook/level1", params={"symbol": symbol})
        d = j["data"]
        bid = Decimal(d["bestBid"])
        ask = Decimal(d["bestAsk"])
        px = Decimal(d["price"]) if d.get("price") else (bid + ask) / Decimal("2")
        return px, bid, ask

    def level1_full(self, symbol: str) -> Tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        """[v5.2.0 AUDIT] REST level1 with sizes (for OBI proxy). Never throws KeyError."""
        j = self._request("GET", "/api/v1/market/orderbook/level1", params={"symbol": symbol})
        d = j.get("data", {}) or {}
        bid = Decimal(str(d.get("bestBid") or "0"))
        ask = Decimal(str(d.get("bestAsk") or "0"))
        px = (
            Decimal(str(d.get("price") or "0"))
            if d.get("price")
            else ((bid + ask) / Decimal("2") if (bid > 0 and ask > 0) else max(bid, ask, D0))
        )
        bid_sz = Decimal(str(d.get("bestBidSize") or d.get("bidSize") or "0"))
        ask_sz = Decimal(str(d.get("bestAskSize") or d.get("askSize") or "0"))
        return px, bid, ask, bid_sz, ask_sz

    # ----------------------------
    # Candle / kline data
    # ----------------------------
    def klines(
        self, symbol: str, typ: str, limit: int
    ) -> Tuple[List[Decimal], List[Decimal], List[Decimal], List[Decimal]]:
        """Returns (highs, lows, closes, volumes).
        KuCoin kline: [time, open, close, high, low, volume, turnover]
        """
        j = self._request("GET", "/api/v1/market/candles", params={"symbol": symbol, "type": typ})
        rows = j["data"][: (limit + 1)]
        rows = list(reversed(rows))
        if len(rows) > limit:
            rows = rows[:-1]  # drop latest partial candle
        highs = [Decimal(r[3]) for r in rows]
        lows = [Decimal(r[4]) for r in rows]
        closes = [Decimal(r[2]) for r in rows]
        vols = [Decimal(r[5]) for r in rows]
        return highs, lows, closes, vols

    # ----------------------------
    # Spot account balances
    # ----------------------------
    def accounts(self, currency: str) -> Tuple[Decimal, Decimal]:
        j = self._request("GET", "/api/v1/accounts", params={"currency": currency, "type": "trade"})
        free = D0
        total = D0
        for a in j["data"]:
            free += Decimal(a["available"])
            total += Decimal(a["balance"])
        return free, total

    # ----------------------------
    # Margin account helpers
    # (FIXED: originally at module level outside the class — now properly inside)
    # ----------------------------
    def margin_accounts(self) -> List[dict]:
        """Cross margin balances (assets + liabilities). GET /api/v3/margin/accounts"""
        j = self._request("GET", "/api/v3/margin/accounts")
        data = j.get("data", None)
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            maybe = data.get("accounts") or data.get("items") or data.get("list")
            if isinstance(maybe, list):
                return maybe
            out: List[dict] = []
            for _k, _v in data.items():
                if isinstance(_v, dict):
                    out.append(_v)
            return out
        return []

    def isolated_accounts(self, symbol: str, quote_currency: str) -> dict:
        """Isolated margin account info. GET /api/v3/isolated/accounts
        Uses a tiny in-process cache so build_snapshot does not double-hit REST.
        """
        now = time.time()
        cache_ts = getattr(self, "_iso_cache_ts", 0.0)
        cache_key = getattr(self, "_iso_cache_key", "")
        if (now - cache_ts) < 0.8 and cache_key == f"{symbol}|{quote_currency}":
            return getattr(self, "_iso_cache_data", {}) or {}

        params = {
            "symbol": symbol,
            "quoteCurrency": quote_currency,
            "queryType": "ISOLATED",
        }
        j = self._request("GET", "/api/v3/isolated/accounts", params=params)
        data = j.get("data", {}) or {}
        self._iso_cache_ts = now
        self._iso_cache_key = f"{symbol}|{quote_currency}"
        self._iso_cache_data = data
        return data

    def accounts_any(self, currency: str) -> Tuple[Decimal, Decimal, Decimal]:
        """Unified balance accessor. Returns (available, total, liability).

        IMPORTANT: Returns RAW values. Caller computes net = total - liability.
        """
        if str(getattr(CFG, "account_mode", "spot")).lower() != "margin":
            free, total = self.accounts(currency)
            return free, total, D0

        base, quote = CFG.symbol.split("-")

        # Isolated margin (per symbol)
        if bool(getattr(CFG, "margin_isolated", False)):
            data = self.isolated_accounts(CFG.symbol, quote)
            assets = data.get("assets", []) or []
            rec = None
            for it in assets:
                if isinstance(it, dict) and str(it.get("symbol")) == CFG.symbol:
                    rec = it
                    break
            if rec is None and assets and isinstance(assets[0], dict):
                rec = assets[0]
            if not isinstance(rec, dict):
                return D0, D0, D0

            if currency == base:
                a = rec.get("baseAsset", {}) or {}
            elif currency == quote:
                a = rec.get("quoteAsset", {}) or {}
            else:
                a = {}

            free = Decimal(str(a.get("available") or a.get("availableBalance") or "0"))
            total = Decimal(str(a.get("total") or a.get("totalBalance") or a.get("balance") or "0"))
            liab = Decimal(str(a.get("liability") or a.get("borrowed") or "0"))
            return free, total, liab

        # Cross margin (account-wide)
        free = D0
        total = D0
        liab = D0
        for a in self.margin_accounts():
            if not isinstance(a, dict):
                continue
            if str(a.get("currency")) != currency:
                continue
            free = Decimal(str(a.get("available") or a.get("availableBalance") or "0"))
            total = Decimal(str(a.get("total") or a.get("totalBalance") or a.get("balance") or "0"))
            liab = Decimal(str(a.get("liability") or a.get("borrowed") or "0"))
            break
        return free, total, liab

    # ----------------------------
    # Margin HF orders
    # ----------------------------
    def place_margin_limit(
        self,
        symbol: str,
        side: str,
        price: str,
        size: str,
        client_oid: str,
        post_only: bool,
        auto_borrow: bool = False,
        auto_repay: bool = False,
    ) -> str:
        payload = {
            "clientOid": client_oid,
            "side": side,
            "symbol": symbol,
            "type": "limit",
            "price": price,
            "size": size,
        }
        if bool(getattr(CFG, "margin_isolated", False)):
            payload["isIsolated"] = True
        if post_only:
            payload["postOnly"] = True
        if auto_borrow:
            payload["autoBorrow"] = True
        if auto_repay:
            payload["autoRepay"] = True
        j = self._request("POST", "/api/v3/hf/margin/order", data=payload)
        return j["data"]["orderId"]

    def place_margin_market(
        self,
        symbol: str,
        side: str,
        size: str,
        client_oid: str,
        auto_borrow: bool = False,
        auto_repay: bool = False,
    ) -> str:
        payload = {
            "clientOid": client_oid,
            "side": side,
            "symbol": symbol,
            "type": "market",
            "size": size,
        }
        if bool(getattr(CFG, "margin_isolated", False)):
            payload["isIsolated"] = True
        if auto_borrow:
            payload["autoBorrow"] = True
        if auto_repay:
            payload["autoRepay"] = True
        j = self._request("POST", "/api/v3/hf/margin/order", data=payload)
        return j["data"]["orderId"]

    def cancel_margin(self, symbol: str, order_id: str):
        self._request("DELETE", f"/api/v3/hf/margin/orders/{order_id}", params={"symbol": symbol})

    def get_margin_order(self, symbol: str, order_id: str) -> dict:
        j = self._request("GET", f"/api/v3/hf/margin/orders/{order_id}", params={"symbol": symbol})
        return j.get("data", {}) or {}

    def list_open_margin_orders(self, symbol: str) -> List[dict]:
        j = self._request(
            "GET",
            "/api/v3/hf/margin/orders/active",
            params={"symbol": symbol, "tradeType": getattr(CFG, "margin_trade_type", "MARGIN_TRADE")},
        )
        data = j.get("data", []) or []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            items = data.get("items") or data.get("list") or data.get("orders") or []
            if isinstance(items, list):
                return items
        return []

    def list_open_margin_order_symbols(self) -> List[str]:
        j = self._request(
            "GET",
            "/api/v3/hf/margin/order/active/symbols",
            params={"tradeType": getattr(CFG, "margin_trade_type", "MARGIN_TRADE")},
        )
        data = j.get("data", []) or []
        out: List[str] = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, str):
                    out.append(it)
                elif isinstance(it, dict):
                    sym = it.get("symbol") or it.get("symbolName")
                    if sym:
                        out.append(str(sym))
        elif isinstance(data, dict):
            items = data.get("items") or data.get("list") or data.get("symbols") or []
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict):
                        sym = it.get("symbol") or it.get("symbolName")
                        if sym:
                            out.append(str(sym))
        return out

    # ----------------------------
    # Spot orders
    # ----------------------------
    def place_limit(
        self, symbol: str, side: str, price: str, size: str, client_oid: str, post_only: bool
    ) -> str:
        payload = {
            "clientOid": client_oid,
            "side": side,
            "symbol": symbol,
            "type": "limit",
            "price": price,
            "size": size,
        }
        if post_only:
            payload["postOnly"] = True
        j = self._request("POST", "/api/v1/orders", data=payload)
        return j["data"]["orderId"]

    def place_market(self, symbol: str, side: str, size: str, client_oid: str) -> str:
        payload = {
            "clientOid": client_oid,
            "side": side,
            "symbol": symbol,
            "type": "market",
            "size": size,
        }
        j = self._request("POST", "/api/v1/orders", data=payload)
        return j["data"]["orderId"]

    def cancel(self, order_id: str):
        self._request("DELETE", f"/api/v1/orders/{order_id}")

    def get_order(self, order_id: str) -> dict:
        j = self._request("GET", f"/api/v1/orders/{order_id}")
        return j.get("data", {}) or {}

    def list_open_orders(self, symbol: str) -> List[dict]:
        j = self._request("GET", "/api/v1/orders", params={"status": "active", "symbol": symbol})
        return (j.get("data", {}) or {}).get("items", []) or []

    def list_fills(
        self,
        symbol: str,
        side: Optional[str] = None,
        page_size: int = 50,
        order_id: Optional[str] = None,
    ) -> List[dict]:
        """Recent fills for avg-cost / fill recovery."""
        params: Dict[str, Any] = {"symbol": symbol, "limit": int(page_size)}
        if side:
            params["side"] = side
        if order_id:
            params["orderId"] = order_id

        if CFG.account_mode == "margin":
            params["tradeType"] = CFG.margin_trade_type
            j = self._request("GET", "/api/v3/hf/margin/fills", params=params)
            items = (j.get("data", {}) or {}).get("items", [])
            return items if isinstance(items, list) else []

        params["tradeType"] = "TRADE"
        j = self._request("GET", "/api/v1/hf/fills", params=params)
        items = (j.get("data", {}) or {}).get("items", [])
        return items if isinstance(items, list) else []

    # ----------------------------
    # Unified wrappers (spot or margin depending on config)
    # ----------------------------
    def place_limit_any(
        self,
        symbol: str,
        side: str,
        price: str,
        size: str,
        client_oid: str,
        post_only: bool,
        auto_borrow: bool = False,
        auto_repay: bool = False,
    ) -> str:
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            return self.place_margin_limit(symbol, side, price, size, client_oid, post_only, auto_borrow, auto_repay)
        return self.place_limit(symbol, side, price, size, client_oid, post_only)

    def place_market_any(
        self,
        symbol: str,
        side: str,
        size: str,
        client_oid: str,
        auto_borrow: bool = False,
        auto_repay: bool = False,
    ) -> str:
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            return self.place_margin_market(symbol, side, size, client_oid, auto_borrow, auto_repay)
        return self.place_market(symbol, side, size, client_oid)

    def cancel_any(self, symbol: str, order_id: str):
        """Cancel wrapper that heals endpoint mismatches (spot vs margin)."""
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            try:
                return self.cancel_margin(symbol, order_id)
            except Exception as e:
                if "Only Support margin trade order" in str(e):
                    return self.cancel(order_id)
                raise
        return self.cancel(order_id)

    def get_order_any(self, symbol: str, order_id: str) -> dict:
        """Get-order wrapper with endpoint mismatch recovery."""
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            try:
                return self.get_margin_order(symbol, order_id)
            except Exception as e:
                if "Only Support margin trade order" in str(e):
                    return self.get_order(order_id)
                raise
        return self.get_order(order_id)

    def list_open_orders_any(self, symbol: str) -> List[dict]:
        """[DAILY AUDIT FIX] Never hide a margin open-order fetch failure."""
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            return self.list_open_margin_orders(symbol)
        return self.list_open_orders(symbol)

    def list_open_margin_order_symbols_any(self) -> List[str]:
        if str(getattr(CFG, "account_mode", "spot")).lower() == "margin":
            return self.list_open_margin_order_symbols()
        return []
