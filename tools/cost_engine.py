#!/usr/bin/env python3
"""Cost Engine v0 — paper / read-only all-in cost, net edge and breakeven funding.

Implements roadmap P0 (`03-proposals/2026-09-17-impl-roadmap-from-share-v1.md` §4) under the
04-risk conditional pass of the same date: Phase C *paper / read-only engineering only*;
Execution refused; merging this ≠ any trading clearance.

What it is: the cost arithmetic that Phase A (`strategies/paper_arb_scanner.py`) and Phase B
(`strategies/paper_combo_scanner.py`) already carry inline, promoted to one reusable, tested
module. Given bid/ask-priced execution legs — or already-priced quote-currency cost buckets —
it returns

  * all_in_cost_bps          sum of the component costs, bps of underlying notional
  * net_edge_bps             gross_edge_bps − all_in_cost_bps over the *holding horizon*
  * breakeven_funding_rate   the per-interval funding rate at which net_edge_bps == 0
  * calibrated               False unless the caller cites a 04-risk calibration reference
  * component breakdown      fees, half_spread_slip, impact, borrow, transfer, capital_opp,
                             funding_uncertainty, hedge_rebalance (+ carried extras such as
                             funding_expected / vol_path_haircut)

Hard gates (enforced in code, tested, not just documented):
  * every result carries action == "observe_only", will_send_http == False; the module has
    no network code path at all (no HTTP client, no trade / amend / execution);
  * executable prices are bid/ask only: a LegSpec whose price_type is mark / mid / last, or
    whose side/price_type convention is wrong (buy must hit the ask, sell the bid), is refused;
    a mark price can only travel as `mark_ref`; a one-sided book is skipped, never guessed;
  * `calibrated` defaults to False; claiming True without a calibration reference is refused;
    even a calibrated result never carries a tradable APY claim — annualised numbers exist only
    as `_ref` display conversions (`apy_ref()`), never as net edge or as a threshold input;
  * `current_funding × 365` is not a net edge: evaluate() accepts a hold-horizon gross only
    (`gross_basis="hold_horizon"`); an annualised gross is refused with
    NaiveAnnualizationRefused; FundingLeg expectation is min(|now|, |next|) × H intervals.

The spot-grid / local_paper mainline is untouched: this module does not import or alter
tools/local_paper_grid.py or strategies/grid_ab_compare.py. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- constants

ENGINE = "cost_engine"
VERSION = "v0"
ACTION = "observe_only"
WILL_SEND_HTTP = False
MODE = "paper_read_only"

CORE_COMPONENTS = (
    "fees",
    "half_spread_slip",
    "impact",
    "borrow",
    "transfer",
    "capital_opp",
    "funding_uncertainty",
    "hedge_rebalance",
)
# Extras Phase A / B already carry; passed through and summed, never silently dropped.
KNOWN_EXTRA_COMPONENTS = ("funding_expected", "vol_path_haircut")

EDGE_BASIS = "hold_horizon"
REFUSED_GROSS_BASES = frozenset(
    {"annualized", "annualised", "apy", "apr", "x365", "x1095", "per_year", "yearly"}
)

INSTRUMENT_KINDS = frozenset({"spot", "perp", "option"})
EXECUTABLE_PRICE_TYPES = frozenset({"bid", "ask"})
SIDE_TO_PRICE_TYPE = {"buy": "ask", "sell": "bid"}

SECONDS_PER_YEAR = 365.0 * 86_400.0
DEFAULT_FUNDING_INTERVAL_SEC = 8 * 3600

# Substrings that may never appear in a result (same list as the Phase B scanner).
FORBIDDEN_LABELS = ("risk_free", "riskfree", "risk-free", "无风险", "稳赚", "guaranteed")

DISCLAIMER = (
    "Cost Engine v0：纸面只读成本算术（observe_only，will_send_http=false）。"
    "all_in_cost 为分量之和；net_edge 按持有期计，不年化；breakeven_funding 是使净边为 0 的"
    "每期 funding 门槛，不是预测。calibrated=false 时不得升级任何「可交易」话术；"
    "年化字段一律带 _ref，仅为展示换算，不是收益承诺。不下单。"
)

# --------------------------------------------------------------------------- errors


class CostEngineError(ValueError):
    """Bad input to the cost engine."""


class ExecutablePriceViolation(CostEngineError):
    """A leg tried to use a non bid/ask price as executable, or a one-sided book."""


class NaiveAnnualizationRefused(CostEngineError):
    """Someone tried to feed an annualised (× 365-style) number in as gross / net edge."""


class CalibrationClaimRefused(CostEngineError):
    """`calibrated=True` was claimed without a calibration reference."""


class ObserveOnlyViolation(RuntimeError):
    """A result tried to leave observe_only / will_send_http=false."""


# --------------------------------------------------------------------------- helpers


def bps(x: float, base: float) -> float:
    return x / base * 1e4 if base else 0.0


def _require_nonneg(name: str, value: float) -> None:
    if value < 0:
        raise CostEngineError(f"{name} must be >= 0, got {value!r}")


def _require_pos(name: str, value: float) -> None:
    if value <= 0:
        raise CostEngineError(f"{name} must be > 0, got {value!r}")


# --------------------------------------------------------------------------- fee schedule


@dataclass(frozen=True)
class FeeSchedule:
    """Taker fees in bps of notional. Same shape as the Phase A schedule (any object with these
    attributes is accepted). Defaults are OKX retail-tier placeholders — calibrate before use."""

    spot_taker_bps: float = 10.0
    perp_taker_bps: float = 5.0
    option_taker_bps: float = 3.0  # of underlying notional ...
    option_fee_cap_pct_premium: float = 12.5  # ... capped at this % of premium (OKX rule)
    option_settlement_bps: float = 2.0  # exercise / settlement fee per option leg

    def to_dict(self) -> dict:
        return {
            "spot_taker_bps": self.spot_taker_bps,
            "perp_taker_bps": self.perp_taker_bps,
            "option_taker_bps": self.option_taker_bps,
            "option_fee_cap_pct_premium": self.option_fee_cap_pct_premium,
            "option_settlement_bps": self.option_settlement_bps,
            "note": "taker-only (crossing bid/ask); placeholders, calibrate before use",
        }


# --------------------------------------------------------------------------- component primitives
# All primitives return *quote currency* amounts (USDT). evaluate() converts to bps of notional.


def fee_quote(
    kind: str,
    notional_quote: float,
    fees: FeeSchedule,
    *,
    crossings: int = 1,
    premium_quote: float | None = None,
    settlement: bool = False,
) -> float:
    """Taker fee for one leg. spot / perp: notional × taker × crossings. option: per crossing
    min(underlying notional × option_taker, premium × cap%), plus one settlement fee if held to
    expiry. `notional_quote` is always the *underlying* notional."""
    if kind not in INSTRUMENT_KINDS:
        raise CostEngineError(f"unknown instrument kind {kind!r}")
    if crossings < 0:
        raise CostEngineError("crossings must be >= 0")
    _require_nonneg("notional_quote", notional_quote)
    if kind == "option":
        if premium_quote is None:
            raise CostEngineError("option fee needs premium_quote (fee cap is % of premium)")
        _require_nonneg("premium_quote", premium_quote)
        trade = min(
            notional_quote * fees.option_taker_bps / 1e4,
            premium_quote * fees.option_fee_cap_pct_premium / 100.0,
        )
        settle = notional_quote * fees.option_settlement_bps / 1e4 if settlement else 0.0
        return trade * crossings + settle
    rate = fees.spot_taker_bps if kind == "spot" else fees.perp_taker_bps
    return notional_quote * rate / 1e4 * crossings


def half_spread_quote(
    bid: float | None,
    ask: float | None,
    qty: float,
    *,
    crossings: int = 1,
    quote_conv: float = 1.0,
) -> float:
    """Half the bid/ask spread × qty per crossing. The *entry* spread is already inside a bid/ask
    executable price, so callers pass the number of *additional* crossings (exits, rolls)."""
    if bid is None or ask is None:
        raise ExecutablePriceViolation("half spread needs a two-sided bid/ask book (not guessed)")
    if crossings < 0:
        raise CostEngineError("crossings must be >= 0")
    _require_pos("qty", qty)
    return max(ask - bid, 0.0) / 2.0 * qty * crossings * quote_conv


def impact_quote(
    executable_px: float, vwap: float | None, qty: float, *, quote_conv: float = 1.0
) -> float:
    """VWAP-vs-top-of-book slippage for eating `qty` of one side. None VWAP → 0 (not measured)."""
    _require_pos("qty", qty)
    if vwap is None:
        return 0.0
    return abs(vwap - executable_px) * qty * quote_conv


def borrow_quote(borrowed_notional_quote: float, apr: float, hold_years: float) -> float:
    _require_nonneg("borrowed_notional_quote", borrowed_notional_quote)
    _require_nonneg("apr", apr)
    _require_nonneg("hold_years", hold_years)
    return borrowed_notional_quote * apr * hold_years


def transfer_quote(notional_quote: float, transfer_bps: float) -> float:
    _require_nonneg("notional_quote", notional_quote)
    _require_nonneg("transfer_bps", transfer_bps)
    return notional_quote * transfer_bps / 1e4


def capital_opp_quote(capital_quote: float, ref_rate_apr: float, hold_years: float) -> float:
    """Opportunity cost of the capital locked (margin + premium paid) at `ref_rate_apr`.
    ref_rate_apr = 0 means no opportunity-cost claim is made (Phase A/B default)."""
    _require_nonneg("capital_quote", capital_quote)
    _require_nonneg("ref_rate_apr", ref_rate_apr)
    _require_nonneg("hold_years", hold_years)
    return capital_quote * ref_rate_apr * hold_years


def funding_uncertainty_quote(
    notional_quote: float,
    sigma_bps_per_interval: float,
    intervals: float,
    *,
    pred_gap_rate: float = 0.0,
) -> float:
    """Funding path uncertainty charged as a cost: σ per interval × H plus the |now − next|
    prediction gap × H (both in rate terms of notional)."""
    _require_nonneg("notional_quote", notional_quote)
    _require_nonneg("sigma_bps_per_interval", sigma_bps_per_interval)
    _require_nonneg("intervals", intervals)
    _require_nonneg("pred_gap_rate", pred_gap_rate)
    return notional_quote * (sigma_bps_per_interval / 1e4 * intervals + pred_gap_rate * intervals)


def hedge_rebalance_quote(notional_quote: float, hedge_rebalance_bps: float) -> float:
    """Flat re-hedge allowance. A paper delta simulation (Phase B B2) passes its own quote cost
    straight into the `hedge_rebalance` bucket instead."""
    _require_nonneg("notional_quote", notional_quote)
    _require_nonneg("hedge_rebalance_bps", hedge_rebalance_bps)
    return notional_quote * hedge_rebalance_bps / 1e4


# --------------------------------------------------------------------------- legs (bid/ask only)


@dataclass(frozen=True)
class LegSpec:
    """One execution leg priced at bid/ask only.

    side buy → executes at `ask`; side sell → at `bid`. `price_type` may be left empty (derived)
    or given explicitly — anything but the matching bid/ask is refused. `mark_ref` is carried for
    reference only and never priced. `crossings` = spread / fee crossings modelled for the leg
    (2 = entry + exit; 1 = entry only, e.g. an option held to expiry with `settlement=True`).
    `vwap` is the book-walk VWAP for `qty` (None → impact not measured → 0).
    `quote_conv` converts price units to quote currency (coin-priced premium × spot bid/ask).
    `slip_crossings` — additional half-spread crossings charged to `half_spread_slip`
    *independently of the fee crossings*. None (default) keeps the Phase A/B convention
    `crossings − 1`. A single-pass multi-leg spot cycle (T1 triangle) uses `crossings=1`
    (one fee per leg) with `slip_crossings=1` as an explicit non-atomic re-quote haircut.
    """

    instrument: str
    kind: str
    side: str
    qty: float
    bid: float | None
    ask: float | None
    price_type: str = ""
    vwap: float | None = None
    quote_conv: float = 1.0
    crossings: int = 2
    settlement: bool = False
    mark_ref: float | None = None
    slip_crossings: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in INSTRUMENT_KINDS:
            raise CostEngineError(f"leg kind must be spot|perp|option, got {self.kind!r}")
        if self.side not in SIDE_TO_PRICE_TYPE:
            raise CostEngineError(f"leg side must be buy|sell, got {self.side!r}")
        want = SIDE_TO_PRICE_TYPE[self.side]
        pt = self.price_type or want
        if pt not in EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(
                f"refused: executable price_type {pt!r} for {self.instrument} "
                "(bid/ask only; mark/mid/last are not tradable)"
            )
        if pt != want:
            raise ExecutablePriceViolation(
                f"refused: {self.side} must use {want}, got {pt!r} for {self.instrument}"
            )
        object.__setattr__(self, "price_type", pt)
        _require_pos("qty", self.qty)
        _require_pos("quote_conv", self.quote_conv)
        if self.crossings < 1:
            raise CostEngineError("crossings must be >= 1 (the entry always crosses)")
        if self.slip_crossings is not None and self.slip_crossings < 0:
            raise CostEngineError("slip_crossings must be >= 0")
        px = self.executable_price
        if px is None:
            raise ExecutablePriceViolation(
                f"refused: {self.instrument} has no {want} — one-sided book is skipped, not guessed"
            )
        if px <= 0:
            raise CostEngineError("executable price must be > 0")

    @property
    def executable_price(self) -> float | None:
        return self.ask if self.side == "buy" else self.bid

    @property
    def two_sided(self) -> bool:
        return self.bid is not None and self.ask is not None

    @property
    def spread_crossings(self) -> int:
        """Half-spread crossings charged to `half_spread_slip` (entry spread excluded)."""
        return self.slip_crossings if self.slip_crossings is not None else self.crossings - 1

    def exec_quote(self) -> float:
        """Executable value of the leg in quote currency (price × qty × quote_conv)."""
        return float(self.executable_price) * self.qty * self.quote_conv

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "kind": self.kind,
            "side": self.side,
            "qty": self.qty,
            "executable_price": self.executable_price,
            "price_type": self.price_type,
            "bid": self.bid,
            "ask": self.ask,
            "vwap": self.vwap,
            "quote_conv": self.quote_conv,
            "crossings": self.crossings,
            "settlement": self.settlement,
            "mark_ref": self.mark_ref,
            "slip_crossings": self.slip_crossings,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> LegSpec:
        return cls(
            instrument=str(d["instrument"]),
            kind=str(d["kind"]),
            side=str(d["side"]),
            qty=float(d["qty"]),
            bid=float(d["bid"]) if d.get("bid") is not None else None,
            ask=float(d["ask"]) if d.get("ask") is not None else None,
            price_type=str(d.get("price_type") or ""),
            vwap=float(d["vwap"]) if d.get("vwap") is not None else None,
            quote_conv=float(d.get("quote_conv", 1.0)),
            crossings=int(d.get("crossings", 2)),
            settlement=bool(d.get("settlement", False)),
            mark_ref=float(d["mark_ref"]) if d.get("mark_ref") is not None else None,
            slip_crossings=(
                int(d["slip_crossings"]) if d.get("slip_crossings") is not None else None
            ),
        )


def leg_costs(
    legs: Sequence[LegSpec], fees: FeeSchedule, *, underlying_notional_quote: float
) -> dict[str, float]:
    """fees / half_spread_slip / impact in quote currency from bid/ask legs.

    * fees: spot / perp on their own executable notional per crossing; options on the
      *underlying* notional with the premium cap, plus settlement if held to expiry;
    * half_spread_slip: `spread_crossings` additional half-spreads per leg (default
      crossings − 1) — the entry spread is already embedded in the bid/ask executable price;
    * impact: |VWAP − top| × qty per leg (entry only; None VWAP → 0).
    """
    _require_pos("underlying_notional_quote", underlying_notional_quote)
    fees_q = 0.0
    spread_q = 0.0
    impact_q = 0.0
    for leg in legs:
        if leg.kind == "option":
            fees_q += fee_quote(
                "option",
                underlying_notional_quote,
                fees,
                crossings=leg.crossings,
                premium_quote=leg.exec_quote(),
                settlement=leg.settlement,
            )
        else:
            fees_q += fee_quote(leg.kind, leg.exec_quote(), fees, crossings=leg.crossings)
        if leg.spread_crossings > 0:
            spread_q += half_spread_quote(
                leg.bid, leg.ask, leg.qty, crossings=leg.spread_crossings, quote_conv=leg.quote_conv
            )
        impact_q += impact_quote(
            float(leg.executable_price), leg.vwap, leg.qty, quote_conv=leg.quote_conv
        )
    return {"fees": fees_q, "half_spread_slip": spread_q, "impact": impact_q}


# --------------------------------------------------------------------------- funding


@dataclass(frozen=True)
class FundingLeg:
    """Conservative funding expectation over a *horizon in intervals* — never × 365.

    `rate_now` / `rate_next` are signed per-interval rates as published (positive = longs pay
    shorts). `receiver` says which side the position is on: `short_perp` receives positive
    funding, `long_perp` receives negative funding. The rate used is min(|now|, |next|) when
    both point the same way; a predicted sign flip zeroes it and raises a flag.
    """

    rate_now: float
    horizon_intervals: int
    interval_sec: int = DEFAULT_FUNDING_INTERVAL_SEC
    rate_next: float | None = None
    receiver: str = "short_perp"

    def __post_init__(self) -> None:
        if self.receiver not in ("short_perp", "long_perp"):
            raise CostEngineError("receiver must be short_perp|long_perp")
        if self.horizon_intervals < 0:
            raise CostEngineError("horizon_intervals must be >= 0")
        _require_pos("interval_sec", self.interval_sec)
        if self.horizon_intervals > self.intervals_per_year:
            raise NaiveAnnualizationRefused(
                "horizon_intervals exceeds one year of intervals — annualised horizons are not "
                "a funding expectation; use apy_ref() for display only"
            )

    @property
    def intervals_per_year(self) -> float:
        return SECONDS_PER_YEAR / self.interval_sec

    @property
    def hold_years(self) -> float:
        return self.horizon_intervals * self.interval_sec / SECONDS_PER_YEAR

    def _received(self, rate: float) -> float:
        return rate if self.receiver == "short_perp" else -rate

    def used_rate_per_interval(self) -> tuple[float, list[str]]:
        """Signed per-interval rate *received* by the position (negative = paying), and flags."""
        flags: list[str] = []
        now = self._received(self.rate_now)
        if self.rate_next is None:
            return now, flags
        nxt = self._received(self.rate_next)
        if now == 0.0 or nxt == 0.0:
            return min(now, nxt, key=abs), flags
        if (now > 0) != (nxt > 0):
            flags.append("funding_sign_flip_predicted")
            return 0.0, flags
        return (now if abs(now) <= abs(nxt) else nxt), flags

    def pred_gap_rate(self) -> float:
        return abs(abs(self.rate_now) - abs(self.rate_next)) if self.rate_next is not None else 0.0

    def expected_quote(self, notional_quote: float) -> float:
        """Signed funding P&L over the horizon: used_rate × H × notional. Not annualised."""
        _require_nonneg("notional_quote", notional_quote)
        used, _ = self.used_rate_per_interval()
        return used * self.horizon_intervals * notional_quote

    def context(self, notional_quote: float) -> FundingContext:
        """Split the expectation into the gross credit / cost buckets evaluate() understands."""
        exp = self.expected_quote(notional_quote)
        used, _ = self.used_rate_per_interval()
        return FundingContext(
            intervals=float(self.horizon_intervals),
            interval_sec=self.interval_sec,
            funding_gross_quote=max(exp, 0.0),
            funding_cost_quote=max(-exp, 0.0),
            rate_used_per_interval=used,
        )

    def to_dict(self) -> dict:
        used, flags = self.used_rate_per_interval()
        return {
            "rate_now": self.rate_now,
            "rate_next": self.rate_next,
            "receiver": self.receiver,
            "interval_sec": self.interval_sec,
            "horizon_intervals": self.horizon_intervals,
            "hold_years": round(self.hold_years, 8),
            "rate_used_per_interval_received": used,
            "rule": "min(|now|, |next|) same sign; predicted flip → 0; × H intervals, never × 365",
            "flags": flags,
        }


@dataclass(frozen=True)
class FundingContext:
    """How funding enters a record, so evaluate() can solve for the breakeven rate.

    intervals            H — holding horizon in funding intervals (0 → no breakeven)
    funding_gross_quote  funding *credited inside gross_quote* (>= 0)
    funding_cost_quote   funding *charged inside the components* (`funding_expected`) (>= 0)
    rate_used_per_interval  informational: signed per-interval rate received the caller used
    """

    intervals: float
    interval_sec: int = DEFAULT_FUNDING_INTERVAL_SEC
    funding_gross_quote: float = 0.0
    funding_cost_quote: float = 0.0
    rate_used_per_interval: float | None = None

    def __post_init__(self) -> None:
        _require_nonneg("intervals", self.intervals)
        _require_pos("interval_sec", self.interval_sec)
        _require_nonneg("funding_gross_quote", self.funding_gross_quote)
        _require_nonneg("funding_cost_quote", self.funding_cost_quote)

    @property
    def intervals_per_year(self) -> float:
        return SECONDS_PER_YEAR / self.interval_sec

    @classmethod
    def from_dict(cls, d: Mapping) -> FundingContext:
        return cls(
            intervals=float(d["intervals"]),
            interval_sec=int(d.get("interval_sec", DEFAULT_FUNDING_INTERVAL_SEC)),
            funding_gross_quote=float(d.get("funding_gross_quote", 0.0)),
            funding_cost_quote=float(d.get("funding_cost_quote", 0.0)),
            rate_used_per_interval=(
                float(d["rate_used_per_interval"])
                if d.get("rate_used_per_interval") is not None
                else None
            ),
        )


def breakeven_funding(
    *,
    gross_quote: float,
    all_in_cost_quote: float,
    notional_quote: float,
    funding: FundingContext | None,
) -> dict:
    """Per-interval funding rate f* received by the position at which net edge == 0.

    With G_f / C_f the funding credited in gross / charged in costs and G_o / C_o everything
    else: net = (G_f − C_f) + G_o − C_o and (G_f − C_f) = f × H × N, so
        f* = (C_o − G_o) / (H × N),   headroom = f_implied − f*,   net = headroom × H × N.
    funding_uncertainty stays inside C_o (it does not scale with f). No funding context, or
    H == 0 → None with a reason. This is a threshold, not a forecast.
    """
    if funding is None:
        return {"rate_per_interval": None, "reason": "no_funding_context"}
    if funding.intervals <= 0:
        return {"rate_per_interval": None, "reason": "zero_horizon_intervals"}
    other_gross = gross_quote - funding.funding_gross_quote
    other_cost = all_in_cost_quote - funding.funding_cost_quote
    denom = funding.intervals * notional_quote
    rate = (other_cost - other_gross) / denom
    implied = (funding.funding_gross_quote - funding.funding_cost_quote) / denom
    return {
        "rate_per_interval": round(rate, 10),
        "bps_per_interval": round(rate * 1e4, 4),
        "intervals": funding.intervals,
        "interval_sec": funding.interval_sec,
        "rate_used_per_interval": funding.rate_used_per_interval,
        "rate_implied_by_inputs_per_interval": round(implied, 10),
        "headroom_bps_per_interval": round((implied - rate) * 1e4, 4),
        "apr_ref": {
            "value": round(rate * funding.intervals_per_year, 8),
            "display_only": True,
            "return_promise": False,
            "note": "breakeven × intervals/year — display conversion, not a tradable APY",
        },
        "definition": (
            "per-interval funding rate received by the position at which net_edge_bps == 0 "
            "over the horizon; funding_uncertainty stays in costs; threshold, not a forecast"
        ),
    }


def apy_ref(edge_bps: float, hold_years: float | None) -> dict:
    """Display-only annualisation of a horizon edge. Refuses hold_years <= 0. Never a threshold
    input, never a net edge — the fields say so."""
    if hold_years is None or hold_years <= 0:
        raise CostEngineError("apy_ref needs hold_years > 0")
    return {
        "apy_ref_bps": round(edge_bps / hold_years, 4),
        "hold_years": round(hold_years, 8),
        "display_only": True,
        "return_promise": False,
        "is_net_edge": False,
        "note": "simple (non-compounded) edge_bps / hold_years; display conversion only",
    }


# --------------------------------------------------------------------------- config / result


@dataclass(frozen=True)
class CostEngineConfig:
    """`calibrated` may only be True with a `calibration_ref` (e.g. the 04-risk note that did the
    calibration on historical books). Even then no tradable-APY claim is emitted."""

    calibrated: bool = False
    calibration_ref: str | None = None
    round_dp: int = 4

    def __post_init__(self) -> None:
        if self.calibrated and not (self.calibration_ref or "").strip():
            raise CalibrationClaimRefused(
                "refused: calibrated=True needs a calibration_ref (04-risk calibration note)"
            )
        if self.round_dp < 0:
            raise CostEngineError("round_dp must be >= 0")

    def to_dict(self) -> dict:
        return {
            "engine": ENGINE,
            "version": VERSION,
            "calibrated": self.calibrated,
            "calibration_ref": self.calibration_ref,
            "round_dp": self.round_dp,
        }


DEFAULT_CONFIG = CostEngineConfig()


@dataclass(frozen=True)
class CostResult:
    notional_quote: float
    hold_years: float | None
    components_quote: dict[str, float]
    components_bps: dict[str, float]
    all_in_cost_quote: float
    all_in_cost_bps: float
    gross_edge_bps: float
    net_edge_bps: float
    breakeven_funding: dict
    calibrated: bool
    calibration_ref: str | None
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def breakeven_funding_rate(self) -> float | None:
        return self.breakeven_funding.get("rate_per_interval")

    def to_dict(self) -> dict:
        core = {k: self.components_bps[k] for k in CORE_COMPONENTS}
        extra = {k: v for k, v in self.components_bps.items() if k not in CORE_COMPONENTS}
        d = {
            "engine": ENGINE,
            "version": VERSION,
            "mode": MODE,
            "action": ACTION,
            "will_send_http": WILL_SEND_HTTP,
            "notional_quote": round(self.notional_quote, 6),
            "hold_years": round(self.hold_years, 8) if self.hold_years is not None else None,
            "components_bps": {**core, **extra},
            "components_core": list(CORE_COMPONENTS),
            "components_extra": sorted(extra),
            "components_quote": {k: round(v, 8) for k, v in self.components_quote.items()},
            "all_in_cost_bps": self.all_in_cost_bps,
            "all_in_cost_quote": round(self.all_in_cost_quote, 8),
            "gross_edge_bps": self.gross_edge_bps,
            "net_edge_bps": self.net_edge_bps,
            "edge_basis": EDGE_BASIS,
            "annualized": False,
            "breakeven_funding_rate": self.breakeven_funding_rate,
            "breakeven_funding": self.breakeven_funding,
            "calibrated": self.calibrated,
            "calibration_ref": self.calibration_ref,
            "tradable_claim_allowed": False,
            "flags": list(self.flags),
            "notes": list(self.notes),
            "disclaimer": DISCLAIMER,
        }
        return finalize(d)


def finalize(d: dict) -> dict:
    """Force the observe-only constants and refuse forbidden labels. Raises rather than emits."""
    if d.get("action") != ACTION or d.get("will_send_http") is not False:
        raise ObserveOnlyViolation(
            f"refused: action={d.get('action')!r} will_send_http={d.get('will_send_http')!r}"
        )
    if d.get("annualized") is not False or d.get("edge_basis") != EDGE_BASIS:
        raise NaiveAnnualizationRefused("refused: result must be hold-horizon, not annualised")
    if d.get("tradable_claim_allowed") is not False:
        raise CalibrationClaimRefused("refused: v0 never allows a tradable claim")
    text = json.dumps(d, ensure_ascii=False).lower()
    for bad in FORBIDDEN_LABELS:
        if bad in text:
            raise CostEngineError(f"refused: forbidden label {bad!r} in cost engine output")
    return d


def evaluate(
    *,
    gross_quote: float,
    notional_quote: float,
    components_quote: Mapping[str, float],
    hold_years: float | None = None,
    funding: FundingContext | None = None,
    config: CostEngineConfig = DEFAULT_CONFIG,
    gross_basis: str = EDGE_BASIS,
    notes: Sequence[str] = (),
) -> CostResult:
    """all-in cost / net edge / breakeven funding from quote-currency cost buckets.

    `components_quote` holds the eight core buckets (missing ones default to 0 and are flagged)
    plus any extras (`funding_expected`, `vol_path_haircut`, …) which are summed too. Each
    component is rounded to `round_dp` bps before summing, exactly like the Phase A/B records,
    so `all_in_cost_bps` reproduces their `costs_bps.total`.
    """
    if gross_basis != EDGE_BASIS:
        hint = " (annualised)" if gross_basis in REFUSED_GROSS_BASES else ""
        raise NaiveAnnualizationRefused(
            f"refused: gross_basis {gross_basis!r}{hint} — evaluate() takes a hold-horizon gross; "
            "current_funding × 365 is not a net edge"
        )
    _require_pos("notional_quote", notional_quote)
    if hold_years is not None and hold_years < 0:
        raise CostEngineError("hold_years must be >= 0")
    flags: list[str] = []
    comps: dict[str, float] = {}
    missing = [k for k in CORE_COMPONENTS if k not in components_quote]
    for k in CORE_COMPONENTS:
        comps[k] = float(components_quote.get(k, 0.0))
    for k, v in components_quote.items():
        if k in CORE_COMPONENTS:
            continue
        if k == "total":
            raise CostEngineError("components_quote must not carry a 'total' (engine sums)")
        comps[k] = float(v)
        if k not in KNOWN_EXTRA_COMPONENTS:
            flags.append(f"unknown_extra_component:{k}")
    if missing:
        flags.append("core_components_defaulted_to_zero:" + ",".join(missing))
    negatives = sorted(k for k, v in comps.items() if v < 0)
    if negatives:
        flags.append("negative_cost_component:" + ",".join(negatives))
    dp = config.round_dp
    comps_bps = {k: round(bps(v, notional_quote), dp) for k, v in comps.items()}
    all_in_bps = round(sum(comps_bps.values()), dp)
    all_in_quote = sum(comps.values())
    gross_bps = round(bps(gross_quote, notional_quote), dp)
    net_bps = round(gross_bps - all_in_bps, dp)
    be = breakeven_funding(
        gross_quote=gross_quote,
        all_in_cost_quote=all_in_quote,
        notional_quote=notional_quote,
        funding=funding,
    )
    if hold_years is None and funding is not None and funding.intervals > 0:
        hold_years = funding.intervals * funding.interval_sec / SECONDS_PER_YEAR
    return CostResult(
        notional_quote=notional_quote,
        hold_years=hold_years,
        components_quote=comps,
        components_bps=comps_bps,
        all_in_cost_quote=all_in_quote,
        all_in_cost_bps=all_in_bps,
        gross_edge_bps=gross_bps,
        net_edge_bps=net_bps,
        breakeven_funding=be,
        calibrated=config.calibrated,
        calibration_ref=config.calibration_ref,
        flags=flags,
        notes=list(notes),
    )


def evaluate_legs(
    *,
    legs: Sequence[LegSpec],
    fees: FeeSchedule,
    gross_quote: float,
    underlying_notional_quote: float,
    other_components_quote: Mapping[str, float] | None = None,
    hold_years: float | None = None,
    funding: FundingContext | None = None,
    config: CostEngineConfig = DEFAULT_CONFIG,
    notes: Sequence[str] = (),
) -> CostResult:
    """Convenience: fees / half_spread_slip / impact from bid/ask legs, the remaining buckets
    (borrow, transfer, capital_opp, funding_uncertainty, hedge_rebalance, extras) from
    `other_components_quote`. A leg bucket also present in `other_components_quote` is refused
    so nothing is double counted."""
    other = dict(other_components_quote or {})
    from_legs = leg_costs(legs, fees, underlying_notional_quote=underlying_notional_quote)
    clash = sorted(set(from_legs) & set(other))
    if clash:
        raise CostEngineError(f"{clash} come from the legs; do not pass them again")
    return evaluate(
        gross_quote=gross_quote,
        notional_quote=underlying_notional_quote,
        components_quote={**from_legs, **other},
        hold_years=hold_years,
        funding=funding,
        config=config,
        notes=notes,
    )


# --------------------------------------------------------------------------- summary helper


def summarize_blocks(blocks: Sequence[Mapping], *, enabled: bool = True) -> dict:
    """Aggregate `cost_engine` blocks emitted on scanner records (for summary JSON)."""
    be = [
        b["breakeven_funding"]["bps_per_interval"]
        for b in blocks
        if b.get("breakeven_funding", {}).get("rate_per_interval") is not None
    ]
    all_in = [b["all_in_cost_bps"] for b in blocks]
    return {
        "enabled": enabled,
        "engine": ENGINE,
        "version": VERSION,
        "calibrated": any(bool(b.get("calibrated")) for b in blocks),
        "tradable_claim_allowed": False,
        "records": len(blocks),
        "records_with_breakeven": len(be),
        "median_all_in_cost_bps": round(statistics.median(all_in), 4) if all_in else None,
        "median_breakeven_funding_bps_per_interval": (
            round(statistics.median(be), 4) if be else None
        ),
        "edge_basis": EDGE_BASIS,
        "note": "all_in reproduces costs_bps.total; breakeven only where funding is the thesis",
    }


# --------------------------------------------------------------------------- case files (fixtures)


def run_case(case: Mapping) -> dict:
    """Evaluate one fixture case. Cases may describe buckets (`components_quote`) or bid/ask
    `legs`; `expect` holds numbers to reproduce, `expect_error` the refusal class expected."""
    cid = case.get("id", "?")
    expect_error = case.get("expect_error")
    try:
        cfg = CostEngineConfig(
            calibrated=bool(case.get("calibrated", False)),
            calibration_ref=case.get("calibration_ref"),
        )
        funding = FundingContext.from_dict(case["funding"]) if case.get("funding") else None
        if case.get("funding_leg"):
            fl = case["funding_leg"]
            funding = FundingLeg(
                rate_now=float(fl["rate_now"]),
                horizon_intervals=int(fl["horizon_intervals"]),
                interval_sec=int(fl.get("interval_sec", DEFAULT_FUNDING_INTERVAL_SEC)),
                rate_next=float(fl["rate_next"]) if fl.get("rate_next") is not None else None,
                receiver=fl.get("receiver", "short_perp"),
            ).context(float(case["notional_quote"]))
        if "legs" in case:
            fees_d = case.get("fees") or {}
            fees = FeeSchedule(**{k: float(v) for k, v in fees_d.items()})
            res = evaluate_legs(
                legs=[LegSpec.from_dict(x) for x in case["legs"]],
                fees=fees,
                gross_quote=float(case.get("gross_quote", 0.0)),
                underlying_notional_quote=float(case["notional_quote"]),
                other_components_quote=case.get("other_components_quote"),
                hold_years=case.get("hold_years"),
                funding=funding,
                config=cfg,
                notes=case.get("notes", ()),
            )
        else:
            res = evaluate(
                gross_quote=float(case.get("gross_quote", 0.0)),
                notional_quote=float(case["notional_quote"]),
                components_quote=case.get("components_quote") or {},
                hold_years=case.get("hold_years"),
                funding=funding,
                config=cfg,
                gross_basis=case.get("gross_basis", EDGE_BASIS),
                notes=case.get("notes", ()),
            )
    except CostEngineError as exc:
        name = type(exc).__name__
        ok = expect_error is not None and name == expect_error
        return {
            "id": cid,
            "ok": ok,
            "refused": name,
            "message": str(exc),
            "expect_error": expect_error,
        }
    if expect_error is not None:
        return {"id": cid, "ok": False, "error": f"expected refusal {expect_error}, got a result"}
    out = res.to_dict()
    diffs = {}
    for k, want in (case.get("expect") or {}).items():
        got = _lookup(out, k)
        if isinstance(want, bool) or want is None:
            same = got is want
        elif isinstance(want, (int, float)):
            same = isinstance(got, (int, float)) and abs(float(got) - float(want)) <= 1e-6
        else:
            same = got == want
        if not same:
            diffs[k] = {"expect": want, "got": got}
    return {"id": cid, "ok": not diffs, "diffs": diffs, "result": out}


def _lookup(d: Mapping, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def run_cases(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    results = [run_case(c) for c in doc.get("cases", [])]
    return {
        "engine": ENGINE,
        "version": VERSION,
        "mode": MODE,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
        "data_source": {"kind": "fixture", "path": str(path), "http_fetch": False},
        "note": doc.get("note", ""),
        "cases": results,
        "cases_total": len(results),
        "cases_ok": sum(1 for r in results if r["ok"]),
        "all_ok": all(r["ok"] for r in results) and bool(results),
        "calibrated": False,
        "tradable_claim_allowed": False,
        "mainline_unchanged": "spot_grid_local_paper",
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- cli

DEFAULT_CASES = ROOT / "fixtures" / "cost_engine" / "2026-09-17-cost-cases.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cost Engine v0 — all-in cost / net edge / breakeven funding from bid/ask "
        "costs. Paper, read-only, observe_only, will_send_http=false, calibrated=false. "
        "Replays a JSON case file offline and reports mismatches / refusals.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="JSON case file (offline)")
    p.add_argument("--out", default="", help="write the report JSON here (default stdout)")
    p.add_argument("--quiet", action="store_true", help="only the one-line verdict on stderr")
    p.add_argument(
        "--full", action="store_true", help="include each case's full result in the report"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_cases(args.cases)
    if not args.full:
        for r in report["cases"]:
            r.pop("result", None)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    elif not args.quiet:
        print(text)
    print(
        f"cost_engine {VERSION}: {report['cases_ok']}/{report['cases_total']} cases ok; "
        f"observe_only; calibrated=false",
        file=sys.stderr,
    )
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CostEngineError, ObserveOnlyViolation, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
