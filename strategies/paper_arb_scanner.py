#!/usr/bin/env python3
"""Paper multi-leg arb scanner — Phase A, read-only, observe-only.

Implements proposal `paper-multi-leg-arb-scanner-v1` (risk clearance: conditional pass,
read-only / paper side branch). Three Phase A families, same venue:

  A1  spot–perp funding carry            taxonomy = relative_value
  A2  put-call parity conversion/reversal taxonomy = identity_approx
                                          (perp as forward proxy → identity_approx_with_perp_proxy)
  A3  box spread implied financing        taxonomy = identity_approx

Hard gates (enforced in code, tested, not just documented):
  * every record: action == "observe_only", will_send_http == False;
  * executable prices are bid/ask only — a Leg with price_type mark/mid/last is refused;
  * no order / amend / withdraw / transfer code path; the only network I/O is the
    public GET order-book / funding / instruments path of tools/okx_readonly_client.py;
  * leverage concept 1x; short option legs must be covered (spread or underlying);
    a naked short option is refused before a record is built;
  * safety_buffer_bps is the *sum of components* (fee_roundtrip + slip + funding_uncert +
    model_haircut), configurable, reported as `calibrated: false` unless the caller says
    otherwise. Nothing here is a return promise; relative-value records carry an explicit
    "not riskless" flag.

The spot-grid / local_paper mainline is untouched: this module does not import or alter
tools/local_paper_grid.py or strategies/grid_ab_compare.py.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import okx_readonly_client as okx  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")

# --------------------------------------------------------------------------- constants

ACTION = "observe_only"
WILL_SEND_HTTP = False
MODE = "paper_read_only"
DEFAULT_VENUE = "okx"

FAMILY_A1 = "A1_funding_carry"
FAMILY_A2_CONV = "A2_pcp_conversion"
FAMILY_A2_REV = "A2_pcp_reversal"
FAMILY_A3 = "A3_box"
FAMILIES = (FAMILY_A1, FAMILY_A2_CONV, FAMILY_A2_REV, FAMILY_A3)

TAXONOMY_RV = "relative_value"
TAXONOMY_IDENTITY = "identity_approx"
TAXONOMY_IDENTITY_PERP_PROXY = "identity_approx_with_perp_proxy"
TAXONOMIES = (TAXONOMY_RV, TAXONOMY_IDENTITY, TAXONOMY_IDENTITY_PERP_PROXY)

HYPOTHESIS = {
    FAMILY_A1: "H-A1",
    FAMILY_A2_CONV: "H-A2",
    FAMILY_A2_REV: "H-A2",
    FAMILY_A3: "H-A3",
}
FALSIFY_IF = {
    "H-A1": "pass_rate_near_zero_or_pre_funding_expectation_nonpositive",
    "H-A2": "executable_deviation_within_fee_band_or_explained_by_borrow_funding",
    "H-A3": "fee_adjusted_box_rate_spread_not_above_zero",
}

EXECUTABLE_PRICE_TYPES = frozenset({"bid", "ask"})
SIDE_TO_PRICE_TYPE = {"buy": "ask", "sell": "bid"}

COST_KEYS = (
    "fees",
    "half_spread_slip",
    "hedge_rebalance",
    "borrow",
    "transfer",
    "capital_opp",
    "funding_expected",
    "funding_uncertainty",
    "impact",
)

REQUIRED_FIELDS = (
    "timestamp",
    "venue",
    "family",
    "taxonomy",
    "instruments",
    "legs",
    "executable_prices",
    "gross_edge_bps",
    "costs_bps",
    "net_edge_bps",
    "safety_buffer_bps",
    "passes_threshold",
    "margin_capital_required",
    "persistence",
    "liquidity",
    "risk_flags",
    "hypothesis_id",
    "action",
    "will_send_http",
)

SOURCE_INSPIRATION = "chatgpt_share_unverified"
CROSS_CHECKED_INBOX = [
    "01-inbox/2026-09-16-call-put-combo-arb-pack.md",
    "01-inbox/2026-09-16-arb-reading-pack.md",
]

DISCLAIMER = (
    "纸面只读扫描（observe_only，will_send_http=false）。净边 = bid/ask 可执行毛边 − 全成本，"
    "门限 safety_buffer 为分量之和且默认未标定；relative_value 记录属 carry / 相对价值，"
    "承担 funding 路径与基差风险；identity_approx 记录仍有执行 / 结算 / 保证金风险。"
    "不是收益承诺，不下单。"
)

# Flags that invalidate a record for threshold purposes (still logged, never passes).
INVALIDATING_FLAGS = frozenset(
    {
        "borrow_unavailable",
        "one_sided_book",
        "insufficient_depth",
        "expired_or_no_time_to_expiry",
        "short_box_margin_not_approved",
        "missing_book",
        "funding_sign_flip_predicted",
    }
)

MS_PER_YEAR = 365.0 * 86_400_000.0


# --------------------------------------------------------------------------- errors


class ExecutablePriceViolation(ValueError):
    """A leg tried to use a non bid/ask price as executable."""


class NakedShortOptionRefused(ValueError):
    """A record would contain an uncovered short option leg."""


class ObserveOnlyViolation(RuntimeError):
    """A record tried to leave observe_only / will_send_http=false."""


# --------------------------------------------------------------------------- market data model


@dataclass(frozen=True)
class Level:
    px: float
    sz: float  # base units (ETH), never contracts


@dataclass(frozen=True)
class Walk:
    """Result of eating a book side for a target quantity."""

    vwap: float | None
    filled_qty: float
    levels_used: int
    complete: bool


@dataclass(frozen=True)
class Book:
    """One instrument's order book. Best price first on both sides. Sizes in base units.

    kind: spot | perp | option. `mark_px` is carried for *reference only*; it is never
    used as an executable price anywhere in this module.
    """

    inst_id: str
    kind: str
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    ts_ms: int = 0
    mark_px: float | None = None
    opt_type: str | None = None  # "C" | "P"
    strike: float | None = None
    expiry_ms: int | None = None
    premium_ccy: str = "quote"  # "quote" (USDT) | "base" (coin-priced, OKX options)
    settle_ccy: str | None = None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].px if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].px if self.asks else None

    @property
    def two_sided(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    def side_levels(self, side: str) -> tuple[Level, ...]:
        # buying eats asks; selling eats bids
        return self.asks if side == "buy" else self.bids

    def walk(self, side: str, qty: float) -> Walk:
        levels = self.side_levels(side)
        remaining = qty
        notional = 0.0
        used = 0
        for lvl in levels:
            if remaining <= 1e-12:
                break
            take = min(lvl.sz, remaining)
            notional += take * lvl.px
            remaining -= take
            used += 1
        filled = qty - remaining
        if filled <= 0:
            return Walk(None, 0.0, 0, False)
        return Walk(notional / filled, filled, used, remaining <= 1e-12)

    def depth_qty(self, side: str, top_n: int) -> float:
        return sum(lvl.sz for lvl in self.side_levels(side)[:top_n])

    @classmethod
    def from_dict(cls, d: dict, kind: str | None = None) -> Book:
        def lv(rows) -> tuple[Level, ...]:
            return tuple(Level(float(r[0]), float(r[1])) for r in rows if float(r[1]) > 0)

        return cls(
            inst_id=d["inst_id"],
            kind=kind or d.get("kind") or "spot",
            bids=lv(d.get("bids") or []),
            asks=lv(d.get("asks") or []),
            ts_ms=int(d.get("ts_ms") or 0),
            mark_px=float(d["mark_px"]) if d.get("mark_px") is not None else None,
            opt_type=d.get("opt_type"),
            strike=float(d["strike"]) if d.get("strike") is not None else None,
            expiry_ms=int(d["expiry_ms"]) if d.get("expiry_ms") is not None else None,
            premium_ccy=d.get("premium_ccy", "quote"),
            settle_ccy=d.get("settle_ccy"),
        )


@dataclass(frozen=True)
class Funding:
    rate: float  # decimal per interval (e.g. 0.0001 = 1 bp / 8h)
    next_rate: float | None = None
    interval_sec: int = 8 * 3600
    next_funding_time_ms: int | None = None


@dataclass(frozen=True)
class Snapshot:
    ts_ms: int
    venue: str
    spot: Book | None
    perp: Book | None
    funding: Funding | None
    options: tuple[Book, ...] = ()
    spot_borrow_apr: float | None = None  # None → borrow unavailable (shorts skipped)
    source: dict = field(default_factory=dict)

    def option_chain(self) -> dict[int, dict[float, dict[str, Book]]]:
        """expiry_ms → strike → {"C": Book, "P": Book}."""
        chain: dict[int, dict[float, dict[str, Book]]] = {}
        for b in self.options:
            if b.kind != "option" or b.expiry_ms is None or b.strike is None or not b.opt_type:
                continue
            chain.setdefault(b.expiry_ms, {}).setdefault(b.strike, {})[b.opt_type] = b
        return chain


def load_fixture(path: str | os.PathLike) -> list[Snapshot]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    venue = doc.get("venue", DEFAULT_VENUE)
    borrow = doc.get("spot_borrow_apr")
    out: list[Snapshot] = []
    for s in doc["snapshots"]:
        fr = s.get("funding")
        out.append(
            Snapshot(
                ts_ms=int(s["ts_ms"]),
                venue=s.get("venue", venue),
                spot=Book.from_dict(s["spot"], "spot") if s.get("spot") else None,
                perp=Book.from_dict(s["perp"], "perp") if s.get("perp") else None,
                funding=Funding(
                    rate=float(fr["rate"]),
                    next_rate=float(fr["next_rate"]) if fr.get("next_rate") is not None else None,
                    interval_sec=int(fr.get("interval_sec", 8 * 3600)),
                    next_funding_time_ms=fr.get("next_time_ms"),
                )
                if fr
                else None,
                options=tuple(Book.from_dict(o, "option") for o in s.get("options") or []),
                spot_borrow_apr=(
                    float(s["spot_borrow_apr"])
                    if s.get("spot_borrow_apr") is not None
                    else (float(borrow) if borrow is not None else None)
                ),
                source={
                    "kind": "fixture",
                    "path": str(path),
                    "http_fetch": False,
                    "note": doc.get("note", ""),
                },
            )
        )
    return out


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class FeeSchedule:
    """Taker fees in bps of notional. Defaults are OKX retail-tier placeholders; override."""

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


@dataclass(frozen=True)
class SafetyBuffer:
    """safety_buffer_bps = sum of auditable components. fee_roundtrip None → use the
    record's own computed fees (so the buffer never undercounts fees)."""

    fee_roundtrip_bps: float | None = None
    slip_buffer_bps: float = 5.0
    funding_uncert_bps: float = 5.0
    model_haircut_bps: float = 10.0
    calibrated: bool = False

    def resolve(self, fees_bps: float) -> dict:
        fee = self.fee_roundtrip_bps if self.fee_roundtrip_bps is not None else fees_bps
        comps = {
            "fee_roundtrip_bps": round(fee, 4),
            "slip_buffer_bps": self.slip_buffer_bps,
            "funding_uncert_bps": self.funding_uncert_bps,
            "model_haircut_bps": self.model_haircut_bps,
        }
        return {
            "components": comps,
            "total_bps": round(sum(comps.values()), 4),
            "calibrated": self.calibrated,
            "rule": "net_edge_bps > safety_buffer_bps AND persistence.ok AND liquidity.ok "
            "AND no invalidating risk_flags",
        }


@dataclass(frozen=True)
class ScanConfig:
    qty: float = 1.0  # base units per leg (delta-neutral sizing)
    horizon_intervals: int = 3  # A1 holding horizon in funding intervals (3 × 8h = 1 day)
    ref_rate_apr: float = 0.0  # discount / opportunity rate; 0 = no discounting claimed
    funding_sigma_bps_per_interval: float = 2.0
    hedge_rebalance_bps: float = 2.0
    transfer_bps: float = 0.0
    depth_mult: float = 2.0
    top_n: int = 5
    persistence_min_samples: int = 3
    persistence_min_sec: float = 60.0
    pcp_anchor: str = "spot"  # spot | perp
    credit_favorable_basis: bool = False
    paper_fills: bool = False
    paper_extra_slip_bps: float = 5.0
    max_box_pairs: int = 6
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    buffer: SafetyBuffer = field(default_factory=SafetyBuffer)

    def validate(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.horizon_intervals < 1:
            raise ValueError("horizon_intervals must be >= 1")
        if self.pcp_anchor not in ("spot", "perp"):
            raise ValueError("pcp_anchor must be spot|perp")
        if self.persistence_min_samples < 1:
            raise ValueError("persistence_min_samples must be >= 1")

    def to_dict(self) -> dict:
        return {
            "qty": self.qty,
            "horizon_intervals": self.horizon_intervals,
            "ref_rate_apr": self.ref_rate_apr,
            "funding_sigma_bps_per_interval": self.funding_sigma_bps_per_interval,
            "hedge_rebalance_bps": self.hedge_rebalance_bps,
            "transfer_bps": self.transfer_bps,
            "depth_mult": self.depth_mult,
            "top_n": self.top_n,
            "persistence_min_samples": self.persistence_min_samples,
            "persistence_min_sec": self.persistence_min_sec,
            "pcp_anchor": self.pcp_anchor,
            "credit_favorable_basis": self.credit_favorable_basis,
            "paper_fills": self.paper_fills,
            "paper_extra_slip_bps": self.paper_extra_slip_bps,
            "max_box_pairs": self.max_box_pairs,
            "leverage_concept": 1,
            "fees": self.fees.to_dict(),
            "safety_buffer": {
                "fee_roundtrip_bps": self.buffer.fee_roundtrip_bps,
                "slip_buffer_bps": self.buffer.slip_buffer_bps,
                "funding_uncert_bps": self.buffer.funding_uncert_bps,
                "model_haircut_bps": self.buffer.model_haircut_bps,
                "calibrated": self.buffer.calibrated,
            },
        }


# --------------------------------------------------------------------------- legs


@dataclass(frozen=True)
class Leg:
    instrument: str
    side: str  # buy | sell
    qty: float
    executable_price: float
    price_type: str  # ask (buy) | bid (sell) — nothing else is accepted
    role: str  # spot | perp | call | put
    mark_price_ref: float | None = None
    premium_ccy: str = "quote"
    strike: float | None = None
    expiry_ms: int | None = None

    def __post_init__(self) -> None:
        if self.side not in SIDE_TO_PRICE_TYPE:
            raise ValueError(f"leg side must be buy|sell, got {self.side!r}")
        if self.price_type not in EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(
                f"refused: executable price_type {self.price_type!r} for {self.instrument} "
                "(bid/ask only; mark/mid/last are not tradable)"
            )
        if SIDE_TO_PRICE_TYPE[self.side] != self.price_type:
            raise ExecutablePriceViolation(
                f"refused: {self.side} must use {SIDE_TO_PRICE_TYPE[self.side]}, "
                f"got {self.price_type!r} for {self.instrument}"
            )
        if self.executable_price <= 0 or self.qty <= 0:
            raise ValueError("leg price and qty must be > 0")

    def to_dict(self) -> dict:
        d = {
            "instrument": self.instrument,
            "side": self.side,
            "qty": self.qty,
            "executable_price": self.executable_price,
            "price_type": self.price_type,
            "role": self.role,
            "mark_price_ref": self.mark_price_ref,
        }
        if self.role in ("call", "put"):
            d["premium_ccy"] = self.premium_ccy
            d["strike"] = self.strike
            d["expiry_ms"] = self.expiry_ms
        return d


def make_leg(book: Book, side: str, qty: float, role: str) -> Leg | None:
    px = book.best_ask if side == "buy" else book.best_bid
    if px is None:
        return None
    return Leg(
        instrument=book.inst_id,
        side=side,
        qty=qty,
        executable_price=px,
        price_type=SIDE_TO_PRICE_TYPE[side],
        role=role,
        mark_price_ref=book.mark_px,
        premium_ccy=book.premium_ccy,
        strike=book.strike,
        expiry_ms=book.expiry_ms,
    )


def assert_no_naked_short_options(legs: list[Leg]) -> None:
    """Every short option must be covered: by a long option of the same type (spread) or by
    an underlying position in the right direction (short call ↔ long underlying,
    short put ↔ short underlying). Raises otherwise. Never builds a record on failure."""
    underlying = [leg for leg in legs if leg.role in ("spot", "perp")]
    for leg in legs:
        if leg.role not in ("call", "put") or leg.side != "sell":
            continue
        same_type_long = any(
            o.role == leg.role and o.side == "buy" and o.expiry_ms == leg.expiry_ms for o in legs
        )
        need_side = "buy" if leg.role == "call" else "sell"
        covered_by_underlying = any(u.side == need_side for u in underlying)
        if not (same_type_long or covered_by_underlying):
            raise NakedShortOptionRefused(
                f"refused: short {leg.role} {leg.instrument} has no covering leg"
            )


# --------------------------------------------------------------------------- persistence


class PersistenceTracker:
    """Rolling 'edge stayed above buffer with the same sign' tracker, keyed per opportunity."""

    def __init__(self, min_samples: int, min_sec: float):
        self.min_samples = min_samples
        self.min_sec = min_sec
        self._state: dict[str, dict] = {}

    def update(self, key: str, ts_ms: int, net_bps: float, buffer_bps: float) -> dict:
        qualifies = net_bps > buffer_bps
        st = self._state.get(key)
        if qualifies:
            if st is None or not st["active"]:
                st = {"active": True, "first_ts": ts_ms, "last_ts": ts_ms, "samples": 1}
            else:
                st["samples"] += 1
                st["last_ts"] = ts_ms
        else:
            st = {"active": False, "first_ts": ts_ms, "last_ts": ts_ms, "samples": 0}
        self._state[key] = st
        duration = max(0.0, (st["last_ts"] - st["first_ts"]) / 1000.0)
        ok = qualifies and st["samples"] >= self.min_samples and duration >= self.min_sec
        return {
            "samples_ok": st["samples"],
            "duration_sec": round(duration, 3),
            "ok": ok,
            "min_samples": self.min_samples,
            "min_sec": self.min_sec,
        }


# --------------------------------------------------------------------------- helpers


def bps(x: float, base: float) -> float:
    return x / base * 1e4 if base else 0.0


def years_between(start_ms: int, end_ms: int) -> float:
    return max(0.0, (end_ms - start_ms) / MS_PER_YEAR)


def premium_to_quote(px: float, premium_ccy: str, side: str, spot: Book | None) -> float | None:
    """Convert an option premium to quote currency conservatively.

    Coin-priced premium you *pay* costs coin bought at the spot ask; premium you *receive*
    is coin sold at the spot bid.
    """
    if premium_ccy == "quote":
        return px
    if spot is None:
        return None
    conv = spot.best_ask if side == "buy" else spot.best_bid
    return px * conv if conv else None


def iso_cst(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, SHANGHAI).isoformat(timespec="seconds")


def liquidity_check(
    legs: list[Leg], books: dict[str, Book], qty: float, cfg: ScanConfig
) -> tuple[dict, list[str], float]:
    """Depth filter + impact. Returns (liquidity block, flags, impact_bps-numerator in quote).

    Impact is measured as VWAP-vs-top-of-book slippage for `qty` on every leg, in quote
    currency (coin premiums converted at spot bid/ask). Caller divides by notional.
    """
    flags: list[str] = []
    notes: list[str] = []
    impact_quote = 0.0
    ok = True
    spot = next((b for b in books.values() if b.kind == "spot"), None)
    per_leg = []
    for leg in legs:
        book = books[leg.instrument]
        if not book.two_sided:
            flags.append("one_sided_book")
            ok = False
        depth = book.depth_qty(leg.side, cfg.top_n)
        need = qty * cfg.depth_mult
        w = book.walk(leg.side, qty)
        if not w.complete:
            flags.append("insufficient_depth")
            ok = False
        if depth < need:
            ok = False
            notes.append(f"{leg.instrument} {leg.side}: top{cfg.top_n} depth {depth:g} < {need:g}")
        slip_px = abs((w.vwap or leg.executable_price) - leg.executable_price)
        slip_quote = premium_to_quote(slip_px, leg.premium_ccy, leg.side, spot) or 0.0
        impact_quote += slip_quote * qty
        per_leg.append(
            {
                "instrument": leg.instrument,
                "side": leg.side,
                "top_n_depth_qty": round(depth, 8),
                "required_qty": round(need, 8),
                "vwap": w.vwap,
                "filled_qty": w.filled_qty,
                "complete": w.complete,
            }
        )
    if not ok and "illiquid" not in flags:
        flags.append("illiquid")
    return (
        {
            "ok": ok,
            "depth_mult": cfg.depth_mult,
            "top_n": cfg.top_n,
            "legs": per_leg,
            "notes": "; ".join(notes) if notes else ("ok" if ok else "see risk_flags"),
        },
        flags,
        impact_quote,
    )


def option_fee_quote(
    premium_quote: float, notional_quote: float, fees: FeeSchedule, settlement: bool
) -> float:
    trade = min(
        notional_quote * fees.option_taker_bps / 1e4,
        premium_quote * fees.option_fee_cap_pct_premium / 100.0,
    )
    settle = notional_quote * fees.option_settlement_bps / 1e4 if settlement else 0.0
    return trade + settle


# --------------------------------------------------------------------------- record


def build_record(
    *,
    snap: Snapshot,
    family: str,
    taxonomy: str,
    legs: list[Leg],
    books: dict[str, Book],
    gross_quote: float,
    notional_quote: float,
    costs_quote: dict[str, float],
    margin_capital: float,
    risk_flags: list[str],
    cfg: ScanConfig,
    tracker: PersistenceTracker,
    persistence_key: str,
    extra: dict,
) -> dict:
    if family not in FAMILIES:
        raise ValueError(f"unknown family {family}")
    if taxonomy not in TAXONOMIES:
        raise ValueError(f"unknown taxonomy {taxonomy}")
    assert_no_naked_short_options(legs)

    liquidity, liq_flags, impact_quote = liquidity_check(legs, books, cfg.qty, cfg)
    costs_quote = dict(costs_quote)
    costs_quote["impact"] = impact_quote
    costs_bps = {k: round(bps(costs_quote.get(k, 0.0), notional_quote), 4) for k in COST_KEYS}
    costs_bps["total"] = round(sum(costs_bps[k] for k in COST_KEYS), 4)
    gross_bps = round(bps(gross_quote, notional_quote), 4)
    net_bps = round(gross_bps - costs_bps["total"], 4)

    buffer = cfg.buffer.resolve(costs_bps["fees"])
    flags = sorted(set(risk_flags) | set(liq_flags))
    invalidated = sorted(set(flags) & INVALIDATING_FLAGS)
    persistence = tracker.update(persistence_key, snap.ts_ms, net_bps, buffer["total_bps"])
    edge_exceeds_buffer = net_bps > buffer["total_bps"]
    passes = bool(edge_exceeds_buffer and persistence["ok"] and liquidity["ok"] and not invalidated)

    executable_prices = {
        inst: {"bid": b.best_bid, "ask": b.best_ask, "mark_ref_only": b.mark_px}
        for inst, b in books.items()
        if inst in {leg.instrument for leg in legs}
    }

    rec: dict = {
        "timestamp": iso_cst(snap.ts_ms),
        "ts_ms": snap.ts_ms,
        "venue": snap.venue,
        "mode": MODE,
        "family": family,
        "taxonomy": taxonomy,
        "taxonomy_base": TAXONOMY_IDENTITY
        if taxonomy == TAXONOMY_IDENTITY_PERP_PROXY
        else taxonomy,
        "instruments": sorted({leg.instrument for leg in legs}),
        "legs": [leg.to_dict() for leg in legs],
        "executable_prices": executable_prices,
        "notional_quote": round(notional_quote, 6),
        "notional_basis": extra.pop("notional_basis", "underlying_ask_x_qty"),
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
        "risk_flags": flags,
        "hypothesis_id": HYPOTHESIS[family],
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "source_inspiration": SOURCE_INSPIRATION,
        "cross_checked_inbox": CROSS_CHECKED_INBOX,
    }
    rec.update(extra)
    if cfg.paper_fills:
        rec["paper_fill"] = paper_fill(legs, books, notional_quote / cfg.qty, net_bps, cfg)
    return finalize_record(rec)


def finalize_record(rec: dict) -> dict:
    """Force the observe-only constants and validate the schema. Raises on any attempt to
    emit a record that is not observe_only / will_send_http=false / bid-ask priced."""
    if rec.get("action") != ACTION or rec.get("will_send_http") is not False:
        raise ObserveOnlyViolation(
            f"refused: action={rec.get('action')!r} will_send_http={rec.get('will_send_http')!r}"
        )
    missing = [k for k in REQUIRED_FIELDS if k not in rec]
    if missing:
        raise ValueError(f"record missing required fields: {missing}")
    if rec["taxonomy"] not in TAXONOMIES:
        raise ValueError(f"bad taxonomy {rec['taxonomy']!r}")
    if rec["family"] == FAMILY_A1 and rec["taxonomy"] != TAXONOMY_RV:
        raise ValueError("A1 must be relative_value")
    if rec["family"] != FAMILY_A1 and rec["taxonomy"] == TAXONOMY_RV:
        raise ValueError("A2/A3 must be identity_approx(_with_perp_proxy)")
    for leg in rec["legs"]:
        if leg["price_type"] not in EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(f"refused: leg price_type {leg['price_type']!r}")
    if "total" not in rec["costs_bps"]:
        raise ValueError("costs_bps.total required")
    return rec


def paper_fill(
    legs: list[Leg],
    books: dict[str, Book],
    notional_per_unit: float,
    net_bps: float,
    cfg: ScanConfig,
) -> dict:
    """Conservative hypothetical fill: eat the book for qty (VWAP, already in `impact`) and
    charge an extra slip haircut of `paper_extra_slip_bps` of *underlying notional* per leg.
    No order exists; this is bookkeeping only."""
    per_leg = []
    haircut_quote = notional_per_unit * cfg.paper_extra_slip_bps / 1e4
    for leg in legs:
        book = books[leg.instrument]
        w = book.walk(leg.side, leg.qty)
        vwap = w.vwap
        if vwap is not None:
            if leg.role in ("call", "put") and leg.premium_ccy == "base":
                haircut = cfg.paper_extra_slip_bps / 1e4  # coin per unit of underlying
            else:
                haircut = haircut_quote
            vwap = vwap + haircut if leg.side == "buy" else vwap - haircut
        per_leg.append(
            {
                "instrument": leg.instrument,
                "side": leg.side,
                "qty": leg.qty,
                "fill_px_hypothetical": round(vwap, 10) if vwap is not None else None,
                "filled_qty": w.filled_qty,
                "complete": w.complete,
            }
        )
    return {
        "enabled": True,
        "mode": "conservative_eat_bid_ask_plus_extra_slip",
        "extra_slip_bps_per_leg": cfg.paper_extra_slip_bps,
        "legs": per_leg,
        "net_edge_after_fill_bps": round(net_bps - cfg.paper_extra_slip_bps * len(legs), 4),
        "order_sent": False,
        "will_send_http": WILL_SEND_HTTP,
    }


# --------------------------------------------------------------------------- scanner


class ArbScanner:
    def __init__(self, cfg: ScanConfig, tracker: PersistenceTracker | None = None):
        cfg.validate()
        self.cfg = cfg
        self.tracker = tracker or PersistenceTracker(
            cfg.persistence_min_samples, cfg.persistence_min_sec
        )

    def scan(self, snap: Snapshot) -> list[dict]:
        out: list[dict] = []
        out.extend(self.scan_a1(snap))
        out.extend(self.scan_a2(snap))
        out.extend(self.scan_a3(snap))
        return out

    # ---- A1 spot–perp funding carry (relative_value)

    def scan_a1(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        spot, perp, fr = snap.spot, snap.perp, snap.funding
        if spot is None or perp is None or fr is None:
            return []
        if not (spot.two_sided and perp.two_sided):
            return []
        qty = cfg.qty
        H = cfg.horizon_intervals
        positive = fr.rate >= 0
        flags = [
            "relative_value_not_riskless",
            "funding_path_risk",
            "basis_risk",
            "leverage_1x_margin_still_applies",
        ]
        if positive:
            spot_leg = make_leg(spot, "buy", qty, "spot")
            perp_leg = make_leg(perp, "sell", qty, "perp")
            direction = "long_spot_short_perp"
            # entry basis: perp bid above spot ask is favorable for the short perp
            basis_bps = bps(perp.best_bid - spot.best_ask, spot.best_ask)
        else:
            spot_leg = make_leg(spot, "sell", qty, "spot")
            perp_leg = make_leg(perp, "buy", qty, "perp")
            direction = "short_spot_long_perp"
            basis_bps = bps(spot.best_bid - perp.best_ask, spot.best_ask)
            flags.append("requires_spot_borrow")
        if spot_leg is None or perp_leg is None:
            return []
        legs = [spot_leg, perp_leg]
        notional = spot.best_ask * qty

        f_now = abs(fr.rate)
        f_used = f_now
        if fr.next_rate is not None:
            if (fr.next_rate >= 0) != positive and fr.next_rate != 0:
                flags.append("funding_sign_flip_predicted")
                f_used = 0.0
            else:
                f_used = min(f_now, abs(fr.next_rate))
        funding_quote = f_used * H * notional
        if basis_bps < 0:
            flags.append("adverse_entry_basis")
        basis_credit = basis_bps if (basis_bps < 0 or cfg.credit_favorable_basis) else 0.0
        gross_quote = funding_quote + basis_credit / 1e4 * notional

        hold_years = H * fr.interval_sec / (365.0 * 86400.0)
        fees = cfg.fees
        fees_quote = notional * (2 * fees.spot_taker_bps + 2 * fees.perp_taker_bps) / 1e4
        # exit crosses both spreads (entry spread already embedded in bid/ask legs)
        half_spread_quote = qty * (
            (spot.best_ask - spot.best_bid) / 2 + (perp.best_ask - perp.best_bid) / 2
        )
        margin_capital = 2 * notional  # spot notional + perp margin at 1x
        borrow_quote = 0.0
        if not positive:
            if snap.spot_borrow_apr is None:
                flags.append("borrow_unavailable")
            else:
                borrow_quote = notional * snap.spot_borrow_apr * hold_years
        f_pred_gap = abs(f_now - abs(fr.next_rate)) if fr.next_rate is not None else 0.0
        funding_uncert_quote = notional * (
            cfg.funding_sigma_bps_per_interval / 1e4 * H + f_pred_gap * H
        )
        costs = {
            "fees": fees_quote,
            "half_spread_slip": half_spread_quote,
            "hedge_rebalance": notional * cfg.hedge_rebalance_bps / 1e4,
            "borrow": borrow_quote,
            "transfer": notional * cfg.transfer_bps / 1e4,
            "capital_opp": margin_capital * cfg.ref_rate_apr * hold_years,
            "funding_expected": 0.0,
            "funding_uncertainty": funding_uncert_quote,
        }
        extra = {
            "direction": direction,
            "funding": {
                "rate_now": fr.rate,
                "rate_next": fr.next_rate,
                "rate_used_per_interval": f_used if positive else -f_used,
                "interval_sec": fr.interval_sec,
                "horizon_intervals": H,
                "expected_funding_bps": round(bps(funding_quote, notional), 4),
            },
            "basis_entry_bps": round(basis_bps, 4),
            "basis_credited_bps": round(basis_credit, 4),
            "invalidation": [
                "funding_sign_flip",
                "basis_blowout",
                "margin_stress",
                "depth_collapse",
            ],
        }
        rec = build_record(
            snap=snap,
            family=FAMILY_A1,
            taxonomy=TAXONOMY_RV,
            legs=legs,
            books={spot.inst_id: spot, perp.inst_id: perp},
            gross_quote=gross_quote,
            notional_quote=notional,
            costs_quote=costs,
            margin_capital=margin_capital,
            risk_flags=flags,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_A1}|{spot.inst_id}|{perp.inst_id}|{direction}",
            extra=extra,
        )
        return [rec]

    # ---- A2 put-call parity conversion / reversal (identity_approx)

    def _anchor(self, snap: Snapshot) -> tuple[Book | None, str, str]:
        if self.cfg.pcp_anchor == "perp" and snap.perp is not None:
            return snap.perp, "perp", TAXONOMY_IDENTITY_PERP_PROXY
        return snap.spot, "spot", TAXONOMY_IDENTITY

    def _option_common_flags(self, books: list[Book], snap: Snapshot) -> list[str]:
        flags = ["identity_approx_not_riskless", "european_cash_settled_assumed"]
        if any(b.premium_ccy == "base" for b in books):
            flags.append("option_premium_in_base_ccy_converted_at_spot_bid_ask")
        if snap.spot is not None and any(
            b.settle_ccy and b.settle_ccy.upper() != snap.spot.inst_id.split("-")[-1].upper()
            for b in books
        ):
            flags.append("settlement_anchor_mismatch_vs_spot_quote_ccy")
        return flags

    def scan_a2(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        anchor, anchor_kind, taxonomy = self._anchor(snap)
        if anchor is None or not anchor.two_sided or snap.spot is None:
            return []
        out: list[dict] = []
        qty = cfg.qty
        for expiry_ms, strikes in sorted(snap.option_chain().items()):
            T = years_between(snap.ts_ms, expiry_ms)
            df = math.exp(-cfg.ref_rate_apr * T)
            for K, pair in sorted(strikes.items()):
                call, put = pair.get("C"), pair.get("P")
                if call is None or put is None:
                    continue
                for family in (FAMILY_A2_CONV, FAMILY_A2_REV):
                    rec = self._pcp_record(
                        snap,
                        anchor,
                        anchor_kind,
                        taxonomy,
                        family,
                        call,
                        put,
                        K,
                        expiry_ms,
                        T,
                        df,
                        qty,
                    )
                    if rec is not None:
                        out.append(rec)
        return out

    def _pcp_record(
        self,
        snap: Snapshot,
        anchor: Book,
        anchor_kind: str,
        taxonomy: str,
        family: str,
        call: Book,
        put: Book,
        K: float,
        expiry_ms: int,
        T: float,
        df: float,
        qty: float,
    ) -> dict | None:
        cfg = self.cfg
        spot = snap.spot
        conversion = family == FAMILY_A2_CONV
        flags = self._option_common_flags([call, put], snap)
        if T <= 0:
            flags.append("expired_or_no_time_to_expiry")
        if conversion:
            # long underlying @ ask, long put @ ask, short call @ bid → K at expiry
            u_leg = make_leg(anchor, "buy", qty, anchor_kind)
            p_leg = make_leg(put, "buy", qty, "put")
            c_leg = make_leg(call, "sell", qty, "call")
        else:
            # short underlying @ bid, short put @ bid, long call @ ask → −K at expiry
            u_leg = make_leg(anchor, "sell", qty, anchor_kind)
            p_leg = make_leg(put, "sell", qty, "put")
            c_leg = make_leg(call, "buy", qty, "call")
        if u_leg is None or p_leg is None or c_leg is None:
            return None  # a missing side cannot be priced executably; skip, not guess
        legs = [u_leg, p_leg, c_leg]
        p_q = premium_to_quote(p_leg.executable_price, put.premium_ccy, p_leg.side, spot)
        c_q = premium_to_quote(c_leg.executable_price, call.premium_ccy, c_leg.side, spot)
        if p_q is None or c_q is None:
            return None
        notional = anchor.best_ask * qty
        if conversion:
            cost_entry = (u_leg.executable_price + p_q - c_q) * qty
            gross_quote = K * df * qty - cost_entry
            margin_capital = cost_entry
        else:
            proceeds = (u_leg.executable_price + p_q - c_q) * qty
            gross_quote = proceeds - K * df * qty
            # collateral for the short underlying (1x) + short put margin approximated by K
            margin_capital = (u_leg.executable_price + K) * qty
            flags.append(
                "requires_spot_borrow" if anchor_kind == "spot" else "short_perp_funding_exposure"
            )
            flags.append("margin_model_approx")

        fees = cfg.fees
        u_fee_bps = fees.spot_taker_bps if anchor_kind == "spot" else fees.perp_taker_bps
        fees_quote = notional * u_fee_bps / 1e4
        fees_quote += option_fee_quote(p_q * qty, notional, fees, settlement=True)
        fees_quote += option_fee_quote(c_q * qty, notional, fees, settlement=True)
        half_spread_quote = 0.0
        funding_expected_quote = 0.0
        funding_uncert_quote = 0.0
        borrow_quote = 0.0
        if anchor_kind == "perp":
            # perp must be closed at expiry (cross spread) and carries funding until then
            half_spread_quote = qty * (anchor.best_ask - anchor.best_bid) / 2
            fees_quote += notional * fees.perp_taker_bps / 1e4
            fr = snap.funding
            intervals = (T * 365 * 86400) / (fr.interval_sec if fr else 8 * 3600)
            if fr is not None:
                # long perp pays positive funding; short perp pays negative funding
                signed = fr.rate if conversion else -fr.rate
                funding_expected_quote = max(0.0, signed) * intervals * notional
                funding_uncert_quote = (
                    notional * cfg.funding_sigma_bps_per_interval / 1e4 * intervals
                )
            else:
                flags.append("funding_unknown_for_perp_proxy")
            flags.append("perp_forward_proxy_funding_in_costs")
        elif not conversion:
            if snap.spot_borrow_apr is None:
                flags.append("borrow_unavailable")
            else:
                borrow_quote = notional * snap.spot_borrow_apr * T
        costs = {
            "fees": fees_quote,
            "half_spread_slip": half_spread_quote,
            "hedge_rebalance": 0.0,
            "borrow": borrow_quote,
            "transfer": notional * cfg.transfer_bps / 1e4,
            "capital_opp": margin_capital * cfg.ref_rate_apr * T,
            "funding_expected": funding_expected_quote,
            "funding_uncertainty": funding_uncert_quote,
        }
        extra = {
            "anchor": anchor_kind,
            "strike": K,
            "expiry_ms": expiry_ms,
            "expiry": datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
            "T_years": round(T, 6),
            "discount_factor": round(df, 8),
            "ref_rate_apr": cfg.ref_rate_apr,
            "pcp": {
                "call_exec_quote": round(c_q, 6),
                "put_exec_quote": round(p_q, 6),
                "underlying_exec": u_leg.executable_price,
                "K_df": round(K * df, 6),
                "identity": "C - P ≈ F - K·df (executable bid/ask substituted)",
            },
            "invalidation": ["edge_collapsed", "contract_halt", "borrow_spike", "index_anomaly"],
        }
        books = {anchor.inst_id: anchor, call.inst_id: call, put.inst_id: put}
        if spot is not None:
            books.setdefault(spot.inst_id, spot)
        return build_record(
            snap=snap,
            family=family,
            taxonomy=taxonomy,
            legs=legs,
            books=books,
            gross_quote=gross_quote,
            notional_quote=notional,
            costs_quote=costs,
            margin_capital=margin_capital,
            risk_flags=flags,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{family}|{anchor.inst_id}|{expiry_ms}|{K}",
            extra=extra,
        )

    # ---- A3 box spread implied financing (identity_approx)

    def scan_a3(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        spot = snap.spot
        if spot is None or not spot.two_sided:
            return []
        out: list[dict] = []
        for expiry_ms, strikes in sorted(snap.option_chain().items()):
            T = years_between(snap.ts_ms, expiry_ms)
            df = math.exp(-cfg.ref_rate_apr * T)
            ks = sorted(k for k, pair in strikes.items() if "C" in pair and "P" in pair)
            pairs = [(k1, k2) for i, k1 in enumerate(ks) for k2 in ks[i + 1 :]]
            for k1, k2 in pairs[: cfg.max_box_pairs]:
                for direction in ("buy_box", "sell_box"):
                    rec = self._box_record(snap, strikes, k1, k2, expiry_ms, T, df, direction)
                    if rec is not None:
                        out.append(rec)
        return out

    def _box_record(
        self,
        snap: Snapshot,
        strikes: dict[float, dict[str, Book]],
        k1: float,
        k2: float,
        expiry_ms: int,
        T: float,
        df: float,
        direction: str,
    ) -> dict | None:
        cfg = self.cfg
        spot = snap.spot
        qty = cfg.qty
        c1, p1 = strikes[k1]["C"], strikes[k1]["P"]
        c2, p2 = strikes[k2]["C"], strikes[k2]["P"]
        flags = self._option_common_flags([c1, p1, c2, p2], snap)
        flags += ["four_leg_execution_risk", "capital_locked_to_expiry"]
        if T <= 0:
            flags.append("expired_or_no_time_to_expiry")
        buy = direction == "buy_box"
        if buy:
            # long call K1 @ ask, short call K2 @ bid, long put K2 @ ask, short put K1 @ bid
            legs = [
                make_leg(c1, "buy", qty, "call"),
                make_leg(c2, "sell", qty, "call"),
                make_leg(p2, "buy", qty, "put"),
                make_leg(p1, "sell", qty, "put"),
            ]
        else:
            legs = [
                make_leg(c1, "sell", qty, "call"),
                make_leg(c2, "buy", qty, "call"),
                make_leg(p2, "sell", qty, "put"),
                make_leg(p1, "buy", qty, "put"),
            ]
            flags.append("short_box_margin_not_approved")
        if any(leg is None for leg in legs):
            return None
        legs = [leg for leg in legs if leg is not None]
        quotes = []
        for leg in legs:
            q = premium_to_quote(leg.executable_price, leg.premium_ccy, leg.side, spot)
            if q is None:
                return None
            quotes.append(q if leg.side == "buy" else -q)
        premium_exec = sum(quotes) * qty  # net debit (+) / credit (−) in quote
        payoff_pv = (k2 - k1) * df * qty
        notional = spot.best_ask * qty
        if buy:
            gross_quote = payoff_pv - premium_exec
            margin_capital = max(premium_exec, 0.0)
        else:
            gross_quote = -premium_exec - payoff_pv  # credit received minus PV owed
            margin_capital = (k2 - k1) * qty  # full liability at expiry
        implied_rate = None
        net_prem = premium_exec if buy else -premium_exec
        if net_prem > 0 and T > 0:
            implied_rate = -math.log(net_prem / ((k2 - k1) * qty)) / T
        fees = cfg.fees
        fees_quote = 0.0
        for leg, q in zip(legs, quotes, strict=True):
            fees_quote += option_fee_quote(abs(q) * qty, notional, fees, settlement=True)
        costs = {
            "fees": fees_quote,
            "half_spread_slip": 0.0,
            "hedge_rebalance": 0.0,
            "borrow": 0.0,
            "transfer": notional * cfg.transfer_bps / 1e4,
            "capital_opp": margin_capital * cfg.ref_rate_apr * T,
            "funding_expected": 0.0,
            "funding_uncertainty": 0.0,
        }
        extra = {
            "direction": direction,
            "strikes": [k1, k2],
            "expiry_ms": expiry_ms,
            "expiry": datetime.fromtimestamp(expiry_ms / 1000, UTC).isoformat(),
            "T_years": round(T, 6),
            "discount_factor": round(df, 8),
            "ref_rate_apr": cfg.ref_rate_apr,
            "box": {
                "payoff_quote": round((k2 - k1) * qty, 6),
                "payoff_pv_quote": round(payoff_pv, 6),
                "premium_exec_quote": round(premium_exec, 6),
                "implied_box_rate_apr": round(implied_rate, 6)
                if implied_rate is not None
                else None,
                "implied_minus_ref_rate_apr": (
                    round(implied_rate - cfg.ref_rate_apr, 6) if implied_rate is not None else None
                ),
            },
            "invalidation": [
                "unsynthesizable_book",
                "edge_collapsed",
                "expiry_strike_mismatch",
                "margin_rule_change",
            ],
        }
        books = {b.inst_id: b for b in (c1, c2, p1, p2)}
        books[spot.inst_id] = spot
        return build_record(
            snap=snap,
            family=FAMILY_A3,
            taxonomy=TAXONOMY_IDENTITY,
            legs=legs,
            books=books,
            gross_quote=gross_quote,
            notional_quote=notional,
            costs_quote=costs,
            margin_capital=margin_capital,
            risk_flags=flags,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_A3}|{expiry_ms}|{k1}|{k2}|{direction}",
            extra=extra,
        )


# --------------------------------------------------------------------------- summary


def summarize(records: list[dict], snapshots: int, cfg: ScanConfig, source: dict) -> dict:
    fam: dict[str, dict] = {}
    for f in FAMILIES:
        rows = [r for r in records if r["family"] == f]
        nets = [r["net_edge_bps"] for r in rows]
        fam[f] = {
            "records": len(rows),
            "edge_exceeds_buffer": sum(1 for r in rows if r["edge_exceeds_buffer"]),
            "passes_threshold": sum(1 for r in rows if r["passes_threshold"]),
            "median_net_edge_bps": round(statistics.median(nets), 4) if nets else None,
            "max_net_edge_bps": round(max(nets), 4) if nets else None,
            "taxonomies": sorted({r["taxonomy"] for r in rows}),
        }
    hyp: dict[str, dict] = {}
    for hid in ("H-A1", "H-A2", "H-A3"):
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
        hyp[hid] = {
            "records": len(rows),
            "passes_threshold": n_pass,
            "pass_rate": round(n_pass / len(rows), 4) if rows else None,
            "status": status,
            "falsify_if": FALSIFY_IF[hid],
            "note": "paper/read-only sample; no return claim; calibrate buffer before judging",
        }
    return {
        "mode": MODE,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
        "venue": records[0]["venue"] if records else DEFAULT_VENUE,
        "snapshots": snapshots,
        "records": len(records),
        "families": fam,
        "hypotheses": hyp,
        "config": cfg.to_dict(),
        "data_source": source,
        "policy": okx.POLICY,
        "mainline_unchanged": "spot_grid_local_paper",
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- okx public source


class OkxPublicSnapshotSource:
    """Builds a Snapshot from OKX *public* GET endpoints via tools/okx_readonly_client.py.

    Requests per snapshot: 1 spot book + 1 perp book + 1 funding + option books
    (2 × strikes × expiries). Instrument metadata is fetched once. No auth, no POST.
    """

    def __init__(
        self,
        client: okx.OkxPublicClient,
        spot_inst: str = "ETH-USDT",
        perp_inst: str = "ETH-USDT-SWAP",
        opt_family: str | None = "ETH-USD",
        n_strikes: int = 3,
        max_expiries: int = 1,
        min_days_to_expiry: float = 2.0,
        book_depth: int = 5,
        spot_borrow_apr: float | None = None,
    ):
        self.client = client
        self.spot_inst = spot_inst
        self.perp_inst = perp_inst
        self.opt_family = opt_family
        self.n_strikes = n_strikes
        self.max_expiries = max_expiries
        self.min_days_to_expiry = min_days_to_expiry
        self.book_depth = book_depth
        self.spot_borrow_apr = spot_borrow_apr
        self._perp_ct_val: float | None = None
        self._options_meta: list[dict] | None = None

    def _book(self, inst_id: str, kind: str, ct_val: float = 1.0, **kw) -> Book:
        raw = self.client.get_books(inst_id, sz=self.book_depth)
        return Book(
            inst_id=inst_id,
            kind=kind,
            bids=tuple(Level(px, sz * ct_val) for px, sz in raw["bids"]),
            asks=tuple(Level(px, sz * ct_val) for px, sz in raw["asks"]),
            ts_ms=raw["ts_ms"],
            **kw,
        )

    def _load_meta(self) -> None:
        if self._perp_ct_val is None:
            rows = self.client.get_instruments("SWAP", inst_id=self.perp_inst)
            self._perp_ct_val = (rows[0].get("ctVal") if rows else None) or 1.0
        if self._options_meta is None and self.opt_family:
            self._options_meta = [
                r
                for r in self.client.get_instruments("OPTION", inst_family=self.opt_family)
                if r.get("state", "live") == "live" and r.get("strike") and r.get("expTime_ms")
            ]

    def _select_options(self, now_ms: int, spot_ref: float) -> list[dict]:
        if not self._options_meta:
            return []
        min_exp = now_ms + self.min_days_to_expiry * 86_400_000
        expiries = sorted(
            {r["expTime_ms"] for r in self._options_meta if r["expTime_ms"] >= min_exp}
        )
        chosen: list[dict] = []
        for exp in expiries[: self.max_expiries]:
            rows = [r for r in self._options_meta if r["expTime_ms"] == exp]
            strikes = sorted({r["strike"] for r in rows}, key=lambda k: abs(k - spot_ref))
            keep = set(strikes[: self.n_strikes])
            chosen.extend(r for r in rows if r["strike"] in keep and r["optType"] in ("C", "P"))
        return chosen

    def fetch(self) -> Snapshot:
        self._load_meta()
        spot = self._book(self.spot_inst, "spot")
        perp = self._book(self.perp_inst, "perp", ct_val=self._perp_ct_val or 1.0)
        fr_raw = self.client.get_funding_rate(self.perp_inst)
        funding = (
            Funding(
                rate=fr_raw["rate"],
                next_rate=fr_raw["next_rate"],
                interval_sec=fr_raw["interval_sec"],
                next_funding_time_ms=fr_raw["next_funding_time_ms"],
            )
            if fr_raw.get("rate") is not None
            else None
        )
        now_ms = spot.ts_ms or int(time.time() * 1000)
        options: list[Book] = []
        ref = spot.best_ask or spot.best_bid or 0.0
        for meta in self._select_options(now_ms, ref):
            options.append(
                self._book(
                    meta["instId"],
                    "option",
                    ct_val=meta.get("ctVal") or 1.0,
                    opt_type=meta["optType"],
                    strike=meta["strike"],
                    expiry_ms=meta["expTime_ms"],
                    premium_ccy="base",  # OKX coin-margined options quote premium in coin
                    settle_ccy=meta.get("settleCcy"),
                )
            )
        return Snapshot(
            ts_ms=now_ms,
            venue=DEFAULT_VENUE,
            spot=spot,
            perp=perp,
            funding=funding,
            options=tuple(options),
            spot_borrow_apr=self.spot_borrow_apr,
            source={
                "kind": "okx_public_books",
                "endpoints": [okx.PATH_BOOKS, okx.PATH_FUNDING_RATE, okx.PATH_INSTRUMENTS],
                "auth": "none",
                "read_only": True,
                "http_fetch": True,
                "requests_made": self.client.requests_made,
                "option_books": len(options),
                "assumptions": [
                    "option px = premium in coin per 1 unit of underlying; "
                    "sizes = contracts × ctVal",
                    "spot quote ccy (USDT) vs option settlement (coin, USD index): "
                    "anchor mismatch flagged",
                ],
            },
        )


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper multi-leg arb scanner (Phase A: A1 funding carry, A2 PCP, A3 box). "
        "Read-only, observe_only, will_send_http=false. Emits JSON lines.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", choices=["fixture", "okx-public"], default="fixture")
    p.add_argument(
        "--fixture",
        default=str(ROOT / "fixtures" / "arb_books" / "2026-09-16-eth-books-sample.json"),
        help="fixture JSON with snapshots (offline replay)",
    )
    p.add_argument("--venue", default=DEFAULT_VENUE)
    p.add_argument("--spot", default="ETH-USDT")
    p.add_argument("--perp", default="ETH-USDT-SWAP")
    p.add_argument("--opt-family", default="ETH-USD", help="'' to skip options")
    p.add_argument("--n-strikes", type=int, default=3)
    p.add_argument("--max-expiries", type=int, default=1)
    p.add_argument("--min-days-to-expiry", type=float, default=2.0)
    p.add_argument("--book-depth", type=int, default=5)
    p.add_argument("--samples", type=int, default=1, help="okx-public: snapshots to take")
    p.add_argument("--interval-sec", type=float, default=20.0, help="okx-public: seconds between")
    p.add_argument("--base-url", default=okx.OKX_PUBLIC_BASE)
    p.add_argument("--timeout", type=float, default=10.0)

    p.add_argument("--qty", type=float, default=1.0)
    p.add_argument("--horizon-intervals", type=int, default=3)
    p.add_argument("--ref-rate-apr", type=float, default=0.0)
    p.add_argument("--spot-borrow-apr", type=float, default=None, help="unset → shorts flagged")
    p.add_argument("--funding-sigma-bps", type=float, default=2.0)
    p.add_argument("--hedge-rebalance-bps", type=float, default=2.0)
    p.add_argument("--transfer-bps", type=float, default=0.0)
    p.add_argument("--depth-mult", type=float, default=2.0)
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--persistence-min-samples", type=int, default=3)
    p.add_argument("--persistence-min-sec", type=float, default=60.0)
    p.add_argument("--pcp-anchor", choices=["spot", "perp"], default="spot")
    p.add_argument("--credit-favorable-basis", action="store_true")
    p.add_argument("--max-box-pairs", type=int, default=6)

    p.add_argument("--fee-spot-bps", type=float, default=10.0)
    p.add_argument("--fee-perp-bps", type=float, default=5.0)
    p.add_argument("--fee-option-bps", type=float, default=3.0)
    p.add_argument("--fee-option-cap-pct", type=float, default=12.5)
    p.add_argument("--fee-option-settle-bps", type=float, default=2.0)

    p.add_argument(
        "--buffer-fee-roundtrip-bps",
        type=float,
        default=None,
        help="None → the record's own computed fees",
    )
    p.add_argument("--buffer-slip-bps", type=float, default=5.0)
    p.add_argument("--buffer-funding-uncert-bps", type=float, default=5.0)
    p.add_argument("--buffer-haircut-bps", type=float, default=10.0)
    p.add_argument(
        "--buffer-calibrated",
        action="store_true",
        help="only set after calibrating on historical books (04-risk)",
    )

    p.add_argument("--paper-fills", action="store_true")
    p.add_argument("--paper-extra-slip-bps", type=float, default=5.0)

    p.add_argument("--out", default="", help="JSONL records file (default stdout)")
    p.add_argument("--summary-out", default="", help="summary JSON file")
    p.add_argument("--print-summary", action="store_true", help="summary JSON to stderr")
    p.add_argument("--only-exceeding", action="store_true", help="emit only edge_exceeds_buffer")
    p.add_argument("--quiet", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> ScanConfig:
    return ScanConfig(
        qty=args.qty,
        horizon_intervals=args.horizon_intervals,
        ref_rate_apr=args.ref_rate_apr,
        funding_sigma_bps_per_interval=args.funding_sigma_bps,
        hedge_rebalance_bps=args.hedge_rebalance_bps,
        transfer_bps=args.transfer_bps,
        depth_mult=args.depth_mult,
        top_n=args.top_n,
        persistence_min_samples=args.persistence_min_samples,
        persistence_min_sec=args.persistence_min_sec,
        pcp_anchor=args.pcp_anchor,
        credit_favorable_basis=args.credit_favorable_basis,
        paper_fills=args.paper_fills,
        paper_extra_slip_bps=args.paper_extra_slip_bps,
        max_box_pairs=args.max_box_pairs,
        fees=FeeSchedule(
            spot_taker_bps=args.fee_spot_bps,
            perp_taker_bps=args.fee_perp_bps,
            option_taker_bps=args.fee_option_bps,
            option_fee_cap_pct_premium=args.fee_option_cap_pct,
            option_settlement_bps=args.fee_option_settle_bps,
        ),
        buffer=SafetyBuffer(
            fee_roundtrip_bps=args.buffer_fee_roundtrip_bps,
            slip_buffer_bps=args.buffer_slip_bps,
            funding_uncert_bps=args.buffer_funding_uncert_bps,
            model_haircut_bps=args.buffer_haircut_bps,
            calibrated=args.buffer_calibrated,
        ),
    )


def run(args: argparse.Namespace) -> tuple[list[dict], dict]:
    cfg = config_from_args(args)
    scanner = ArbScanner(cfg)
    records: list[dict] = []
    if args.source == "fixture":
        snaps = load_fixture(args.fixture)
        if args.spot_borrow_apr is not None:
            snaps = [replace(s, spot_borrow_apr=args.spot_borrow_apr) for s in snaps]
        for s in snaps:
            records.extend(scanner.scan(s))
        source = snaps[0].source if snaps else {"kind": "fixture", "http_fetch": False}
        n = len(snaps)
    else:
        client = okx.OkxPublicClient(base_url=args.base_url, timeout=args.timeout)
        src = OkxPublicSnapshotSource(
            client,
            spot_inst=args.spot,
            perp_inst=args.perp,
            opt_family=args.opt_family or None,
            n_strikes=args.n_strikes,
            max_expiries=args.max_expiries,
            min_days_to_expiry=args.min_days_to_expiry,
            book_depth=args.book_depth,
            spot_borrow_apr=args.spot_borrow_apr,
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
    return records, summarize(records, n, cfg, source)


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
        RuntimeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
