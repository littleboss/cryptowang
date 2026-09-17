#!/usr/bin/env python3
"""Paper combo scanner — Phase B, read-only, observe-only (hedged combos / relative value).

Implements proposal `paper-combo-strategies-phase-b-v1` under the 04-risk conditional pass
(read-only metrics + local paper fills only). Three families, same venue, all
`taxonomy = relative_value`:

  B1  spot long + perp short + short OTM call   hedge_mode = static_combo
  B2  calendar (long far / short near, same K)  hedge_mode = paper_delta_sim_only
  B3  25Δ risk-reversal, short wing covered     hedge_mode = options_rr_static
                                                 (alt: options_rr_plus_paper_delta)

Built on strategies/paper_arb_scanner.py (Phase A): Book / Leg / Snapshot / fixture loader /
liquidity + persistence filters / OKX public snapshot source are reused, not copied. Phase A
scanners (A1/A2/A3) are untouched and keep running in parallel; A1 is evaluated on the same
snapshots as the H-B1 comparator.

Hard gates (enforced in code, tested, not just documented):
  * every record: action == "observe_only", will_send_http == False, phase == "B",
    taxonomy == "relative_value"; forbidden labels (risk_free / 无风险 / 稳赚 / guaranteed)
    anywhere in a record are refused;
  * executable prices are bid/ask only (Phase A `Leg` gate). Mark / mid are carried as
    reference fields only — implied vol, delta and the model "fair" values are derived from
    mid quotes and are *selection / reference* quantities, never executable prices;
  * no order / amend / withdraw / transfer code path; the only network I/O is the public GET
    order-book / funding / instruments path of tools/okx_readonly_client.py;
  * leverage concept 1x; no naked short option: B1's short call must be covered by the spot
    leg's notional (qty >= call qty), B2's short near leg by the long far leg (same type,
    same strike, expiry >= near — a reverse calendar is refused), B3's short wing by a further
    OTM long option of the same type (a plain RR without the wing is *not built*);
  * B2 delta hedging is paper-simulated only (`paper_delta_sim.live_hedge_http == False`);
    the simulated hedge cost goes into `costs_bps.hedge_rebalance`, never into "residual";
  * safety_buffer_bps is the sum of components (fee_roundtrip + slip + funding_uncert +
    vol_path_haircut + model_haircut), `calibrated: false` unless the caller says otherwise.
    Nothing here is a return promise.

The spot-grid / local_paper mainline is untouched: this module does not import or alter
tools/local_paper_grid.py or strategies/grid_ab_compare.py.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "strategies"))

import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
from paper_arb_scanner import (  # noqa: E402
    Book,
    ExecutablePriceViolation,
    FeeSchedule,
    Funding,
    Leg,
    NakedShortOptionRefused,
    ObserveOnlyViolation,
    PersistenceTracker,
    Snapshot,
    bps,
    iso_cst,
    liquidity_check,
    load_fixture,
    make_leg,
    option_fee_quote,
    premium_to_quote,
    years_between,
)

# --------------------------------------------------------------------------- constants

PHASE = "B"
ACTION = pas.ACTION
WILL_SEND_HTTP = pas.WILL_SEND_HTTP
MODE = pas.MODE
DEFAULT_VENUE = pas.DEFAULT_VENUE
TAXONOMY = pas.TAXONOMY_RV

FAMILY_B1 = "B1_covered_call_carry"
FAMILY_B2 = "B2_calendar_vol"
FAMILY_B3 = "B3_put_skew_rr"
FAMILIES = (FAMILY_B1, FAMILY_B2, FAMILY_B3)
COMBO_ID = {FAMILY_B1: "B1", FAMILY_B2: "B2", FAMILY_B3: "B3"}

HEDGE_STATIC = "static_combo"
HEDGE_PAPER_DELTA = "paper_delta_sim_only"
HEDGE_RR_STATIC = "options_rr_static"
HEDGE_RR_PAPER_DELTA = "options_rr_plus_paper_delta"
HEDGE_MODES = {
    FAMILY_B1: (HEDGE_STATIC,),
    FAMILY_B2: (HEDGE_PAPER_DELTA,),
    FAMILY_B3: (HEDGE_RR_STATIC, HEDGE_RR_PAPER_DELTA),
}

HYPOTHESIS = {FAMILY_B1: "H-B1", FAMILY_B2: "H-B2", FAMILY_B3: "H-B3"}
FALSIFY_IF = {
    "H-B1": "pass_rate_near_zero_or_path_expectation_nonpositive_or_worse_than_A1_after_call_tail",
    "H-B2": "after_paper_hedge_costs_pass_rate_near_zero_or_edge_only_exists_on_mid_mark",
    "H-B3": "executable_RR_deviation_within_fee_band_or_mean_reversion_fails_to_positive_"
    "expectation",
}

# Mandatory residual-risk names per family (proposal §3 / §4). A record missing any is refused.
RESIDUAL_RISKS = {
    FAMILY_B1: ("gamma", "funding_flip", "gap", "margin", "basis", "capped_upside"),
    FAMILY_B2: (
        "gamma",
        "vega_term_structure",
        "gap",
        "margin",
        "hedge_slippage_underestimation",
        "model_risk",
    ),
    FAMILY_B3: ("skew_trend", "gamma", "gap", "margin", "spot_direction_bleed", "liquidity"),
}

# Substrings that may never appear anywhere in an emitted record (case-insensitive).
FORBIDDEN_LABELS = ("risk_free", "riskfree", "risk-free", "无风险", "稳赚", "guaranteed")

COST_KEYS = pas.COST_KEYS + ("vol_path_haircut",)
REQUIRED_FIELDS = pas.REQUIRED_FIELDS + ("phase", "combo_id", "hedge_mode", "residual_risks")

INVALIDATING_FLAGS = pas.INVALIDATING_FLAGS | frozenset(
    {"iv_unestimable", "rr_reference_unavailable", "hold_horizon_zero"}
)

SOURCE_INSPIRATION = pas.SOURCE_INSPIRATION
CROSS_CHECKED_INBOX = pas.CROSS_CHECKED_INBOX + ["01-inbox/2026-09-17-paper-multi-leg-arb-scan.md"]
RELATED_PHASE_A = "A1_A2_A3_scanners_remain"

DISCLAIMER = (
    "Phase B 纸面只读组合评分（observe_only，will_send_http=false）。B1/B2/B3 全部 relative_value："
    "承担 gamma / funding 翻转 / 跳空 / 保证金 / 基差 / 偏斜趋势等残留风险，不是身份套利，"
    "更不是任何形式的确定收益。净边 = bid/ask 可执行毛边 − 全成本（含 B2 纸面 delta 对冲成本）；"
    "模型公平价 / IV / delta 来自 mid，仅作参照与选约。门限 safety_buffer 为分量之和且默认未标定。"
    "不下单，不对冲，不并入网格主线。"
)

MS_PER_YEAR = pas.MS_PER_YEAR
SQRT_2PI = math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------- errors


class ComboSchemaViolation(ValueError):
    """A Phase B record broke a Phase B schema rule (taxonomy, hedge_mode, residual risks…)."""


class ForbiddenLabelViolation(ValueError):
    """A record contained a risk-free / guaranteed style label."""


# --------------------------------------------------------------------------- black-76 (reference)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float  # per 1.00 of vol (multiply by 0.01 for one vol point)
    d1: float | None


def black76(F: float, K: float, T: float, sigma: float, is_call: bool, df: float = 1.0) -> Greeks:
    """Black-76 on a forward. Used only for reference values (fair, delta, gamma, vega)."""
    if F <= 0 or K <= 0:
        raise ValueError("F and K must be > 0")
    if T <= 0 or sigma <= 0:
        intrinsic = max(F - K, 0.0) if is_call else max(K - F, 0.0)
        delta = (1.0 if F > K else 0.0) if is_call else (-1.0 if F < K else 0.0)
        return Greeks(df * intrinsic, df * delta, 0.0, 0.0, None)
    s_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s_t * s_t) / s_t
    d2 = d1 - s_t
    if is_call:
        price = df * (F * norm_cdf(d1) - K * norm_cdf(d2))
        delta = df * norm_cdf(d1)
    else:
        price = df * (K * norm_cdf(-d2) - F * norm_cdf(-d1))
        delta = -df * norm_cdf(-d1)
    gamma = df * norm_pdf(d1) / (F * s_t)
    vega = df * F * norm_pdf(d1) * math.sqrt(T)
    return Greeks(price, delta, gamma, vega, d1)


def implied_vol(
    price: float,
    F: float,
    K: float,
    T: float,
    is_call: bool,
    df: float = 1.0,
    lo: float = 1e-4,
    hi: float = 5.0,
    tol: float = 1e-9,
    max_iter: int = 200,
) -> float | None:
    """Bisection on Black-76. None when the price is outside no-arbitrage bounds or T <= 0.
    The input is a *reference* (mid) price; the result is a selection / reference quantity."""
    if T <= 0 or price <= 0 or F <= 0 or K <= 0:
        return None
    intrinsic = df * (max(F - K, 0.0) if is_call else max(K - F, 0.0))
    upper = df * (F if is_call else K)
    if price <= intrinsic + 1e-12 or price >= upper - 1e-12:
        return None
    f_lo = black76(F, K, T, lo, is_call, df).price - price
    f_hi = black76(F, K, T, hi, is_call, df).price - price
    if f_lo > 0 or f_hi < 0:
        return None
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        f_mid = black76(F, K, T, mid, is_call, df).price - price
        if abs(f_mid) < tol or (hi - lo) < 1e-10:
            return mid
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class ComboSafetyBuffer:
    """safety_buffer_bps = sum of auditable components. Phase B adds `vol_path_haircut_bps`
    (04-risk condition). fee_roundtrip None → the record's own computed fees."""

    fee_roundtrip_bps: float | None = None
    slip_buffer_bps: float = 5.0
    funding_uncert_bps: float = 5.0
    vol_path_haircut_bps: float = 10.0
    model_haircut_bps: float = 10.0
    calibrated: bool = False

    def resolve(self, fees_bps: float) -> dict:
        fee = self.fee_roundtrip_bps if self.fee_roundtrip_bps is not None else fees_bps
        comps = {
            "fee_roundtrip_bps": round(fee, 4),
            "slip_buffer_bps": self.slip_buffer_bps,
            "funding_uncert_bps": self.funding_uncert_bps,
            "vol_path_haircut_bps": self.vol_path_haircut_bps,
            "model_haircut_bps": self.model_haircut_bps,
        }
        return {
            "components": comps,
            "total_bps": round(sum(comps.values()), 4),
            "calibrated": self.calibrated,
            "rule": "net_edge_bps > safety_buffer_bps AND persistence.ok AND liquidity.ok "
            "AND no invalidating risk_flags",
            "note": "uncalibrated buffer → no 'tradable edge' claim may be made",
        }

    def to_dict(self) -> dict:
        return {
            "fee_roundtrip_bps": self.fee_roundtrip_bps,
            "slip_buffer_bps": self.slip_buffer_bps,
            "funding_uncert_bps": self.funding_uncert_bps,
            "vol_path_haircut_bps": self.vol_path_haircut_bps,
            "model_haircut_bps": self.model_haircut_bps,
            "calibrated": self.calibrated,
        }


@dataclass(frozen=True)
class ComboConfig:
    qty: float = 1.0  # base units per leg; short option qty == spot qty (covered)
    horizon_intervals: int = 3  # holding horizon in funding intervals (3 × 8h = 1 day)
    ref_rate_apr: float = 0.0  # 0 = no discounting / opportunity cost claimed
    funding_sigma_bps_per_interval: float = 2.0
    transfer_bps: float = 0.0
    vol_path_haircut_bps: float = 10.0  # cost per short option leg, of underlying notional
    depth_mult: float = 2.0
    top_n: int = 5
    persistence_min_samples: int = 3
    persistence_min_sec: float = 60.0
    credit_favorable_basis: bool = False
    paper_fills: bool = False
    paper_extra_slip_bps: float = 5.0
    # B1
    b1_delta_min: float = 0.15
    b1_delta_max: float = 0.25
    b1_moneyness_min: float = 0.05  # fallback band K ∈ [S(1+min), S(1+max)] when no Δ match
    b1_moneyness_max: float = 0.30
    b1_max_calls_per_expiry: int = 2
    # B2
    b2_opt_type: str = "C"  # main scan; the other type is comparison only
    b2_max_pairs: int = 6
    hedge_instrument: str = "perp"  # perp | spot — paper-simulated only
    rebalances_per_day: float = 3.0
    hedge_slip_bps: float = 2.0
    # B3
    b3_target_delta: float = 0.25
    b3_wing_delta: float = 0.10
    b3_delta_tol: float = 0.10
    b3_rr_ref_mode: str = "rolling_mid"  # rolling_mid | fixed
    b3_rr_ref_fixed_volpts: float = 0.0
    b3_rr_ref_min_samples: int = 2
    b3_paper_delta: bool = False
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    buffer: ComboSafetyBuffer = field(default_factory=ComboSafetyBuffer)

    def validate(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.horizon_intervals < 1:
            raise ValueError("horizon_intervals must be >= 1")
        if self.persistence_min_samples < 1:
            raise ValueError("persistence_min_samples must be >= 1")
        if not (0 < self.b1_delta_min < self.b1_delta_max < 1):
            raise ValueError("b1 delta band must satisfy 0 < min < max < 1")
        if self.b2_opt_type not in ("C", "P"):
            raise ValueError("b2_opt_type must be C|P")
        if self.hedge_instrument not in ("perp", "spot"):
            raise ValueError("hedge_instrument must be perp|spot")
        if self.b3_rr_ref_mode not in ("rolling_mid", "fixed"):
            raise ValueError("b3_rr_ref_mode must be rolling_mid|fixed")
        if not (0 < self.b3_wing_delta < self.b3_target_delta < 0.5):
            raise ValueError("need 0 < wing_delta < target_delta < 0.5")
        if self.rebalances_per_day <= 0:
            raise ValueError("rebalances_per_day must be > 0")

    def horizon_years(self, interval_sec: int) -> float:
        return self.horizon_intervals * interval_sec / (365.0 * 86400.0)

    def to_dict(self) -> dict:
        return {
            "qty": self.qty,
            "horizon_intervals": self.horizon_intervals,
            "ref_rate_apr": self.ref_rate_apr,
            "funding_sigma_bps_per_interval": self.funding_sigma_bps_per_interval,
            "transfer_bps": self.transfer_bps,
            "vol_path_haircut_bps": self.vol_path_haircut_bps,
            "depth_mult": self.depth_mult,
            "top_n": self.top_n,
            "persistence_min_samples": self.persistence_min_samples,
            "persistence_min_sec": self.persistence_min_sec,
            "credit_favorable_basis": self.credit_favorable_basis,
            "paper_fills": self.paper_fills,
            "paper_extra_slip_bps": self.paper_extra_slip_bps,
            "b1": {
                "delta_band": [self.b1_delta_min, self.b1_delta_max],
                "moneyness_fallback_band": [self.b1_moneyness_min, self.b1_moneyness_max],
                "max_calls_per_expiry": self.b1_max_calls_per_expiry,
                "hedge_mode": HEDGE_STATIC,
            },
            "b2": {
                "opt_type": self.b2_opt_type,
                "max_pairs": self.b2_max_pairs,
                "hedge_instrument": self.hedge_instrument,
                "rebalances_per_day": self.rebalances_per_day,
                "hedge_slip_bps": self.hedge_slip_bps,
                "hedge_mode": HEDGE_PAPER_DELTA,
                "live_delta_hedge": False,
            },
            "b3": {
                "target_delta": self.b3_target_delta,
                "wing_delta": self.b3_wing_delta,
                "delta_tol": self.b3_delta_tol,
                "rr_ref_mode": self.b3_rr_ref_mode,
                "rr_ref_fixed_volpts": self.b3_rr_ref_fixed_volpts,
                "rr_ref_min_samples": self.b3_rr_ref_min_samples,
                "paper_delta": self.b3_paper_delta,
                "hedge_mode": HEDGE_RR_PAPER_DELTA if self.b3_paper_delta else HEDGE_RR_STATIC,
                "structure": "rr_with_wing_cover (naked RR is not built)",
            },
            "leverage_concept": 1,
            "fees": self.fees.to_dict(),
            "safety_buffer": self.buffer.to_dict(),
        }

    def phase_a_config(self) -> pas.ScanConfig:
        """Phase A config mirroring the shared knobs, for the A1 same-window comparator."""
        return pas.ScanConfig(
            qty=self.qty,
            horizon_intervals=self.horizon_intervals,
            ref_rate_apr=self.ref_rate_apr,
            funding_sigma_bps_per_interval=self.funding_sigma_bps_per_interval,
            transfer_bps=self.transfer_bps,
            depth_mult=self.depth_mult,
            top_n=self.top_n,
            persistence_min_samples=self.persistence_min_samples,
            persistence_min_sec=self.persistence_min_sec,
            credit_favorable_basis=self.credit_favorable_basis,
            fees=self.fees,
            buffer=pas.SafetyBuffer(
                fee_roundtrip_bps=self.buffer.fee_roundtrip_bps,
                slip_buffer_bps=self.buffer.slip_buffer_bps,
                funding_uncert_bps=self.buffer.funding_uncert_bps,
                model_haircut_bps=self.buffer.model_haircut_bps,
                calibrated=self.buffer.calibrated,
            ),
        )


# --------------------------------------------------------------------------- cover gate


def _lg(leg, key: str):
    return leg[key] if isinstance(leg, dict) else getattr(leg, key)


def assert_covered_short_options(legs: list, *, b1_spot_cover: bool = False) -> list[dict]:
    """Phase B cover rule. Every short option must be covered by
      * a long option of the same type with expiry >= the short's expiry and qty >= short qty
        (vertical / calendar / diagonal — defined risk; a *reverse* calendar is naked after the
        near leg expires and is refused), or
      * an underlying leg in the right direction with qty >= short qty
        (short call ↔ long spot/perp, short put ↔ short spot/perp).
    With `b1_spot_cover=True` a short call must specifically be covered by *spot* notional.
    Returns the cover description list; raises NakedShortOptionRefused otherwise."""
    covers: list[dict] = []
    underlying = [leg for leg in legs if _lg(leg, "role") in ("spot", "perp")]
    for leg in legs:
        role, side = _lg(leg, "role"), _lg(leg, "side")
        if role not in ("call", "put") or side != "sell":
            continue
        qty = _lg(leg, "qty")
        exp = _lg(leg, "expiry_ms")
        inst = _lg(leg, "instrument")
        option_cover = next(
            (
                o
                for o in legs
                if _lg(o, "role") == role
                and _lg(o, "side") == "buy"
                and _lg(o, "qty") >= qty
                and (_lg(o, "expiry_ms") or 0) >= (exp or 0)
            ),
            None,
        )
        need_side = "buy" if role == "call" else "sell"
        und_cover = next(
            (
                u
                for u in underlying
                if _lg(u, "side") == need_side
                and _lg(u, "qty") >= qty
                and (not b1_spot_cover or _lg(u, "role") == "spot")
            ),
            None,
        )
        if b1_spot_cover and role == "call":
            if und_cover is None:
                raise NakedShortOptionRefused(
                    f"refused: short call {inst} not covered by spot notional (qty {qty})"
                )
            covers.append(
                {
                    "short_leg": inst,
                    "covered_by": _lg(und_cover, "instrument"),
                    "cover_type": "spot_notional",
                    "cover_qty": _lg(und_cover, "qty"),
                }
            )
            continue
        if option_cover is not None:
            covers.append(
                {
                    "short_leg": inst,
                    "covered_by": _lg(option_cover, "instrument"),
                    "cover_type": "same_type_long_option_expiry_gte",
                    "cover_qty": _lg(option_cover, "qty"),
                }
            )
        elif und_cover is not None:
            covers.append(
                {
                    "short_leg": inst,
                    "covered_by": _lg(und_cover, "instrument"),
                    "cover_type": "underlying_notional",
                    "cover_qty": _lg(und_cover, "qty"),
                }
            )
        else:
            raise NakedShortOptionRefused(f"refused: short {role} {inst} has no covering leg")
    return covers


# --------------------------------------------------------------------------- chain analytics (ref)


@dataclass(frozen=True)
class OptRef:
    """Reference analytics for one option book. All derived from *mid* quotes; none of these
    is an executable price."""

    book: Book
    T: float
    is_call: bool
    mid_quote: float
    bid_quote: float | None
    ask_quote: float | None
    iv_mid: float | None
    iv_bid: float | None
    iv_ask: float | None
    delta: float | None
    gamma: float | None
    vega: float | None

    @property
    def strike(self) -> float:
        return float(self.book.strike)


class ChainAnalytics:
    """Per-snapshot reference analytics: forward, discount factor, IV / greeks per option,
    ATM vol per expiry. `F_ref` is the perp mid (else spot mid) — a *reference* forward."""

    def __init__(self, snap: Snapshot, ref_rate_apr: float):
        self.snap = snap
        self.ref_rate_apr = ref_rate_apr
        spot = snap.spot
        if spot is None or not spot.two_sided:
            raise ValueError("ChainAnalytics needs a two-sided spot book")
        self.spot_mid = (spot.best_bid + spot.best_ask) / 2.0
        perp = snap.perp
        if perp is not None and perp.two_sided:
            self.F_ref = (perp.best_bid + perp.best_ask) / 2.0
            self.forward_source = "perp_mid"
        else:
            self.F_ref = self.spot_mid
            self.forward_source = "spot_mid"
        self._cache: dict[str, OptRef | None] = {}
        self._chain = snap.option_chain()

    def df(self, T: float) -> float:
        return math.exp(-self.ref_rate_apr * T)

    def ref_quote(self, px: float, book: Book) -> float:
        return px * self.spot_mid if book.premium_ccy == "base" else px

    def analyze(self, book: Book) -> OptRef | None:
        if book.inst_id in self._cache:
            return self._cache[book.inst_id]
        out = None
        if book.kind == "option" and book.strike and book.expiry_ms and book.opt_type in ("C", "P"):
            T = years_between(self.snap.ts_ms, book.expiry_ms)
            is_call = book.opt_type == "C"
            K = float(book.strike)
            df = self.df(T)
            bid_q = self.ref_quote(book.best_bid, book) if book.best_bid else None
            ask_q = self.ref_quote(book.best_ask, book) if book.best_ask else None
            if bid_q is not None and ask_q is not None:
                mid_q = (bid_q + ask_q) / 2.0
            else:
                mid_q = bid_q if bid_q is not None else (ask_q or 0.0)
            iv_mid = implied_vol(mid_q, self.F_ref, K, T, is_call, df) if mid_q > 0 else None
            iv_bid = implied_vol(bid_q, self.F_ref, K, T, is_call, df) if bid_q else None
            iv_ask = implied_vol(ask_q, self.F_ref, K, T, is_call, df) if ask_q else None
            g = black76(self.F_ref, K, T, iv_mid, is_call, df) if iv_mid else None
            out = OptRef(
                book=book,
                T=T,
                is_call=is_call,
                mid_quote=mid_q,
                bid_quote=bid_q,
                ask_quote=ask_q,
                iv_mid=iv_mid,
                iv_bid=iv_bid,
                iv_ask=iv_ask,
                delta=g.delta if g else None,
                gamma=g.gamma if g else None,
                vega=g.vega if g else None,
            )
        self._cache[book.inst_id] = out
        return out

    def atm_iv(self, expiry_ms: int) -> tuple[float | None, str]:
        """Flat-skew reference vol for an expiry: mean of the mid IVs of the call and put at the
        strike nearest F_ref (whichever are estimable)."""
        strikes = self._chain.get(expiry_ms) or {}
        if not strikes:
            return None, "none"
        K = min(strikes, key=lambda k: abs(k - self.F_ref))
        ivs = []
        for typ in ("C", "P"):
            b = strikes[K].get(typ)
            r = self.analyze(b) if b is not None else None
            if r is not None and r.iv_mid is not None:
                ivs.append(r.iv_mid)
        if not ivs:
            return None, "none"
        return sum(ivs) / len(
            ivs
        ), f"atm_mid_iv_K{K:g}_{'CP'[: len(ivs)] if len(ivs) == 2 else 'single'}"


# --------------------------------------------------------------------------- paper delta hedge sim


def paper_delta_hedge_sim(
    *,
    net_delta: float,
    net_gamma: float,
    hedge_book: Book,
    hedge_kind: str,
    funding: Funding | None,
    sigma_ref: float,
    hold_years: float,
    cfg: ComboConfig,
) -> dict:
    """Deterministic *paper* delta hedge cost model. No order exists; nothing is sent.

    hedge_units = −net_delta (neutralise at entry), unwound at exit; between, `rebalances_per_day`
    rebalances each trading E|Δδ| ≈ |Γ| · S · σ · √dt · √(2/π). Every hedge trade pays taker fee
    + half spread + `hedge_slip_bps`. A perp hedge also pays funding when on the paying side
    (receiving side is not credited) and carries funding uncertainty."""
    S = (hedge_book.best_bid + hedge_book.best_ask) / 2.0
    hedge_units = -net_delta
    fee_bps = cfg.fees.perp_taker_bps if hedge_kind == "perp" else cfg.fees.spot_taker_bps
    half_spread_bps = bps((hedge_book.best_ask - hedge_book.best_bid) / 2.0, S)
    per_unit_cost_bps = fee_bps + half_spread_bps + cfg.hedge_slip_bps
    entry_exit_q = abs(hedge_units) * S * 2.0 * per_unit_cost_bps / 1e4
    n_rebal = int(math.floor(round(hold_years * 365.0 * cfg.rebalances_per_day, 9)))
    dt = 1.0 / (365.0 * cfg.rebalances_per_day)
    exp_abs_dS = S * sigma_ref * math.sqrt(dt) * math.sqrt(2.0 / math.pi)
    exp_abs_ddelta = abs(net_gamma) * exp_abs_dS
    rebal_q = n_rebal * exp_abs_ddelta * S * per_unit_cost_bps / 1e4
    funding_paid_q = 0.0
    funding_uncert_q = 0.0
    intervals = 0.0
    if hedge_kind == "perp" and funding is not None:
        intervals = hold_years * 365.0 * 86400.0 / funding.interval_sec
        signed = funding.rate if hedge_units > 0 else -funding.rate  # long perp pays +funding
        funding_paid_q = max(0.0, signed) * intervals * abs(hedge_units) * S
        funding_uncert_q = (
            abs(hedge_units) * S * cfg.funding_sigma_bps_per_interval / 1e4 * intervals
        )
    return {
        "enabled": True,
        "mode": HEDGE_PAPER_DELTA,
        "hedge_instrument": hedge_book.inst_id,
        "hedge_kind": hedge_kind,
        "live_hedge_http": False,
        "order_sent": False,
        "will_send_http": WILL_SEND_HTTP,
        "initial_net_delta": round(net_delta, 8),
        "hedge_units": round(hedge_units, 8),
        "net_gamma": round(net_gamma, 10),
        "sigma_ref": round(sigma_ref, 6),
        "hold_years": round(hold_years, 6),
        "rebalances": n_rebal,
        "expected_abs_delta_change_per_rebalance": round(exp_abs_ddelta, 8),
        "per_unit_cost_bps": round(per_unit_cost_bps, 4),
        "entry_exit_cost_quote": round(entry_exit_q, 6),
        "rebalance_cost_quote": round(rebal_q, 6),
        "cost_quote": round(entry_exit_q + rebal_q, 6),
        "funding_intervals": round(intervals, 4),
        "funding_paid_quote": round(funding_paid_q, 6),
        "funding_uncert_quote": round(funding_uncert_q, 6),
        "hedge_notional_quote": round(abs(hedge_units) * S, 6),
        "assumptions": [
            "hedge trades are hypothetical; fee + half spread + slip per unit, both ways",
            "E|Δδ| per rebalance = |Γ|·S·σ_ref·√dt·√(2/π) (lognormal, reference vol)",
            "perp hedge funding charged when paying, not credited when receiving",
            "gap / jump risk is NOT captured here → residual_risks + vol_path_haircut",
        ],
    }


def paper_delta_disabled(hedge_mode: str) -> dict:
    return {
        "enabled": False,
        "mode": hedge_mode,
        "live_hedge_http": False,
        "order_sent": False,
        "will_send_http": WILL_SEND_HTTP,
        "note": "static combo: no rebalance modelled; hedge_rebalance cost = 0 by construction",
    }


# --------------------------------------------------------------------------- record


def build_combo_record(
    *,
    snap: Snapshot,
    family: str,
    hedge_mode: str,
    legs: list[Leg],
    books: dict[str, Book],
    gross_quote: float,
    notional_quote: float,
    costs_quote: dict[str, float],
    margin_capital: float,
    risk_flags: list[str],
    residual_risks: list[str],
    paper_delta_sim: dict,
    cfg: ComboConfig,
    tracker: PersistenceTracker,
    persistence_key: str,
    extra: dict,
) -> dict:
    if family not in FAMILIES:
        raise ComboSchemaViolation(f"unknown Phase B family {family}")
    if hedge_mode not in HEDGE_MODES[family]:
        raise ComboSchemaViolation(f"{family}: hedge_mode {hedge_mode!r} not allowed")
    covers = assert_covered_short_options(legs, b1_spot_cover=(family == FAMILY_B1))

    liquidity, liq_flags, impact_quote = liquidity_check(legs, books, cfg.qty, cfg)
    costs_quote = dict(costs_quote)
    costs_quote["impact"] = impact_quote
    costs_bps = {k: round(bps(costs_quote.get(k, 0.0), notional_quote), 4) for k in COST_KEYS}
    costs_bps["total"] = round(sum(costs_bps[k] for k in COST_KEYS), 4)
    gross_bps = round(bps(gross_quote, notional_quote), 4)
    net_bps = round(gross_bps - costs_bps["total"], 4)

    buffer = cfg.buffer.resolve(costs_bps["fees"])
    flags = sorted(set(risk_flags) | set(liq_flags) | {"relative_value_not_riskless"})
    invalidated = sorted(set(flags) & INVALIDATING_FLAGS)
    persistence = tracker.update(persistence_key, snap.ts_ms, net_bps, buffer["total_bps"])
    edge_exceeds_buffer = net_bps > buffer["total_bps"]
    passes = bool(edge_exceeds_buffer and persistence["ok"] and liquidity["ok"] and not invalidated)

    leg_insts = {leg.instrument for leg in legs}
    executable_prices = {
        inst: {"bid": b.best_bid, "ask": b.best_ask, "mark_ref_only": b.mark_px}
        for inst, b in books.items()
        if inst in leg_insts
    }
    residual = sorted(set(RESIDUAL_RISKS[family]) | set(residual_risks))

    rec: dict = {
        "timestamp": iso_cst(snap.ts_ms),
        "ts_ms": snap.ts_ms,
        "venue": snap.venue,
        "mode": MODE,
        "phase": PHASE,
        "family": family,
        "combo_id": COMBO_ID[family],
        "taxonomy": TAXONOMY,
        "hedge_mode": hedge_mode,
        "instruments": sorted(leg_insts),
        "legs": [leg.to_dict() for leg in legs],
        "cover": covers,
        "executable_prices": executable_prices,
        "notional_quote": round(notional_quote, 6),
        "notional_basis": extra.pop("notional_basis", "spot_ask_x_qty"),
        "gross_edge_bps": gross_bps,
        "costs_bps": costs_bps,
        "net_edge_bps": net_bps,
        "safety_buffer_bps": buffer["total_bps"],
        "safety_buffer": buffer,
        "edge_exceeds_buffer": edge_exceeds_buffer,
        "passes_threshold": passes,
        "invalidated_by": invalidated,
        "margin_capital_required": round(margin_capital, 6),
        "leverage_concept": 1,
        "persistence": persistence,
        "liquidity": liquidity,
        "residual_risks": residual,
        "risk_flags": flags,
        "paper_delta_sim": paper_delta_sim,
        "hypothesis_id": HYPOTHESIS[family],
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "source_inspiration": SOURCE_INSPIRATION,
        "cross_checked_inbox": CROSS_CHECKED_INBOX,
        "related_phase_A": RELATED_PHASE_A,
    }
    rec.update(extra)
    if cfg.paper_fills:
        rec["paper_fill"] = pas.paper_fill(legs, books, notional_quote / cfg.qty, net_bps, cfg)
    return finalize_combo_record(rec)


def finalize_combo_record(rec: dict) -> dict:
    """Force the observe-only constants and validate the Phase B schema. Raises on any attempt
    to emit a record that is not observe_only / will_send_http=false / bid-ask priced /
    relative_value / covered / free of forbidden labels."""
    if rec.get("action") != ACTION or rec.get("will_send_http") is not False:
        raise ObserveOnlyViolation(
            f"refused: action={rec.get('action')!r} will_send_http={rec.get('will_send_http')!r}"
        )
    missing = [k for k in REQUIRED_FIELDS if k not in rec]
    if missing:
        raise ComboSchemaViolation(f"record missing required fields: {missing}")
    if rec["phase"] != PHASE:
        raise ComboSchemaViolation(f"phase must be {PHASE!r}, got {rec['phase']!r}")
    fam = rec["family"]
    if fam not in FAMILIES:
        raise ComboSchemaViolation(f"unknown family {fam!r}")
    if rec["taxonomy"] != TAXONOMY:
        raise ComboSchemaViolation(f"Phase B must be {TAXONOMY!r}, got {rec['taxonomy']!r}")
    if rec["combo_id"] != COMBO_ID[fam]:
        raise ComboSchemaViolation(f"combo_id {rec['combo_id']!r} does not match {fam}")
    if rec["hedge_mode"] not in HEDGE_MODES[fam]:
        raise ComboSchemaViolation(f"{fam}: hedge_mode {rec['hedge_mode']!r} not allowed")
    missing_rr = sorted(set(RESIDUAL_RISKS[fam]) - set(rec["residual_risks"]))
    if missing_rr:
        raise ComboSchemaViolation(f"{fam}: residual_risks missing {missing_rr}")
    if "relative_value_not_riskless" not in rec["risk_flags"]:
        raise ComboSchemaViolation("risk_flags must carry relative_value_not_riskless")
    for leg in rec["legs"]:
        if leg["price_type"] not in pas.EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(f"refused: leg price_type {leg['price_type']!r}")
        if leg["price_type"] != pas.SIDE_TO_PRICE_TYPE[leg["side"]]:
            raise ExecutablePriceViolation(f"refused: {leg['side']} @ {leg['price_type']}")
    assert_covered_short_options(rec["legs"], b1_spot_cover=(fam == FAMILY_B1))
    if "total" not in rec["costs_bps"] or "vol_path_haircut" not in rec["costs_bps"]:
        raise ComboSchemaViolation("costs_bps needs total and vol_path_haircut")
    if "vol_path_haircut_bps" not in rec["safety_buffer"]["components"]:
        raise ComboSchemaViolation("safety_buffer must include vol_path_haircut_bps")
    sim = rec.get("paper_delta_sim") or {}
    if sim.get("live_hedge_http") is not False or sim.get("order_sent") is not False:
        raise ObserveOnlyViolation("refused: paper_delta_sim must have live_hedge_http=false")
    if fam == FAMILY_B2 and sim.get("enabled") is not True:
        raise ComboSchemaViolation("B2 must carry an enabled paper delta simulation")
    if rec["hedge_mode"] in (HEDGE_PAPER_DELTA, HEDGE_RR_PAPER_DELTA) and not sim.get("enabled"):
        raise ComboSchemaViolation(f"hedge_mode {rec['hedge_mode']} requires paper_delta_sim")
    text = json.dumps(rec, ensure_ascii=False).lower()
    for bad in FORBIDDEN_LABELS:
        if bad in text:
            raise ForbiddenLabelViolation(f"refused: forbidden label {bad!r} in record")
    return rec


# --------------------------------------------------------------------------- scanner


class ComboScanner:
    def __init__(self, cfg: ComboConfig, tracker: PersistenceTracker | None = None):
        cfg.validate()
        self.cfg = cfg
        self.tracker = tracker or PersistenceTracker(
            cfg.persistence_min_samples, cfg.persistence_min_sec
        )
        self.skipped: Counter = Counter()
        self._rr_history: dict[int, list[float]] = {}
        self._a1 = pas.ArbScanner(cfg.phase_a_config())
        self.a1_reference_records: list[dict] = []

    # ---- shared

    def scan(self, snap: Snapshot) -> list[dict]:
        out: list[dict] = []
        a1 = self.a1_reference(snap)
        out.extend(self.scan_b1(snap, a1))
        out.extend(self.scan_b2(snap))
        out.extend(self.scan_b3(snap))
        return out

    def a1_reference(self, snap: Snapshot) -> dict | None:
        """Phase A A1 on the same snapshot — H-B1's comparator. Not emitted as a Phase B row."""
        recs = self._a1.scan_a1(snap)
        if not recs:
            return None
        self.a1_reference_records.append(recs[0])
        r = recs[0]
        return {
            "family": r["family"],
            "direction": r["direction"],
            "net_edge_bps": r["net_edge_bps"],
            "edge_exceeds_buffer": r["edge_exceeds_buffer"],
            "passes_threshold": r["passes_threshold"],
        }

    def _analytics(self, snap: Snapshot) -> ChainAnalytics | None:
        try:
            return ChainAnalytics(snap, self.cfg.ref_rate_apr)
        except ValueError:
            self.skipped["no_two_sided_spot"] += 1
            return None

    @staticmethod
    def _hold(cfg: ComboConfig, T: float, interval_sec: int) -> dict:
        horizon_years = cfg.horizon_years(interval_sec)
        hold_to_expiry = T <= horizon_years
        hold_years = min(horizon_years, T)
        return {
            "horizon_intervals": cfg.horizon_intervals,
            "horizon_years": round(horizon_years, 6),
            "hold_years": round(hold_years, 6),
            "hold_to_expiry": hold_to_expiry,
            "exit_rule": "settle_at_expiry"
            if hold_to_expiry
            else "buy_back_at_horizon_cross_spread",
        }

    def _option_exit_costs(
        self,
        legs: list[Leg],
        books: dict[str, Book],
        spot: Book,
        notional: float,
        hold_to_expiry: bool,
    ) -> tuple[float, float]:
        """(fees_quote, spread_quote) for the option legs: entry taker fee always; at exit either
        the settlement fee (held to expiry) or a buy-back taker fee + the full bid/ask spread
        (conservative: crossing the spread once more at an unknown future book)."""
        fees_q = 0.0
        spread_q = 0.0
        for leg in legs:
            if leg.role not in ("call", "put"):
                continue
            b = books[leg.instrument]
            exec_q = premium_to_quote(leg.executable_price, leg.premium_ccy, leg.side, spot) or 0.0
            fees_q += option_fee_quote(
                exec_q * leg.qty, notional, self.cfg.fees, settlement=hold_to_expiry
            )
            if not hold_to_expiry and b.two_sided:
                bid_q = premium_to_quote(b.best_bid, b.premium_ccy, "sell", spot) or 0.0
                ask_q = premium_to_quote(b.best_ask, b.premium_ccy, "buy", spot) or 0.0
                other = ask_q if leg.side == "sell" else bid_q
                fees_q += option_fee_quote(
                    other * leg.qty, notional, self.cfg.fees, settlement=False
                )
                spread_q += max(ask_q - bid_q, 0.0) * leg.qty
        return fees_q, spread_q

    # ---- B1 spot long + perp short + short OTM call (static_combo)

    def _select_b1_calls(self, an: ChainAnalytics, strikes: dict) -> tuple[list[OptRef], list[str]]:
        cfg = self.cfg
        cands: list[OptRef] = []
        flags: list[str] = []
        for K, pair in sorted(strikes.items()):
            call = pair.get("C")
            if call is None or not call.two_sided or K <= an.F_ref:
                continue
            r = an.analyze(call)
            if r is None or r.iv_mid is None or r.delta is None:
                self.skipped["b1_call_iv_unestimable"] += 1
                continue
            cands.append(r)
        in_band = [r for r in cands if cfg.b1_delta_min <= r.delta <= cfg.b1_delta_max]
        if not in_band:
            lo, hi = an.F_ref * (1 + cfg.b1_moneyness_min), an.F_ref * (1 + cfg.b1_moneyness_max)
            in_band = [r for r in cands if lo <= r.strike <= hi]
            if in_band:
                flags.append("otm_selection_by_moneyness_fallback")
        target = 0.5 * (cfg.b1_delta_min + cfg.b1_delta_max)
        in_band.sort(key=lambda r: abs((r.delta or 0.0) - target))
        return in_band[: cfg.b1_max_calls_per_expiry], flags

    def scan_b1(self, snap: Snapshot, a1_ref: dict | None = None) -> list[dict]:
        spot, perp, fr = snap.spot, snap.perp, snap.funding
        if spot is None or perp is None or not (spot.two_sided and perp.two_sided):
            self.skipped["b1_missing_spot_or_perp"] += 1
            return []
        if fr is None:
            self.skipped["b1_no_funding"] += 1
            return []
        an = self._analytics(snap)
        if an is None:
            return []
        out: list[dict] = []
        for expiry_ms, strikes in sorted(an._chain.items()):
            T = years_between(snap.ts_ms, expiry_ms)
            if T <= 0:
                self.skipped["b1_expired"] += 1
                continue
            cands, sel_flags = self._select_b1_calls(an, strikes)
            if not cands:
                self.skipped["b1_no_call_in_band"] += 1
            for r in cands:
                rec = self._b1_record(snap, an, r, expiry_ms, T, sel_flags, a1_ref)
                if rec is not None:
                    out.append(rec)
        return out

    def _b1_record(
        self,
        snap: Snapshot,
        an: ChainAnalytics,
        call_ref: OptRef,
        expiry_ms: int,
        T: float,
        sel_flags: list[str],
        a1_ref: dict | None,
    ) -> dict | None:
        cfg = self.cfg
        spot, perp, fr = snap.spot, snap.perp, snap.funding
        call = call_ref.book
        qty = cfg.qty
        spot_leg = make_leg(spot, "buy", qty, "spot")
        perp_leg = make_leg(perp, "sell", qty, "perp")
        call_leg = make_leg(call, "sell", qty, "call")
        if spot_leg is None or perp_leg is None or call_leg is None:
            return None
        legs = [spot_leg, perp_leg, call_leg]
        N = spot.best_ask * qty
        K = call_ref.strike
        flags = list(sel_flags) + [
            "funding_path_risk",
            "basis_risk",
            "short_call_caps_upside",
            "leverage_1x_margin_still_applies",
            "model_fair_from_mid_iv_reference_only",
            "assumes_skew_convergence_by_exit",
        ]
        if call.premium_ccy == "base":
            flags.append("option_premium_in_base_ccy_converted_at_spot_bid_ask")

        hold = self._hold(cfg, T, fr.interval_sec)
        intervals_to_expiry = int((expiry_ms - snap.ts_ms) // (fr.interval_sec * 1000))
        H = min(cfg.horizon_intervals, max(intervals_to_expiry, 0))
        if H == 0:
            flags.append("hold_horizon_zero")
        positive = fr.rate >= 0
        f_now = fr.rate
        funding_q = 0.0
        funding_paid_q = 0.0
        if positive:
            f_used = f_now
            if fr.next_rate is not None:
                if fr.next_rate < 0:
                    flags.append("funding_sign_flip_predicted")
                    f_used = 0.0
                else:
                    f_used = min(f_now, fr.next_rate)
            funding_q = f_used * H * N
        else:
            f_used = 0.0
            flags.append("funding_negative_for_short_perp")
            funding_paid_q = abs(f_now) * H * N
        f_pred_gap = abs(abs(f_now) - abs(fr.next_rate)) if fr.next_rate is not None else 0.0
        funding_uncert_q = N * (cfg.funding_sigma_bps_per_interval / 1e4 * H + f_pred_gap * H)

        basis_bps = bps(perp.best_bid - spot.best_ask, spot.best_ask)
        if basis_bps < 0:
            flags.append("adverse_entry_basis")
        basis_credit_bps = basis_bps if (basis_bps < 0 or cfg.credit_favorable_basis) else 0.0

        c_bid_q = (premium_to_quote(call.best_bid, call.premium_ccy, "sell", spot) or 0.0) * qty
        sigma_ref, sigma_src = an.atm_iv(expiry_ms)
        if sigma_ref is None:
            sigma_ref, sigma_src = call_ref.iv_mid, "own_mid_iv_fallback"
            flags.append("reference_vol_fallback_own_mid")
        fair_q = black76(an.F_ref, K, T, sigma_ref, True, an.df(T)).price * qty
        premium_edge_q = c_bid_q - fair_q
        gross_q = funding_q + premium_edge_q + basis_credit_bps / 1e4 * N

        fees = cfg.fees
        fees_q = N * (2 * fees.spot_taker_bps + 2 * fees.perp_taker_bps) / 1e4
        books = {spot.inst_id: spot, perp.inst_id: perp, call.inst_id: call}
        opt_fees_q, opt_spread_q = self._option_exit_costs(
            [call_leg], books, spot, N, hold["hold_to_expiry"]
        )
        fees_q += opt_fees_q
        half_spread_q = qty * (
            (spot.best_ask - spot.best_bid) / 2 + (perp.best_ask - perp.best_bid) / 2
        )
        half_spread_q += opt_spread_q
        margin = 2 * N  # spot notional + perp margin at 1x; the call is covered by the spot leg
        costs = {
            "fees": fees_q,
            "half_spread_slip": half_spread_q,
            "hedge_rebalance": 0.0,
            "borrow": 0.0,
            "transfer": N * cfg.transfer_bps / 1e4,
            "capital_opp": margin * cfg.ref_rate_apr * hold["hold_years"],
            "funding_expected": funding_paid_q,
            "funding_uncertainty": funding_uncert_q,
            "vol_path_haircut": N * cfg.vol_path_haircut_bps / 1e4,
        }
        extra = {
            "direction": "long_spot_short_perp_short_otm_call",
            "strike": K,
            "expiry_ms": expiry_ms,
            "expiry": datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
            "T_years": round(T, 6),
            "hold": hold,
            "funding": {
                "rate_now": fr.rate,
                "rate_next": fr.next_rate,
                "rate_used_per_interval": f_used,
                "interval_sec": fr.interval_sec,
                "intervals_used": H,
                "expected_funding_bps": round(bps(funding_q, N), 4),
                "funding_paid_bps": round(bps(funding_paid_q, N), 4),
            },
            "basis_entry_bps": round(basis_bps, 4),
            "basis_credited_bps": round(basis_credit_bps, 4),
            "call": {
                "moneyness": round(K / an.F_ref - 1.0, 6),
                "delta_mid_ref": round(call_ref.delta, 6),
                "iv_mid_ref": round(call_ref.iv_mid, 6),
                "iv_bid_ref": round(call_ref.iv_bid, 6) if call_ref.iv_bid else None,
                "sigma_ref": round(sigma_ref, 6),
                "sigma_ref_source": sigma_src,
                "forward_ref": round(an.F_ref, 6),
                "forward_source": an.forward_source,
                "bid_exec_quote": round(c_bid_q, 6),
                "fair_ref_quote": round(fair_q, 6),
                "premium_edge_bps": round(bps(premium_edge_q, N), 4),
                "theta_carry_credited": False,
                "upside_drag_model": "fair value at flat ATM reference vol (Black-76 on F_ref)",
            },
            "a1_reference_same_window": a1_ref,
            "invalidation": [
                "funding_sign_flip",
                "call_deep_itm_exit_cost",
                "basis_blowout",
                "depth_collapse",
                "margin_stress",
                "otm_band_drift",
            ],
        }
        rec = build_combo_record(
            snap=snap,
            family=FAMILY_B1,
            hedge_mode=HEDGE_STATIC,
            legs=legs,
            books=books,
            gross_quote=gross_q,
            notional_quote=N,
            costs_quote=costs,
            margin_capital=margin,
            risk_flags=flags,
            residual_risks=[],
            paper_delta_sim=paper_delta_disabled(HEDGE_STATIC),
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_B1}|{spot.inst_id}|{perp.inst_id}|{call.inst_id}",
            extra=extra,
        )
        if a1_ref is not None:
            rec["b1_minus_a1_net_edge_bps"] = round(rec["net_edge_bps"] - a1_ref["net_edge_bps"], 4)
        return rec

    # ---- B2 calendar (long far / short near, same type, same K) — paper delta sim only

    def scan_b2(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        spot = snap.spot
        if spot is None or not spot.two_sided:
            self.skipped["b2_missing_spot"] += 1
            return []
        an = self._analytics(snap)
        if an is None:
            return []
        expiries = [e for e in sorted(an._chain) if years_between(snap.ts_ms, e) > 0]
        if len(expiries) < 2:
            self.skipped["b2_single_expiry"] += 1
            return []
        hedge_book = (
            snap.perp if (cfg.hedge_instrument == "perp" and snap.perp is not None) else spot
        )
        hedge_kind = "perp" if hedge_book is snap.perp else "spot"
        if not hedge_book.two_sided:
            self.skipped["b2_hedge_book_one_sided"] += 1
            return []
        out: list[dict] = []
        typ = cfg.b2_opt_type
        for near_exp, far_exp in zip(expiries, expiries[1:], strict=False):
            near_s, far_s = an._chain[near_exp], an._chain[far_exp]
            common = [
                K
                for K in near_s
                if K in far_s
                and near_s[K].get(typ) is not None
                and far_s[K].get(typ) is not None
                and near_s[K][typ].two_sided
                and far_s[K][typ].two_sided
            ]
            common.sort(key=lambda k: abs(k - an.F_ref))
            for K in common[: cfg.b2_max_pairs]:
                rec = self._b2_record(
                    snap, an, near_s[K][typ], far_s[K][typ], K, hedge_book, hedge_kind
                )
                if rec is not None:
                    out.append(rec)
        return out

    def _b2_record(
        self,
        snap: Snapshot,
        an: ChainAnalytics,
        near: Book,
        far: Book,
        K: float,
        hedge_book: Book,
        hedge_kind: str,
    ) -> dict | None:
        cfg = self.cfg
        spot = snap.spot
        qty = cfg.qty
        role = "call" if near.opt_type == "C" else "put"
        far_leg = make_leg(far, "buy", qty, role)
        near_leg = make_leg(near, "sell", qty, role)
        if far_leg is None or near_leg is None:
            return None
        legs = [far_leg, near_leg]
        near_ref, far_ref = an.analyze(near), an.analyze(far)
        if (
            near_ref is None
            or far_ref is None
            or near_ref.iv_mid is None
            or far_ref.iv_mid is None
            or near_ref.delta is None
            or far_ref.delta is None
        ):
            self.skipped["b2_iv_unestimable"] += 1
            return None
        N = spot.best_ask * qty
        T_near, T_far = near_ref.T, far_ref.T
        interval_sec = snap.funding.interval_sec if snap.funding else 8 * 3600
        hold = self._hold(cfg, T_near, interval_sec)
        flags = [
            "model_fair_from_mid_iv_reference_only",
            "paper_delta_hedge_cost_in_costs",
            "near_leg_short_gamma",
            "leverage_1x_margin_still_applies",
        ]
        if near.premium_ccy == "base" or far.premium_ccy == "base":
            flags.append("option_premium_in_base_ccy_converted_at_spot_bid_ask")

        sigma_ref = near_ref.iv_mid  # flat term-structure reference
        far_fair_q = (
            black76(an.F_ref, K, T_far, sigma_ref, far_ref.is_call, an.df(T_far)).price * qty
        )
        near_fair_q = (
            black76(an.F_ref, K, T_near, sigma_ref, near_ref.is_call, an.df(T_near)).price * qty
        )
        fair_debit_q = far_fair_q - near_fair_q
        far_ask_q = (premium_to_quote(far.best_ask, far.premium_ccy, "buy", spot) or 0.0) * qty
        near_bid_q = (premium_to_quote(near.best_bid, near.premium_ccy, "sell", spot) or 0.0) * qty
        exec_debit_q = far_ask_q - near_bid_q
        gross_q = fair_debit_q - exec_debit_q
        slope_mid = far_ref.iv_mid - near_ref.iv_mid
        if gross_q <= 0 and slope_mid > 0:
            flags.append("term_structure_not_cheap_vs_thesis")

        net_delta = qty * (far_ref.delta - near_ref.delta)
        net_gamma = qty * ((far_ref.gamma or 0.0) - (near_ref.gamma or 0.0))
        sim = paper_delta_hedge_sim(
            net_delta=net_delta,
            net_gamma=net_gamma,
            hedge_book=hedge_book,
            hedge_kind=hedge_kind,
            funding=snap.funding,
            sigma_ref=sigma_ref,
            hold_years=hold["hold_years"],
            cfg=cfg,
        )
        books = {near.inst_id: near, far.inst_id: far, spot.inst_id: spot}
        if hedge_book.inst_id not in books:
            books[hedge_book.inst_id] = hedge_book
        # far leg always unwinds at horizon (it outlives the near leg): buy-back fee + spread
        far_fees_q, far_spread_q = self._option_exit_costs([far_leg], books, spot, N, False)
        near_fees_q, near_spread_q = self._option_exit_costs(
            [near_leg], books, spot, N, hold["hold_to_expiry"]
        )
        margin = max(exec_debit_q, 0.0) + sim["hedge_notional_quote"]
        costs = {
            "fees": far_fees_q + near_fees_q,
            "half_spread_slip": far_spread_q + near_spread_q,
            "hedge_rebalance": sim["cost_quote"],
            "borrow": 0.0,
            "transfer": N * cfg.transfer_bps / 1e4,
            "capital_opp": margin * cfg.ref_rate_apr * hold["hold_years"],
            "funding_expected": sim["funding_paid_quote"],
            "funding_uncertainty": sim["funding_uncert_quote"],
            "vol_path_haircut": N * cfg.vol_path_haircut_bps / 1e4,
        }
        extra = {
            "direction": f"long_far_short_near_{role}_calendar",
            "strike": K,
            "opt_type": near.opt_type,
            "near_expiry_ms": near.expiry_ms,
            "far_expiry_ms": far.expiry_ms,
            "near_expiry": datetime.fromtimestamp(near.expiry_ms / 1000, UTC).isoformat(),
            "far_expiry": datetime.fromtimestamp(far.expiry_ms / 1000, UTC).isoformat(),
            "T_near_years": round(T_near, 6),
            "T_far_years": round(T_far, 6),
            "hold": hold,
            "term_structure": {
                "iv_near_mid": round(near_ref.iv_mid, 6),
                "iv_far_mid": round(far_ref.iv_mid, 6),
                "iv_near_bid": round(near_ref.iv_bid, 6) if near_ref.iv_bid else None,
                "iv_far_ask": round(far_ref.iv_ask, 6) if far_ref.iv_ask else None,
                "slope_mid": round(slope_mid, 6),
                "sigma_ref": round(sigma_ref, 6),
                "sigma_ref_source": "near_leg_mid_iv_flat_reference",
                "forward_ref": round(an.F_ref, 6),
                "forward_source": an.forward_source,
                "thesis": "far leg cheap vs near-leg vol; edge = fair_debit(σ_near) − exec_debit",
            },
            "calendar": {
                "exec_debit_quote": round(exec_debit_q, 6),
                "fair_debit_ref_quote": round(fair_debit_q, 6),
                "edge_quote": round(gross_q, 6),
                "far_ask_quote": round(far_ask_q, 6),
                "near_bid_quote": round(near_bid_q, 6),
                "theta_carry_credited": False,
            },
            "invalidation": [
                "near_expiry_without_roll_rule",
                "edge_inside_buffer",
                "hedge_cost_dominates",
                "term_structure_flip_vs_thesis",
                "illiquid_leg",
            ],
        }
        return build_combo_record(
            snap=snap,
            family=FAMILY_B2,
            hedge_mode=HEDGE_PAPER_DELTA,
            legs=legs,
            books=books,
            gross_quote=gross_q,
            notional_quote=N,
            costs_quote=costs,
            margin_capital=margin,
            risk_flags=flags,
            residual_risks=[],
            paper_delta_sim=sim,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_B2}|{near.inst_id}|{far.inst_id}",
            extra=extra,
        )

    # ---- B3 25Δ risk-reversal with wing cover (options_rr_static / + paper delta)

    @staticmethod
    def _nearest_delta(cands: list[OptRef], target: float) -> OptRef | None:
        cands = [c for c in cands if c.delta is not None]
        return min(cands, key=lambda c: abs(abs(c.delta) - target)) if cands else None

    def _rr_reference(self, expiry_ms: int, rr_mid: float) -> tuple[float | None, dict]:
        cfg = self.cfg
        hist = self._rr_history.setdefault(expiry_ms, [])
        if cfg.b3_rr_ref_mode == "fixed":
            ref = cfg.b3_rr_ref_fixed_volpts / 100.0
            info = {"mode": "fixed", "samples": None, "ref_volpts": round(ref * 100, 4)}
        elif len(hist) >= cfg.b3_rr_ref_min_samples:
            ref = statistics.median(hist)
            info = {
                "mode": "rolling_median_mid_prior_samples",
                "samples": len(hist),
                "ref_volpts": round(ref * 100, 4),
            }
        else:
            ref = None
            info = {
                "mode": "rolling_median_mid_prior_samples",
                "samples": len(hist),
                "ref_volpts": None,
            }
        hist.append(rr_mid)
        return ref, info

    def scan_b3(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        spot = snap.spot
        if spot is None or not spot.two_sided:
            self.skipped["b3_missing_spot"] += 1
            return []
        an = self._analytics(snap)
        if an is None:
            return []
        out: list[dict] = []
        for expiry_ms, strikes in sorted(an._chain.items()):
            if years_between(snap.ts_ms, expiry_ms) <= 0:
                self.skipped["b3_expired"] += 1
                continue
            puts, calls = [], []
            for K, pair in strikes.items():
                for typ, bucket in (("P", puts), ("C", calls)):
                    b = pair.get(typ)
                    if b is None or not b.two_sided:
                        continue
                    if (typ == "P" and K >= an.F_ref) or (typ == "C" and K <= an.F_ref):
                        continue
                    r = an.analyze(b)
                    if r is not None and r.iv_mid is not None and r.delta is not None:
                        bucket.append(r)
            put25 = self._nearest_delta(puts, cfg.b3_target_delta)
            call25 = self._nearest_delta(calls, cfg.b3_target_delta)
            if put25 is None or call25 is None:
                self.skipped["b3_no_otm_pair"] += 1
                continue
            rr_mid = call25.iv_mid - put25.iv_mid
            rr_ref, ref_info = self._rr_reference(expiry_ms, rr_mid)
            wing_put = self._nearest_delta(
                [p for p in puts if p.strike < put25.strike], cfg.b3_wing_delta
            )
            wing_call = self._nearest_delta(
                [c for c in calls if c.strike > call25.strike], cfg.b3_wing_delta
            )
            for direction, wing in (("long_rr", wing_put), ("short_rr", wing_call)):
                if wing is None:
                    self.skipped[f"b3_no_wing_cover_{direction}"] += 1  # naked RR is never built
                    continue
                rec = self._b3_record(
                    snap, an, expiry_ms, direction, call25, put25, wing, rr_mid, rr_ref, ref_info
                )
                if rec is not None:
                    out.append(rec)
        return out

    def _b3_record(
        self,
        snap: Snapshot,
        an: ChainAnalytics,
        expiry_ms: int,
        direction: str,
        call25: OptRef,
        put25: OptRef,
        wing: OptRef,
        rr_mid: float,
        rr_ref: float | None,
        ref_info: dict,
    ) -> dict | None:
        cfg = self.cfg
        spot = snap.spot
        qty = cfg.qty
        long_rr = direction == "long_rr"
        if long_rr:
            # buy 25Δ call @ask, sell 25Δ put @bid, buy further-OTM put wing @ask (covers short put)
            legs = [
                make_leg(call25.book, "buy", qty, "call"),
                make_leg(put25.book, "sell", qty, "put"),
                make_leg(wing.book, "buy", qty, "put"),
            ]
            rr_exec = (call25.iv_ask, put25.iv_bid)
        else:
            legs = [
                make_leg(call25.book, "sell", qty, "call"),
                make_leg(put25.book, "buy", qty, "put"),
                make_leg(wing.book, "buy", qty, "call"),
            ]
            rr_exec = (call25.iv_bid, put25.iv_ask)
        if any(leg is None for leg in legs):
            return None
        legs = [leg for leg in legs if leg is not None]
        if rr_exec[0] is None or rr_exec[1] is None:
            self.skipped["b3_exec_iv_unestimable"] += 1
            return None
        rr_exec_v = rr_exec[0] - rr_exec[1]
        N = spot.best_ask * qty
        T = call25.T
        interval_sec = snap.funding.interval_sec if snap.funding else 8 * 3600
        hold = self._hold(cfg, T, interval_sec)
        flags = [
            "model_fair_from_mid_iv_reference_only",
            "rr_is_directional_not_delta_neutral",
            "wing_assumed_fair_only_spread_and_fees_charged",
            "leverage_1x_margin_still_applies",
        ]
        if any(b.premium_ccy == "base" for b in (call25.book, put25.book, wing.book)):
            flags.append("option_premium_in_base_ccy_converted_at_spot_bid_ask")
        if (
            abs(abs(call25.delta) - cfg.b3_target_delta) > cfg.b3_delta_tol
            or abs(abs(put25.delta) - cfg.b3_target_delta) > cfg.b3_delta_tol
        ):
            flags.append("delta_off_target")
        if rr_ref is None:
            flags.append("rr_reference_unavailable")
            rr_ref_used = rr_mid  # edge then equals minus the spread crossing; never passes
        else:
            rr_ref_used = rr_ref
        edge_vol = (rr_ref_used - rr_exec_v) if long_rr else (rr_exec_v - rr_ref_used)
        vega_scale_q = 0.5 * ((call25.vega or 0.0) + (put25.vega or 0.0)) * qty
        gross_q = edge_vol * vega_scale_q

        books = {b.inst_id: b for b in (call25.book, put25.book, wing.book)}
        books[spot.inst_id] = spot
        fees_q, exit_spread_q = self._option_exit_costs(
            legs, books, spot, N, hold["hold_to_expiry"]
        )
        wing_bid_q = (
            premium_to_quote(wing.book.best_bid, wing.book.premium_ccy, "sell", spot) or 0.0
        )
        wing_ask_q = premium_to_quote(wing.book.best_ask, wing.book.premium_ccy, "buy", spot) or 0.0
        half_spread_q = exit_spread_q + (wing_ask_q - wing_bid_q) / 2.0 * qty
        if long_rr:
            call_ask_q = (
                premium_to_quote(call25.book.best_ask, call25.book.premium_ccy, "buy", spot) or 0.0
            )
            margin = (call_ask_q + wing_ask_q) * qty + (put25.strike - wing.strike) * qty
        else:
            put_ask_q = (
                premium_to_quote(put25.book.best_ask, put25.book.premium_ccy, "buy", spot) or 0.0
            )
            margin = (put_ask_q + wing_ask_q) * qty + (wing.strike - call25.strike) * qty

        hedge_mode = HEDGE_RR_STATIC
        sim = paper_delta_disabled(HEDGE_RR_STATIC)
        hedge_cost_q = 0.0
        if cfg.b3_paper_delta:
            hedge_book = (
                snap.perp
                if (
                    cfg.hedge_instrument == "perp" and snap.perp is not None and snap.perp.two_sided
                )
                else spot
            )
            hedge_kind = "perp" if hedge_book is snap.perp else "spot"
            sgn = 1.0 if long_rr else -1.0
            net_delta = qty * (sgn * call25.delta - sgn * put25.delta + wing.delta)
            net_gamma = qty * (
                sgn * (call25.gamma or 0.0) - sgn * (put25.gamma or 0.0) + (wing.gamma or 0.0)
            )
            sim = paper_delta_hedge_sim(
                net_delta=net_delta,
                net_gamma=net_gamma,
                hedge_book=hedge_book,
                hedge_kind=hedge_kind,
                funding=snap.funding,
                sigma_ref=0.5 * (call25.iv_mid + put25.iv_mid),
                hold_years=hold["hold_years"],
                cfg=cfg,
            )
            hedge_mode = HEDGE_RR_PAPER_DELTA
            hedge_cost_q = sim["cost_quote"]
            margin += sim["hedge_notional_quote"]
            books.setdefault(hedge_book.inst_id, hedge_book)
        costs = {
            "fees": fees_q,
            "half_spread_slip": half_spread_q,
            "hedge_rebalance": hedge_cost_q,
            "borrow": 0.0,
            "transfer": N * cfg.transfer_bps / 1e4,
            "capital_opp": margin * cfg.ref_rate_apr * hold["hold_years"],
            "funding_expected": sim.get("funding_paid_quote", 0.0),
            "funding_uncertainty": sim.get("funding_uncert_quote", 0.0),
            "vol_path_haircut": N * cfg.vol_path_haircut_bps / 1e4,
        }
        extra = {
            "direction": direction,
            "structure": "rr_target_delta_with_wing_cover",
            "expiry_ms": expiry_ms,
            "expiry": datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
            "T_years": round(T, 6),
            "hold": hold,
            "strikes": {"call": call25.strike, "put": put25.strike, "wing": wing.strike},
            "deltas_mid_ref": {
                "call": round(call25.delta, 6),
                "put": round(put25.delta, 6),
                "wing": round(wing.delta, 6),
                "target": cfg.b3_target_delta,
                "wing_target": cfg.b3_wing_delta,
            },
            "rr": {
                "definition": "IV(call) − IV(put), decimal vol; exec uses buy@ask / sell@bid IVs",
                "rr_mid_ref": round(rr_mid, 6),
                "rr_exec": round(rr_exec_v, 6),
                "rr_ref": round(rr_ref, 6) if rr_ref is not None else None,
                "rr_ref_info": ref_info,
                "edge_volpts": round(edge_vol * 100, 4),
                "vega_scale_quote_per_vol": round(vega_scale_q, 6),
                "iv_call_mid": round(call25.iv_mid, 6),
                "iv_put_mid": round(put25.iv_mid, 6),
                "forward_ref": round(an.F_ref, 6),
                "forward_source": an.forward_source,
            },
            "invalidation": [
                "skew_trends_against_mean_reversion",
                "edge_inside_buffer",
                "delta_unestimable_or_illiquid",
                "index_anomaly_or_halt",
            ],
        }
        return build_combo_record(
            snap=snap,
            family=FAMILY_B3,
            hedge_mode=hedge_mode,
            legs=legs,
            books=books,
            gross_quote=gross_q,
            notional_quote=N,
            costs_quote=costs,
            margin_capital=margin,
            risk_flags=flags,
            residual_risks=[],
            paper_delta_sim=sim,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_B3}|{expiry_ms}|{direction}|{call25.strike}|{put25.strike}|{wing.strike}",
            extra=extra,
        )


# --------------------------------------------------------------------------- summary


def summarize(
    records: list[dict],
    snapshots: int,
    cfg: ComboConfig,
    source: dict,
    skipped: Counter | None = None,
    a1_reference: list[dict] | None = None,
) -> dict:
    fam: dict[str, dict] = {}
    for f in FAMILIES:
        rows = [r for r in records if r["family"] == f]
        nets = [r["net_edge_bps"] for r in rows]
        fam[f] = {
            "combo_id": COMBO_ID[f],
            "records": len(rows),
            "edge_exceeds_buffer": sum(1 for r in rows if r["edge_exceeds_buffer"]),
            "passes_threshold": sum(1 for r in rows if r["passes_threshold"]),
            "median_net_edge_bps": round(statistics.median(nets), 4) if nets else None,
            "max_net_edge_bps": round(max(nets), 4) if nets else None,
            "taxonomies": sorted({r["taxonomy"] for r in rows}),
            "hedge_modes": sorted({r["hedge_mode"] for r in rows}),
            "residual_risks_required": list(RESIDUAL_RISKS[f]),
        }
    a1_rows = a1_reference or []
    a1_pass = sum(1 for r in a1_rows if r["passes_threshold"])
    a1_block = {
        "records": len(a1_rows),
        "passes_threshold": a1_pass,
        "pass_rate": round(a1_pass / len(a1_rows), 4) if a1_rows else None,
        "median_net_edge_bps": round(statistics.median([r["net_edge_bps"] for r in a1_rows]), 4)
        if a1_rows
        else None,
        "note": "Phase A A1 on the same snapshots — H-B1 comparator; not a Phase B record",
    }
    hyp: dict[str, dict] = {}
    for f in FAMILIES:
        hid = HYPOTHESIS[f]
        rows = [r for r in records if r["hypothesis_id"] == hid]
        n_pass = sum(1 for r in rows if r["passes_threshold"])
        if not rows:
            status = "no_samples"
        elif snapshots < cfg.persistence_min_samples:
            status = "insufficient_samples_for_persistence"
        elif n_pass == 0:
            status = "no_pass_on_window"  # consistent with falsification; not proof
        else:
            status = "not_yet_falsified_on_window"
        entry = {
            "records": len(rows),
            "passes_threshold": n_pass,
            "pass_rate": round(n_pass / len(rows), 4) if rows else None,
            "status": status,
            "falsify_if": FALSIFY_IF[hid],
            "note": "paper/read-only sample; no return claim; calibrate buffer before judging",
        }
        if hid == "H-B1":
            b1_rate = entry["pass_rate"]
            entry["a1_pass_rate_same_window"] = a1_block["pass_rate"]
            entry["b1_pass_rate_minus_a1"] = (
                round(b1_rate - a1_block["pass_rate"], 4)
                if b1_rate is not None and a1_block["pass_rate"] is not None
                else None
            )
        hyp[hid] = entry
    return {
        "mode": MODE,
        "phase": PHASE,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
        "live_delta_hedge": False,
        "venue": records[0]["venue"] if records else DEFAULT_VENUE,
        "snapshots": snapshots,
        "records": len(records),
        "families": fam,
        "hypotheses": hyp,
        "a1_reference_same_window": a1_block,
        "skipped": dict(sorted((skipped or Counter()).items())),
        "config": cfg.to_dict(),
        "data_source": source,
        "policy": okx.POLICY,
        "taxonomy_rule": {
            "all_phase_B": TAXONOMY,
            "forbidden_label_gate": "enforced_in_finalize_combo_record",
        },
        "mainline_unchanged": "spot_grid_local_paper",
        "related_phase_A": RELATED_PHASE_A,
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper combo scanner (Phase B: B1 covered-call carry, B2 calendar vol, "
        "B3 25Δ risk-reversal with wing cover). Read-only, observe_only, will_send_http=false, "
        "taxonomy=relative_value, paper-simulated delta only. Emits JSON lines.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", choices=["fixture", "okx-public"], default="fixture")
    p.add_argument(
        "--fixture",
        default=str(ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-combo-books-sample.json"),
        help="fixture JSON with snapshots (offline replay; Phase A fixtures also load)",
    )
    p.add_argument("--venue", default=DEFAULT_VENUE)
    p.add_argument("--spot", default="ETH-USDT")
    p.add_argument("--perp", default="ETH-USDT-SWAP")
    p.add_argument("--opt-family", default="ETH-USD", help="'' to skip options (then no records)")
    p.add_argument(
        "--n-strikes", type=int, default=8, help="okx-public: strikes nearest spot per expiry"
    )
    p.add_argument("--max-expiries", type=int, default=2, help="okx-public: B2 needs >= 2")
    p.add_argument("--min-days-to-expiry", type=float, default=2.0)
    p.add_argument("--book-depth", type=int, default=5)
    p.add_argument("--samples", type=int, default=1, help="okx-public: snapshots to take")
    p.add_argument("--interval-sec", type=float, default=20.0, help="okx-public: seconds between")
    p.add_argument("--base-url", default=okx.OKX_PUBLIC_BASE)
    p.add_argument("--timeout", type=float, default=10.0)

    p.add_argument("--qty", type=float, default=1.0)
    p.add_argument(
        "--horizon-intervals", type=int, default=3, help="funding intervals (3 × 8h = 1d)"
    )
    p.add_argument("--ref-rate-apr", type=float, default=0.0)
    p.add_argument("--funding-sigma-bps", type=float, default=2.0)
    p.add_argument("--transfer-bps", type=float, default=0.0)
    p.add_argument(
        "--vol-path-haircut-bps", type=float, default=10.0, help="cost per short option leg"
    )
    p.add_argument("--depth-mult", type=float, default=2.0)
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--persistence-min-samples", type=int, default=3)
    p.add_argument("--persistence-min-sec", type=float, default=60.0)
    p.add_argument("--credit-favorable-basis", action="store_true")

    p.add_argument("--b1-delta-min", type=float, default=0.15)
    p.add_argument("--b1-delta-max", type=float, default=0.25)
    p.add_argument("--b1-moneyness-min", type=float, default=0.05)
    p.add_argument("--b1-moneyness-max", type=float, default=0.30)
    p.add_argument("--b1-max-calls", type=int, default=2)

    p.add_argument("--b2-opt-type", choices=["C", "P"], default="C")
    p.add_argument("--b2-max-pairs", type=int, default=6)
    p.add_argument(
        "--hedge-instrument", choices=["perp", "spot"], default="perp", help="paper sim only"
    )
    p.add_argument("--rebalances-per-day", type=float, default=3.0)
    p.add_argument("--hedge-slip-bps", type=float, default=2.0)

    p.add_argument("--b3-target-delta", type=float, default=0.25)
    p.add_argument("--b3-wing-delta", type=float, default=0.10)
    p.add_argument("--b3-delta-tol", type=float, default=0.10)
    p.add_argument("--b3-rr-ref-mode", choices=["rolling_mid", "fixed"], default="rolling_mid")
    p.add_argument("--b3-rr-ref-fixed-volpts", type=float, default=0.0)
    p.add_argument("--b3-rr-ref-min-samples", type=int, default=2)
    p.add_argument(
        "--b3-paper-delta", action="store_true", help="hedge_mode=options_rr_plus_paper_delta"
    )

    p.add_argument("--fee-spot-bps", type=float, default=10.0)
    p.add_argument("--fee-perp-bps", type=float, default=5.0)
    p.add_argument("--fee-option-bps", type=float, default=3.0)
    p.add_argument("--fee-option-cap-pct", type=float, default=12.5)
    p.add_argument("--fee-option-settle-bps", type=float, default=2.0)

    p.add_argument(
        "--buffer-fee-roundtrip-bps", type=float, default=None, help="None → record fees"
    )
    p.add_argument("--buffer-slip-bps", type=float, default=5.0)
    p.add_argument("--buffer-funding-uncert-bps", type=float, default=5.0)
    p.add_argument("--buffer-vol-path-haircut-bps", type=float, default=10.0)
    p.add_argument("--buffer-haircut-bps", type=float, default=10.0)
    p.add_argument(
        "--buffer-calibrated", action="store_true", help="only after 04-risk calibration"
    )

    p.add_argument("--paper-fills", action="store_true")
    p.add_argument("--paper-extra-slip-bps", type=float, default=5.0)

    p.add_argument("--out", default="", help="JSONL records file (default stdout)")
    p.add_argument("--summary-out", default="", help="summary JSON file")
    p.add_argument("--print-summary", action="store_true", help="summary JSON to stderr")
    p.add_argument("--only-exceeding", action="store_true", help="emit only edge_exceeds_buffer")
    p.add_argument("--quiet", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> ComboConfig:
    return ComboConfig(
        qty=args.qty,
        horizon_intervals=args.horizon_intervals,
        ref_rate_apr=args.ref_rate_apr,
        funding_sigma_bps_per_interval=args.funding_sigma_bps,
        transfer_bps=args.transfer_bps,
        vol_path_haircut_bps=args.vol_path_haircut_bps,
        depth_mult=args.depth_mult,
        top_n=args.top_n,
        persistence_min_samples=args.persistence_min_samples,
        persistence_min_sec=args.persistence_min_sec,
        credit_favorable_basis=args.credit_favorable_basis,
        paper_fills=args.paper_fills,
        paper_extra_slip_bps=args.paper_extra_slip_bps,
        b1_delta_min=args.b1_delta_min,
        b1_delta_max=args.b1_delta_max,
        b1_moneyness_min=args.b1_moneyness_min,
        b1_moneyness_max=args.b1_moneyness_max,
        b1_max_calls_per_expiry=args.b1_max_calls,
        b2_opt_type=args.b2_opt_type,
        b2_max_pairs=args.b2_max_pairs,
        hedge_instrument=args.hedge_instrument,
        rebalances_per_day=args.rebalances_per_day,
        hedge_slip_bps=args.hedge_slip_bps,
        b3_target_delta=args.b3_target_delta,
        b3_wing_delta=args.b3_wing_delta,
        b3_delta_tol=args.b3_delta_tol,
        b3_rr_ref_mode=args.b3_rr_ref_mode,
        b3_rr_ref_fixed_volpts=args.b3_rr_ref_fixed_volpts,
        b3_rr_ref_min_samples=args.b3_rr_ref_min_samples,
        b3_paper_delta=args.b3_paper_delta,
        fees=FeeSchedule(
            spot_taker_bps=args.fee_spot_bps,
            perp_taker_bps=args.fee_perp_bps,
            option_taker_bps=args.fee_option_bps,
            option_fee_cap_pct_premium=args.fee_option_cap_pct,
            option_settlement_bps=args.fee_option_settle_bps,
        ),
        buffer=ComboSafetyBuffer(
            fee_roundtrip_bps=args.buffer_fee_roundtrip_bps,
            slip_buffer_bps=args.buffer_slip_bps,
            funding_uncert_bps=args.buffer_funding_uncert_bps,
            vol_path_haircut_bps=args.buffer_vol_path_haircut_bps,
            model_haircut_bps=args.buffer_haircut_bps,
            calibrated=args.buffer_calibrated,
        ),
    )


def run(args: argparse.Namespace) -> tuple[list[dict], dict]:
    cfg = config_from_args(args)
    scanner = ComboScanner(cfg)
    records: list[dict] = []
    if args.source == "fixture":
        snaps = load_fixture(args.fixture)
        for s in snaps:
            records.extend(scanner.scan(s))
        source = snaps[0].source if snaps else {"kind": "fixture", "http_fetch": False}
        n = len(snaps)
    else:
        client = okx.OkxPublicClient(base_url=args.base_url, timeout=args.timeout)
        src = pas.OkxPublicSnapshotSource(
            client,
            spot_inst=args.spot,
            perp_inst=args.perp,
            opt_family=args.opt_family or None,
            n_strikes=args.n_strikes,
            max_expiries=args.max_expiries,
            min_days_to_expiry=args.min_days_to_expiry,
            book_depth=args.book_depth,
        )
        n = max(args.samples, 1)
        source = {}
        for i in range(n):
            snap = replace(src.fetch(), venue=args.venue)
            records.extend(scanner.scan(snap))
            source = snap.source
            if i < n - 1:
                time.sleep(args.interval_sec)
    if args.only_exceeding:
        records = [r for r in records if r["edge_exceeds_buffer"]]
    summary = summarize(records, n, cfg, source, scanner.skipped, scanner.a1_reference_records)
    return records, summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records, summary = run(args)
    lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(lines + ("\n" if lines else ""))
    elif not args.quiet:
        if lines:
            print(lines)
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    if args.print_summary:
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        okx.ReadOnlyViolation,
        okx.OkxApiError,
        ObserveOnlyViolation,
        ExecutablePriceViolation,
        NakedShortOptionRefused,
        ComboSchemaViolation,
        ForbiddenLabelViolation,
        RuntimeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
