#!/usr/bin/env python3
"""E2 Phase A — demo OMS intent builder for the E2-G1 ETH-USDT spot layered limit grid.

Phase A is *connectivity dry-run only*: this module builds an order-intent plan (the JSON
that a Phase B demo executor would one day hand to OKX simulated trading) and stops there.

Hard gates (enforced in code, not just in docs):
  * ``will_send_http`` defaults to False and the module has no HTTP code at all: no urllib,
    no third-party HTTP client, no python-okx import. ``DemoOms.place_order`` is a loud refusal.
  * ``will_send_http=True`` is refused in Phase A. Without the demo env checks (OKX_SIMULATED=1
    + the three OKX_* keys) the refusal is ``DemoEnvCheckFailed``; with them it is still
    refused (``PhaseASendNotImplemented``): the gated demo executor lives in the sibling
    ``strategies/e2_demo_oms_v2.py`` (OMS-v2) and still needs its own 04-risk pass + explicit
    user confirmation per run. This file stays HTTP-free.
  * Risk prechecks reject: ``live=True``, missing / misplaced absolute stop-loss, total
    notional > 100 USDT, > 10 levels, any instrument other than ETH-USDT, non-``cash``
    tdMode or lever != 1, market / IOC / FOK order types, buy levels at or above the
    reference price (taker sweep), ``auto_reband=True``, missing simulated-trading metadata.
  * Secrets are never read into the plan; the env check only reports set / unset.

Explicitly NOT implemented here: place / amend / cancel order HTTP, withdraw, transfer,
market or IOC sweeps, auto re-banding, T1 / T2 arbitrage execution. The official OKX grid
bots (A / B′ / C) are untouched: this module holds no bot identifier at all.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

SCHEMA = "e2_demo_oms_intent_plan_v1"
STRATEGY_ID = "E2-G1"
STRATEGY_LABEL = "ETH-USDT spot layered limit grid executor (Phase A: intent builder only)"
PHASE = "A"
APPROVED_INST_ID = "ETH-USDT"
MAX_LEVELS = 10
MAX_TOTAL_NOTIONAL_USDT = Decimal("100")
ALLOWED_ORD_TYPES = frozenset({"limit", "post_only"})
FORBIDDEN_ORD_TYPES = frozenset(
    {"market", "ioc", "fok", "optimal_limit_ioc", "mmp", "mmp_and_post_only"}
)
CL_ORD_ID_PREFIX = "e2g1a"
ORDER_TAG = "e2g1phaseA"

ENV_KEY = "OKX_API_KEY"
ENV_SECRET = "OKX_API_SECRET"
ENV_PASSPHRASE = "OKX_API_PASSPHRASE"
ENV_SIMULATED = "OKX_SIMULATED"

# Static ETH-USDT spot instrument meta. Deliberately NOT fetched: Phase A has no network path.
STATIC_INSTRUMENT = {
    "instId": APPROVED_INST_ID,
    "instType": "SPOT",
    "tickSz": "0.01",
    "lotSz": "0.000001",
    "minSz": "0.001",
    "source": "static_default_not_fetched",
}

PHASE_B_REQUIRES = [
    "separate 04-risk review for Phase B (demo orders) — this PR does not ask for it",
    "explicit user confirmation in chat (not model self-confirm)",
    f"{ENV_SIMULATED}=1 + {ENV_KEY}/{ENV_SECRET}/{ENV_PASSPHRASE} for OKX demo trading only",
    "the gated executor is strategies/e2_demo_oms_v2.py (OMS-v2); this Phase A file stays "
    "HTTP-free",
]

RISK_NOTES = [
    "Phase A = connectivity dry-run: builds intents, sends nothing, places nothing",
    "buy ladder only; every level is a resting limit below the reference price (no taker sweep)",
    "stop-loss is an absolute price below the lowest level; on trigger: cancel intents, "
    "human review, no auto-restart",
    "no auto re-banding: a moved range is a new plan that needs a new confirmation",
    "official OKX grid bots A / B′ / C are untouched; this module holds no bot identifier",
    "T1 / T2 arbitrage live paths, withdraw, transfer, market / IOC orders: out of scope, refused",
]

POLICY = {
    "strategy_id": STRATEGY_ID,
    "phase": PHASE,
    "mode": "dry_run",
    "will_send_http": False,
    "http_code_present": False,
    "trading_http": {
        "order": False,
        "amend": False,
        "cancel": False,
        "transfer": False,
        "withdraw": False,
    },
    "venue_label": "okx_demo (intent metadata only; no socket is ever opened here)",
    "approved_inst_ids": [APPROVED_INST_ID],
    "max_levels": MAX_LEVELS,
    "max_total_notional_usdt": float(MAX_TOTAL_NOTIONAL_USDT),
    "td_mode": "cash",
    "lever": 1,
    "ord_types_allowed": sorted(ALLOWED_ORD_TYPES),
    "ord_types_forbidden": sorted(FORBIDDEN_ORD_TYPES),
    "auto_reband": False,
    "official_bots_untouched": ["A", "B′", "C"],
    "phase_b_requires": PHASE_B_REQUIRES,
}


class RiskPrecheckRefused(RuntimeError):
    """Raised before any plan is built when a config would break a Phase A gate."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


class SendRefused(RiskPrecheckRefused):
    """``will_send_http=True`` (or any place-order attempt) inside Phase A."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class GridIntentConfig:
    """Inputs for one E2-G1 buy-ladder plan. Prices are absolute USDT quotes."""

    inst_id: str = APPROVED_INST_ID
    ref_px: str = "2500"
    lower_px: str = "2300"
    upper_px: str = "2500"
    levels: int = 10
    total_notional_usdt: str = "100"
    sl_trigger_px: str | None = "2150"
    ord_type: str = "limit"
    td_mode: str = "cash"
    lever: int = 1
    live: bool = False
    will_send_http: bool = False
    auto_reband: bool = False
    intent_metadata: dict = field(
        default_factory=lambda: {
            "x-simulated-trading": "1",
            "simulated_trading": True,
            "venue": "okx_demo",
            "demo_only": True,
        }
    )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DemoEnvCheck:
    """Result of the demo-env inspection. Values are never stored, only set / unset."""

    simulated: bool
    keys_present: bool
    ok: bool
    detail: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _redact(value: str | None) -> str:
    return f"<set:{len(value)} chars>" if value else "<unset>"


def check_demo_env(env: dict[str, str] | None = None) -> DemoEnvCheck:
    """Report whether the OKX *demo* env contract is met. Never returns secret values."""
    e = os.environ if env is None else env
    key, secret, pw = e.get(ENV_KEY, ""), e.get(ENV_SECRET, ""), e.get(ENV_PASSPHRASE, "")
    simulated = e.get(ENV_SIMULATED, "") == "1"
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
        },
    )


# --------------------------------------------------------------------------- prechecks


def _dec(value, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        raise RiskPrecheckRefused("BadNumber", f"{name}={value!r} is not a number") from None
    if not d.is_finite():
        raise RiskPrecheckRefused("BadNumber", f"{name}={value!r} must be finite")
    return d


def run_prechecks(cfg: GridIntentConfig, env: dict[str, str] | None = None) -> list[dict]:
    """Return the ordered list of passed checks; raise ``RiskPrecheckRefused`` on the first failure.

    The order matters for reviewers: the *send* gates come first so that a plan is never
    even sized when someone asks for live or HTTP.
    """
    passed: list[dict] = []

    def ok(code: str, note: str) -> None:
        passed.append({"code": code, "ok": True, "note": note})

    if cfg.live:
        raise RiskPrecheckRefused("LiveFlagRefused", "live=True is refused in every phase")
    ok("live_flag", "live=False")

    if cfg.will_send_http:
        check = check_demo_env(env)
        if not check.ok:
            raise SendRefused(
                "DemoEnvCheckFailed",
                f"will_send_http=True requires {ENV_SIMULATED}=1 and the three OKX_* keys; "
                f"and even then Phase A refuses to send ({check.detail})",
            )
        raise SendRefused(
            "PhaseASendNotImplemented",
            "will_send_http=True is refused: Phase A builds intents only; the gated demo "
            "executor is strategies/e2_demo_oms_v2.py (OMS-v2) and still needs 04-risk + "
            "explicit user confirmation per run",
        )
    ok("will_send_http", "will_send_http=False (default)")

    meta = cfg.intent_metadata or {}
    if not (
        meta.get("x-simulated-trading") == "1"
        and meta.get("simulated_trading") is True
        and meta.get("venue") == "okx_demo"
        and meta.get("demo_only") is True
    ):
        raise RiskPrecheckRefused(
            "SimulatedIntentMetadataMissing",
            "intent_metadata must carry x-simulated-trading='1', simulated_trading=True, "
            "venue='okx_demo', demo_only=True",
        )
    ok("simulated_intent_metadata", "x-simulated-trading=1 / venue=okx_demo / demo_only")

    if cfg.inst_id != APPROVED_INST_ID:
        raise RiskPrecheckRefused(
            "InstrumentNotApproved", f"{cfg.inst_id!r} is not approved for {STRATEGY_ID}"
        )
    ok("instrument", cfg.inst_id)

    if cfg.td_mode != "cash" or cfg.lever != 1:
        raise RiskPrecheckRefused(
            "TdModeForbidden",
            f"tdMode must be 'cash' with lever=1 (got {cfg.td_mode!r}, lever={cfg.lever})",
        )
    ok("td_mode", "cash / 1x")

    if cfg.ord_type in FORBIDDEN_ORD_TYPES or cfg.ord_type not in ALLOWED_ORD_TYPES:
        raise RiskPrecheckRefused(
            "OrderTypeForbidden",
            f"ordType {cfg.ord_type!r} refused; allowed: {sorted(ALLOWED_ORD_TYPES)}",
        )
    ok("ord_type", cfg.ord_type)

    if cfg.auto_reband:
        raise RiskPrecheckRefused("AutoRebandRefused", "auto_reband=True is refused")
    ok("auto_reband", "off")

    if not (1 <= cfg.levels <= MAX_LEVELS):
        raise RiskPrecheckRefused(
            "LevelsOverCap", f"levels={cfg.levels} must be within 1..{MAX_LEVELS}"
        )
    ok("levels", f"{cfg.levels} <= {MAX_LEVELS}")

    total = _dec(cfg.total_notional_usdt, "total_notional_usdt")
    if total <= 0 or total > MAX_TOTAL_NOTIONAL_USDT:
        raise RiskPrecheckRefused(
            "NotionalOverCap",
            f"total_notional_usdt={total} must be within (0, {MAX_TOTAL_NOTIONAL_USDT}]",
        )
    ok("total_notional", f"{total} <= {MAX_TOTAL_NOTIONAL_USDT}")

    lower = _dec(cfg.lower_px, "lower_px")
    upper = _dec(cfg.upper_px, "upper_px")
    ref = _dec(cfg.ref_px, "ref_px")
    if not (0 < lower < upper):
        raise RiskPrecheckRefused("BadRange", f"need 0 < lower_px ({lower}) < upper_px ({upper})")
    if ref <= 0:
        raise RiskPrecheckRefused("BadRange", f"ref_px must be > 0 (got {ref})")
    step = (upper - lower) / cfg.levels
    highest_buy = upper - step
    if highest_buy >= ref:
        raise RiskPrecheckRefused(
            "BuyLevelAboveReference",
            f"highest buy level {highest_buy} >= ref_px {ref}: a resting limit at or above the "
            "reference would cross the book (taker sweep) — refused",
        )
    ok("range", f"{lower}..{upper} step {step} below ref {ref}")

    if cfg.sl_trigger_px in (None, ""):
        raise RiskPrecheckRefused(
            "StopLossMissing", "sl_trigger_px (absolute price) is required for every plan"
        )
    sl = _dec(cfg.sl_trigger_px, "sl_trigger_px")
    if not (0 < sl < lower):
        raise RiskPrecheckRefused(
            "StopLossPlacement",
            f"sl_trigger_px={sl} must be an absolute price strictly below lower_px={lower}",
        )
    ok("stop_loss", f"absolute {sl} < lower {lower}")
    return passed


# --------------------------------------------------------------------------- plan builder


def _quantize_down(value: Decimal, unit: Decimal) -> Decimal:
    return (value / unit).to_integral_value(rounding=ROUND_DOWN) * unit


def _quantize_px(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_HALF_EVEN) * tick


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


def _cl_ord_id(level: int, px: str, sz: str) -> str:
    seed = f"{STRATEGY_ID}|{APPROVED_INST_ID}|{level}|{px}|{sz}".encode()
    digest = hashlib.sha256(seed).hexdigest()
    return f"{CL_ORD_ID_PREFIX}{digest[:16]}"


def build_intent_plan(
    cfg: GridIntentConfig,
    *,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Run the prechecks, then build a deterministic buy-ladder intent plan.

    Deterministic by construction: no clock is consulted unless ``now`` is given, prices and
    sizes are Decimal-quantised to the static tick / lot, and ``plan_id`` is a SHA-256 over the
    orders. Nothing here touches the network.
    """
    prechecks = run_prechecks(cfg, env)

    tick = Decimal(STATIC_INSTRUMENT["tickSz"])
    lot = Decimal(STATIC_INSTRUMENT["lotSz"])
    min_sz = Decimal(STATIC_INSTRUMENT["minSz"])
    lower = Decimal(cfg.lower_px)
    upper = Decimal(cfg.upper_px)
    total = Decimal(cfg.total_notional_usdt)
    step = (upper - lower) / cfg.levels
    per_level = total / cfg.levels

    orders: list[dict] = []
    notional_sum = Decimal("0")
    for i in range(cfg.levels):
        px = _quantize_px(lower + step * i, tick)
        exit_px = _quantize_px(lower + step * (i + 1), tick)
        sz = _quantize_down(per_level / px, lot)
        if sz < min_sz:
            raise RiskPrecheckRefused(
                "SizeBelowMin",
                f"level {i + 1}: sz {sz} < minSz {min_sz} at px {px} "
                f"(per-level notional {per_level} too small)",
            )
        notional = (sz * px).quantize(Decimal("0.0001"))
        notional_sum += notional
        px_s, sz_s = _fmt(px), _fmt(sz)
        orders.append(
            {
                "level": i + 1,
                "instId": cfg.inst_id,
                "tdMode": cfg.td_mode,
                "side": "buy",
                "ordType": cfg.ord_type,
                "px": px_s,
                "sz": sz_s,
                "clOrdId": _cl_ord_id(i + 1, px_s, sz_s),
                "tag": ORDER_TAG,
                "notional_usdt": float(notional),
                "paired_exit_px": _fmt(exit_px),
                "paired_exit_note": "sell intent only after this level fills; not built in Phase A",
            }
        )
    if notional_sum > MAX_TOTAL_NOTIONAL_USDT:
        raise RiskPrecheckRefused(
            "NotionalOverCap", f"sized notional {notional_sum} > {MAX_TOTAL_NOTIONAL_USDT}"
        )

    canonical = json.dumps(orders, sort_keys=True, separators=(",", ":")).encode()
    plan_id = hashlib.sha256(canonical).hexdigest()
    plan = {
        "schema": SCHEMA,
        "strategy_id": STRATEGY_ID,
        "strategy_label": STRATEGY_LABEL,
        "phase": PHASE,
        "mode": "dry_run",
        "action": "build_intent_only",
        "will_send_http": False,
        "http_sent": False,
        "orders_placed": 0,
        "venue": "okx_demo",
        "intent_metadata": dict(cfg.intent_metadata),
        "endpoint_label": "POST /api/v5/trade/order (label only — never called in Phase A)",
        "policy": POLICY,
        "config": cfg.to_dict(),
        "instrument": dict(STATIC_INSTRUMENT),
        "grid": {
            "kind": "buy_ladder_resting_limits",
            "levels": cfg.levels,
            "lower_px": _fmt(lower),
            "upper_px": _fmt(upper),
            "ref_px": _fmt(Decimal(cfg.ref_px)),
            "step_px": _fmt(_quantize_px(step, tick)),
            "highest_buy_px": orders[-1]["px"],
        },
        "orders": orders,
        "totals": {
            "orders": len(orders),
            "notional_usdt": float(notional_sum),
            "cap_notional_usdt": float(MAX_TOTAL_NOTIONAL_USDT),
            "cap_levels": MAX_LEVELS,
            "requested_notional_usdt": float(total),
        },
        "stop_loss": {
            "slTriggerPx": _fmt(Decimal(cfg.sl_trigger_px)),
            "basis": "absolute_px",
            "on_trigger": "cancel all open intents → human review; no auto-restart, no re-band",
        },
        "prechecks": prechecks,
        "plan_id": plan_id,
        "phase_b_requires": PHASE_B_REQUIRES,
        "risk_notes": RISK_NOTES,
    }
    if now is not None:
        plan["generated_at"] = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return plan


# --------------------------------------------------------------------------- oms


@dataclass(frozen=True)
class SubmitResult:
    sent: bool
    orders_placed: int
    reason: str
    plan_id: str

    def to_dict(self) -> dict:
        return asdict(self)


class DemoOms:
    """Phase A OMS: build plans, never send.

    ``submit`` is the seam a Phase B executor would replace; here it records the intent and
    returns ``sent=False``. ``place_order`` / ``amend_order`` / ``cancel_order`` exist only so
    that any accidental call fails loudly instead of silently no-op'ing.
    """

    def __init__(self, *, will_send_http: bool = False, env: dict[str, str] | None = None):
        self.will_send_http = will_send_http
        self._env = env
        self.submitted: list[str] = []
        self.place_order_calls = 0

    def build_plan(self, cfg: GridIntentConfig, *, now: datetime | None = None) -> dict:
        if self.will_send_http and not cfg.will_send_http:
            cfg = GridIntentConfig(**{**cfg.to_dict(), "will_send_http": True})
        return build_intent_plan(cfg, env=self._env, now=now)

    def submit(self, plan: dict) -> SubmitResult:
        if self.will_send_http or plan.get("will_send_http") is not False:
            raise SendRefused(
                "PhaseASendNotImplemented",
                "DemoOms.submit refuses to send: Phase A is dry-run only",
            )
        self.submitted.append(plan["plan_id"])
        return SubmitResult(
            sent=False,
            orders_placed=0,
            reason="phase_a_dry_run: intents recorded, nothing sent",
            plan_id=plan["plan_id"],
        )

    # ---- explicitly not implemented (loud failures, not silent no-ops)

    def place_order(self, *_a, **_k):
        self.place_order_calls += 1
        raise SendRefused("PlaceOrderRefused", "place_order is not implemented in Phase A")

    def amend_order(self, *_a, **_k):
        raise SendRefused("AmendRefused", "amend_order is not implemented in Phase A")

    def cancel_order(self, *_a, **_k):
        raise SendRefused("CancelRefused", "cancel_order is not implemented in Phase A")


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=f"{STRATEGY_ID} Phase A demo OMS: build an ETH-USDT buy-ladder intent plan "
        "(dry-run JSON). Never sends HTTP; will_send_http is refused.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    plan = sub.add_parser("plan", help="build the intent plan (default command)")
    plan.add_argument("--inst-id", default=APPROVED_INST_ID)
    plan.add_argument("--ref-px", default="2500", help="reference price (input; not fetched)")
    plan.add_argument("--lower-px", default="2300")
    plan.add_argument("--upper-px", default="2500")
    plan.add_argument("--levels", type=int, default=10, help=f"buy levels, <= {MAX_LEVELS}")
    plan.add_argument(
        "--total-notional",
        default="100",
        help=f"USDT across all levels, <= {MAX_TOTAL_NOTIONAL_USDT}",
    )
    plan.add_argument("--sl-trigger-px", default="2150", help="absolute stop-loss, required")
    plan.add_argument("--ord-type", default="limit", choices=sorted(ALLOWED_ORD_TYPES))
    plan.add_argument("--live", action="store_true", help="refused (exit 3)")
    plan.add_argument("--will-send-http", action="store_true", help="refused in Phase A (exit 3)")
    plan.add_argument("--auto-reband", action="store_true", help="refused (exit 3)")
    plan.add_argument("--out", default="", help="also write the JSON artifact here")
    plan.add_argument("--with-timestamp", action="store_true", help="add generated_at (UTC)")

    sub.add_parser("policy", help="print the Phase A policy (offline)")
    sub.add_parser("check-env", help="report demo env contract (set/unset only; no values)")
    return p


def _dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:] or ["plan"])
    cmd = args.cmd or "plan"

    if cmd == "policy":
        print(_dump({"policy": POLICY, "risk_notes": RISK_NOTES}))
        return 0
    if cmd == "check-env":
        check = check_demo_env()
        print(
            _dump(
                {
                    "demo_env": check.to_dict(),
                    "will_send_http": False,
                    "note": "env ok ≠ permission to send; Phase A never sends",
                }
            )
        )
        return 0

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
    try:
        oms = DemoOms(will_send_http=args.will_send_http)
        plan = oms.build_plan(cfg, now=datetime.now(UTC) if args.with_timestamp else None)
        result = oms.submit(plan)
    except RiskPrecheckRefused as exc:
        print(
            _dump(
                {
                    "refused": True,
                    "code": exc.code,
                    "reason": exc.reason,
                    "will_send_http": False,
                    "http_sent": False,
                    "orders_placed": 0,
                    "strategy_id": STRATEGY_ID,
                    "phase": PHASE,
                }
            )
        )
        return 3
    plan["submit_result"] = result.to_dict()
    text = _dump(plan)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
