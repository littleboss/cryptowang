#!/usr/bin/env python3
"""Paper spot triangular arb scanner — menu T1, read-only, observe-only.

Implements T1 of `03-proposals/2026-09-17-demo-strategy-menu-stable-edge-v1.md` under the
04-risk conditional pass of the same date (D0–D3: read-only scan + paper fills only; no demo
orders; no live). Same-venue three-leg spot cycle on the whitelisted triple

    ETH-USDT · BTC-USDT · ETH-BTC        (both cycle directions, home currency USDT)

taxonomy = same_venue_microstructure  — never `risk_free`: the three legs are not atomic,
impact / queue position / leftover inventory remain.

Hard gates (enforced in code, tested, not just documented):
  * every record: action == "observe_only", will_send_http == False; no order / amend /
    withdraw / transfer code path; the only network I/O is the public GET order-book path of
    tools/okx_readonly_client.py (`--source okx-public`), default is an offline fixture;
  * executable prices are bid/ask only (buy @ask, sell @bid) — mid / mark / last never price
    a leg; a one-sided book skips the direction, it is never guessed;
  * sync: the three books must share a timestamp window; skew > `max_book_skew_ms` (200 ms)
    → `stale_book`, the record is logged but can never pass; a missing book timestamp is
    treated the same way (`book_timestamp_missing`). The gate is `sync_gate=strict` and is
    never relaxed here; to *lower the skew itself* the okx-public source issues the three
    GETs concurrently (`--fetch-mode parallel`, default; `sequential` is kept for A/B) and can
    re-fetch within the same snapshot (`--sync-retries`). Every fetch is documented on the
    record (`book_sync.fetch`: per-leg local sent/recv ms, fetch span, venue-clock skew,
    attempts) and the summary splits every metric into full sample vs the `!stale_book`
    subset (`sync_report`) so "no edge" and "gate removed the sample" stay distinguishable;
  * liquidity: per-leg half spread must be ≤ `max_half_spread_bps` (15 bp, C4 spirit) or the
    record is `illiquid`; depth / full walk of `qty` is required as in Phase A;
  * costs: fees + half_spread_slip (non-atomic re-quote haircut) + impact at minimum, and
    **every gross / net edge number on a record comes from tools/cost_engine.py
    (`evaluate_legs`)** — this module does not compute a net edge of its own;
  * `residual_risks` is mandatory and always names the non-atomic three-leg / leftover
    inventory risk; leverage concept 1x; whitelist is fixed for this PR (other triples need a
    named 04-risk approval → `TriangleNotWhitelisted`).

Output: JSONL records + a summary with `net_positive_rate`, `cost_kill_rate` and the synthetic
fill success rate under the simultaneous-book assumption. None of it is a return promise.

The spot-grid / local_paper mainline is untouched: this module does not import or alter
tools/local_paper_grid.py or strategies/grid_ab_compare.py.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "strategies"))

import cost_engine as ce  # noqa: E402
import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402

# --------------------------------------------------------------------------- constants

ACTION = "observe_only"
WILL_SEND_HTTP = False
MODE = "paper_read_only"
PHASE = "T"
MENU_ID = "T1"
FAMILY = "T1_spot_triangle"
TAXONOMY = "same_venue_microstructure"
HYPOTHESIS_ID = "H-T1"
FALSIFY_IF = "net_positive_rate_stays_zero_after_cost_engine_on_synced_liquid_books"
DEFAULT_VENUE = "okx"
HOME_CCY = "USDT"

# 04-risk whitelist for this PR. Anything else is refused, not scanned.
WHITELIST_LEGS = ("ETH-USDT", "BTC-USDT", "ETH-BTC")
WHITELIST_TRIANGLES = frozenset({frozenset(WHITELIST_LEGS)})

DIRECTION_A = "usdt_eth_btc_usdt"  # home → a → b → home
DIRECTION_B = "usdt_btc_eth_usdt"  # home → b → a → home
DIRECTIONS = (DIRECTION_A, DIRECTION_B)

EXECUTABLE_PRICE_TYPES = frozenset({"bid", "ask"})
SIDE_TO_PRICE_TYPE = {"buy": "ask", "sell": "bid"}

# Sync gate label. Only "strict" exists in this module: the 200 ms window is applied as-is.
# A written 04-risk relaxation would have to introduce `relaxed` *with* split reporting; it
# must never be a silent knob change (see 04-risk 2026-09-18 t1-stale-fix-reread-v1).
SYNC_GATE = "strict"

# okx-public book fetch modes. `parallel` issues the three GETs concurrently (skew ≈ server
# clock spread, not the sum of three round trips); `sequential` is the T1-001/002 behaviour
# kept only for before/after comparison.
FETCH_PARALLEL = "parallel"
FETCH_SEQUENTIAL = "sequential"
FETCH_MODES = (FETCH_PARALLEL, FETCH_SEQUENTIAL)
DEFAULT_FETCH_MODE = FETCH_PARALLEL

# Cost buckets carried on costs_bps (engine core set; no funding thesis for spot triangles).
COST_KEYS = tuple(ce.CORE_COMPONENTS)

RESIDUAL_RISKS = (
    "non_atomic_three_leg",
    "leftover_inventory_on_partial_or_cancelled_leg",
    "impact_and_queue_position",
    "fee_tier_uncertainty",
    "book_staleness_between_legs",
    "demo_vs_live_liquidity_gap",
)

REQUIRED_FIELDS = (
    "timestamp",
    "venue",
    "phase",
    "menu_id",
    "family",
    "taxonomy",
    "direction",
    "instruments",
    "legs",
    "executable_prices",
    "triangle",
    "book_sync",
    "gross_edge_bps",
    "costs_bps",
    "net_edge_bps",
    "cost_engine",
    "safety_buffer_bps",
    "passes_threshold",
    "margin_capital_required",
    "persistence",
    "liquidity",
    "synthetic_fill",
    "residual_risks",
    "risk_flags",
    "hypothesis_id",
    "action",
    "will_send_http",
)

INVALIDATING_FLAGS = frozenset(
    {
        "stale_book",
        "book_timestamp_missing",
        "illiquid",
        "insufficient_depth",
        "one_sided_book",
        "missing_book",
        "half_spread_over_cap",
    }
)

FORBIDDEN_LABELS = ce.FORBIDDEN_LABELS

DISCLAIMER = (
    "T1 纸面只读三角扫描（observe_only，will_send_http=false）。同所三腿现货环，taxonomy="
    "same_venue_microstructure：三腿非原子、有冲击 / 排队 / 残留库存，非零风险。"
    "毛边 / 净边 / 成本拆解全部由 tools/cost_engine.py evaluate_legs 给出（bid/ask 可执行价、"
    "持有期口径、不年化、calibrated=false）。汇总只报 net>0 率 / cost_kill_rate / 纸面合成成交率，"
    "不是收益承诺，不下单，不并入网格主线。"
)


# --------------------------------------------------------------------------- errors


class TriangleNotWhitelisted(ValueError):
    """A triangle outside the 04-risk whitelist for this PR was requested."""


class TriangleSchemaViolation(ValueError):
    """A record breaks the T1 schema (taxonomy / residual_risks / required fields)."""


class ForbiddenLabelViolation(ValueError):
    """A record text carries a forbidden label (risk_free / 稳赚 / guaranteed …)."""


ObserveOnlyViolation = pas.ObserveOnlyViolation
ExecutablePriceViolation = pas.ExecutablePriceViolation


# --------------------------------------------------------------------------- market data model


@dataclass(frozen=True)
class Triangle:
    """Three spot legs over currencies home / a / b: `a-home`, `b-home`, `a-b`."""

    home: str = "USDT"
    a: str = "ETH"
    b: str = "BTC"

    @property
    def a_home(self) -> str:
        return f"{self.a}-{self.home}"

    @property
    def b_home(self) -> str:
        return f"{self.b}-{self.home}"

    @property
    def a_b(self) -> str:
        return f"{self.a}-{self.b}"

    @property
    def legs(self) -> tuple[str, str, str]:
        return (self.a_home, self.b_home, self.a_b)

    def validate(self) -> None:
        if frozenset(self.legs) not in WHITELIST_TRIANGLES:
            raise TriangleNotWhitelisted(
                f"refused: triangle {self.legs} is not on the 04-risk whitelist "
                f"{WHITELIST_LEGS} (other triples need a named approval)"
            )

    def to_dict(self) -> dict:
        return {
            "home_ccy": self.home,
            "a": self.a,
            "b": self.b,
            "legs": list(self.legs),
            "whitelist": list(WHITELIST_LEGS),
        }


DEFAULT_TRIANGLE = Triangle()


@dataclass(frozen=True)
class TriangleSnapshot:
    ts_ms: int
    venue: str
    books: dict[str, pas.Book]
    source: dict = field(default_factory=dict)


def load_fixture(path: str | Path) -> list[TriangleSnapshot]:
    """Fixture: {venue, note, snapshots:[{ts_ms, books:[{inst_id,bids,asks,ts_ms}, …]}]}."""
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    venue = doc.get("venue", DEFAULT_VENUE)
    out: list[TriangleSnapshot] = []
    for s in doc["snapshots"]:
        books = {}
        for raw in s.get("books") or []:
            b = pas.Book.from_dict(raw, "spot")
            books[b.inst_id] = b
        out.append(
            TriangleSnapshot(
                ts_ms=int(s["ts_ms"]),
                venue=s.get("venue", venue),
                books=books,
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
class TriangleSafetyBuffer:
    """safety_buffer_bps = sum of auditable components; fee part defaults to the record's own
    cost_engine fees so the buffer never undercounts fees. No funding term (spot only)."""

    fee_roundtrip_bps: float | None = None
    slip_buffer_bps: float = 5.0
    nonatomic_haircut_bps: float = 5.0
    model_haircut_bps: float = 5.0
    calibrated: bool = False

    def resolve(self, fees_bps: float) -> dict:
        fee = self.fee_roundtrip_bps if self.fee_roundtrip_bps is not None else fees_bps
        comps = {
            "fee_roundtrip_bps": round(fee, 4),
            "slip_buffer_bps": self.slip_buffer_bps,
            "nonatomic_haircut_bps": self.nonatomic_haircut_bps,
            "model_haircut_bps": self.model_haircut_bps,
        }
        return {
            "components": comps,
            "total_bps": round(sum(comps.values()), 4),
            "calibrated": self.calibrated,
            "rule": "net_edge_bps > safety_buffer_bps AND persistence.ok AND liquidity.ok "
            "AND book_sync.ok AND no invalidating risk_flags",
        }


@dataclass(frozen=True)
class TriangleConfig:
    notional_quote: float = 100.0  # home-currency (USDT) notional pushed round the cycle
    max_book_skew_ms: int = 200  # 04-risk: three-leg timestamp alignment tolerance
    max_half_spread_bps: float = 15.0  # 04-risk: per spot leg, else `illiquid`
    nonatomic_slip_half_spreads: int = 1  # extra half-spread crossings per leg (re-quote)
    ref_rate_apr: float = 0.0  # capital opportunity cost; 0 = no claim (cycle is seconds)
    depth_mult: float = 2.0
    top_n: int = 5
    persistence_min_samples: int = 3
    persistence_min_sec: float = 60.0
    triangle: Triangle = field(default_factory=Triangle)
    fees: ce.FeeSchedule = field(default_factory=ce.FeeSchedule)
    buffer: TriangleSafetyBuffer = field(default_factory=TriangleSafetyBuffer)

    def validate(self) -> None:
        if self.notional_quote <= 0:
            raise ValueError("notional_quote must be > 0")
        if self.max_book_skew_ms < 0:
            raise ValueError("max_book_skew_ms must be >= 0")
        if self.max_half_spread_bps <= 0:
            raise ValueError("max_half_spread_bps must be > 0")
        if self.nonatomic_slip_half_spreads < 0:
            raise ValueError("nonatomic_slip_half_spreads must be >= 0")
        if self.ref_rate_apr < 0:
            raise ValueError("ref_rate_apr must be >= 0")
        if self.persistence_min_samples < 1:
            raise ValueError("persistence_min_samples must be >= 1")
        if self.top_n < 1:
            raise ValueError("top_n must be >= 1")
        self.triangle.validate()

    def to_dict(self) -> dict:
        return {
            "notional_quote": self.notional_quote,
            "notional_ccy": self.triangle.home,
            "max_book_skew_ms": self.max_book_skew_ms,
            "max_half_spread_bps": self.max_half_spread_bps,
            "nonatomic_slip_half_spreads": self.nonatomic_slip_half_spreads,
            "ref_rate_apr": self.ref_rate_apr,
            "depth_mult": self.depth_mult,
            "top_n": self.top_n,
            "persistence_min_samples": self.persistence_min_samples,
            "persistence_min_sec": self.persistence_min_sec,
            "leverage_concept": 1,
            "triangle": self.triangle.to_dict(),
            "directions": list(DIRECTIONS),
            "fees": self.fees.to_dict(),
            "cost_engine": {
                "enabled": True,
                "mandatory": True,
                "engine": ce.ENGINE,
                "version": ce.VERSION,
                "entry": "evaluate_legs",
                "calibrated": False,
                "tradable_claim_allowed": False,
            },
            "safety_buffer": {
                "fee_roundtrip_bps": self.buffer.fee_roundtrip_bps,
                "slip_buffer_bps": self.buffer.slip_buffer_bps,
                "nonatomic_haircut_bps": self.buffer.nonatomic_haircut_bps,
                "model_haircut_bps": self.buffer.model_haircut_bps,
                "calibrated": self.buffer.calibrated,
            },
        }


# --------------------------------------------------------------------------- cycle arithmetic


@dataclass(frozen=True)
class CycleLeg:
    """One executed leg of the cycle: base-unit qty at the bid/ask executable price."""

    instrument: str
    side: str  # buy | sell
    qty: float  # base units of `instrument`
    executable_price: float
    price_type: str
    base_ccy: str
    quote_ccy: str
    quote_conv_to_home: float  # price units → home currency (1.0 for home-quoted legs)
    book_ts_ms: int

    def __post_init__(self) -> None:
        if self.side not in SIDE_TO_PRICE_TYPE:
            raise ValueError(f"leg side must be buy|sell, got {self.side!r}")
        if self.price_type not in EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(
                f"refused: executable price_type {self.price_type!r} for {self.instrument} "
                "(bid/ask only; mid/mark/last are not tradable)"
            )
        if SIDE_TO_PRICE_TYPE[self.side] != self.price_type:
            raise ExecutablePriceViolation(
                f"refused: {self.side} must use {SIDE_TO_PRICE_TYPE[self.side]}, "
                f"got {self.price_type!r} for {self.instrument}"
            )
        if self.executable_price <= 0 or self.qty <= 0 or self.quote_conv_to_home <= 0:
            raise ValueError("leg price, qty and quote_conv must be > 0")

    @property
    def exec_notional_home(self) -> float:
        return self.executable_price * self.qty * self.quote_conv_to_home

    def to_dict(self) -> dict:
        return {
            "instrument": self.instrument,
            "side": self.side,
            "qty": round(self.qty, 12),
            "executable_price": self.executable_price,
            "price_type": self.price_type,
            "role": "spot",
            "base_ccy": self.base_ccy,
            "quote_ccy": self.quote_ccy,
            "quote_conv_to_home": self.quote_conv_to_home,
            "exec_notional_home": round(self.exec_notional_home, 8),
            "book_ts_ms": self.book_ts_ms,
        }


@dataclass(frozen=True)
class Cycle:
    direction: str
    path: tuple[str, ...]
    legs: tuple[CycleLeg, CycleLeg, CycleLeg]
    home_in: float
    home_out: float  # before fees / slip / impact (those come from the cost engine)
    cross_exec: float
    cross_implied_from_directs: float

    @property
    def gross_quote(self) -> float:
        return self.home_out - self.home_in


def _px(book: pas.Book, side: str) -> float | None:
    return book.best_ask if side == "buy" else book.best_bid


def build_cycle(
    tri: Triangle, books: dict[str, pas.Book], direction: str, notional_home: float
) -> Cycle | None:
    """Push `notional_home` of the home currency round the three legs at bid/ask.

    Direction A (home→a→b→home): buy a @ask(a-home) → sell a for b @bid(a-b) → sell b @bid(b-home)
    Direction B (home→b→a→home): buy b @ask(b-home) → buy a with b @ask(a-b) → sell a @bid(a-home)
    The a-b leg is priced in b; its home conversion uses the b-home *ask* (conservative for
    costs). Returns None if any needed side is missing — a one-sided book is not guessed.
    """
    ah, bh, ab = books.get(tri.a_home), books.get(tri.b_home), books.get(tri.a_b)
    if ah is None or bh is None or ab is None:
        return None
    conv_ab = bh.best_ask
    if conv_ab is None:
        return None
    if direction == DIRECTION_A:
        p1, p2, p3 = _px(ah, "buy"), _px(ab, "sell"), _px(bh, "sell")
        if p1 is None or p2 is None or p3 is None:
            return None
        qty_a = notional_home / p1
        qty_b = qty_a * p2
        home_out = qty_b * p3
        legs = (
            CycleLeg(tri.a_home, "buy", qty_a, p1, "ask", tri.a, tri.home, 1.0, ah.ts_ms),
            CycleLeg(tri.a_b, "sell", qty_a, p2, "bid", tri.a, tri.b, conv_ab, ab.ts_ms),
            CycleLeg(tri.b_home, "sell", qty_b, p3, "bid", tri.b, tri.home, 1.0, bh.ts_ms),
        )
        # profitable iff bid(a-b) > ask(a-home) / bid(b-home)
        implied = p1 / p3
        path = (tri.home, tri.a, tri.b, tri.home)
    elif direction == DIRECTION_B:
        p1, p2, p3 = _px(bh, "buy"), _px(ab, "buy"), _px(ah, "sell")
        if p1 is None or p2 is None or p3 is None:
            return None
        qty_b = notional_home / p1
        qty_a = qty_b / p2
        home_out = qty_a * p3
        legs = (
            CycleLeg(tri.b_home, "buy", qty_b, p1, "ask", tri.b, tri.home, 1.0, bh.ts_ms),
            CycleLeg(tri.a_b, "buy", qty_a, p2, "ask", tri.a, tri.b, conv_ab, ab.ts_ms),
            CycleLeg(tri.a_home, "sell", qty_a, p3, "bid", tri.a, tri.home, 1.0, ah.ts_ms),
        )
        # profitable iff ask(a-b) < bid(a-home) / ask(b-home)
        implied = p3 / p1
        path = (tri.home, tri.b, tri.a, tri.home)
    else:
        raise ValueError(f"unknown direction {direction!r}")
    return Cycle(
        direction=direction,
        path=path,
        legs=legs,
        home_in=notional_home,
        home_out=home_out,
        cross_exec=p2,
        cross_implied_from_directs=implied,
    )


# --------------------------------------------------------------------------- gates


def book_sync(
    books: dict[str, pas.Book], insts: tuple[str, ...], max_skew_ms: int
) -> tuple[dict, list[str]]:
    """Three-leg timestamp window. skew = max(ts) − min(ts); > max → stale_book (never passes).
    A book without a timestamp cannot prove alignment → book_timestamp_missing (same effect)."""
    flags: list[str] = []
    ts = {i: (books[i].ts_ms if i in books else None) for i in insts}
    missing = [i for i, t in ts.items() if not t]
    present = [t for t in ts.values() if t]
    skew = (max(present) - min(present)) if len(present) == len(insts) and present else None
    ok = not missing and skew is not None and skew <= max_skew_ms
    if missing:
        flags.append("book_timestamp_missing")
    if skew is not None and skew > max_skew_ms:
        flags.append("stale_book")
    return (
        {
            "ok": ok,
            "sync_gate": SYNC_GATE,
            "book_ts_ms": ts,
            "skew_ms": skew,
            "max_skew_ms": max_skew_ms,
            "rule": "three books within max_skew_ms; else stale_book, never counts as pass",
        },
        flags,
    )


def half_spread_bps(book: pas.Book) -> float | None:
    """Half the bid/ask width relative to the bid, in bp. A width metric — not a price."""
    if not book.two_sided:
        return None
    return (book.best_ask - book.best_bid) / 2.0 / book.best_bid * 1e4


def triangle_liquidity(
    cycle: Cycle, books: dict[str, pas.Book], cfg: TriangleConfig
) -> tuple[dict, list[str], list[pas.Walk]]:
    """Per-leg half-spread cap (≤ max_half_spread_bps) + depth / full walk of qty.
    Returns (liquidity block, flags, walks in leg order) — walks feed impact via VWAP."""
    flags: list[str] = []
    notes: list[str] = []
    ok = True
    per_leg = []
    walks: list[pas.Walk] = []
    for leg in cycle.legs:
        book = books[leg.instrument]
        hs = half_spread_bps(book)
        if not book.two_sided:
            flags.append("one_sided_book")
            ok = False
        if hs is not None and hs > cfg.max_half_spread_bps:
            flags.append("half_spread_over_cap")
            ok = False
            notes.append(f"{leg.instrument} half_spread {hs:.2f} bp > {cfg.max_half_spread_bps:g}")
        depth = book.depth_qty(leg.side, cfg.top_n)
        need = leg.qty * cfg.depth_mult
        w = book.walk(leg.side, leg.qty)
        walks.append(w)
        if not w.complete:
            flags.append("insufficient_depth")
            ok = False
        if depth < need:
            ok = False
            notes.append(f"{leg.instrument} {leg.side}: top{cfg.top_n} depth {depth:g} < {need:g}")
        per_leg.append(
            {
                "instrument": leg.instrument,
                "side": leg.side,
                "half_spread_bps": round(hs, 4) if hs is not None else None,
                "half_spread_cap_bps": cfg.max_half_spread_bps,
                "half_spread_ok": hs is not None and hs <= cfg.max_half_spread_bps,
                "top_n_depth_qty": round(depth, 10),
                "required_qty": round(need, 10),
                "vwap": w.vwap,
                "filled_qty": round(w.filled_qty, 12),
                "complete": w.complete,
            }
        )
    if not ok and "illiquid" not in flags:
        flags.append("illiquid")
    return (
        {
            "ok": ok,
            "max_half_spread_bps": cfg.max_half_spread_bps,
            "depth_mult": cfg.depth_mult,
            "top_n": cfg.top_n,
            "legs": per_leg,
            "notes": "; ".join(notes) if notes else ("ok" if ok else "see risk_flags"),
        },
        sorted(set(flags)),
        walks,
    )


def synthetic_fill(cycle: Cycle, walks: list[pas.Walk], sync_ok: bool) -> dict:
    """Paper-only 'would all three legs fill?' under the simultaneous-book assumption:
    every leg walks to completion on its own side AND the books are in sync. Bookkeeping only —
    no order exists (`order_sent=false`)."""
    reasons: list[str] = []
    legs = []
    for leg, w in zip(cycle.legs, walks, strict=True):
        if not w.complete:
            reasons.append(f"{leg.instrument}:{leg.side}:partial_fill")
        legs.append(
            {
                "instrument": leg.instrument,
                "side": leg.side,
                "qty": round(leg.qty, 12),
                "filled_qty": round(w.filled_qty, 12),
                "fill_vwap_hypothetical": w.vwap,
                "complete": w.complete,
            }
        )
    if not sync_ok:
        reasons.append("books_not_synced")
    return {
        "assumption": "all_three_legs_fill_simultaneously_on_the_walked_books",
        "ok": not reasons,
        "reasons": reasons,
        "legs": legs,
        "order_sent": False,
        "will_send_http": WILL_SEND_HTTP,
        "note": "paper bookkeeping; real legs are sequential and non-atomic (residual_risks)",
    }


# --------------------------------------------------------------------------- record


def evaluate_cycle(
    cycle: Cycle, books: dict[str, pas.Book], walks: list[pas.Walk], cfg: TriangleConfig
) -> ce.CostResult:
    """The only place a T1 edge is computed: Cost Engine v0 `evaluate_legs` on bid/ask legs —
    one fee crossing per leg, `nonatomic_slip_half_spreads` extra half-spreads as the re-quote
    haircut, VWAP from the book walk for impact."""
    specs = []
    for leg, w in zip(cycle.legs, walks, strict=True):
        book = books[leg.instrument]
        specs.append(
            ce.LegSpec(
                instrument=leg.instrument,
                kind="spot",
                side=leg.side,
                qty=leg.qty,
                bid=book.best_bid,
                ask=book.best_ask,
                vwap=w.vwap,
                quote_conv=leg.quote_conv_to_home,
                crossings=1,
                slip_crossings=cfg.nonatomic_slip_half_spreads,
            )
        )
    hold_years = 0.0  # intra-second cycle; no holding horizon, no funding thesis
    other = {
        "borrow": 0.0,
        "transfer": 0.0,
        "capital_opp": ce.capital_opp_quote(cycle.home_in, cfg.ref_rate_apr, hold_years),
        "funding_uncertainty": 0.0,
        "hedge_rebalance": 0.0,
    }
    return ce.evaluate_legs(
        legs=specs,
        fees=cfg.fees,
        gross_quote=cycle.gross_quote,
        underlying_notional_quote=cycle.home_in,
        other_components_quote=other,
        hold_years=hold_years,
        notes=[
            "T1 spot triangle: gross = home_out − home_in at bid/ask; one fee crossing per leg; "
            f"{cfg.nonatomic_slip_half_spreads} extra half-spread(s) per leg as non-atomic "
            "re-quote haircut; impact = VWAP vs top for qty",
            "a-b leg costs converted to home ccy at b-home ask (conservative)",
        ],
    )


def build_record(
    *,
    snap: TriangleSnapshot,
    cycle: Cycle,
    books: dict[str, pas.Book],
    cfg: TriangleConfig,
    tracker: pas.PersistenceTracker,
) -> dict:
    insts = tuple(leg.instrument for leg in cycle.legs)
    sync, sync_flags = book_sync(books, insts, cfg.max_book_skew_ms)
    # How the three books were obtained (okx-public: fetch mode, per-leg local sent/recv,
    # attempts). Fixture snapshots carry no fetch → None. Documentation only; never a gate input.
    sync["fetch"] = snap.source.get("fetch")
    liquidity, liq_flags, walks = triangle_liquidity(cycle, books, cfg)
    res = evaluate_cycle(cycle, books, walks, cfg)
    engine = res.to_dict()

    costs_bps = {k: engine["components_bps"][k] for k in COST_KEYS}
    for k, v in engine["components_bps"].items():
        costs_bps.setdefault(k, v)
    costs_bps["total"] = engine["all_in_cost_bps"]
    gross_bps = engine["gross_edge_bps"]
    net_bps = engine["net_edge_bps"]

    buffer = cfg.buffer.resolve(costs_bps["fees"])
    flags = sorted(
        set(sync_flags)
        | set(liq_flags)
        | {"same_venue_microstructure_not_riskless", "non_atomic_three_leg"}
    )
    invalidated = sorted(set(flags) & INVALIDATING_FLAGS)
    key = f"{FAMILY}|{snap.venue}|{cycle.direction}|{'|'.join(insts)}"
    persistence = tracker.update(key, snap.ts_ms, net_bps, buffer["total_bps"])
    edge_exceeds_buffer = net_bps > buffer["total_bps"]
    passes = bool(
        edge_exceeds_buffer
        and persistence["ok"]
        and liquidity["ok"]
        and sync["ok"]
        and not invalidated
    )
    fill = synthetic_fill(cycle, walks, sync["ok"])

    executable_prices = {
        i: {"bid": books[i].best_bid, "ask": books[i].best_ask, "ts_ms": books[i].ts_ms}
        for i in insts
    }
    dominant = max(((k, v) for k, v in costs_bps.items() if k != "total"), key=lambda kv: kv[1])[0]
    cross_dev_bps = (
        (cycle.cross_exec - cycle.cross_implied_from_directs)
        / cycle.cross_implied_from_directs
        * 1e4
    )
    if cycle.direction == DIRECTION_B:
        cross_dev_bps = -cross_dev_bps  # buying the cross: cheaper than implied is favourable

    rec: dict = {
        "timestamp": pas.iso_cst(snap.ts_ms),
        "ts_ms": snap.ts_ms,
        "venue": snap.venue,
        "mode": MODE,
        "phase": PHASE,
        "menu_id": MENU_ID,
        "family": FAMILY,
        "taxonomy": TAXONOMY,
        "direction": cycle.direction,
        "instruments": sorted(insts),
        "legs": [leg.to_dict() for leg in cycle.legs],
        "executable_prices": executable_prices,
        "triangle": {
            "home_ccy": cfg.triangle.home,
            "path": list(cycle.path),
            "legs_order": list(insts),
            "home_in": round(cycle.home_in, 8),
            "home_out_before_costs": round(cycle.home_out, 8),
            "gross_quote": round(cycle.gross_quote, 8),
            "cross_instrument": cfg.triangle.a_b,
            "cross_exec": cycle.cross_exec,
            "cross_implied_from_directs": round(cycle.cross_implied_from_directs, 12),
            "cross_favourable_deviation_bps": round(cross_dev_bps, 4),
            "cross_quote_conv_to_home": "b_home_ask",
        },
        "book_sync": sync,
        "notional_quote": round(cycle.home_in, 6),
        "notional_basis": "home_ccy_notional_in",
        "gross_edge_bps": gross_bps,
        "costs_bps": costs_bps,
        "net_edge_bps": net_bps,
        "net_edge_source": "tools/cost_engine.py:evaluate_legs",
        "dominant_cost_component": dominant,
        "cost_engine": engine,
        "safety_buffer_bps": buffer["total_bps"],
        "safety_buffer": buffer,
        "edge_exceeds_buffer": edge_exceeds_buffer,
        "gross_positive": gross_bps > 0,
        "net_positive": net_bps > 0,
        "cost_killed": bool(gross_bps > 0 and net_bps <= 0),
        "passes_threshold": passes,
        "invalidated_by": invalidated,
        "margin_capital_required": round(cycle.home_in, 6),
        "leverage_concept": 1,
        "persistence": persistence,
        "liquidity": liquidity,
        "synthetic_fill": fill,
        "residual_risks": list(RESIDUAL_RISKS),
        "risk_flags": flags,
        "hypothesis_id": HYPOTHESIS_ID,
        "invalidation": [
            "book_desync_between_legs",
            "leg_2_or_3_requote_beyond_haircut",
            "depth_collapse_on_cross_leg",
            "fee_tier_worse_than_placeholder",
        ],
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
    }
    return finalize_record(rec)


def finalize_record(rec: dict) -> dict:
    """Force the observe-only constants and validate the T1 schema. Raises rather than emits."""
    if rec.get("action") != ACTION or rec.get("will_send_http") is not False:
        raise ObserveOnlyViolation(
            f"refused: action={rec.get('action')!r} will_send_http={rec.get('will_send_http')!r}"
        )
    missing = [k for k in REQUIRED_FIELDS if k not in rec]
    if missing:
        raise TriangleSchemaViolation(f"record missing required fields: {missing}")
    if rec["taxonomy"] != TAXONOMY:
        raise TriangleSchemaViolation(f"T1 must be {TAXONOMY}, got {rec['taxonomy']!r}")
    if rec["family"] != FAMILY or rec["menu_id"] != MENU_ID or rec["phase"] != PHASE:
        raise TriangleSchemaViolation("family / menu_id / phase must be T1_spot_triangle / T1 / T")
    if rec["direction"] not in DIRECTIONS:
        raise TriangleSchemaViolation(f"bad direction {rec['direction']!r}")
    if not set(RESIDUAL_RISKS) <= set(rec.get("residual_risks") or []):
        raise TriangleSchemaViolation("residual_risks must include the mandatory T1 list")
    if "non_atomic_three_leg" not in rec["risk_flags"]:
        raise TriangleSchemaViolation("risk_flags must carry non_atomic_three_leg")
    if len(rec["legs"]) != 3:
        raise TriangleSchemaViolation("a triangle record has exactly three legs")
    for leg in rec["legs"]:
        if leg["price_type"] not in EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(f"refused: leg price_type {leg['price_type']!r}")
        if leg["price_type"] != SIDE_TO_PRICE_TYPE[leg["side"]]:
            raise ExecutablePriceViolation(f"refused: {leg['side']} at {leg['price_type']}")
    if "total" not in rec["costs_bps"]:
        raise TriangleSchemaViolation("costs_bps.total required")
    ceb = rec["cost_engine"]
    if ceb.get("action") != ACTION or ceb.get("will_send_http") is not False:
        raise ObserveOnlyViolation("refused: cost_engine block is not observe_only")
    if ceb.get("tradable_claim_allowed") is not False or ceb.get("annualized") is not False:
        raise TriangleSchemaViolation("refused: cost_engine block claims tradable / annualised")
    if ceb.get("engine") != ce.ENGINE or ceb.get("calibrated") is not False:
        raise TriangleSchemaViolation("refused: cost_engine block missing or calibrated claim")
    if abs(ceb["all_in_cost_bps"] - rec["costs_bps"]["total"]) > 1e-9:
        raise TriangleSchemaViolation("cost_engine.all_in_cost_bps must equal costs_bps.total")
    if abs(ceb["net_edge_bps"] - rec["net_edge_bps"]) > 1e-9:
        raise TriangleSchemaViolation("net_edge_bps must be the cost_engine net edge")
    if abs(ceb["gross_edge_bps"] - rec["gross_edge_bps"]) > 1e-9:
        raise TriangleSchemaViolation("gross_edge_bps must be the cost_engine gross edge")
    if rec["synthetic_fill"].get("order_sent") is not False:
        raise ObserveOnlyViolation("refused: synthetic_fill.order_sent must be false")
    if rec.get("leverage_concept", 1) != 1:
        raise TriangleSchemaViolation("leverage_concept must be 1")
    if rec["passes_threshold"] and (rec["invalidated_by"] or not rec["book_sync"]["ok"]):
        raise TriangleSchemaViolation("a stale / invalidated record can never pass")
    text = json.dumps(rec, ensure_ascii=False).lower()
    for bad in FORBIDDEN_LABELS:
        if bad in text:
            raise ForbiddenLabelViolation(f"refused: forbidden label {bad!r} in record")
    return rec


# --------------------------------------------------------------------------- scanner


class TriangleScanner:
    def __init__(self, cfg: TriangleConfig, tracker: pas.PersistenceTracker | None = None):
        cfg.validate()
        self.cfg = cfg
        self.tracker = tracker or pas.PersistenceTracker(
            cfg.persistence_min_samples, cfg.persistence_min_sec
        )
        self.skipped: Counter[str] = Counter()

    def scan(self, snap: TriangleSnapshot) -> list[dict]:
        tri = self.cfg.triangle
        books = {i: snap.books[i] for i in tri.legs if i in snap.books}
        missing = [i for i in tri.legs if i not in books]
        if missing:
            self.skipped["missing_book"] += 1
            return []
        out: list[dict] = []
        for direction in DIRECTIONS:
            if not all(books[i].two_sided for i in tri.legs):
                self.skipped["one_sided_book"] += 1
                continue
            cycle = build_cycle(tri, books, direction, self.cfg.notional_quote)
            if cycle is None:
                self.skipped["one_sided_book"] += 1
                continue
            out.append(
                build_record(
                    snap=snap, cycle=cycle, books=books, cfg=self.cfg, tracker=self.tracker
                )
            )
        return out


# --------------------------------------------------------------------------- summary


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _metrics(rows: list[dict]) -> dict:
    nets = [r["net_edge_bps"] for r in rows]
    grosses = [r["gross_edge_bps"] for r in rows]
    n = len(rows)
    gross_pos = sum(1 for r in rows if r["gross_positive"])
    net_pos = sum(1 for r in rows if r["net_positive"])
    killed = sum(1 for r in rows if r["cost_killed"])
    fills = sum(1 for r in rows if r["synthetic_fill"]["ok"])
    return {
        "records": n,
        "gross_positive": gross_pos,
        "gross_positive_rate": _rate(gross_pos, n),
        "net_positive": net_pos,
        "net_positive_rate": _rate(net_pos, n),
        "cost_killed": killed,
        "cost_kill_rate": _rate(killed, gross_pos),
        "cost_kill_rate_definition": "gross_edge_bps > 0 AND net_edge_bps <= 0, over gross>0",
        "edge_exceeds_buffer": sum(1 for r in rows if r["edge_exceeds_buffer"]),
        "passes_threshold": sum(1 for r in rows if r["passes_threshold"]),
        "pass_rate": _rate(sum(1 for r in rows if r["passes_threshold"]), n),
        "synthetic_fill_ok": fills,
        "synthetic_fill_success_rate": _rate(fills, n),
        "stale_book": sum(1 for r in rows if "stale_book" in r["risk_flags"]),
        "book_timestamp_missing": sum(
            1 for r in rows if "book_timestamp_missing" in r["risk_flags"]
        ),
        "illiquid": sum(1 for r in rows if "illiquid" in r["risk_flags"]),
        "median_gross_edge_bps": round(statistics.median(grosses), 4) if grosses else None,
        "median_net_edge_bps": round(statistics.median(nets), 4) if nets else None,
        "max_net_edge_bps": round(max(nets), 4) if nets else None,
        "dominant_cost_component": dict(Counter(r["dominant_cost_component"] for r in rows)),
    }


def _percentile(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def sync_report(records: list[dict], cfg: TriangleConfig, source: dict | None = None) -> dict:
    """Full sample vs `!stale_book` subset, side by side (04-risk split reporting).

    The gate is not touched: `synced` is exactly `book_sync.ok` under the strict window. The
    report exists so a re-scan can show whether the synced subset is non-empty (method
    success) and what `net>0` / cost_kill / pass look like *on that subset* — without which
    "no edge" and "the gate removed every sample" are indistinguishable."""
    synced = [r for r in records if r["book_sync"]["ok"]]
    unsynced = [r for r in records if not r["book_sync"]["ok"]]
    skews = sorted(
        r["book_sync"]["skew_ms"] for r in records if r["book_sync"]["skew_ms"] is not None
    )
    n = len(records)
    stale = sum(1 for r in records if "stale_book" in r["risk_flags"])
    missing = sum(1 for r in records if "book_timestamp_missing" in r["risk_flags"])
    fetch = (source or {}).get("fetch") or {}
    within = sum(1 for s in skews if s <= cfg.max_book_skew_ms)
    return {
        "sync_gate": SYNC_GATE,
        "max_skew_ms": cfg.max_book_skew_ms,
        "gate_unchanged": True,
        "fetch_mode": fetch.get("mode"),
        "sync_retries_allowed": fetch.get("sync_retries_allowed"),
        "records": n,
        "synced": len(synced),
        "synced_rate": _rate(len(synced), n),
        "stale_book": stale,
        "stale_book_rate": _rate(stale, n),
        "book_timestamp_missing": missing,
        "skew_ms": {
            "n": len(skews),
            "min": skews[0] if skews else None,
            "median": round(statistics.median(skews), 1) if skews else None,
            "p90": _percentile(skews, 0.9),
            "max": skews[-1] if skews else None,
            "within_gate": within,
            "within_gate_rate": _rate(within, len(skews)),
        },
        "subsets": {
            "all": _metrics(records),
            "synced": _metrics(synced),
            "stale_or_missing": _metrics(unsynced),
        },
        "method_success_rule": "synced_rate > 0 (target >= 0.20) — measurability, not an edge",
        "edge_rule": "reproducible net_edge_bps > 0 on the synced subset across >= 2 windows "
        "→ only allows *drafting* a separate demo-size case; this module never orders",
        "note": "same strict window as book_sync; strict is the only gate mode in this "
        "module. Rejected fetch attempts (if --sync-retries > 0) are listed per record "
        "under book_sync.fetch.attempt_skews_ms, never scored.",
    }


def summarize(
    records: list[dict],
    snapshots: int,
    cfg: TriangleConfig,
    source: dict,
    skipped: Counter | None = None,
) -> dict:
    overall = _metrics(records)
    n_pass = overall["passes_threshold"]
    sync = sync_report(records, cfg, source)
    if not records:
        status = "no_samples"
    elif snapshots < cfg.persistence_min_samples:
        status = "insufficient_samples_for_persistence"
    elif n_pass == 0:
        status = "no_pass_on_window"  # consistent with falsification; not proof either way
    else:
        status = "not_yet_falsified_on_window"
    return {
        "mode": MODE,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
        "phase": PHASE,
        "menu_id": MENU_ID,
        "family": FAMILY,
        "taxonomy": TAXONOMY,
        "venue": records[0]["venue"] if records else DEFAULT_VENUE,
        "snapshots": snapshots,
        "records": len(records),
        "skipped": dict(skipped or {}),
        "metrics": overall,
        "sync_report": sync,
        "directions": {
            d: _metrics([r for r in records if r["direction"] == d]) for d in DIRECTIONS
        },
        "hypotheses": {
            HYPOTHESIS_ID: {
                "records": len(records),
                "net_positive_rate": overall["net_positive_rate"],
                "cost_kill_rate": overall["cost_kill_rate"],
                "synthetic_fill_success_rate": overall["synthetic_fill_success_rate"],
                "passes_threshold": n_pass,
                "pass_rate": overall["pass_rate"],
                "synced_subset": {
                    "records": sync["synced"],
                    "synced_rate": sync["synced_rate"],
                    "net_positive_rate": sync["subsets"]["synced"]["net_positive_rate"],
                    "cost_kill_rate": sync["subsets"]["synced"]["cost_kill_rate"],
                    "pass_rate": sync["subsets"]["synced"]["pass_rate"],
                },
                "status": status,
                "falsify_if": FALSIFY_IF,
                "note": "paper/read-only sample; no return claim; calibrate buffer before judging",
            }
        },
        "cost_engine": ce.summarize_blocks([r["cost_engine"] for r in records], enabled=True),
        "config": cfg.to_dict(),
        "data_source": source,
        "policy": okx.POLICY,
        "mainline_unchanged": "spot_grid_local_paper",
        "related_menu": "T2..T8 not implemented here; A/B scanners remain",
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- okx public source


def _wall_ms() -> int:
    return time.time_ns() // 1_000_000


class OkxPublicTriangleSource:
    """Builds a TriangleSnapshot from three public GET order books (no auth, no POST).
    Spot books report sizes in base units already (no ctVal). Each book keeps its own OKX
    timestamp so the sync gate can measure skew.

    Skew root cause (T1-001/002, stale_book 48/48, skew ≈ 400–500 ms): the three GETs were
    issued one after another, so the venue timestamps of book 1 and book 3 were separated by
    two full round trips. `fetch_mode=parallel` (default) issues the three GETs concurrently
    from a small thread pool; the venue-clock skew then reflects server-side spread plus
    thread start jitter instead of accumulated latency. `sequential` is kept for A/B.

    Same-window alignment (`sync_retries`): if the parallel fetch still lands outside the
    window, the three books are re-fetched up to N more times *within the same snapshot*
    and the first attempt inside the window is used; if none is, the last attempt is kept
    and is still `stale_book` under the unchanged gate. Every attempt's skew is documented.

    Only read-only GETs happen here; nothing in this class can send an order."""

    def __init__(
        self,
        client: okx.OkxPublicClient,
        triangle: Triangle = DEFAULT_TRIANGLE,
        book_depth: int = 5,
        fetch_mode: str = DEFAULT_FETCH_MODE,
        sync_retries: int = 0,
        max_skew_ms: int = TriangleConfig.max_book_skew_ms,
        clock: Callable[[], int] | None = None,
    ):
        triangle.validate()
        if fetch_mode not in FETCH_MODES:
            raise ValueError(f"fetch_mode must be one of {FETCH_MODES}, got {fetch_mode!r}")
        if sync_retries < 0:
            raise ValueError("sync_retries must be >= 0")
        if max_skew_ms < 0:
            raise ValueError("max_skew_ms must be >= 0")
        self.client = client
        self.triangle = triangle
        self.book_depth = book_depth
        self.fetch_mode = fetch_mode
        self.sync_retries = sync_retries
        self.max_skew_ms = max_skew_ms
        self._clock = clock or _wall_ms

    def _book(self, inst_id: str) -> pas.Book:
        raw = self.client.get_books(inst_id, sz=self.book_depth)
        return pas.Book(
            inst_id=inst_id,
            kind="spot",
            bids=tuple(pas.Level(px, sz) for px, sz in raw["bids"]),
            asks=tuple(pas.Level(px, sz) for px, sz in raw["asks"]),
            ts_ms=raw["ts_ms"],
        )

    def _timed_book(self, inst_id: str) -> tuple[pas.Book, dict]:
        sent = self._clock()
        book = self._book(inst_id)
        recv = self._clock()
        return book, {
            "instrument": inst_id,
            "sent_ms": sent,
            "recv_ms": recv,
            "latency_ms": recv - sent,
            "book_ts_ms": book.ts_ms,
        }

    def _fetch_once(self, attempt: int) -> tuple[dict[str, pas.Book], dict]:
        legs = self.triangle.legs
        started = self._clock()
        if self.fetch_mode == FETCH_PARALLEL:
            with ThreadPoolExecutor(max_workers=len(legs), thread_name_prefix="t1-book") as ex:
                results = list(ex.map(self._timed_book, legs))
        else:
            results = [self._timed_book(i) for i in legs]
        finished = self._clock()
        books = {b.inst_id: b for b, _ in results}
        ts = [b.ts_ms for b in books.values() if b.ts_ms]
        skew = (max(ts) - min(ts)) if len(ts) == len(legs) else None
        sent = [t["sent_ms"] for _, t in results]
        return books, {
            "attempt": attempt,
            "started_ms": started,
            "finished_ms": finished,
            "fetch_span_ms": finished - started,
            "send_spread_ms": max(sent) - min(sent),
            "book_ts_skew_ms": skew,
            "legs": [t for _, t in results],
        }

    def fetch(self) -> TriangleSnapshot:
        attempts: list[dict] = []
        books: dict[str, pas.Book] = {}
        chosen: dict = {}
        for attempt in range(1, self.sync_retries + 2):
            books, chosen = self._fetch_once(attempt)
            attempts.append(chosen)
            skew = chosen["book_ts_skew_ms"]
            if skew is not None and skew <= self.max_skew_ms:
                break
        ts = [b.ts_ms for b in books.values() if b.ts_ms]
        now_ms = max(ts) if ts else self._clock()
        return TriangleSnapshot(
            ts_ms=now_ms,
            venue=DEFAULT_VENUE,
            books=books,
            source={
                "kind": "okx_public_books",
                "endpoints": [okx.PATH_BOOKS],
                "auth": "none",
                "read_only": True,
                "http_fetch": True,
                "requests_made": self.client.requests_made,
                "books_per_snapshot": len(books),
                "fetch": {
                    "mode": self.fetch_mode,
                    "sync_retries_allowed": self.sync_retries,
                    "attempts": len(attempts),
                    "selected_attempt": chosen["attempt"],
                    "attempt_skews_ms": [a["book_ts_skew_ms"] for a in attempts],
                    "selection_rule": "first attempt with book_ts_skew_ms <= max_skew_ms; "
                    "otherwise the last attempt is kept and stays stale_book (gate unchanged)",
                    "max_skew_ms": self.max_skew_ms,
                    "started_ms": chosen["started_ms"],
                    "finished_ms": chosen["finished_ms"],
                    "fetch_span_ms": chosen["fetch_span_ms"],
                    "send_spread_ms": chosen["send_spread_ms"],
                    "book_ts_skew_ms": chosen["book_ts_skew_ms"],
                    "legs": chosen["legs"],
                    "clocks": "sent_ms/recv_ms/started_ms/finished_ms = local wall clock; "
                    "book_ts_ms = OKX venue clock; skew is venue-clock only",
                },
                "assumptions": [
                    "spot sizes are base units; three public GETs per attempt "
                    f"({self.fetch_mode}); skew measured from OKX book timestamps and gated "
                    "by max_book_skew_ms (strict, unchanged)"
                ],
            },
        )


# --------------------------------------------------------------------------- cli

DEFAULT_FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-btc-usdt-triangle-sample.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper spot triangular arb scanner (menu T1: ETH-USDT / BTC-USDT / ETH-BTC, "
        "both directions). Read-only, observe_only, will_send_http=false, "
        "taxonomy=same_venue_microstructure; every edge comes from tools/cost_engine.py. "
        "Emits JSON lines + summary (net_positive_rate, cost_kill_rate, synthetic fill rate).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--source", choices=["fixture", "okx-public"], default="fixture")
    p.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="fixture JSON (offline)")
    p.add_argument("--venue", default=DEFAULT_VENUE, help="label only (okx | okx_demo)")
    p.add_argument("--home", default="USDT", help="home ccy (whitelist: USDT)")
    p.add_argument("--leg-a", default="ETH", help="first intermediate ccy (whitelist: ETH)")
    p.add_argument("--leg-b", default="BTC", help="second intermediate ccy (whitelist: BTC)")
    p.add_argument("--book-depth", type=int, default=5)
    p.add_argument("--samples", type=int, default=1, help="okx-public: snapshots to take")
    p.add_argument("--interval-sec", type=float, default=20.0, help="okx-public: seconds between")
    p.add_argument("--base-url", default=okx.OKX_PUBLIC_BASE)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument(
        "--fetch-mode",
        choices=list(FETCH_MODES),
        default=DEFAULT_FETCH_MODE,
        help="okx-public: parallel = the three book GETs are issued concurrently (skew no "
        "longer sums three round trips); sequential = T1-001/002 behaviour, kept for A/B",
    )
    p.add_argument(
        "--sync-retries",
        type=int,
        default=0,
        help="okx-public: re-fetch the three books up to N more times within the same "
        "snapshot while venue-clock skew > --max-book-skew-ms; the gate itself is unchanged "
        "and every attempt's skew is written to book_sync.fetch.attempt_skews_ms",
    )

    p.add_argument("--notional-quote", type=float, default=100.0, help="home ccy notional in")
    p.add_argument("--max-book-skew-ms", type=int, default=200)
    p.add_argument("--max-half-spread-bps", type=float, default=15.0)
    p.add_argument("--nonatomic-slip-half-spreads", type=int, default=1)
    p.add_argument("--ref-rate-apr", type=float, default=0.0)
    p.add_argument("--depth-mult", type=float, default=2.0)
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--persistence-min-samples", type=int, default=3)
    p.add_argument("--persistence-min-sec", type=float, default=60.0)
    p.add_argument("--fee-spot-bps", type=float, default=10.0, help="taker placeholder")

    p.add_argument("--buffer-fee-roundtrip-bps", type=float, default=None)
    p.add_argument("--buffer-slip-bps", type=float, default=5.0)
    p.add_argument("--buffer-nonatomic-bps", type=float, default=5.0)
    p.add_argument("--buffer-haircut-bps", type=float, default=5.0)
    p.add_argument(
        "--buffer-calibrated",
        action="store_true",
        help="only set after calibrating on historical books (04-risk)",
    )

    p.add_argument("--out", default="", help="JSONL records file (default stdout)")
    p.add_argument("--summary-out", default="", help="summary JSON file")
    p.add_argument("--print-summary", action="store_true", help="summary JSON to stderr")
    p.add_argument("--only-exceeding", action="store_true", help="emit only edge_exceeds_buffer")
    p.add_argument("--quiet", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> TriangleConfig:
    return TriangleConfig(
        notional_quote=args.notional_quote,
        max_book_skew_ms=args.max_book_skew_ms,
        max_half_spread_bps=args.max_half_spread_bps,
        nonatomic_slip_half_spreads=args.nonatomic_slip_half_spreads,
        ref_rate_apr=args.ref_rate_apr,
        depth_mult=args.depth_mult,
        top_n=args.top_n,
        persistence_min_samples=args.persistence_min_samples,
        persistence_min_sec=args.persistence_min_sec,
        triangle=Triangle(home=args.home.upper(), a=args.leg_a.upper(), b=args.leg_b.upper()),
        fees=ce.FeeSchedule(spot_taker_bps=args.fee_spot_bps),
        buffer=TriangleSafetyBuffer(
            fee_roundtrip_bps=args.buffer_fee_roundtrip_bps,
            slip_buffer_bps=args.buffer_slip_bps,
            nonatomic_haircut_bps=args.buffer_nonatomic_bps,
            model_haircut_bps=args.buffer_haircut_bps,
            calibrated=args.buffer_calibrated,
        ),
    )


def run(args: argparse.Namespace) -> tuple[list[dict], dict]:
    cfg = config_from_args(args)
    scanner = TriangleScanner(cfg)
    records: list[dict] = []
    if args.source == "fixture":
        snaps = load_fixture(args.fixture)
        if args.venue != DEFAULT_VENUE:
            snaps = [replace(s, venue=args.venue) for s in snaps]
        for s in snaps:
            records.extend(scanner.scan(s))
        source = snaps[0].source if snaps else {"kind": "fixture", "http_fetch": False}
        n = len(snaps)
    else:
        client = okx.OkxPublicClient(base_url=args.base_url, timeout=args.timeout)
        src = OkxPublicTriangleSource(
            client,
            triangle=cfg.triangle,
            book_depth=args.book_depth,
            fetch_mode=args.fetch_mode,
            sync_retries=args.sync_retries,
            max_skew_ms=cfg.max_book_skew_ms,
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
    return records, summarize(records, n, cfg, source, scanner.skipped)


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
        TriangleNotWhitelisted,
        TriangleSchemaViolation,
        ForbiddenLabelViolation,
        ce.CostEngineError,
        RuntimeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
