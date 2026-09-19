#!/usr/bin/env python3
"""E2 OMS-v2 — gated OKX *demo* executor for the E2-G1 ETH-USDT spot buy ladder.

Phase A (``strategies/e2_demo_oms.py``) builds intents and has no HTTP code. OMS-v2 is the
sibling that closes the loop *on OKX demo trading only*: place / cancel / amend spot **limit**
orders, but only when **all** of the following hold at call time:

  1. ``DemoOmsV2(will_send_http=True)`` — the default is ``False`` and, in that state, this
     class behaves exactly like Phase A: ``submit`` records the plan and returns ``sent=False``;
     ``place_order`` / ``amend_order`` / ``cancel_order`` raise ``SendRefused`` with the Phase A
     codes (``PlaceOrderRefused`` / ``AmendRefused`` / ``CancelRefused``).
  2. Demo env contract: the three ``OKX_*`` keys are set **and** ``OKX_SIMULATED=1`` (alias:
     ``OKX_FLAG=1``, the python-okx ``flag`` convention). Otherwise ``DemoEnvCheckFailed``;
     the refusal reports set / unset only, never a value.
  3. Demo host: ``base_url`` must be ``https://www.okx.com`` (OKX demo is the same host plus the
     ``x-simulated-trading: 1`` header) or a plain-HTTP loopback address used by the unit tests.
     Anything else → ``LiveHostRefused``. The header is forced on every request and re-checked
     immediately before the socket opens (``SimulatedHeaderMissing``).
  4. Endpoint allow-list: ``POST /api/v5/trade/order`` · ``cancel-order`` · ``amend-order`` and
     ``GET /api/v5/trade/orders-pending`` · ``/api/v5/trade/order``. Everything else — batch
     orders, algo / tradingBot (grid stop / amend), withdraw, transfer, asset, sub-account,
     margin, close-position — is refused before any I/O (``EndpointForbidden``).
  5. Body prechecks: ``ETH-USDT`` · ``tdMode=cash`` · ``side=buy`` (sell is an intent-only stub,
     ``SellSendNotArmed``) · ``ordType ∈ {limit, post_only}`` (market / IOC / FOK refused) ·
     px > 0 below the reference · sz ≥ minSz · notional ≤ 100 USDT per plan and per OMS
     instance · ≤ 10 orders per instance · only the known OKX order fields.
  6. Ownership: cancel / amend accept **only** ``clOrdId`` values with this OMS's prefix
     (``e2g1v2``). ``cancel_tagged`` lists pending orders and skips every foreign order (e.g.
     the 8 connectivity-trial buys placed outside this module). No ``ordId``-only operations.

``will_send_http=True`` without the checks refuses; the checks without the flag never send.
Merging this file arms nothing: every real send additionally needs the 04-risk pass and the
user's explicit confirmation in chat, then the env, then the flag on the command line.

Explicitly NOT here: live trading, market / IOC / FOK sweeps, withdraw, transfer, any
``tradingBot`` (official grid bot A / B′ / C) endpoint, T1 / T2 arbitrage execution, auto
re-banding, automatic sell placement (G1b is an intent builder only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "strategies"))

import e2_demo_oms as phase_a  # noqa: E402
from e2_demo_oms import (  # noqa: E402
    ALLOWED_ORD_TYPES,
    APPROVED_INST_ID,
    ENV_KEY,
    ENV_PASSPHRASE,
    ENV_SECRET,
    ENV_SIMULATED,
    FORBIDDEN_ORD_TYPES,
    MAX_LEVELS,
    MAX_TOTAL_NOTIONAL_USDT,
    STATIC_INSTRUMENT,
    STRATEGY_ID,
    DemoEnvCheck,
    GridIntentConfig,
    RiskPrecheckRefused,
    SendRefused,
)
from okx_readonly_client import okx_timestamp, sign  # noqa: E402

OMS_VERSION = "v2"
SCHEMA_RUN = "e2_demo_oms_v2_run_v1"
SCHEMA_EXECUTION = "e2_demo_oms_v2_execution_report_v1"
SCHEMA_SELL_INTENTS = "e2_demo_oms_v2_sell_intents_v1"

ENV_FLAG = "OKX_FLAG"  # alias for OKX_SIMULATED (python-okx `flag="1"` convention)

OKX_DEMO_BASE_URL = "https://www.okx.com"
ALLOWED_BASE_URLS = frozenset({OKX_DEMO_BASE_URL})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
USER_AGENT = "cryptowang-e2-oms-v2/0.1 (okx demo only; x-simulated-trading forced)"
SIMULATED_HEADER = "x-simulated-trading"

PATH_ORDER = "/api/v5/trade/order"
PATH_CANCEL = "/api/v5/trade/cancel-order"
PATH_AMEND = "/api/v5/trade/amend-order"
PATH_PENDING = "/api/v5/trade/orders-pending"
ALLOWED_ENDPOINTS = frozenset(
    {
        ("POST", PATH_ORDER),
        ("POST", PATH_CANCEL),
        ("POST", PATH_AMEND),
        ("GET", PATH_PENDING),
        ("GET", PATH_ORDER),
    }
)
# Belt and braces: even if someone widens the allow-list these fragments stay refused.
FORBIDDEN_PATH_FRAGMENTS = (
    "batch",
    "tradingbot",
    "algo",
    "withdraw",
    "transfer",
    "/asset/",
    "sub-account",
    "subaccount",
    "close-position",
    "margin",
    "position",
    "set-",
    "convert",
    "rfq",
    "block",
    "sprd",
    "copytrading",
    "/finance/",
    "earn",
    "loan",
    "/users/",
    "/broker/",
)

# Ownership: only orders carrying this clOrdId prefix may be cancelled / amended. The prefix is
# distinct from Phase A's `e2g1a` (intent ids) so a plan JSON placed by hand outside this module
# is treated as foreign too.
OMS_CL_ORD_ID_PREFIX = "e2g1v2"
ORDER_TAG = "e2g1omsv2"
SELL_INTENT_PREFIX = "e2g1s"
CL_ORD_ID_MAX_LEN = 32
MAX_ORDERS_PER_INSTANCE = MAX_LEVELS
ALLOWED_ORDER_FIELDS = frozenset(
    {"instId", "tdMode", "side", "ordType", "px", "sz", "clOrdId", "tag"}
)
SENDABLE_SIDES = frozenset({"buy"})

ARMING_REQUIRES = [
    "04-risk pass for the specific demo run (merge of this PR is not that pass)",
    "explicit user confirmation in chat (not model self-confirm)",
    f"{ENV_SIMULATED}=1 (or {ENV_FLAG}=1) + {ENV_KEY}/{ENV_SECRET}/{ENV_PASSPHRASE} for OKX demo",
    "DemoOmsV2(will_send_http=True) / CLI --will-send-http on the command line",
    "base_url in the demo allow-list; x-simulated-trading: 1 forced on every request",
]

RISK_NOTES = [
    "default will_send_http=False: identical to Phase A (records intents, sends nothing)",
    "armed path is OKX demo only: env contract + demo host + forced x-simulated-trading header",
    "spot limit / post_only buys only; market / IOC / FOK refused; sells are intent-only "
    "(G1b stub)",
    f"cancel / amend only touch clOrdId prefix {OMS_CL_ORD_ID_PREFIX!r}; foreign orders "
    "(incl. the 8 connectivity-trial buys) are listed as skipped, never cancelled",
    "no tradingBot / algo endpoint: official OKX grid bots A / B′ / C are untouched",
    "withdraw, transfer, asset, sub-account, margin endpoints refused before any I/O",
    "merge ≠ arming: each real demo run needs 04-risk + explicit user confirmation + env + flag",
]

POLICY = {
    "strategy_id": STRATEGY_ID,
    "oms_version": OMS_VERSION,
    "mode_default": "dry_run",
    "will_send_http": False,
    "http_code_present": True,
    "http_client": "urllib (stdlib) inside DemoHttpSender only; no SDK, no httpx / requests",
    "trading_http": {
        "order": False,
        "amend": False,
        "cancel": False,
        "transfer": False,
        "withdraw": False,
    },
    "trading_http_when_armed_demo": {
        "order": "spot limit / post_only buy, ETH-USDT, cash, ≤ 100 USDT, ≤ 10 orders",
        "amend": f"clOrdId prefix {OMS_CL_ORD_ID_PREFIX!r} only (newPx / newSz)",
        "cancel": f"clOrdId prefix {OMS_CL_ORD_ID_PREFIX!r} only; foreign orders skipped",
        "transfer": False,
        "withdraw": False,
    },
    "venue": "okx_demo",
    "forced_headers": {SIMULATED_HEADER: "1"},
    "allowed_base_urls": sorted(ALLOWED_BASE_URLS),
    "loopback_allowed_for_tests": sorted(LOOPBACK_HOSTS),
    "allowed_endpoints": sorted(f"{m} {p}" for m, p in ALLOWED_ENDPOINTS),
    "forbidden_path_fragments": list(FORBIDDEN_PATH_FRAGMENTS),
    "cl_ord_id_prefix": OMS_CL_ORD_ID_PREFIX,
    "order_tag": ORDER_TAG,
    "sides_sendable": sorted(SENDABLE_SIDES),
    "sell_ladder": "G1b intent builder only (prefix e2g1s); not sendable in OMS-v2",
    "approved_inst_ids": [APPROVED_INST_ID],
    "max_levels": MAX_LEVELS,
    "max_orders_per_instance": MAX_ORDERS_PER_INSTANCE,
    "max_total_notional_usdt": float(MAX_TOTAL_NOTIONAL_USDT),
    "td_mode": "cash",
    "lever": 1,
    "ord_types_allowed": sorted(ALLOWED_ORD_TYPES),
    "ord_types_forbidden": sorted(FORBIDDEN_ORD_TYPES),
    "auto_reband": False,
    "official_bots_untouched": ["A", "B′", "C"],
    "merge_is_arming": False,
    "arming_requires": ARMING_REQUIRES,
}


class DemoApiError(RuntimeError):
    """OKX returned a non-zero code (or per-order sCode). Never carries headers or secrets."""

    def __init__(self, code: str, msg: str, path: str):
        super().__init__(f"OKX demo API error {code}: {msg} ({path})")
        self.code = code
        self.msg = msg
        self.path = path


# --------------------------------------------------------------------------- env + host gates


def _redact(value: str | None) -> str:
    return f"<set:{len(value)} chars>" if value else "<unset>"


def check_send_env(env: dict[str, str] | None = None) -> DemoEnvCheck:
    """Phase A env contract plus the ``OKX_FLAG=1`` alias. Never returns secret values."""
    e = os.environ if env is None else env
    key, secret, pw = e.get(ENV_KEY, ""), e.get(ENV_SECRET, ""), e.get(ENV_PASSPHRASE, "")
    simulated = e.get(ENV_SIMULATED, "") == "1" or e.get(ENV_FLAG, "") == "1"
    keys_present = bool(key and secret and pw)
    return DemoEnvCheck(
        simulated=simulated,
        keys_present=keys_present,
        ok=simulated and keys_present,
        detail={
            ENV_KEY: _redact(key),
            ENV_SECRET: _redact(secret),
            ENV_PASSPHRASE: _redact(pw),
            ENV_SIMULATED: e.get(ENV_SIMULATED, "<unset>"),
            ENV_FLAG: e.get(ENV_FLAG, "<unset>"),
        },
    )


def assert_demo_base_url(base_url: str) -> str:
    """Allow ``https://www.okx.com`` (demo = same host + header) or plain-HTTP loopback."""
    u = urllib.parse.urlsplit(base_url)
    normalized = f"{u.scheme}://{u.netloc}".lower()
    if u.path not in ("", "/") or u.query or u.fragment:
        raise SendRefused(
            "LiveHostRefused", f"base_url must be scheme://host only, got {base_url!r}"
        )
    if normalized in ALLOWED_BASE_URLS:
        return normalized
    if u.scheme == "http" and (u.hostname or "").lower() in LOOPBACK_HOSTS:
        return normalized
    raise SendRefused(
        "LiveHostRefused",
        f"base_url {base_url!r} is not in the demo allow-list {sorted(ALLOWED_BASE_URLS)} "
        "(loopback http only for tests)",
    )


def assert_endpoint_allowed(method: str, path: str) -> None:
    """Refuse anything but the five allow-listed trade endpoints. No I/O."""
    m = method.upper()
    if "?" in path or "#" in path:
        raise SendRefused("EndpointForbidden", "query strings must be passed separately")
    low = path.lower()
    for frag in FORBIDDEN_PATH_FRAGMENTS:
        if frag in low:
            raise SendRefused(
                "EndpointForbidden", f"{m} {path!r} matches forbidden fragment {frag!r}"
            )
    if (m, path) not in ALLOWED_ENDPOINTS:
        raise SendRefused("EndpointForbidden", f"{m} {path!r} is not an allow-listed demo endpoint")


# --------------------------------------------------------------------------- body prechecks


def _dec_pos(value, name: str) -> Decimal:
    d = phase_a._dec(value, name)
    if d <= 0:
        raise RiskPrecheckRefused("BadNumber", f"{name}={value!r} must be > 0")
    return d


def assert_own_cl_ord_id(cl_ord_id) -> str:
    """Only ids minted by this OMS may be cancelled / amended."""
    if not isinstance(cl_ord_id, str) or not cl_ord_id:
        raise SendRefused("ForeignOrderRefused", "clOrdId is required (ordId-only is refused)")
    if not cl_ord_id.startswith(OMS_CL_ORD_ID_PREFIX):
        raise SendRefused(
            "ForeignOrderRefused",
            f"clOrdId {cl_ord_id!r} does not carry the OMS-v2 prefix {OMS_CL_ORD_ID_PREFIX!r}; "
            "orders placed outside this module are never touched",
        )
    if not cl_ord_id.isalnum() or len(cl_ord_id) > CL_ORD_ID_MAX_LEN:
        raise SendRefused("ForeignOrderRefused", f"clOrdId {cl_ord_id!r} is not a valid OKX id")
    return cl_ord_id


def assert_order_body_allowed(body: dict, *, ref_px: Decimal | str | None = None) -> Decimal:
    """Validate one POST /trade/order body; return its notional (USDT). Raises before I/O."""
    extra = set(body) - ALLOWED_ORDER_FIELDS
    if extra:
        raise SendRefused("UnknownOrderField", f"refused fields {sorted(extra)} in order body")
    missing = ALLOWED_ORDER_FIELDS - set(body)
    if missing:
        raise SendRefused("OrderFieldMissing", f"order body lacks {sorted(missing)}")
    if body["instId"] != APPROVED_INST_ID:
        raise RiskPrecheckRefused("InstrumentNotApproved", f"{body['instId']!r} is not approved")
    if body["tdMode"] != "cash":
        raise RiskPrecheckRefused("TdModeForbidden", f"tdMode {body['tdMode']!r} (cash only)")
    if body["side"] == "sell":
        raise SendRefused(
            "SellSendNotArmed",
            "sell orders are intent-only in OMS-v2 (G1b stub); sending sells needs its own "
            "04-risk pass",
        )
    if body["side"] not in SENDABLE_SIDES:
        raise SendRefused("SideForbidden", f"side {body['side']!r} refused")
    if body["ordType"] in FORBIDDEN_ORD_TYPES or body["ordType"] not in ALLOWED_ORD_TYPES:
        raise RiskPrecheckRefused(
            "OrderTypeForbidden",
            f"ordType {body['ordType']!r} refused; allowed: {sorted(ALLOWED_ORD_TYPES)}",
        )
    px = _dec_pos(body["px"], "px")
    sz = _dec_pos(body["sz"], "sz")
    if sz < Decimal(STATIC_INSTRUMENT["minSz"]):
        raise RiskPrecheckRefused("SizeBelowMin", f"sz {sz} < minSz {STATIC_INSTRUMENT['minSz']}")
    if ref_px is not None and px >= _dec_pos(ref_px, "ref_px"):
        raise RiskPrecheckRefused(
            "BuyLevelAboveReference", f"px {px} >= ref_px {ref_px}: would cross the book"
        )
    assert_own_cl_ord_id(body["clOrdId"])
    if body["tag"] != ORDER_TAG:
        raise SendRefused("OrderTagMismatch", f"tag must be {ORDER_TAG!r}, got {body['tag']!r}")
    notional = px * sz
    if notional > MAX_TOTAL_NOTIONAL_USDT:
        raise RiskPrecheckRefused(
            "NotionalOverCap", f"order notional {notional} > {MAX_TOTAL_NOTIONAL_USDT}"
        )
    return notional


def assert_amend_allowed(
    cl_ord_id: str,
    *,
    new_px: str | None,
    new_sz: str | None,
    ref_px: Decimal | str | None = None,
) -> dict:
    """Validate an amend request; return the POST /trade/amend-order body."""
    assert_own_cl_ord_id(cl_ord_id)
    if new_px in (None, "") and new_sz in (None, ""):
        raise RiskPrecheckRefused("AmendNoop", "amend needs new_px and/or new_sz")
    body = {"instId": APPROVED_INST_ID, "clOrdId": cl_ord_id}
    if new_px not in (None, ""):
        px = _dec_pos(new_px, "new_px")
        if ref_px is not None and px >= _dec_pos(ref_px, "ref_px"):
            raise RiskPrecheckRefused(
                "BuyLevelAboveReference", f"new_px {px} >= ref_px {ref_px}: would cross the book"
            )
        body["newPx"] = phase_a._fmt(px)
    if new_sz not in (None, ""):
        sz = _dec_pos(new_sz, "new_sz")
        if sz < Decimal(STATIC_INSTRUMENT["minSz"]):
            raise RiskPrecheckRefused("SizeBelowMin", f"new_sz {sz} < minSz")
        body["newSz"] = phase_a._fmt(sz)
    if "newPx" in body and "newSz" in body:
        if Decimal(body["newPx"]) * Decimal(body["newSz"]) > MAX_TOTAL_NOTIONAL_USDT:
            raise RiskPrecheckRefused("NotionalOverCap", "amended notional > cap")
    return body


def verify_plan(plan: dict) -> None:
    """A plan handed to the armed path must be an untampered Phase A intent plan."""
    if plan.get("schema") != phase_a.SCHEMA or plan.get("strategy_id") != STRATEGY_ID:
        raise SendRefused("PlanRejected", "not an e2_demo_oms_intent_plan_v1 for E2-G1")
    if plan.get("will_send_http") is not False or plan.get("mode") != "dry_run":
        raise SendRefused(
            "PlanRejected", "intent plan must be a dry_run plan (flags live in the OMS)"
        )
    orders = plan.get("orders") or []
    canonical = json.dumps(orders, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != plan.get("plan_id"):
        raise SendRefused("PlanRejected", "plan_id does not match orders (tampered plan)")
    if not (1 <= len(orders) <= MAX_LEVELS):
        raise RiskPrecheckRefused("LevelsOverCap", f"{len(orders)} orders")
    total = sum((Decimal(o["px"]) * Decimal(o["sz"]) for o in orders), Decimal("0"))
    if total > MAX_TOTAL_NOTIONAL_USDT:
        raise RiskPrecheckRefused("NotionalOverCap", f"plan notional {total} > cap")


def v2_cl_ord_id(plan_id: str, order: dict) -> str:
    """Deterministic OMS-v2 id per (plan, level, px, sz): re-submitting the same plan collides."""
    seed = f"{plan_id}|{order['level']}|{order['px']}|{order['sz']}".encode()
    return f"{OMS_CL_ORD_ID_PREFIX}{hashlib.sha256(seed).hexdigest()[:16]}"


def order_body_from_intent(plan_id: str, order: dict) -> dict:
    return {
        "instId": order["instId"],
        "tdMode": order["tdMode"],
        "side": order["side"],
        "ordType": order["ordType"],
        "px": order["px"],
        "sz": order["sz"],
        "clOrdId": v2_cl_ord_id(plan_id, order),
        "tag": ORDER_TAG,
    }


# --------------------------------------------------------------------------- sender


@dataclass(frozen=True)
class DemoCredentials:
    api_key: str
    api_secret: str
    passphrase: str
    simulated: bool

    def __repr__(self) -> str:  # never leak values
        return (
            f"DemoCredentials(api_key={_redact(self.api_key)}, "
            f"api_secret={_redact(self.api_secret)}, passphrase={_redact(self.passphrase)}, "
            f"simulated={self.simulated})"
        )

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DemoCredentials:
        check = check_send_env(env)
        if not check.ok:
            raise SendRefused(
                "DemoEnvCheckFailed",
                f"sending requires {ENV_SIMULATED}=1 (or {ENV_FLAG}=1) and the three OKX_* "
                f"keys; got {check.detail}",
            )
        e = os.environ if env is None else env
        return cls(e[ENV_KEY], e[ENV_SECRET], e[ENV_PASSPHRASE], simulated=True)


class DemoHttpSender:
    """Signed OKX v5 REST for the allow-listed demo trade endpoints. stdlib urllib only.

    Instances are meant to be created by ``DemoOmsV2`` after its gate; constructing one directly
    still re-runs the host and credential checks and can never reach a non-demo host.
    """

    def __init__(
        self,
        creds: DemoCredentials,
        *,
        base_url: str = OKX_DEMO_BASE_URL,
        timeout: float = 10.0,
    ):
        if not creds.simulated:
            raise SendRefused("DemoEnvCheckFailed", "sender requires simulated credentials")
        self.base_url = assert_demo_base_url(base_url)
        self._creds = creds
        self.timeout = timeout
        self.requests_made = 0
        # method / path / clOrdId only — never headers, never bodies with anything secret.
        self.audit: list[dict] = []

    def __repr__(self) -> str:
        return f"DemoHttpSender(base_url={self.base_url!r}, creds={self._creds!r})"

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        ts = okx_timestamp()
        return {
            "OK-ACCESS-KEY": self._creds.api_key,
            "OK-ACCESS-SIGN": sign(self._creds.api_secret, ts, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._creds.passphrase,
            SIMULATED_HEADER: "1",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self, method: str, path: str, *, query: dict | None = None, body: dict | None = None
    ) -> list:
        method = method.upper()
        assert_endpoint_allowed(method, path)
        q = {k: v for k, v in (query or {}).items() if v not in (None, "")}
        request_path = path + (f"?{urllib.parse.urlencode(q)}" if q else "")
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = self._headers(method, request_path, payload)
        if headers.get(SIMULATED_HEADER) != "1":
            raise SendRefused("SimulatedHeaderMissing", f"{SIMULATED_HEADER}: 1 is mandatory")
        req = urllib.request.Request(
            f"{self.base_url}{request_path}",
            data=payload.encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        if req.get_header(SIMULATED_HEADER.capitalize()) != "1":
            raise SendRefused("SimulatedHeaderMissing", "header lost before send")
        self.audit.append(
            {"method": method, "path": path, "clOrdId": (body or {}).get("clOrdId"), "query": q}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                doc = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                doc = json.loads(raw)
            except ValueError:
                raise DemoApiError(str(e.code), "non-JSON error body", path) from None
        self.requests_made += 1
        return self._check(doc, path)

    @staticmethod
    def _check(doc: dict, path: str) -> list:
        data = doc.get("data") or []
        if str(doc.get("code")) != "0":
            first = data[0] if data and isinstance(data[0], dict) else {}
            code = str(first.get("sCode") or doc.get("code"))
            msg = str(first.get("sMsg") or doc.get("msg"))
            raise DemoApiError(code, msg, path)
        for row in data:
            if isinstance(row, dict) and row.get("sCode") not in (None, "", "0"):
                raise DemoApiError(str(row["sCode"]), str(row.get("sMsg")), path)
        return data

    # ---- allow-listed operations

    def place_limit_order(self, body: dict, *, ref_px: Decimal | str | None = None) -> dict:
        assert_order_body_allowed(body, ref_px=ref_px)
        rows = self._request("POST", PATH_ORDER, body=body)
        return rows[0] if rows else {}

    def cancel_order(self, cl_ord_id: str) -> dict:
        assert_own_cl_ord_id(cl_ord_id)
        rows = self._request(
            "POST", PATH_CANCEL, body={"instId": APPROVED_INST_ID, "clOrdId": cl_ord_id}
        )
        return rows[0] if rows else {}

    def amend_order(
        self,
        cl_ord_id: str,
        *,
        new_px: str | None = None,
        new_sz: str | None = None,
        ref_px: Decimal | str | None = None,
    ) -> dict:
        body = assert_amend_allowed(cl_ord_id, new_px=new_px, new_sz=new_sz, ref_px=ref_px)
        rows = self._request("POST", PATH_AMEND, body=body)
        return rows[0] if rows else {}

    def list_pending(self) -> list[dict]:
        return self._request(
            "GET", PATH_PENDING, query={"instType": "SPOT", "instId": APPROVED_INST_ID}
        )


# --------------------------------------------------------------------------- oms


@dataclass(frozen=True)
class ExecutionReport:
    schema: str
    oms_version: str
    sent: bool
    http_sent: bool
    will_send_http: bool
    orders_placed: int
    orders_failed: int
    reason: str
    plan_id: str
    requests_made: int = 0
    base_url: str | None = None
    results: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class DemoOmsV2:
    """OMS-v2: Phase A behaviour by default; gated OKX-demo sends when armed.

    ``will_send_http`` is the only arming switch and it is per instance. A config that says
    ``will_send_http=True`` cannot arm a disarmed OMS (``SendNotArmed``).
    """

    def __init__(
        self,
        *,
        will_send_http: bool = False,
        env: dict[str, str] | None = None,
        base_url: str = OKX_DEMO_BASE_URL,
        timeout: float = 10.0,
    ):
        self.will_send_http = bool(will_send_http)
        self._env = env
        self.base_url = base_url
        self.timeout = timeout
        self._sender: DemoHttpSender | None = None
        self.submitted: list[str] = []
        self.place_order_calls = 0
        self.placed_cl_ord_ids: list[str] = []
        self.placed_notional_usdt = Decimal("0")

    # ---- gate

    def arm_check(self) -> DemoEnvCheck:
        """Every real send goes through here first. Raises ``SendRefused`` unless fully armed."""
        if not self.will_send_http:
            raise SendRefused(
                "SendNotArmed", "will_send_http=False (default): OMS-v2 records intents only"
            )
        check = check_send_env(self._env)
        if not check.ok:
            raise SendRefused(
                "DemoEnvCheckFailed",
                f"will_send_http=True requires {ENV_SIMULATED}=1 (or {ENV_FLAG}=1) and the three "
                f"OKX_* keys; refused ({check.detail})",
            )
        assert_demo_base_url(self.base_url)
        return check

    def _sender_or_refuse(self, code: str, what: str) -> DemoHttpSender:
        if not self.will_send_http:
            raise SendRefused(
                code, f"{what} refused: will_send_http=False (default, same as Phase A)"
            )
        self.arm_check()
        if self._sender is None:
            self._sender = DemoHttpSender(
                DemoCredentials.from_env(self._env), base_url=self.base_url, timeout=self.timeout
            )
        return self._sender

    # ---- plan

    def build_plan(self, cfg: GridIntentConfig, *, now: datetime | None = None) -> dict:
        """Build the Phase A intent plan. When armed, the gate runs *before* any sizing."""
        if cfg.will_send_http and not self.will_send_http:
            raise SendRefused(
                "SendNotArmed",
                "config asks will_send_http=True but this DemoOmsV2 was created with "
                "will_send_http=False; the OMS instance is the only arming switch",
            )
        if self.will_send_http:
            if cfg.live:
                raise RiskPrecheckRefused("LiveFlagRefused", "live=True is refused in every phase")
            self.arm_check()
            cfg = replace(cfg, will_send_http=False)
        return phase_a.build_intent_plan(cfg, env=self._env, now=now)

    def submit(self, plan: dict) -> ExecutionReport:
        if not self.will_send_http:
            if plan.get("will_send_http") is not False:
                raise SendRefused("SendNotArmed", "plan flag cannot arm a disarmed OMS")
            self.submitted.append(plan["plan_id"])
            return ExecutionReport(
                schema=SCHEMA_EXECUTION,
                oms_version=OMS_VERSION,
                sent=False,
                http_sent=False,
                will_send_http=False,
                orders_placed=0,
                orders_failed=0,
                reason="oms_v2_dry_run: intents recorded, nothing sent (will_send_http=False)",
                plan_id=plan["plan_id"],
            )
        verify_plan(plan)
        if plan["plan_id"] in self.submitted:
            raise SendRefused("DuplicateSubmit", f"plan {plan['plan_id'][:12]} already submitted")
        sender = self._sender_or_refuse("PlaceOrderRefused", "submit")
        ref_px = plan["grid"]["ref_px"]
        bodies = [order_body_from_intent(plan["plan_id"], o) for o in plan["orders"]]
        # Whole ladder is checked before the first byte goes out.
        total = sum((assert_order_body_allowed(b, ref_px=ref_px) for b in bodies), Decimal("0"))
        self._reserve(len(bodies), total)
        self.submitted.append(plan["plan_id"])
        results: list[dict] = []
        placed = failed = 0
        reason = "oms_v2_demo_sent"
        for body in bodies:
            self.place_order_calls += 1
            try:
                row = sender.place_limit_order(body, ref_px=ref_px)
            except DemoApiError as exc:
                failed += 1
                results.append(
                    {"clOrdId": body["clOrdId"], "ok": False, "sCode": exc.code, "sMsg": exc.msg}
                )
                reason = "oms_v2_demo_partial: stopped at first OKX error"
                break
            placed += 1
            self.placed_cl_ord_ids.append(body["clOrdId"])
            self.placed_notional_usdt += Decimal(body["px"]) * Decimal(body["sz"])
            results.append(
                {
                    "clOrdId": body["clOrdId"],
                    "ordId": row.get("ordId"),
                    "ok": True,
                    "px": body["px"],
                    "sz": body["sz"],
                }
            )
        return ExecutionReport(
            schema=SCHEMA_EXECUTION,
            oms_version=OMS_VERSION,
            sent=placed > 0,
            http_sent=sender.requests_made > 0,
            will_send_http=True,
            orders_placed=placed,
            orders_failed=failed,
            reason=reason,
            plan_id=plan["plan_id"],
            requests_made=sender.requests_made,
            base_url=sender.base_url,
            results=results,
        )

    def _reserve(self, n_orders: int, notional: Decimal) -> None:
        if len(self.placed_cl_ord_ids) + n_orders > MAX_ORDERS_PER_INSTANCE:
            raise RiskPrecheckRefused(
                "LevelsOverCap",
                f"{len(self.placed_cl_ord_ids)} placed + {n_orders} > "
                f"{MAX_ORDERS_PER_INSTANCE} per OMS instance",
            )
        if self.placed_notional_usdt + notional > MAX_TOTAL_NOTIONAL_USDT:
            raise RiskPrecheckRefused(
                "NotionalOverCap",
                f"{self.placed_notional_usdt} placed + {notional} > "
                f"{MAX_TOTAL_NOTIONAL_USDT} per OMS instance",
            )

    # ---- single-order operations (Phase A codes when disarmed)

    def place_order(self, body: dict, *, ref_px: Decimal | str | None = None) -> dict:
        self.place_order_calls += 1
        sender = self._sender_or_refuse("PlaceOrderRefused", "place_order")
        notional = assert_order_body_allowed(body, ref_px=ref_px)
        self._reserve(1, notional)
        row = sender.place_limit_order(body, ref_px=ref_px)
        self.placed_cl_ord_ids.append(body["clOrdId"])
        self.placed_notional_usdt += notional
        return row

    def amend_order(
        self,
        cl_ord_id: str,
        *,
        new_px: str | None = None,
        new_sz: str | None = None,
        ref_px: Decimal | str | None = None,
    ) -> dict:
        assert_own_cl_ord_id(cl_ord_id)
        sender = self._sender_or_refuse("AmendRefused", "amend_order")
        return sender.amend_order(cl_ord_id, new_px=new_px, new_sz=new_sz, ref_px=ref_px)

    replace_order = amend_order

    def cancel_order(self, cl_ord_id: str) -> dict:
        assert_own_cl_ord_id(cl_ord_id)
        sender = self._sender_or_refuse("CancelRefused", "cancel_order")
        return sender.cancel_order(cl_ord_id)

    def cancel_tagged(self) -> dict:
        """Cancel every pending ETH-USDT order whose clOrdId carries the OMS-v2 prefix.

        Foreign orders (no prefix — e.g. the 8 connectivity-trial buys) are counted and skipped.
        """
        sender = self._sender_or_refuse("CancelRefused", "cancel_tagged")
        pending = sender.list_pending()

        def is_own(row: dict) -> bool:
            cid = row.get("clOrdId")
            return isinstance(cid, str) and cid.startswith(OMS_CL_ORD_ID_PREFIX)

        own = [r for r in pending if is_own(r)]
        foreign = [
            {"ordId": r.get("ordId"), "clOrdId": r.get("clOrdId") or "", "skipped": True}
            for r in pending
            if not is_own(r)
        ]
        cancelled, failed = [], []
        for r in own:
            try:
                sender.cancel_order(r["clOrdId"])
                cancelled.append(r["clOrdId"])
            except DemoApiError as exc:
                failed.append({"clOrdId": r["clOrdId"], "sCode": exc.code, "sMsg": exc.msg})
        return {
            "schema": "e2_demo_oms_v2_cancel_tagged_v1",
            "oms_version": OMS_VERSION,
            "will_send_http": True,
            "http_sent": True,
            "prefix": OMS_CL_ORD_ID_PREFIX,
            "pending_seen": len(pending),
            "own_seen": len(own),
            "foreign_skipped": len(foreign),
            "foreign": foreign,
            "cancelled": cancelled,
            "failed": failed,
            "requests_made": sender.requests_made,
        }


# --------------------------------------------------------------------------- G1b sell intents


def build_sell_intents(
    plan: dict, filled_levels: list[int], *, now: datetime | None = None
) -> dict:
    """G1b stub: paired sell-limit *intents* for filled buy levels. Never sent, never sendable.

    One sell per filled level at that level's ``paired_exit_px`` (one step above the buy) for the
    same size. This is an intent document only: ``DemoOmsV2`` refuses ``side=sell``
    (``SellSendNotArmed``) even when armed.
    """
    verify_plan(plan)
    if not filled_levels:
        raise RiskPrecheckRefused("NoFilledLevels", "filled_levels is empty")
    if len(set(filled_levels)) != len(filled_levels):
        raise RiskPrecheckRefused("BadFilledLevels", "duplicate levels")
    by_level = {o["level"]: o for o in plan["orders"]}
    intents: list[dict] = []
    for lvl in sorted(filled_levels):
        buy = by_level.get(lvl)
        if buy is None:
            raise RiskPrecheckRefused("BadFilledLevels", f"level {lvl} not in plan")
        exit_px = Decimal(buy["paired_exit_px"])
        if exit_px <= Decimal(buy["px"]):
            raise RiskPrecheckRefused("BadRange", f"exit {exit_px} <= buy {buy['px']}")
        seed = f"{plan['plan_id']}|sell|{lvl}|{buy['paired_exit_px']}|{buy['sz']}".encode()
        intents.append(
            {
                "level": lvl,
                "instId": buy["instId"],
                "tdMode": "cash",
                "side": "sell",
                "ordType": "limit",
                "px": buy["paired_exit_px"],
                "sz": buy["sz"],
                "clOrdId": f"{SELL_INTENT_PREFIX}{hashlib.sha256(seed).hexdigest()[:16]}",
                "paired_buy_clOrdId": v2_cl_ord_id(plan["plan_id"], buy),
                "paired_buy_px": buy["px"],
                "gross_step_usdt": float(
                    ((exit_px - Decimal(buy["px"])) * Decimal(buy["sz"])).quantize(
                        Decimal("0.0001")
                    )
                ),
                "sendable": False,
            }
        )
    doc = {
        "schema": SCHEMA_SELL_INTENTS,
        "strategy_id": STRATEGY_ID,
        "oms_version": OMS_VERSION,
        "kind": "g1b_paired_exit_sell_ladder_intents",
        "mode": "dry_run",
        "action": "build_intent_only",
        "will_send_http": False,
        "http_sent": False,
        "orders_placed": 0,
        "sendable": False,
        "plan_id": plan["plan_id"],
        "filled_levels": sorted(filled_levels),
        "intents": intents,
        "totals": {
            "intents": len(intents),
            "notional_usdt": float(sum(Decimal(i["px"]) * Decimal(i["sz"]) for i in intents)),
        },
        "note": "sizes ignore fee dust on the buy fill; sending sells needs a separate "
        "04-risk pass",
        "risk_notes": RISK_NOTES,
    }
    if now is not None:
        doc["generated_at"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return doc


# --------------------------------------------------------------------------- cli


def _add_send_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--will-send-http",
        action="store_true",
        help="arm OKX *demo* sends; still needs OKX_SIMULATED=1 + OKX_* keys + demo host "
        "(else exit 3)",
    )
    p.add_argument("--base-url", default=OKX_DEMO_BASE_URL, help="demo allow-list only")
    p.add_argument("--timeout", type=float, default=10.0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{STRATEGY_ID} OMS-v2: dry-run by default (same as Phase A). --will-send-http "
        "places / cancels / amends ETH-USDT spot limit buys on OKX demo only, and only when the "
        "demo env + host checks pass. Merge ≠ arming.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    plan = sub.add_parser(
        "plan", help="build the intent plan; with --will-send-http place it on demo"
    )
    plan.add_argument("--inst-id", default=APPROVED_INST_ID)
    plan.add_argument("--ref-px", default="2500", help="reference price (input; not fetched)")
    plan.add_argument("--lower-px", default="2300")
    plan.add_argument("--upper-px", default="2500")
    plan.add_argument("--levels", type=int, default=10, help=f"buy levels, <= {MAX_LEVELS}")
    plan.add_argument("--total-notional", default="100", help=f"USDT, <= {MAX_TOTAL_NOTIONAL_USDT}")
    plan.add_argument("--sl-trigger-px", default="2150", help="absolute stop-loss, required")
    plan.add_argument("--ord-type", default="limit", choices=sorted(ALLOWED_ORD_TYPES))
    plan.add_argument("--live", action="store_true", help="refused (exit 3)")
    plan.add_argument("--auto-reband", action="store_true", help="refused (exit 3)")
    plan.add_argument("--out", default="", help="also write the JSON artifact here")
    plan.add_argument("--with-timestamp", action="store_true", help="add generated_at (UTC)")
    _add_send_flags(plan)

    ct = sub.add_parser(
        "cancel-tagged", help=f"cancel pending orders with clOrdId prefix {OMS_CL_ORD_ID_PREFIX}"
    )
    _add_send_flags(ct)

    c = sub.add_parser("cancel", help="cancel one OMS-v2 order by clOrdId")
    c.add_argument("--cl-ord-id", required=True)
    _add_send_flags(c)

    a = sub.add_parser("amend", help="amend / replace one OMS-v2 order (newPx and/or newSz)")
    a.add_argument("--cl-ord-id", required=True)
    a.add_argument("--new-px", default="")
    a.add_argument("--new-sz", default="")
    a.add_argument("--ref-px", default="", help="if given, newPx must stay below it")
    _add_send_flags(a)

    s = sub.add_parser(
        "sell-intents", help="G1b stub: paired sell intents for filled levels (never sent)"
    )
    s.add_argument("--plan-json", required=True, help="a plan artifact written by `plan --out`")
    s.add_argument("--filled-levels", required=True, help="comma-separated levels, e.g. 1,2")
    s.add_argument("--out", default="")

    sub.add_parser("policy", help="print the OMS-v2 policy (offline)")
    sub.add_parser("check-env", help="report demo env contract (set/unset only; no values)")
    return p


def _dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _refused(exc: RiskPrecheckRefused, will_send_http: bool) -> dict:
    return {
        "refused": True,
        "code": exc.code,
        "reason": exc.reason,
        "will_send_http": will_send_http,
        "http_sent": False,
        "orders_placed": 0,
        "strategy_id": STRATEGY_ID,
        "oms_version": OMS_VERSION,
    }


def _api_error(exc: Exception, will_send_http: bool, http_sent: bool) -> dict:
    return {
        "error": True,
        "kind": type(exc).__name__,
        "detail": str(exc),
        "will_send_http": will_send_http,
        "http_sent": http_sent,
        "strategy_id": STRATEGY_ID,
        "oms_version": OMS_VERSION,
    }


def _cmd_plan(args) -> int:
    cfg = GridIntentConfig(
        inst_id=args.inst_id,
        ref_px=args.ref_px,
        lower_px=args.lower_px,
        upper_px=args.upper_px,
        levels=args.levels,
        total_notional_usdt=args.total_notional,
        sl_trigger_px=args.sl_trigger_px,
        ord_type=args.ord_type,
        live=args.live,
        will_send_http=args.will_send_http,
        auto_reband=args.auto_reband,
    )
    oms = DemoOmsV2(
        will_send_http=args.will_send_http, base_url=args.base_url, timeout=args.timeout
    )
    try:
        plan = oms.build_plan(cfg, now=datetime.now(UTC) if args.with_timestamp else None)
        report = oms.submit(plan)
    except RiskPrecheckRefused as exc:
        print(_dump(_refused(exc, args.will_send_http)))
        return 3
    except (DemoApiError, urllib.error.URLError, OSError) as exc:
        sent = oms._sender is not None and oms._sender.requests_made > 0
        print(_dump(_api_error(exc, args.will_send_http, sent)))
        return 1
    doc = {
        "schema": SCHEMA_RUN,
        "strategy_id": STRATEGY_ID,
        "oms_version": OMS_VERSION,
        "mode": "demo_send" if report.http_sent else "dry_run",
        "will_send_http": args.will_send_http,
        "http_sent": report.http_sent,
        "orders_placed": report.orders_placed,
        "policy": POLICY,
        "plan": plan,
        "execution": report.to_dict(),
        "risk_notes": RISK_NOTES,
    }
    text = _dump(doc)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


def _cmd_cancel_tagged(args) -> int:
    if not args.will_send_http:
        print(
            _dump(
                {
                    "schema": "e2_demo_oms_v2_cancel_tagged_v1",
                    "oms_version": OMS_VERSION,
                    "mode": "dry_run",
                    "will_send_http": False,
                    "http_sent": False,
                    "prefix": OMS_CL_ORD_ID_PREFIX,
                    "note": "listing pending orders needs --will-send-http (signed GET); only "
                    f"clOrdId prefix {OMS_CL_ORD_ID_PREFIX!r} would be cancelled, foreign "
                    "orders skipped",
                    "cancelled": [],
                }
            )
        )
        return 0
    oms = DemoOmsV2(will_send_http=True, base_url=args.base_url, timeout=args.timeout)
    try:
        print(_dump(oms.cancel_tagged()))
    except RiskPrecheckRefused as exc:
        print(_dump(_refused(exc, True)))
        return 3
    except (DemoApiError, urllib.error.URLError, OSError) as exc:
        sent = oms._sender is not None and oms._sender.requests_made > 0
        print(_dump(_api_error(exc, True, sent)))
        return 1
    return 0


def _cmd_single(args) -> int:
    armed = args.will_send_http
    oms = DemoOmsV2(will_send_http=armed, base_url=args.base_url, timeout=args.timeout)
    try:
        if args.cmd == "cancel":
            assert_own_cl_ord_id(args.cl_ord_id)
            body = {"instId": APPROVED_INST_ID, "clOrdId": args.cl_ord_id}
            path = PATH_CANCEL
        else:
            body = assert_amend_allowed(
                args.cl_ord_id,
                new_px=args.new_px or None,
                new_sz=args.new_sz or None,
                ref_px=args.ref_px or None,
            )
            path = PATH_AMEND
        if not armed:
            print(
                _dump(
                    {
                        "schema": f"e2_demo_oms_v2_{args.cmd}_v1",
                        "oms_version": OMS_VERSION,
                        "mode": "dry_run",
                        "will_send_http": False,
                        "http_sent": False,
                        "endpoint_label": f"POST {path} (not called: will_send_http=False)",
                        "body": body,
                    }
                )
            )
            return 0
        if args.cmd == "cancel":
            row = oms.cancel_order(args.cl_ord_id)
        else:
            row = oms.amend_order(
                args.cl_ord_id,
                new_px=args.new_px or None,
                new_sz=args.new_sz or None,
                ref_px=args.ref_px or None,
            )
    except RiskPrecheckRefused as exc:
        print(_dump(_refused(exc, armed)))
        return 3
    except (DemoApiError, urllib.error.URLError, OSError) as exc:
        sent = oms._sender is not None and oms._sender.requests_made > 0
        print(_dump(_api_error(exc, armed, sent)))
        return 1
    print(
        _dump(
            {
                "schema": f"e2_demo_oms_v2_{args.cmd}_v1",
                "oms_version": OMS_VERSION,
                "mode": "demo_send",
                "will_send_http": True,
                "http_sent": True,
                "body": body,
                "result": row,
            }
        )
    )
    return 0


def _cmd_sell_intents(args) -> int:
    with open(args.plan_json, encoding="utf-8") as f:
        raw = json.load(f)
    plan = raw.get("plan", raw)  # accept a `plan --out` artifact of either module
    try:
        levels = [int(x) for x in args.filled_levels.split(",") if x.strip()]
        doc = build_sell_intents(plan, levels)
    except RiskPrecheckRefused as exc:
        print(_dump(_refused(exc, False)))
        return 3
    except ValueError as exc:
        print(_dump(_refused(RiskPrecheckRefused("BadFilledLevels", str(exc)), False)))
        return 3
    text = _dump(doc)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:] or ["plan"])
    cmd = args.cmd or "plan"
    if cmd == "policy":
        print(_dump({"policy": POLICY, "risk_notes": RISK_NOTES}))
        return 0
    if cmd == "check-env":
        check = check_send_env()
        print(
            _dump(
                {
                    "demo_env": check.to_dict(),
                    "will_send_http": False,
                    "note": "env ok ≠ permission to send; arming needs 04-risk + user confirm "
                    "+ --will-send-http",
                }
            )
        )
        return 0
    if cmd == "plan":
        return _cmd_plan(args)
    if cmd == "cancel-tagged":
        return _cmd_cancel_tagged(args)
    if cmd in ("cancel", "amend"):
        return _cmd_single(args)
    if cmd == "sell-intents":
        return _cmd_sell_intents(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
