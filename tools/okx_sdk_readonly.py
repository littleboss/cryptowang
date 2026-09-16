#!/usr/bin/env python3
"""Read-only facade over the official OKX Python SDK (`python-okx`).

Same contract as tools/okx_readonly_client.py (stdlib backend), same CLI subcommands,
same JSON output shape — only the HTTP backend differs. Use this when you want the
official SDK's endpoint constants / signing; use the stdlib client when you want zero
third-party code on the path.

Hard gates (enforced in code, not just in docs):
  * Only three SDK modules are ever imported: okx.MarketData, okx.Account, okx.Grid.
    okx.Trade, okx.Funding, okx.SubAccount, okx.Convert, … are never imported here, and
    tests assert they are not present in sys.modules after importing this module.
  * Every SDK client used here is a *gated* subclass. The gate sits on the two funnels
    all SDK REST calls pass through — `OkxClient._request()` and `httpx.Client.send()` —
    and refuses anything that is not a GET to an allow-listed read-only path *before* a
    socket is opened. So even the SDK's own POST methods that still exist on the
    gated object (e.g. GridAPI.grid_amend_order_algo) raise ReadOnlyViolation.
  * The facade classes do not expose the SDK object. Only explicit read helpers are
    public; place_order / amend_algo / transfer / withdraw exist only to raise.
  * Public market data needs no keys. The private path activates only when
    OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE are all set AND OKX_SIMULATED=1;
    the SDK `flag` (x-simulated-trading) is hard-wired to "1" there. Live keys refused.
  * Secrets come from the environment only, are never printed, and SDK debug logging
    (loguru, which would dump signed headers) is disabled at import.

This module does not implement, wrap, or forward: place order, amend algo, stop algo,
transfer, withdrawal, sub-account funding. Do not add them here. will_send_http = False.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from loguru import logger as _loguru_logger

# Only read-facing SDK modules. Do not add okx.Trade / okx.Funding / okx.SubAccount / ….
from okx import __version__ as OKX_SDK_VERSION
from okx.Account import AccountAPI as _SdkAccountAPI
from okx.Grid import GridAPI as _SdkGridAPI
from okx.MarketData import MarketAPI as _SdkMarketAPI

sys.path.insert(0, str(Path(__file__).resolve().parent))

from okx_readonly_client import (  # noqa: E402
    ENV_KEY,
    ENV_PASSPHRASE,
    ENV_SECRET,
    ENV_SIMULATED,
    OKX_PUBLIC_BASE,
    PATH_ACCOUNT_BALANCE,
    PATH_ACCOUNT_CONFIG,
    PATH_CANDLES,
    PATH_GRID_DETAILS,
    PATH_GRID_HISTORY,
    PATH_GRID_PENDING,
    PATH_GRID_POSITIONS,
    PATH_HISTORY_CANDLES,
    PATH_TICKER,
    CandleRow,
    OkxApiError,
    ReadOnlyCredentials,
    ReadOnlyViolation,
    assert_read_only,
    bot_status_summary,
    redact,
)
from okx_readonly_client import POLICY as _BASE_POLICY  # noqa: E402

# The SDK logs request headers (API key, passphrase, signature) through loguru when
# debug=True. We never pass debug=True, and we additionally silence the whole `okx`
# logger namespace so a future SDK default cannot leak secrets to stderr.
_loguru_logger.disable("okx")

SIMULATED_FLAG = "1"  # x-simulated-trading: 1 (OKX demo trading). Never "0" on the private path.
PUBLIC_FLAG = "0"  # keyless public market data; the header is irrelevant without auth.

# SDK modules that contain write paths. Never imported here; tests assert none are loaded.
FORBIDDEN_SDK_MODULES = frozenset(
    {
        "okx.Trade",
        "okx.Funding",
        "okx.SubAccount",
        "okx.Convert",
        "okx.SpreadTrading",
        "okx.BlockTrading",
        "okx.CopyTrading",
        "okx.DualInvest",
        "okx.Finance",
        "okx.FDBroker",
        "okx.Affiliate",
        "okx.websocket",
    }
)
ALLOWED_SDK_MODULES = frozenset({"okx", "okx.MarketData", "okx.Account", "okx.Grid"})

POLICY = {
    **_BASE_POLICY,
    "backend": f"python-okx {OKX_SDK_VERSION} (official SDK, gated GET-only subset)",
    "sdk_modules_imported": sorted(ALLOWED_SDK_MODULES),
    "sdk_modules_forbidden": sorted(FORBIDDEN_SDK_MODULES),
    "gate": "OkxClient._request + httpx.Client.send refuse non-GET / non-allow-listed paths",
}


# --------------------------------------------------------------------------- gate


class _ReadOnlyGate:
    """Mixin placed *before* the SDK class in the MRO.

    The SDK routes every REST call through OkxClient._request(method, path, params), and
    OkxClient is an httpx.Client, so every request also passes through send(). Both are
    gated. The public httpx write verbs are stubbed to raise as well.
    """

    _private: bool = False

    def _request(self, method: str, request_path: str, params: Any):
        assert_read_only(method, request_path, private=self._private)
        return super()._request(method, request_path, params)  # type: ignore[misc]

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any):
        if str(method).upper() != "GET":
            raise ReadOnlyViolation(f"refused: httpx.request method {method!r} (GET only)")
        return super().request(method, url, *args, **kwargs)  # type: ignore[misc]

    def send(self, request: httpx.Request, *args: Any, **kwargs: Any):
        if request.method.upper() != "GET":
            raise ReadOnlyViolation(f"refused: httpx.send method {request.method!r} (GET only)")
        return super().send(request, *args, **kwargs)  # type: ignore[misc]

    def stream(self, method: str, *args: Any, **kwargs: Any):
        if str(method).upper() != "GET":
            raise ReadOnlyViolation(f"refused: httpx.stream method {method!r} (GET only)")
        return super().stream(method, *args, **kwargs)  # type: ignore[misc]

    def post(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: POST is not available on the read-only SDK client")

    def put(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: PUT is not available on the read-only SDK client")

    def patch(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: PATCH is not available on the read-only SDK client")

    def delete(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: DELETE is not available on the read-only SDK client")

    def __repr__(self) -> str:  # never leak API_KEY / PASSPHRASE held by the SDK base
        return (
            f"{type(self).__name__}(domain={getattr(self, 'domain', None)!r}, "
            f"private={self._private}, api_key={redact(_sdk_secret(self, 'API_KEY'))})"
        )

    __str__ = __repr__


def _sdk_secret(client: Any, attr: str) -> str | None:
    v = getattr(client, attr, None)
    return None if v in (None, "-1") else str(v)


class GatedMarketAPI(_ReadOnlyGate, _SdkMarketAPI):
    """SDK MarketAPI restricted to keyless public read paths."""

    _private = False


class GatedAccountAPI(_ReadOnlyGate, _SdkAccountAPI):
    """SDK AccountAPI restricted to GET balance / config on demo trading."""

    _private = True


class GatedGridAPI(_ReadOnlyGate, _SdkGridAPI):
    """SDK GridAPI restricted to GET pending / history / details / positions on demo trading."""

    _private = True


def _sdk_kwargs(*, base_url: str, flag: str, timeout: float) -> dict[str, Any]:
    # use_server_time=None avoids the SDK's DeprecationWarning; debug is always False.
    return {
        "use_server_time": None,
        "flag": flag,
        "domain": base_url.rstrip("/"),
        "debug": False,
        "proxy": None,
        "_timeout": timeout,
    }


def _build(cls: type, api_key: str, secret: str, passphrase: str, **kw: Any):
    timeout = kw.pop("_timeout")
    client = cls(api_key, secret, passphrase, **kw)
    client.timeout = httpx.Timeout(timeout)
    return client


def _check_payload(payload: Any, path: str) -> list:
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected SDK payload for {path}: {type(payload).__name__}")
    if str(payload.get("code")) != "0":
        raise OkxApiError(str(payload.get("code")), str(payload.get("msg")), path)
    return payload.get("data") or []


def assert_no_write_sdk_modules_loaded() -> None:
    """Raise if any SDK module with write paths has been imported into this process."""
    loaded = sorted(m for m in FORBIDDEN_SDK_MODULES if m in sys.modules)
    if loaded:
        raise ReadOnlyViolation(f"refused: write-capable SDK modules loaded: {loaded}")


def imported_sdk_modules(source_path: str | os.PathLike[str] | None = None) -> set[str]:
    """Static view: which `okx*` modules does this file import? (used by tests / CI)."""
    path = Path(source_path) if source_path else Path(__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "okx" or alias.name.startswith("okx."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "okx" or node.module.startswith("okx."):
                found.add(node.module)
    return found


# --------------------------------------------------------------------------- public


class OkxSdkPublicClient:
    """Unauthenticated read-only market data via the official SDK. No keys involved."""

    def __init__(self, base_url: str = OKX_PUBLIC_BASE, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_made = 0
        self._market = _build(
            GatedMarketAPI,
            "-1",
            "-1",
            "-1",
            **_sdk_kwargs(base_url=self.base_url, flag=PUBLIC_FLAG, timeout=timeout),
        )

    def __repr__(self) -> str:
        return f"OkxSdkPublicClient(base_url={self.base_url!r}, backend=python-okx)"

    def close(self) -> None:
        self._market.close()

    def _call(self, path: str, fn, *args: Any, **kwargs: Any) -> list:
        # Belt and braces: the gated subclass checks again inside _request.
        assert_read_only("GET", path, private=False)
        payload = fn(*args, **kwargs)
        self.requests_made += 1
        return _check_payload(payload, path)

    def get_ticker(self, inst_id: str) -> dict:
        rows = self._call(PATH_TICKER, self._market.get_ticker, inst_id)
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
        """Return candles oldest→newest. Same pagination semantics as the stdlib client."""
        path = PATH_HISTORY_CANDLES if history else PATH_CANDLES
        fn = self._market.get_history_candlesticks if history else self._market.get_candlesticks
        cap = 100 if history else 300
        if not (1 <= limit <= cap):
            raise ValueError(f"limit must be 1..{cap} for {path}")
        out: dict[int, CandleRow] = {}
        after = str(after_ms) if after_ms is not None else ""
        for _ in range(max(pages, 1)):
            rows = self._call(path, fn, inst_id, after, "", bar, str(limit))
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
            "backend": f"python-okx {OKX_SDK_VERSION}",
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


class OkxSdkReadOnlyPrivateClient:
    """GET-only account / grid-bot status on OKX demo trading through the official SDK."""

    def __init__(
        self,
        creds: ReadOnlyCredentials,
        base_url: str = OKX_PUBLIC_BASE,
        timeout: float = 10.0,
    ):
        if not creds.simulated:
            raise ReadOnlyViolation("refused: private SDK client requires simulated credentials")
        self._creds = creds
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.requests_made = 0
        kw = _sdk_kwargs(base_url=self.base_url, flag=SIMULATED_FLAG, timeout=timeout)
        self._account = _build(
            GatedAccountAPI, creds.api_key, creds.api_secret, creds.passphrase, **kw
        )
        self._grid = _build(GatedGridAPI, creds.api_key, creds.api_secret, creds.passphrase, **kw)
        assert self._account.flag == SIMULATED_FLAG and self._grid.flag == SIMULATED_FLAG

    def __repr__(self) -> str:
        return (
            f"OkxSdkReadOnlyPrivateClient(base_url={self.base_url!r}, creds={self._creds!r}, "
            f"flag={SIMULATED_FLAG!r})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(
        cls, base_url: str = OKX_PUBLIC_BASE, timeout: float = 10.0
    ) -> OkxSdkReadOnlyPrivateClient | None:
        creds = ReadOnlyCredentials.from_env()
        return None if creds is None else cls(creds, base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._account.close()
        self._grid.close()

    def _call(self, path: str, fn, *args: Any, **kwargs: Any) -> list:
        assert_read_only("GET", path, private=True)
        payload = fn(*args, **kwargs)
        self.requests_made += 1
        return _check_payload(payload, path)

    # ---- account (GET)

    def get_balance(self, ccy: str | None = None) -> list:
        return self._call(PATH_ACCOUNT_BALANCE, self._account.get_account_balance, ccy or "")

    def get_account_config(self) -> list:
        return self._call(PATH_ACCOUNT_CONFIG, self._account.get_account_config)

    # ---- grid bot status (GET)

    def get_grid_pending(self, algo_ord_type: str = "grid", inst_id: str | None = None) -> list:
        return self._call(
            PATH_GRID_PENDING,
            self._grid.grid_orders_algo_pending,
            algoOrdType=algo_ord_type,
            instId=inst_id or "",
        )

    def get_grid_history(self, algo_ord_type: str = "grid", inst_id: str | None = None) -> list:
        return self._call(
            PATH_GRID_HISTORY,
            self._grid.grid_orders_algo_history,
            algoOrdType=algo_ord_type,
            instId=inst_id or "",
        )

    def get_grid_details(self, algo_id: str, algo_ord_type: str = "grid") -> dict | None:
        rows = self._call(
            PATH_GRID_DETAILS,
            self._grid.grid_orders_algo_details,
            algoOrdType=algo_ord_type,
            algoId=algo_id,
        )
        return rows[0] if rows else None

    def get_grid_positions(self, algo_id: str, algo_ord_type: str = "grid") -> list:
        return self._call(
            PATH_GRID_POSITIONS,
            self._grid.grid_positions,
            algoOrdType=algo_ord_type,
            algoId=algo_id,
        )

    # ---- explicitly not implemented (loud failures, not silent no-ops)

    def place_order(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: place_order is not implemented in the read-only client")

    def amend_algo(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: amend_algo is not implemented in the read-only client")

    def stop_algo(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: stop_algo is not implemented in the read-only client")

    def transfer(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: transfer is not implemented in the read-only client")

    def withdraw(self, *_a: Any, **_k: Any):
        raise ReadOnlyViolation("refused: withdraw is not implemented in the read-only client")


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="OKX read-only client on the official python-okx SDK: public ticker/candles "
        "(no keys) and optional OKX_SIMULATED=1 account/bot GET status. Never places, amends, "
        "transfers or withdraws.",
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

    sub.add_parser("policy", help="print the read-only policy + SDK module audit (offline)")
    return p


def _write_csv(path: str, candles: list[CandleRow]) -> None:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "vol"])
        for c in candles:
            w.writerow([c.ts_ms, c.open, c.high, c.low, c.close, c.vol])


def policy_document() -> dict:
    assert_no_write_sdk_modules_loaded()
    return {
        "policy": POLICY,
        "sdk_modules_in_source": sorted(imported_sdk_modules()),
        "sdk_modules_loaded": sorted(m for m in sys.modules if m == "okx" or m.startswith("okx.")),
        "env": {
            ENV_KEY: redact(os.environ.get(ENV_KEY)),
            ENV_SECRET: redact(os.environ.get(ENV_SECRET)),
            ENV_PASSPHRASE: redact(os.environ.get(ENV_PASSPHRASE)),
            ENV_SIMULATED: os.environ.get(ENV_SIMULATED, "<unset>"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "policy":
        print(json.dumps(policy_document(), ensure_ascii=False, indent=2))
        return 0

    pub = OkxSdkPublicClient(base_url=args.base_url, timeout=args.timeout)
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
            client = OkxSdkReadOnlyPrivateClient.from_env(
                base_url=args.base_url, timeout=args.timeout
            )
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
        doc: dict = {"policy": POLICY, "venue": "okx_demo", "x-simulated-trading": SIMULATED_FLAG}
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
    except (ReadOnlyViolation, OkxApiError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
