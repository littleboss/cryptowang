#!/usr/bin/env python3
"""OKX read-only client — public market data + optional simulated-account GET status.

Hard gates (enforced in code, not just in docs):
  * GET only. Any non-GET method raises before a socket is opened.
  * Trade / amend / transfer / withdraw paths are refused by an allow-list of
    read-only endpoints plus a deny-list of path fragments.
  * Public market endpoints need no keys. The private read-only path only
    activates when OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE are all set
    AND OKX_SIMULATED=1 (OKX demo trading). Live keys are refused.
  * Secrets are read from the environment only, never printed, never written.
  * stdlib only (urllib, hmac, hashlib). No SDK.

This module does not implement, wrap, or forward: place order, amend algo,
stop algo, transfer, withdrawal, sub-account funding. Do not add them here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

OKX_PUBLIC_BASE = "https://www.okx.com"
USER_AGENT = "cryptowang-readonly/0.1 (GET only; no trade)"

ENV_KEY = "OKX_API_KEY"
ENV_SECRET = "OKX_API_SECRET"
ENV_PASSPHRASE = "OKX_API_PASSPHRASE"
ENV_SIMULATED = "OKX_SIMULATED"

# Public market endpoints (no auth).
PATH_TICKER = "/api/v5/market/ticker"
PATH_CANDLES = "/api/v5/market/candles"
PATH_HISTORY_CANDLES = "/api/v5/market/history-candles"

# Private read-only endpoints (auth, GET only, OKX_SIMULATED=1 only).
PATH_ACCOUNT_BALANCE = "/api/v5/account/balance"
PATH_ACCOUNT_CONFIG = "/api/v5/account/config"
PATH_GRID_PENDING = "/api/v5/tradingBot/grid/orders-algo-pending"
PATH_GRID_HISTORY = "/api/v5/tradingBot/grid/orders-algo-history"
PATH_GRID_DETAILS = "/api/v5/tradingBot/grid/orders-algo-details"
PATH_GRID_POSITIONS = "/api/v5/tradingBot/grid/positions"

PUBLIC_READ_PATHS = frozenset({PATH_TICKER, PATH_CANDLES, PATH_HISTORY_CANDLES})
PRIVATE_READ_PATHS = frozenset(
    {
        PATH_ACCOUNT_BALANCE,
        PATH_ACCOUNT_CONFIG,
        PATH_GRID_PENDING,
        PATH_GRID_HISTORY,
        PATH_GRID_DETAILS,
        PATH_GRID_POSITIONS,
    }
)

# Belt and braces: even if someone widens the allow-list, these fragments are refused.
FORBIDDEN_PATH_FRAGMENTS = (
    "/trade/",
    "amend",
    "cancel",
    "close-position",
    "order-algo",  # POST create/stop/amend algo endpoints share this prefix
    "stop-order-algo",
    "withdraw",
    "transfer",
    "/asset/",
    "sub-account",
    "margin-balance",
    "adjust",
    "set-",
    "place",
)

POLICY = {
    "read_only": True,
    "methods_allowed": ["GET"],
    # repo convention: will_send_http refers to trading HTTP (order / amend / transfer / withdraw)
    "will_send_http": False,
    "http": "read-only GET only (public market data; optional OKX_SIMULATED=1 account/bot status)",
    "order": False,
    "amend": False,
    "transfer": False,
    "withdraw": False,
    "live_keys": "refused (OKX_SIMULATED must be 1)",
    "secrets": "env vars only; never logged, never written",
}


class ReadOnlyViolation(RuntimeError):
    """Raised before any network I/O when a call would break the read-only gate."""


class OkxApiError(RuntimeError):
    def __init__(self, code: str, msg: str, path: str):
        super().__init__(f"OKX API error {code}: {msg} ({path})")
        self.code = code
        self.msg = msg
        self.path = path


# --------------------------------------------------------------------------- gate


def assert_read_only(method: str, path: str, *, private: bool) -> None:
    """Refuse anything that is not a known read-only GET. No I/O."""
    if method.upper() != "GET":
        raise ReadOnlyViolation(f"refused: method {method!r} (GET only)")
    low = path.lower()
    for frag in FORBIDDEN_PATH_FRAGMENTS:
        if frag in low and path not in PUBLIC_READ_PATHS | PRIVATE_READ_PATHS:
            raise ReadOnlyViolation(f"refused: path {path!r} matches forbidden fragment {frag!r}")
    allowed = PRIVATE_READ_PATHS if private else PUBLIC_READ_PATHS
    if path not in allowed:
        kind = "private read-only" if private else "public market"
        raise ReadOnlyViolation(f"refused: path {path!r} not in {kind} allow-list")


def redact(value: str | None) -> str:
    if not value:
        return "<unset>"
    return f"<set:{len(value)} chars>"


# --------------------------------------------------------------------------- http


def _http_get(url: str, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 read-only GET
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except ValueError:
            raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}") from None
    return payload


def _check_payload(payload: dict, path: str) -> list:
    if str(payload.get("code")) != "0":
        raise OkxApiError(str(payload.get("code")), str(payload.get("msg")), path)
    return payload.get("data") or []


# --------------------------------------------------------------------------- public


@dataclass(frozen=True)
class CandleRow:
    """One OKX candle. Raw strings are converted to float; `confirm` kept as int."""

    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    vol: float
    confirm: int

    @classmethod
    def from_okx(cls, row: list) -> CandleRow:
        return cls(
            ts_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            vol=float(row[5]) if len(row) > 5 and row[5] != "" else 0.0,
            confirm=int(row[8]) if len(row) > 8 and row[8] != "" else 1,
        )

    def to_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "vol": self.vol,
            "confirm": self.confirm,
        }


class OkxPublicClient:
    """Unauthenticated read-only market data. No keys involved at all."""

    def __init__(self, base_url: str = OKX_PUBLIC_BASE, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_made = 0

    def _get(self, path: str, query: dict[str, str]) -> list:
        assert_read_only("GET", path, private=False)
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        payload = _http_get(url, {}, self.timeout)
        self.requests_made += 1
        return _check_payload(payload, path)

    def get_ticker(self, inst_id: str) -> dict:
        rows = self._get(PATH_TICKER, {"instId": inst_id})
        if not rows:
            raise RuntimeError(f"no ticker for {inst_id}")
        t = rows[0]
        return {
            "instId": t.get("instId"),
            "last": float(t["last"]),
            "bidPx": float(t["bidPx"]) if t.get("bidPx") else None,
            "askPx": float(t["askPx"]) if t.get("askPx") else None,
            "high24h": float(t["high24h"]) if t.get("high24h") else None,
            "low24h": float(t["low24h"]) if t.get("low24h") else None,
            "vol24h": float(t["vol24h"]) if t.get("vol24h") else None,
            "ts_ms": int(t["ts"]),
        }

    def get_candles(
        self,
        inst_id: str,
        bar: str = "5m",
        limit: int = 300,
        pages: int = 1,
        after_ms: int | None = None,
        history: bool = False,
    ) -> list[CandleRow]:
        """Return candles oldest→newest. `pages` walks back with `after` = oldest ts seen.

        history=True uses /market/history-candles (older data, max 100 per page).
        """
        path = PATH_HISTORY_CANDLES if history else PATH_CANDLES
        cap = 100 if history else 300
        if not (1 <= limit <= cap):
            raise ValueError(f"limit must be 1..{cap} for {path}")
        out: dict[int, CandleRow] = {}
        after = str(after_ms) if after_ms is not None else None
        for _ in range(max(pages, 1)):
            q = {"instId": inst_id, "bar": bar, "limit": str(limit)}
            if after:
                q["after"] = after
            rows = self._get(path, q)
            if not rows:
                break
            for r in rows:
                c = CandleRow.from_okx(r)
                out[c.ts_ms] = c
            after = rows[-1][0]
            if len(rows) < limit:
                break
        return sorted(out.values(), key=lambda c: c.ts_ms)

    def data_source_meta(self, inst_id: str, bar: str, limit: int, pages: int) -> dict:
        return {
            "kind": "okx_public_market_candles",
            "endpoint": f"GET {self.base_url}{PATH_CANDLES}",
            "auth": "none",
            "read_only": True,
            "instId": inst_id,
            "bar": bar,
            "limit": limit,
            "pages": pages,
            "requests_made": self.requests_made,
            "http_fetch": True,
        }


# --------------------------------------------------------------------------- private (read-only)


@dataclass(frozen=True)
class ReadOnlyCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    simulated: bool

    def __repr__(self) -> str:  # never leak values
        return (
            f"ReadOnlyCredentials(api_key={redact(self.api_key)}, "
            f"api_secret={redact(self.api_secret)}, passphrase={redact(self.passphrase)}, "
            f"simulated={self.simulated})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ReadOnlyCredentials | None:
        """None when any key is absent (caller should stub / skip). Raises on live keys."""
        e = os.environ if env is None else env
        key, secret, pw = e.get(ENV_KEY, ""), e.get(ENV_SECRET, ""), e.get(ENV_PASSPHRASE, "")
        if not (key and secret and pw):
            return None
        simulated = e.get(ENV_SIMULATED, "") == "1"
        if not simulated:
            raise ReadOnlyViolation(
                f"refused: {ENV_SIMULATED}=1 is required for the private read-only path "
                "(live account keys are not accepted by this tool)"
            )
        return cls(key, secret, pw, simulated=True)


def okx_timestamp(now: datetime | None = None) -> str:
    dt = now or datetime.now(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """OKX v5 signature: base64(HMAC_SHA256(secret, ts + method + path(+query) + body))."""
    msg = f"{timestamp}{method.upper()}{request_path}{body}".encode()
    return base64.b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()).decode()


class OkxReadOnlyPrivateClient:
    """GET-only account / bot status against OKX demo trading (x-simulated-trading: 1)."""

    def __init__(
        self,
        creds: ReadOnlyCredentials,
        base_url: str = OKX_PUBLIC_BASE,
        timeout: float = 10.0,
    ):
        if not creds.simulated:
            raise ReadOnlyViolation("refused: private client requires simulated credentials")
        self._creds = creds
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_made = 0

    def __repr__(self) -> str:
        return f"OkxReadOnlyPrivateClient(base_url={self.base_url!r}, creds={self._creds!r})"

    @classmethod
    def from_env(
        cls, base_url: str = OKX_PUBLIC_BASE, timeout: float = 10.0
    ) -> OkxReadOnlyPrivateClient | None:
        creds = ReadOnlyCredentials.from_env()
        return None if creds is None else cls(creds, base_url=base_url, timeout=timeout)

    def _headers(self, request_path: str) -> dict[str, str]:
        ts = okx_timestamp()
        return {
            "OK-ACCESS-KEY": self._creds.api_key,
            "OK-ACCESS-SIGN": sign(self._creds.api_secret, ts, "GET", request_path),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._creds.passphrase,
            "x-simulated-trading": "1",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, query: dict[str, str] | None = None) -> list:
        assert_read_only("GET", path, private=True)
        q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
        request_path = path + (f"?{urllib.parse.urlencode(q)}" if q else "")
        payload = _http_get(
            f"{self.base_url}{request_path}", self._headers(request_path), self.timeout
        )
        self.requests_made += 1
        return _check_payload(payload, path)

    # ---- account (GET)

    def get_balance(self, ccy: str | None = None) -> list:
        return self._get(PATH_ACCOUNT_BALANCE, {"ccy": ccy} if ccy else None)

    def get_account_config(self) -> list:
        return self._get(PATH_ACCOUNT_CONFIG)

    # ---- grid bot status (GET)

    def get_grid_pending(self, algo_ord_type: str = "grid", inst_id: str | None = None) -> list:
        return self._get(PATH_GRID_PENDING, {"algoOrdType": algo_ord_type, "instId": inst_id})

    def get_grid_history(self, algo_ord_type: str = "grid", inst_id: str | None = None) -> list:
        return self._get(PATH_GRID_HISTORY, {"algoOrdType": algo_ord_type, "instId": inst_id})

    def get_grid_details(self, algo_id: str, algo_ord_type: str = "grid") -> dict | None:
        rows = self._get(PATH_GRID_DETAILS, {"algoOrdType": algo_ord_type, "algoId": algo_id})
        return rows[0] if rows else None

    def get_grid_positions(self, algo_id: str, algo_ord_type: str = "grid") -> list:
        return self._get(PATH_GRID_POSITIONS, {"algoOrdType": algo_ord_type, "algoId": algo_id})

    # ---- explicitly not implemented (kept as loud failures, not silent no-ops)

    def place_order(self, *_a, **_k):
        raise ReadOnlyViolation("refused: place_order is not implemented in the read-only client")

    def amend_algo(self, *_a, **_k):
        raise ReadOnlyViolation("refused: amend_algo is not implemented in the read-only client")

    def transfer(self, *_a, **_k):
        raise ReadOnlyViolation("refused: transfer is not implemented in the read-only client")

    def withdraw(self, *_a, **_k):
        raise ReadOnlyViolation("refused: withdraw is not implemented in the read-only client")


def bot_status_summary(details: dict) -> dict:
    """Pick the sim-daily-schema-relevant fields from a grid details row (strings → floats)."""

    def f(key: str) -> float | None:
        v = details.get(key)
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    inv = f("investment")
    total = f("totalPnl")
    return {
        "algoId": details.get("algoId"),
        "instId": details.get("instId"),
        "state": details.get("state"),
        "minPx": f("minPx"),
        "maxPx": f("maxPx"),
        "gridNum": int(details["gridNum"]) if details.get("gridNum") else None,
        "investment": inv,
        "lever": details.get("lever"),
        "slTriggerPx": f("slTriggerPx"),
        "tpTriggerPx": f("tpTriggerPx"),
        "okx_bot_total_pnl": total,
        "okx_bot_total_pnl_ratio": f("totalPnlRatio")
        if details.get("totalPnlRatio")
        else (total / inv if total is not None and inv else None),
        "grid_profit": f("gridProfit"),
        "float_profit": f("floatProfit"),
        "arbitrage_num": int(details["arbitrageNum"]) if details.get("arbitrageNum") else None,
        "last_px": f("lastPx") if details.get("lastPx") else None,
        "venue": "okx_demo",
    }


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OKX read-only client: public ticker/candles (no keys) and optional "
        "OKX_SIMULATED=1 account/bot GET status. Never places, amends, transfers or withdraws.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default=OKX_PUBLIC_BASE)
    p.add_argument("--timeout", type=float, default=10.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("ticker", help="public GET /market/ticker")
    t.add_argument("--inst-id", default="ETH-USDT")

    c = sub.add_parser("candles", help="public GET /market/candles (oldest→newest)")
    c.add_argument("--inst-id", default="ETH-USDT")
    c.add_argument("--bar", default="5m")
    c.add_argument("--limit", type=int, default=10)
    c.add_argument("--pages", type=int, default=1)
    c.add_argument("--csv-out", default="", help="write ts,open,high,low,close CSV for replay")

    s = sub.add_parser(
        "private-status",
        help="GET account balance + grid bot status; skipped (exit 0) when env keys are absent",
    )
    s.add_argument("--algo-id", default="", help="optional algoId for details/positions")
    s.add_argument("--inst-id", default="")
    s.add_argument("--ccy", default="USDT")

    sub.add_parser("policy", help="print the read-only policy (offline)")
    return p


def _write_csv(path: str, candles: list[CandleRow]) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "vol"])
        for c in candles:
            w.writerow([c.ts_ms, c.open, c.high, c.low, c.close, c.vol])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "policy":
        print(
            json.dumps(
                {
                    "policy": POLICY,
                    "env": {
                        ENV_KEY: redact(os.environ.get(ENV_KEY)),
                        ENV_SECRET: redact(os.environ.get(ENV_SECRET)),
                        ENV_PASSPHRASE: redact(os.environ.get(ENV_PASSPHRASE)),
                        ENV_SIMULATED: os.environ.get(ENV_SIMULATED, "<unset>"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    pub = OkxPublicClient(base_url=args.base_url, timeout=args.timeout)
    if args.cmd == "ticker":
        doc = {"policy": POLICY, "ticker": pub.get_ticker(args.inst_id)}
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "candles":
        rows = pub.get_candles(args.inst_id, bar=args.bar, limit=args.limit, pages=args.pages)
        if args.csv_out:
            _write_csv(args.csv_out, rows)
        doc = {
            "policy": POLICY,
            "data_source": pub.data_source_meta(args.inst_id, args.bar, args.limit, args.pages),
            "count": len(rows),
            "candles": [c.to_dict() for c in rows],
        }
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "private-status":
        try:
            client = OkxReadOnlyPrivateClient.from_env(base_url=args.base_url, timeout=args.timeout)
        except ReadOnlyViolation as e:
            print(json.dumps({"skipped": True, "reason": str(e), "policy": POLICY}, indent=2))
            return 3
        if client is None:
            print(
                json.dumps(
                    {
                        "skipped": True,
                        "reason": f"{ENV_KEY}/{ENV_SECRET}/{ENV_PASSPHRASE} not all set; "
                        "private read-only path stubbed",
                        "requires": {ENV_SIMULATED: "1"},
                        "policy": POLICY,
                    },
                    indent=2,
                )
            )
            return 0
        doc: dict = {"policy": POLICY, "venue": "okx_demo", "x-simulated-trading": "1"}
        doc["balance"] = client.get_balance(args.ccy or None)
        pending = client.get_grid_pending(inst_id=args.inst_id or None)
        doc["grid_pending"] = [bot_status_summary(row) for row in pending]
        if args.algo_id:
            details = client.get_grid_details(args.algo_id)
            doc["grid_details"] = bot_status_summary(details) if details else None
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReadOnlyViolation, OkxApiError, RuntimeError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
