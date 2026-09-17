#!/usr/bin/env python3
"""Paper combo expansion scanner — C1–C4, read-only, observe-only (low-leg / liquid / falsifiable).

Implements proposal `combo-expansion-low-leg-v1` (`03-proposals/2026-09-17-combo-expansion-
low-leg-v1.md` + `.json`) under the 04-risk *conditional* pass of the same date
(`04-risk/2026-09-17-combo-expansion-low-leg-v1.md`): read-only metrics + paper fills only;
live execution refused; merging this ≠ any trading clearance. Trigger: Cost Engine v0 recompute
of Phase A + B (n = 95) showed net_edge > 0 rate = 0 % — so this branch searches for *fewer-leg,
deeper-book, falsifiable* paper candidates. It does not add legs, leverage or execution.

Four expansion families, all `taxonomy = relative_value`, `phase = "C"`, `combo_id` prefixed
C1–C4. Phase A (A1–A3) and Phase B (B1–B3) scanners are kept and reused, never replaced:

  C1  spot vs dated future basis / term-structure curve   ≤ 2 tradeable legs (spot + nearest
      dated future); far month & perp travel only as `hedge_note` / curve `_ref`
  C2  funding *expectation path* + basis confirmation     ≤ 2 legs (spot + perp); stub path model;
      basis conflict → invalidated; `current_funding × 365` never enters net edge
  C3  B1 / B3 sampler band relaxed by exactly one symmetric step (Δ / moneyness ± 5 pp) → obtain
      non-zero samples → then Cost Engine; coverage gate N ≥ 5 per family before any verdict
  C4  A2 / A3 (and every other leg) high-liquidity filter: half spread ≤ 25 bps of underlying
      notional, top size ≥ 1 contract and ≥ 50 USDT; illiquid → `liquidity_ok = false` (never pass)
      or skipped

Hard gates (enforced in code, tested, not just documented):
  * every record: action == "observe_only", will_send_http == False; no order / amend / withdraw /
    transfer code path; the only network I/O is the public GET path of tools/okx_readonly_client.py
    (reused through the Phase A snapshot source);
  * every record's gross / all-in / net edge comes from tools/cost_engine.py `evaluate()`
    (`net_edge_source`), never from a side channel; the `cost_engine` block is mandatory;
  * executable prices are bid/ask only (Phase A `Leg` gate); mid / mark / model fair values are
    `_ref` anchors only; stub models are labelled `model = "stub"`, `calibrated = false`;
  * taxonomy is `relative_value` for every emitted record (inherited A2 / A3 keep their original
    label in `taxonomy_base`); forbidden labels (risk_free / 无风险 / 稳赚 / guaranteed) refused;
  * default underlying ETH; BTC only with `--allow-btc` (default off, *not* cleared by 04-risk);
  * leverage concept 1x; covered short options only (Phase B cover rule re-asserted, B1 must be
    spot-covered); no naked short vol, no live IOC multi-leg, no cross-venue latency path;
  * C2 refuses any annualised gross (Cost Engine `NaiveAnnualizationRefused`); the stub funding
    path is a sum over H intervals, `annualized_used_in_net_edge = false`;
  * verdict discipline: with fewer than N samples in a family the only allowed wording is
    "coverage_insufficient_no_verdict"; never "falsified" / "dead" / "valid" / "tradable".

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

import cost_engine as ce  # noqa: E402
import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
import paper_combo_scanner as pcs  # noqa: E402
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
    years_between,
)

# --------------------------------------------------------------------------- constants

PHASE = "C"
ACTION = pas.ACTION
WILL_SEND_HTTP = pas.WILL_SEND_HTTP
MODE = pas.MODE
DEFAULT_VENUE = pas.DEFAULT_VENUE
TAXONOMY = pas.TAXONOMY_RV

DEFAULT_UNDERLYING = "ETH"
BTC_UNDERLYING = "BTC"

EXP_C1 = "C1"
EXP_C2 = "C2"
EXP_C3 = "C3"
EXP_C4 = "C4"
EXPANSIONS = (EXP_C1, EXP_C2, EXP_C3, EXP_C4)

FAMILY_C1 = "C1_spot_dated_future_basis"
FAMILY_C2 = "C2_funding_expectation_path"
EXPANSION_NAME = {
    EXP_C1: "spot_vs_dated_futures_basis_curve",
    EXP_C2: "funding_expectation_path_vs_basis_confirmation",
    EXP_C3: "B1_B3_sampler_gate_relaxation",
    EXP_C4: "A2_A3_high_liquidity_strike_filters",
}
HYPOTHESIS = {EXP_C1: "H-C1", EXP_C2: "H-C2", EXP_C3: "H-C3", EXP_C4: "H-C4"}
FALSIFY_IF = {
    "H-C1": "executable_basis_after_all_in_costs_nonpositive_on_covered_window",
    "H-C2": "funding_path_expectation_after_all_in_costs_nonpositive_or_basis_conflicts",
    "H-C3": "relaxed_band_still_yields_n_lt_N_or_net_gt_0_rate_zero_with_coverage_met",
    "H-C4": "liquidity_filtered_A2_A3_net_gt_0_rate_not_above_unfiltered",
}

C3_TARGET_FAMILIES = (pcs.FAMILY_B1, pcs.FAMILY_B3)
C4_TARGET_FAMILIES = (pas.FAMILY_A2_CONV, pas.FAMILY_A2_REV, pas.FAMILY_A3)
BASE_COMBO_ID = {
    pas.FAMILY_A1: "A1",
    pas.FAMILY_A2_CONV: "A2",
    pas.FAMILY_A2_REV: "A2",
    pas.FAMILY_A3: "A3",
    **pcs.COMBO_ID,
}
# C4 / risk: A2 / A3 keep their own leg count; no "optimising fourth leg" may be stacked on.
MAX_LEGS = {
    FAMILY_C1: 2,
    FAMILY_C2: 2,
    pas.FAMILY_A2_CONV: 3,
    pas.FAMILY_A2_REV: 3,
    pas.FAMILY_A3: 4,
    pcs.FAMILY_B1: 3,
    pcs.FAMILY_B3: 3,
}

# The one sampler mechanism this branch is cleared for (04-risk: "一档，二者取一"):
# Δ band and moneyness fallback band each widened symmetrically by 5 pp; B3 Δ tolerance +5 pp.
C3_MECHANISM = "delta_moneyness_band_plus_5pp_symmetric"
C3_BAND_STEP = 0.05
C3_COVERAGE_N = 5  # 04-risk calibration: B1 ≥ 5 and B3 ≥ 5 per window before any cost verdict

# C4 calibrations (04-risk, ETH · OKX framing, paper).
C4_MAX_HALF_SPREAD_BPS = 25.0
C4_MIN_SIZE_CONTRACTS = 1.0
C4_MIN_NOTIONAL_QUOTE = 50.0

HEDGE_STATIC = pcs.HEDGE_STATIC
MODEL_STUB = "stub"
MODEL_INHERITED_B = "inherited_phase_B_reference_mid_iv"
MODEL_INHERITED_A = "inherited_phase_A_identity_approx"
NET_EDGE_SOURCE = "tools/cost_engine.py:evaluate"

RESIDUAL_RISKS = {
    FAMILY_C1: (
        "settlement_index_mismatch",
        "near_far_liquidity_asymmetry",
        "roll_cost",
        "gap",
        "margin",
        "basis",
    ),
    FAMILY_C2: (
        "funding_flip",
        "basis_anchor_mismatch",
        "holding_window_vs_settlement_misalignment",
        "gap",
        "margin",
        "basis",
    ),
    pas.FAMILY_A2_CONV: ("execution_multi_leg", "settlement_index_mismatch", "margin", "gap"),
    pas.FAMILY_A2_REV: (
        "execution_multi_leg",
        "settlement_index_mismatch",
        "margin",
        "gap",
        "spot_borrow",
    ),
    pas.FAMILY_A3: ("execution_multi_leg", "settlement_index_mismatch", "margin", "gap"),
}
SHORT_SPOT_RESIDUALS = ("spot_borrow", "spot_borrow_fee")

FORBIDDEN_LABELS = pcs.FORBIDDEN_LABELS
COST_KEYS = pas.COST_KEYS
REQUIRED_FIELDS = pas.REQUIRED_FIELDS + (
    "phase",
    "expansion_id",
    "combo_id",
    "hedge_mode",
    "residual_risks",
    "liquidity_ok",
    "liquidity_filter",
    "model",
    "calibrated",
    "sample_coverage",
    "cost_engine",
    "net_edge_source",
)

FLAG_BASIS_CONFLICT = "basis_conflict_with_funding_expectation"
FLAG_C4_ILLIQUID = "c4_illiquid_leg"
INVALIDATING_FLAGS = (
    pas.INVALIDATING_FLAGS
    | pcs.INVALIDATING_FLAGS
    | frozenset({FLAG_BASIS_CONFLICT, FLAG_C4_ILLIQUID, "hold_horizon_zero"})
)

VERDICT_COVERAGE_INSUFFICIENT = "coverage_insufficient_no_verdict"
VERDICT_COST_VETO = "cost_veto_on_window"
VERDICT_NET_POSITIVE = "net_positive_observed_uncalibrated_not_tradable"
# Words that may never appear in a verdict string (04-risk: no "已证伪 / 策略已死 / 有效" talk).
BANNED_VERDICT_WORDS = ("falsified", "dead", "valid", "tradable_edge", "proven", "已证伪", "有效")

RELATED_SCANNERS = "A1_A2_A3_B1_B2_B3_scanners_remain"
APPROVALS = {
    "proposal": "03-proposals/2026-09-17-combo-expansion-low-leg-v1.md",
    "proposal_json": "03-proposals/2026-09-17-combo-expansion-low-leg-v1.json",
    "risk": "04-risk/2026-09-17-combo-expansion-low-leg-v1.md",
    "scope": "read-only scan + Cost Engine v0 recompute; no live execution; ≤1x; ETH only",
}

DISCLAIMER = (
    "Combo Expansion C1–C4：纸面只读扩搜（observe_only，will_send_http=false，phase=C）。"
    "全部 relative_value：承担基差 / 结算指数 / funding 翻转 / 借币 / 跳空 / 保证金等残留风险，"
    "不是身份套利，更不是确定收益。净边一律由 tools/cost_engine.py evaluate 产出"
    "（bid/ask 可执行毛边 − all-in 成本，持有期口径，不年化）；fair-value / funding 期望为 "
    "model=stub、calibrated=false 的参照，不得升级为「可交易」。覆盖不足时只写「覆盖不足」。"
    "不下单，不对冲，不并入网格主线。"
)

POLICY = {
    "mode": MODE,
    "phase": PHASE,
    "action": ACTION,
    "will_send_http": WILL_SEND_HTTP,
    "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
    "live_execution": False,
    "leverage_concept_max": 1,
    "taxonomy": TAXONOMY,
    "forbidden_label_gate": "enforced_in_finalize_expansion_record (module FORBIDDEN_LABELS; "
    "same list as Phase B / Cost Engine)",
    "default_underlying": DEFAULT_UNDERLYING,
    "btc": "requires --allow-btc (default off; not cleared by 04-risk)",
    "net_edge_source": NET_EDGE_SOURCE,
    "executable_prices": "bid/ask only; mid / mark / model fair value only as *_ref anchors",
    "C1": {
        "max_tradeable_legs": 2,
        "tradeable_legs": "spot + nearest dated future(s) (--c1-tradeable-expiries, default 1)",
        "hedge_note_only": ["far_month_futures", "perp"],
        "fair_value": "stub S_mid·exp(r·T), model=stub, calibrated=false, anchor_ref only",
        "short_spot": "residual_risks must carry spot_borrow + spot_borrow_fee; no borrow → "
        "invalidated",
    },
    "C2": {
        "max_tradeable_legs": 2,
        "funding_expectation": "stub decayed path Σ_h f_h over H intervals (model=stub, "
        "calibrated=false); never current_funding × 365",
        "basis_confirmation": "perp/spot mid basis must not oppose the expected carry beyond "
        "--c2-basis-conflict-bps, else invalidated",
        "annualized_fields": "_ref display only",
    },
    "C3": {
        "targets": list(C3_TARGET_FAMILIES),
        "mechanism": C3_MECHANISM,
        "band_step": C3_BAND_STEP,
        "coverage_gate_n": C3_COVERAGE_N,
        "b1_spot_cover_enforced": True,
        "naked_short_vol": "refused (Phase B cover rule re-asserted)",
    },
    "C4": {
        "targets": list(C4_TARGET_FAMILIES),
        "max_half_spread_bps": C4_MAX_HALF_SPREAD_BPS,
        "half_spread_basis": "bps of underlying notional per unit",
        "min_size_contracts": C4_MIN_SIZE_CONTRACTS,
        "min_notional_quote": C4_MIN_NOTIONAL_QUOTE,
        "illiquid": "liquidity_ok=false (never passes) or skipped (--c4-mode skip)",
        "extra_optimising_leg": "refused",
    },
    "rejected": [
        "naked_short_vol",
        "cross_venue_latency",
        "multi_leg_live_ioc",
        "live_execution",
        "mark_mid_as_tradable_edge",
        "current_funding_times_365_as_net_edge",
        "merge_into_spot_grid_mainline",
        "leverage_gt_1x",
        "claim_B1_B3_verdict_with_coverage_below_N",
        "BTC_without_named_flag",
    ],
    "verdicts": {
        "n_lt_N": VERDICT_COVERAGE_INSUFFICIENT,
        "n_ge_N_and_net_gt_0_zero": VERDICT_COST_VETO,
        "n_ge_N_and_net_gt_0_some": VERDICT_NET_POSITIVE,
    },
    "approvals": APPROVALS,
    "mainline_unchanged": "spot_grid_local_paper",
    "related_scanners": RELATED_SCANNERS,
}


# --------------------------------------------------------------------------- errors


class ExpansionSchemaViolation(ValueError):
    """An expansion record broke a C1–C4 schema / gate rule."""


class UnderlyingNotApproved(ValueError):
    """Snapshot / config underlying is not ETH and BTC was not explicitly enabled."""


class ForbiddenLabelViolation(pcs.ForbiddenLabelViolation):
    """A record contained a risk-free / guaranteed style label."""


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class LiquidityFilter:
    """C4 hard filter (04-risk calibration). half spread is measured in bps of *underlying
    notional per unit* (coin-priced premiums converted at spot mid), i.e. the same basis every
    cost bucket uses; size is the top-of-book size on the leg's side in base units, divided by
    `contract_size_base` (1.0 = fixture equivalence; OKX ETH options have their own ctVal)."""

    max_half_spread_bps: float = C4_MAX_HALF_SPREAD_BPS
    min_size_contracts: float = C4_MIN_SIZE_CONTRACTS
    contract_size_base: float = 1.0
    min_notional_quote: float = C4_MIN_NOTIONAL_QUOTE
    mode: str = "flag"  # flag → liquidity_ok=false (never passes) | skip → not emitted

    def validate(self) -> None:
        if self.max_half_spread_bps <= 0 or self.max_half_spread_bps > C4_MAX_HALF_SPREAD_BPS:
            raise ValueError(
                f"max_half_spread_bps must be in (0, {C4_MAX_HALF_SPREAD_BPS}] (04-risk cap)"
            )
        if self.min_size_contracts < C4_MIN_SIZE_CONTRACTS:
            raise ValueError(f"min_size_contracts must be >= {C4_MIN_SIZE_CONTRACTS} (04-risk)")
        if self.min_notional_quote < C4_MIN_NOTIONAL_QUOTE:
            raise ValueError(f"min_notional_quote must be >= {C4_MIN_NOTIONAL_QUOTE} (04-risk)")
        if self.contract_size_base <= 0:
            raise ValueError("contract_size_base must be > 0")
        if self.mode not in ("flag", "skip"):
            raise ValueError("liquidity mode must be flag|skip")

    def to_dict(self) -> dict:
        return {
            "max_half_spread_bps": self.max_half_spread_bps,
            "half_spread_basis": "bps_of_underlying_notional_per_unit",
            "min_size_contracts": self.min_size_contracts,
            "contract_size_base": self.contract_size_base,
            "min_notional_quote": self.min_notional_quote,
            "notional_basis": "top_size_base_x_spot_mid",
            "mode": self.mode,
            "calibration": "04-risk/2026-09-17-combo-expansion-low-leg-v1.md §C4",
        }


@dataclass(frozen=True)
class ExpansionConfig:
    underlying: str = DEFAULT_UNDERLYING
    allow_btc: bool = False  # named flag, default off; BTC is not cleared by 04-risk
    expansions: tuple[str, ...] = EXPANSIONS
    qty: float = 1.0
    horizon_intervals: int = 3  # funding intervals (3 × 8h = 1 day)
    ref_rate_apr: float = 0.0
    funding_sigma_bps_per_interval: float = 2.0
    hedge_rebalance_bps: float = 2.0  # C2 (same allowance as A1)
    transfer_bps: float = 0.0
    depth_mult: float = 2.0
    top_n: int = 5
    persistence_min_samples: int = 3
    persistence_min_sec: float = 60.0
    paper_fills: bool = False
    paper_extra_slip_bps: float = 5.0
    # C1
    c1_tradeable_expiries: int = 1  # nearest N dated futures are L2; the rest are hedge_note
    c1_hold: str = "to_expiry"  # to_expiry | horizon
    c1_future_settlement_bps: float = 2.0  # delivery / settlement fee placeholder
    # C2 (stub path model)
    c2_decay: float = 0.7  # per-interval decay of the current rate toward the anchor
    c2_long_run_rate: float = 0.0001  # anchor when no history (OKX baseline interest / 8h)
    c2_history_window: int = 12
    c2_basis_conflict_bps: float = 5.0
    # C3
    c3_mechanism: str = C3_MECHANISM
    c3_band_step: float = C3_BAND_STEP
    coverage_n: int = C3_COVERAGE_N
    # C4 (also applied to C1 / C2 / C3 legs)
    liquidity: LiquidityFilter = field(default_factory=LiquidityFilter)
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    buffer: pas.SafetyBuffer = field(default_factory=pas.SafetyBuffer)

    def validate(self) -> None:
        u = self.underlying.upper()
        if u == BTC_UNDERLYING and not self.allow_btc:
            raise UnderlyingNotApproved(
                "refused: BTC needs --allow-btc (named flag, default off; not cleared by 04-risk)"
            )
        if u not in (DEFAULT_UNDERLYING, BTC_UNDERLYING):
            raise UnderlyingNotApproved(f"refused: underlying {self.underlying!r} not approved")
        bad = [e for e in self.expansions if e not in EXPANSIONS]
        if bad or not self.expansions:
            raise ValueError(f"expansions must be a non-empty subset of {EXPANSIONS}, got {bad}")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.horizon_intervals < 1:
            raise ValueError("horizon_intervals must be >= 1")
        if self.persistence_min_samples < 1:
            raise ValueError("persistence_min_samples must be >= 1")
        if self.c1_tradeable_expiries < 1:
            raise ValueError("c1_tradeable_expiries must be >= 1")
        if self.c1_hold not in ("to_expiry", "horizon"):
            raise ValueError("c1_hold must be to_expiry|horizon")
        if not (0.0 <= self.c2_decay < 1.0):
            raise ValueError("c2_decay must be in [0, 1)")
        if self.c2_history_window < 1:
            raise ValueError("c2_history_window must be >= 1")
        if self.c2_basis_conflict_bps < 0:
            raise ValueError("c2_basis_conflict_bps must be >= 0")
        if self.c3_mechanism != C3_MECHANISM:
            raise ValueError(f"only {C3_MECHANISM!r} is cleared by 04-risk")
        if abs(self.c3_band_step - C3_BAND_STEP) > 1e-12:
            raise ValueError(f"c3_band_step is fixed at {C3_BAND_STEP} (one symmetric step)")
        if self.coverage_n < C3_COVERAGE_N:
            raise ValueError(f"coverage_n must be >= {C3_COVERAGE_N} (04-risk)")
        if self.buffer.calibrated:
            raise ce.CalibrationClaimRefused(
                "refused: expansion v1 is uncalibrated by construction (calibrated=false)"
            )
        self.liquidity.validate()

    def horizon_years(self, interval_sec: int) -> float:
        return self.horizon_intervals * interval_sec / (365.0 * 86400.0)

    def phase_a_config(self) -> pas.ScanConfig:
        """Phase A config for the C4 base scan (A2 / A3), Cost Engine block forced on."""
        return pas.ScanConfig(
            qty=self.qty,
            horizon_intervals=self.horizon_intervals,
            ref_rate_apr=self.ref_rate_apr,
            funding_sigma_bps_per_interval=self.funding_sigma_bps_per_interval,
            hedge_rebalance_bps=self.hedge_rebalance_bps,
            transfer_bps=self.transfer_bps,
            depth_mult=self.depth_mult,
            top_n=self.top_n,
            persistence_min_samples=self.persistence_min_samples,
            persistence_min_sec=self.persistence_min_sec,
            paper_fills=self.paper_fills,
            paper_extra_slip_bps=self.paper_extra_slip_bps,
            cost_engine=True,
            fees=self.fees,
            buffer=self.buffer,
        )

    def phase_b_config(self) -> pcs.ComboConfig:
        """Phase B config with the *default* B1 / B3 bands (coverage baseline for C3)."""
        return pcs.ComboConfig(
            qty=self.qty,
            horizon_intervals=self.horizon_intervals,
            ref_rate_apr=self.ref_rate_apr,
            funding_sigma_bps_per_interval=self.funding_sigma_bps_per_interval,
            transfer_bps=self.transfer_bps,
            depth_mult=self.depth_mult,
            top_n=self.top_n,
            persistence_min_samples=self.persistence_min_samples,
            persistence_min_sec=self.persistence_min_sec,
            paper_fills=self.paper_fills,
            paper_extra_slip_bps=self.paper_extra_slip_bps,
            cost_engine=True,
            fees=self.fees,
            buffer=pcs.ComboSafetyBuffer(
                fee_roundtrip_bps=self.buffer.fee_roundtrip_bps,
                slip_buffer_bps=self.buffer.slip_buffer_bps,
                funding_uncert_bps=self.buffer.funding_uncert_bps,
                model_haircut_bps=self.buffer.model_haircut_bps,
                calibrated=False,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "underlying": self.underlying,
            "allow_btc": self.allow_btc,
            "expansions": list(self.expansions),
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
            "paper_fills": self.paper_fills,
            "paper_extra_slip_bps": self.paper_extra_slip_bps,
            "c1": {
                "tradeable_expiries": self.c1_tradeable_expiries,
                "hold": self.c1_hold,
                "future_settlement_bps": self.c1_future_settlement_bps,
                "future_taker_bps_assumed": self.fees.perp_taker_bps,
                "fair_value_model": MODEL_STUB,
                "hedge_note_only": ["far_month_futures", "perp"],
            },
            "c2": {
                "model": MODEL_STUB,
                "decay": self.c2_decay,
                "long_run_rate": self.c2_long_run_rate,
                "history_window": self.c2_history_window,
                "basis_conflict_bps": self.c2_basis_conflict_bps,
                "annualized_used_in_net_edge": False,
            },
            "c3": {
                "mechanism": self.c3_mechanism,
                "band_step": self.c3_band_step,
                "coverage_n": self.coverage_n,
                "targets": list(C3_TARGET_FAMILIES),
            },
            "c4": {"targets": list(C4_TARGET_FAMILIES), **self.liquidity.to_dict()},
            "leverage_concept": 1,
            "cost_engine": {
                "enabled": True,
                "mandatory": True,
                "engine": ce.ENGINE,
                "version": ce.VERSION,
                "calibrated": False,
                "tradable_claim_allowed": False,
                "net_edge_source": NET_EDGE_SOURCE,
            },
            "fees": self.fees.to_dict(),
            "safety_buffer": {
                "fee_roundtrip_bps": self.buffer.fee_roundtrip_bps,
                "slip_buffer_bps": self.buffer.slip_buffer_bps,
                "funding_uncert_bps": self.buffer.funding_uncert_bps,
                "model_haircut_bps": self.buffer.model_haircut_bps,
                "calibrated": False,
            },
        }


def relax_phase_b_config(base: pcs.ComboConfig, step: float = C3_BAND_STEP) -> tuple:
    """C3: widen the Phase B B1 / B3 sampler bands by exactly one symmetric step.

    B1: Δ band [lo, hi] → [lo − step, hi + step]; moneyness fallback band likewise.
    B3: target-Δ tolerance + step (the B3 sampler is nearest-Δ; the tolerance is its band).
    Everything else — cover rule, hedge modes, cost buckets, vol_path_haircut — unchanged.
    Returns (relaxed config, documented deltas)."""
    relaxed = replace(
        base,
        b1_delta_min=round(max(0.01, base.b1_delta_min - step), 6),
        b1_delta_max=round(min(0.99, base.b1_delta_max + step), 6),
        b1_moneyness_min=round(max(0.0, base.b1_moneyness_min - step), 6),
        b1_moneyness_max=round(base.b1_moneyness_max + step, 6),
        b3_delta_tol=round(base.b3_delta_tol + step, 6),
    )
    relaxed.validate()
    deltas = {
        "mechanism": C3_MECHANISM,
        "step": step,
        "b1_delta_band": {
            "phase_b_default": [base.b1_delta_min, base.b1_delta_max],
            "relaxed": [relaxed.b1_delta_min, relaxed.b1_delta_max],
        },
        "b1_moneyness_fallback_band": {
            "phase_b_default": [base.b1_moneyness_min, base.b1_moneyness_max],
            "relaxed": [relaxed.b1_moneyness_min, relaxed.b1_moneyness_max],
        },
        "b1_max_calls_per_expiry": base.b1_max_calls_per_expiry,
        "b3_delta_tol": {"phase_b_default": base.b3_delta_tol, "relaxed": relaxed.b3_delta_tol},
        "b3_target_delta_unchanged": base.b3_target_delta,
        "b3_wing_delta_unchanged": base.b3_wing_delta,
        "strike_rung_mechanism": "not_used",
        "expiry_pm1_mechanism": "not_used",
        "cover_rule": "unchanged (B1 spot-covered call; B3 wing-covered RR; no naked short vol)",
    }
    return relaxed, deltas


# --------------------------------------------------------------------------- C4 liquidity filter


def _leg_attr(leg, key: str):
    return leg[key] if isinstance(leg, dict) else getattr(leg, key)


def liquidity_filter(
    legs: list, books: dict[str, Book], spot_ref: float, lf: LiquidityFilter
) -> tuple[bool, dict]:
    """C4 per-leg hard filter. Returns (liquidity_ok, block). Never guesses a one-sided book."""
    if spot_ref <= 0:
        raise ValueError("spot_ref must be > 0")
    per_leg: list[dict] = []
    failed: list[str] = []
    for leg in legs:
        inst = _leg_attr(leg, "instrument")
        side = _leg_attr(leg, "side")
        book = books[inst]
        reasons: list[str] = []
        half_spread_bps = None
        top_sz = None
        size_contracts = None
        notional_quote = None
        if not book.two_sided:
            reasons.append("one_sided_book")
        else:
            conv = spot_ref if book.premium_ccy == "base" else 1.0
            half_spread_quote = (book.best_ask - book.best_bid) / 2.0 * conv
            half_spread_bps = round(bps(half_spread_quote, spot_ref), 4)
            top_sz = book.side_levels(side)[0].sz
            size_contracts = round(top_sz / lf.contract_size_base, 6)
            notional_quote = round(top_sz * spot_ref, 6)
            if half_spread_bps > lf.max_half_spread_bps:
                reasons.append("half_spread_gt_max")
            if size_contracts < lf.min_size_contracts:
                reasons.append("size_lt_min_contracts")
            if notional_quote < lf.min_notional_quote:
                reasons.append("notional_lt_min")
        ok = not reasons
        if not ok:
            failed.append(inst)
        per_leg.append(
            {
                "instrument": inst,
                "side": side,
                "half_spread_bps": half_spread_bps,
                "top_size_base": top_sz,
                "size_contracts": size_contracts,
                "notional_quote": notional_quote,
                "ok": ok,
                "reasons": reasons,
            }
        )
    all_ok = not failed
    return all_ok, {
        "ok": all_ok,
        **lf.to_dict(),
        "spot_ref_for_basis": round(spot_ref, 6),
        "legs": per_leg,
        "failed_legs": failed,
        "rule": "every leg: half_spread_bps <= max AND top size >= min contracts AND "
        "notional >= min; fail → liquidity_ok=false (never passes) or skipped",
    }


# --------------------------------------------------------------------------- C2 stub funding path


def funding_expectation_stub(fr: Funding, H: int, cfg: ExpansionConfig) -> dict:
    """Stub `E[funding]` path over H intervals (model=stub, calibrated=false).

    Per-interval received rate f_h = f_used · decay^h + f_anchor · (1 − decay^h), h = 1..H, where
    f_used follows the Cost Engine FundingLeg rule (min(|now|, |next|) same sign, predicted flip
    → 0) and f_anchor is the mean of the last `c2_history_window` settled rates (else the config
    long-run rate). The expectation is Σ_h f_h — a hold-horizon number, never × 365. Uncertainty
    = σ_history · H + |now − next| · H (rate terms of notional)."""
    receiver = "short_perp" if fr.rate >= 0 else "long_perp"
    leg = ce.FundingLeg(
        rate_now=fr.rate,
        horizon_intervals=H,
        interval_sec=fr.interval_sec,
        rate_next=fr.next_rate,
        receiver=receiver,
    )
    used, flags = leg.used_rate_per_interval()
    hist = list(fr.history[-cfg.c2_history_window :]) if fr.history else []
    if hist:
        anchor_published = statistics.fmean(hist)
        anchor_source = f"history_mean_last_{len(hist)}"
        sigma_rate = statistics.pstdev(hist) if len(hist) >= 2 else 0.0
        sigma_source = "history_pstdev" if len(hist) >= 2 else "single_sample_zero"
    else:
        anchor_published = cfg.c2_long_run_rate
        anchor_source = "config_long_run_rate"
        sigma_rate = cfg.funding_sigma_bps_per_interval / 1e4
        sigma_source = "config_funding_sigma_bps"
    anchor_received = anchor_published if receiver == "short_perp" else -anchor_published
    path = [
        used * cfg.c2_decay**h + anchor_received * (1.0 - cfg.c2_decay**h) for h in range(1, H + 1)
    ]
    expected = sum(path)
    pred_gap = leg.pred_gap_rate()
    uncertainty = sigma_rate * H + pred_gap * H
    ipy = leg.intervals_per_year
    return {
        "model": MODEL_STUB,
        "calibrated": False,
        "receiver": receiver,
        "rate_now_published": fr.rate,
        "rate_next_published": fr.next_rate,
        "rate_used_per_interval_received": used,
        "rate_used_rule": "cost_engine.FundingLeg: min(|now|,|next|) same sign; flip → 0",
        "anchor_rate_published": round(anchor_published, 10),
        "anchor_rate_received": round(anchor_received, 10),
        "anchor_source": anchor_source,
        "history_samples": len(hist),
        "decay_per_interval": cfg.c2_decay,
        "intervals": H,
        "interval_sec": fr.interval_sec,
        "path_per_interval_received": [round(x, 10) for x in path],
        "funding_expected_rate_hold_horizon": round(expected, 10),
        "funding_expected_bps": round(expected * 1e4, 4),
        "funding_uncertainty_rate_hold_horizon": round(uncertainty, 10),
        "funding_uncertainty_bps": round(uncertainty * 1e4, 4),
        "sigma_rate_per_interval": round(sigma_rate, 10),
        "sigma_source": sigma_source,
        "pred_gap_rate": round(pred_gap, 10),
        "annualized_used_in_net_edge": False,
        "current_funding_annualized_ref": {
            "bps": round(fr.rate * ipy * 1e4, 4),
            "intervals_per_year": ipy,
            "display_only": True,
            "return_promise": False,
            "is_net_edge": False,
            "banned_from_net_edge": True,
            "note": "current × intervals/year — the narrative Cost kill 001 refuted; shown only "
            "to make the ban auditable",
        },
        "flags": flags,
        "note": "stub path model; calibrated=false; replace with the Funding expectation module "
        "(roadmap P2) when it lands under its own 04-risk",
    }


def basis_confirmation(spot: Book, perp: Book, expected_published: float, tol_bps: float) -> dict:
    """C2: same-window basis must not oppose the expected carry beyond `tol_bps`.

    `expected_published` is the expected funding in the *published* sign convention (positive =
    longs pay shorts): positive expected funding ↔ perp should trade at / above spot; negative ↔
    at / below. A basis of the opposite sign beyond tolerance is a conflict."""
    s_mid = (spot.best_bid + spot.best_ask) / 2.0
    p_mid = (perp.best_bid + perp.best_ask) / 2.0
    basis_mid_bps = bps(p_mid - s_mid, s_mid)
    expected_sign = 1 if expected_published >= 0 else -1
    if abs(basis_mid_bps) <= tol_bps:
        status, conflict = "within_tolerance_neutral", False
    elif (basis_mid_bps > 0) == (expected_sign > 0):
        status, conflict = "confirmed_same_direction", False
    else:
        status, conflict = "conflict_opposite_direction", True
    return {
        "basis_mid_ref_bps": round(basis_mid_bps, 4),
        "basis_mid_ref_only": True,
        "expected_funding_sign_published": expected_sign,
        "tolerance_bps": tol_bps,
        "status": status,
        "confirmed": not conflict,
        "conflict": conflict,
        "rule": "opposite sign beyond tolerance → basis_conflict_with_funding_expectation "
        "(invalidated, never passes)",
    }


# --------------------------------------------------------------------------- record


def _books_of(snap: Snapshot) -> dict[str, Book]:
    books: dict[str, Book] = {}
    for b in (snap.spot, snap.perp, *snap.options, *snap.futures):
        if b is not None:
            books[b.inst_id] = b
    return books


def _spot_mid(spot: Book) -> float:
    return (spot.best_bid + spot.best_ask) / 2.0


def build_expansion_record(
    *,
    snap: Snapshot,
    expansion_id: str,
    family: str,
    legs: list[Leg],
    books: dict[str, Book],
    gross_quote: float,
    notional_quote: float,
    costs_quote: dict[str, float],
    margin_capital: float,
    risk_flags: list[str],
    residual_risks: list[str],
    model: str,
    cfg: ExpansionConfig,
    tracker: PersistenceTracker,
    persistence_key: str,
    coverage: Counter,
    extra: dict,
    funding_ctx: ce.FundingContext | None = None,
    hold_years: float | None = None,
    hedge_note: list[dict] | None = None,
) -> dict:
    """Native C1 / C2 record. gross / all-in / net edge are read *from* Cost Engine v0
    `evaluate()` — the record has no other net-edge arithmetic."""
    if expansion_id not in (EXP_C1, EXP_C2):
        raise ExpansionSchemaViolation("build_expansion_record is for native C1 / C2 only")
    if len(legs) > MAX_LEGS[family]:
        raise ExpansionSchemaViolation(f"{family}: {len(legs)} legs > {MAX_LEGS[family]}")
    pcs.assert_covered_short_options(legs)

    a_cfg = cfg.phase_a_config()
    liquidity, liq_flags, impact_quote = liquidity_check(legs, books, cfg.qty, a_cfg)
    costs_quote = dict(costs_quote)
    costs_quote["impact"] = impact_quote
    res = ce.evaluate(
        gross_quote=gross_quote,
        notional_quote=notional_quote,
        components_quote={k: costs_quote.get(k, 0.0) for k in COST_KEYS},
        hold_years=hold_years,
        funding=funding_ctx,
        gross_basis=ce.EDGE_BASIS,
        notes=[f"{expansion_id} {family}: hold-horizon bid/ask gross; stub refs not priced"],
    )
    engine_block = res.to_dict()
    costs_bps = {k: engine_block["components_bps"][k] for k in COST_KEYS}
    costs_bps["total"] = engine_block["all_in_cost_bps"]
    gross_bps = engine_block["gross_edge_bps"]
    net_bps = engine_block["net_edge_bps"]

    spot = next(b for b in books.values() if b.kind == "spot")
    lf_ok, lf_block = liquidity_filter(legs, books, _spot_mid(spot), cfg.liquidity)
    buffer = cfg.buffer.resolve(costs_bps["fees"])
    flags = set(risk_flags) | set(liq_flags) | {"relative_value_not_riskless"}
    if not lf_ok:
        flags.add(FLAG_C4_ILLIQUID)
    flags = sorted(flags)
    invalidated = sorted(set(flags) & INVALIDATING_FLAGS)
    persistence = tracker.update(persistence_key, snap.ts_ms, net_bps, buffer["total_bps"])
    edge_exceeds_buffer = net_bps > buffer["total_bps"]
    passes = bool(
        edge_exceeds_buffer and persistence["ok"] and liquidity["ok"] and lf_ok and not invalidated
    )
    leg_insts = {leg.instrument for leg in legs}
    executable_prices = {
        inst: {"bid": b.best_bid, "ask": b.best_ask, "mark_ref_only": b.mark_px}
        for inst, b in books.items()
        if inst in leg_insts
    }
    coverage[expansion_id] += 1
    residual = sorted(set(RESIDUAL_RISKS[family]) | set(residual_risks))
    rec: dict = {
        "timestamp": iso_cst(snap.ts_ms),
        "ts_ms": snap.ts_ms,
        "venue": snap.venue,
        "mode": MODE,
        "phase": PHASE,
        "base_phase": None,
        "expansion_id": expansion_id,
        "expansion_name": EXPANSION_NAME[expansion_id],
        "family": family,
        "combo_id": expansion_id,
        "base_combo_id": None,
        "base_family": None,
        "taxonomy": TAXONOMY,
        "taxonomy_base": TAXONOMY,
        "hedge_mode": HEDGE_STATIC,
        "hypothesis_id": HYPOTHESIS[expansion_id],
        "instruments": sorted(leg_insts),
        "legs": [leg.to_dict() for leg in legs],
        "tradeable_legs": len(legs),
        "max_tradeable_legs": MAX_LEGS[family],
        "hedge_note": hedge_note or [],
        "cover": [],
        "executable_prices": executable_prices,
        "notional_quote": round(notional_quote, 6),
        "notional_basis": extra.pop("notional_basis", "spot_ask_x_qty"),
        "gross_edge_bps": gross_bps,
        "costs_bps": costs_bps,
        "net_edge_bps": net_bps,
        "net_edge_source": NET_EDGE_SOURCE,
        "safety_buffer_bps": buffer["total_bps"],
        "safety_buffer": buffer,
        "edge_exceeds_buffer": edge_exceeds_buffer,
        "passes_threshold": passes,
        "invalidated_by": invalidated,
        "margin_capital_required": round(margin_capital, 6),
        "leverage_concept": 1,
        "persistence": persistence,
        "liquidity": liquidity,
        "liquidity_ok": lf_ok,
        "liquidity_filter": lf_block,
        "risk_flags": flags,
        "residual_risks": residual,
        "model": model,
        "calibrated": False,
        "sample_coverage": {
            "family_key": expansion_id,
            "n_in_window_so_far": coverage[expansion_id],
            "gate_n": cfg.coverage_n,
            "gate_met_so_far": coverage[expansion_id] >= cfg.coverage_n,
        },
        "cost_engine": engine_block,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "related_scanners": RELATED_SCANNERS,
        "approvals": APPROVALS,
    }
    rec.update(extra)
    if cfg.paper_fills:
        rec["paper_fill"] = pas.paper_fill(legs, books, notional_quote / cfg.qty, net_bps, a_cfg)
    return finalize_expansion_record(rec)


def wrap_inherited_record(
    rec: dict,
    *,
    expansion_id: str,
    books: dict[str, Book],
    spot_ref: float,
    cfg: ExpansionConfig,
    coverage: Counter,
    extra: dict,
) -> dict:
    """C3 / C4: tag an already-finalised Phase A / B record as an expansion record, apply the
    C4 liquidity filter, relabel taxonomy to relative_value (original kept in `taxonomy_base`).
    The base record's cost buckets / cost_engine block are inherited untouched."""
    if expansion_id not in (EXP_C3, EXP_C4):
        raise ExpansionSchemaViolation("wrap_inherited_record is for C3 / C4 only")
    base_family = rec["family"]
    targets = C3_TARGET_FAMILIES if expansion_id == EXP_C3 else C4_TARGET_FAMILIES
    if base_family not in targets:
        raise ExpansionSchemaViolation(f"{expansion_id} does not target {base_family}")
    if "cost_engine" not in rec:
        raise ExpansionSchemaViolation("inherited record lacks the cost_engine block")
    lf_ok, lf_block = liquidity_filter(rec["legs"], books, spot_ref, cfg.liquidity)
    base_combo = BASE_COMBO_ID[base_family]
    flags = set(rec["risk_flags"]) | {"relative_value_not_riskless"}
    invalidated = set(rec.get("invalidated_by", ()))
    if not lf_ok:
        flags.add(FLAG_C4_ILLIQUID)
        invalidated.add(FLAG_C4_ILLIQUID)
    coverage[base_combo] += 1
    out = {
        **rec,
        "phase": PHASE,
        "base_phase": rec.get("phase", "A"),
        "expansion_id": expansion_id,
        "expansion_name": EXPANSION_NAME[expansion_id],
        "combo_id": f"{expansion_id}-{base_combo}",
        "base_combo_id": base_combo,
        "base_family": base_family,
        "taxonomy": TAXONOMY,
        "taxonomy_base": rec["taxonomy"],
        "hedge_mode": rec.get("hedge_mode", HEDGE_STATIC),
        "hypothesis_id": HYPOTHESIS[expansion_id],
        "base_hypothesis_id": rec["hypothesis_id"],
        "tradeable_legs": len(rec["legs"]),
        "max_tradeable_legs": MAX_LEGS[base_family],
        "residual_risks": sorted(
            set(rec.get("residual_risks", ())) | set(RESIDUAL_RISKS.get(base_family, ()))
        ),
        "risk_flags": sorted(flags),
        "invalidated_by": sorted(invalidated),
        "liquidity_ok": lf_ok,
        "liquidity_filter": lf_block,
        "base_passes_threshold": rec["passes_threshold"],
        "passes_threshold": bool(rec["passes_threshold"] and lf_ok),
        "model": MODEL_INHERITED_B if expansion_id == EXP_C3 else MODEL_INHERITED_A,
        "calibrated": False,
        "net_edge_source": NET_EDGE_SOURCE,
        "sample_coverage": {
            "family_key": base_combo,
            "n_in_window_so_far": coverage[base_combo],
            "gate_n": cfg.coverage_n,
            "gate_met_so_far": coverage[base_combo] >= cfg.coverage_n,
        },
        "related_scanners": RELATED_SCANNERS,
        "approvals": APPROVALS,
        "expansion": extra,
    }
    return finalize_expansion_record(out)


def finalize_expansion_record(rec: dict) -> dict:
    """Force the observe-only constants and validate the C1–C4 schema / gates. Raises rather
    than emits."""
    if rec.get("action") != ACTION or rec.get("will_send_http") is not False:
        raise ObserveOnlyViolation(
            f"refused: action={rec.get('action')!r} will_send_http={rec.get('will_send_http')!r}"
        )
    missing = [k for k in REQUIRED_FIELDS if k not in rec]
    if missing:
        raise ExpansionSchemaViolation(f"record missing required fields: {missing}")
    if rec["phase"] != PHASE:
        raise ExpansionSchemaViolation(f"phase must be {PHASE!r}, got {rec['phase']!r}")
    eid = rec["expansion_id"]
    if eid not in EXPANSIONS:
        raise ExpansionSchemaViolation(f"unknown expansion_id {eid!r}")
    if not str(rec["combo_id"]).startswith(eid):
        raise ExpansionSchemaViolation(f"combo_id {rec['combo_id']!r} must start with {eid}")
    if rec["taxonomy"] != TAXONOMY:
        raise ExpansionSchemaViolation(f"taxonomy must be {TAXONOMY!r}, got {rec['taxonomy']!r}")
    if rec["hypothesis_id"] != HYPOTHESIS[eid]:
        raise ExpansionSchemaViolation("hypothesis_id does not match expansion_id")
    if rec["calibrated"] is not False:
        raise ce.CalibrationClaimRefused("refused: expansion records are calibrated=false")
    if rec["net_edge_source"] != NET_EDGE_SOURCE:
        raise ExpansionSchemaViolation("net_edge_source must be the cost engine")
    if rec["leverage_concept"] != 1:
        raise ExpansionSchemaViolation("leverage_concept must be 1")
    if "relative_value_not_riskless" not in rec["risk_flags"]:
        raise ExpansionSchemaViolation("risk_flags must carry relative_value_not_riskless")
    if not rec["residual_risks"]:
        raise ExpansionSchemaViolation("residual_risks must not be empty")

    legs = rec["legs"]
    fam = rec["family"]
    if len(legs) > MAX_LEGS.get(fam, 4):
        raise ExpansionSchemaViolation(f"{fam}: {len(legs)} legs exceed {MAX_LEGS.get(fam, 4)}")
    for leg in legs:
        if leg["price_type"] not in pas.EXECUTABLE_PRICE_TYPES:
            raise ExecutablePriceViolation(f"refused: leg price_type {leg['price_type']!r}")
        if leg["price_type"] != pas.SIDE_TO_PRICE_TYPE[leg["side"]]:
            raise ExecutablePriceViolation(f"refused: {leg['side']} @ {leg['price_type']}")
    pcs.assert_covered_short_options(legs, b1_spot_cover=(rec.get("base_family") == pcs.FAMILY_B1))

    if eid in (EXP_C1, EXP_C2):
        if fam not in (FAMILY_C1, FAMILY_C2) or rec["model"] != MODEL_STUB:
            raise ExpansionSchemaViolation(f"{eid} must be a native stub-model record")
        if len(legs) > 2:
            raise ExpansionSchemaViolation(f"{eid}: at most 2 tradeable legs")
        for note in rec.get("hedge_note", ()):
            if note.get("tradeable_default") is not False:
                raise ExpansionSchemaViolation("hedge_note entries must be tradeable_default=false")
        missing_rr = sorted(set(RESIDUAL_RISKS[fam]) - set(rec["residual_risks"]))
        if missing_rr:
            raise ExpansionSchemaViolation(f"{fam}: residual_risks missing {missing_rr}")
        short_spot = any(leg["role"] == "spot" and leg["side"] == "sell" for leg in legs)
        if short_spot and not set(SHORT_SPOT_RESIDUALS) <= set(rec["residual_risks"]):
            raise ExpansionSchemaViolation("short spot requires spot_borrow residual risks")
    if eid == EXP_C1:
        fv = rec.get("fair_value") or {}
        if fv.get("model") != MODEL_STUB or fv.get("calibrated") is not False:
            raise ExpansionSchemaViolation("C1 fair_value must be model=stub, calibrated=false")
        if fv.get("used_in_net_edge") is not False:
            raise ExpansionSchemaViolation("C1 fair_value is anchor_ref only")
    if eid == EXP_C2:
        fm = rec.get("funding_model") or {}
        if fm.get("model") != MODEL_STUB or fm.get("calibrated") is not False:
            raise ExpansionSchemaViolation("C2 funding_model must be model=stub, calibrated=false")
        if fm.get("annualized_used_in_net_edge") is not False:
            raise ce.NaiveAnnualizationRefused("refused: annualised funding in C2 net edge")
        bc = rec.get("basis_confirmation") or {}
        if bc.get("conflict") and FLAG_BASIS_CONFLICT not in rec["invalidated_by"]:
            raise ExpansionSchemaViolation("C2 basis conflict must invalidate the record")
    if eid == EXP_C3 and rec.get("base_family") not in C3_TARGET_FAMILIES:
        raise ExpansionSchemaViolation("C3 targets B1 / B3 only")
    if eid == EXP_C4 and rec.get("base_family") not in C4_TARGET_FAMILIES:
        raise ExpansionSchemaViolation("C4 targets A2 / A3 only")

    if rec["liquidity_ok"] is not True:
        if rec["passes_threshold"] is not False:
            raise ExpansionSchemaViolation("liquidity_ok=false can never pass")
        if FLAG_C4_ILLIQUID not in rec["risk_flags"]:
            raise ExpansionSchemaViolation("illiquid record must carry c4_illiquid_leg")
    if rec["passes_threshold"] and rec["invalidated_by"]:
        raise ExpansionSchemaViolation("invalidated record can never pass")

    ceb = rec["cost_engine"]
    if ceb.get("action") != ACTION or ceb.get("will_send_http") is not False:
        raise ObserveOnlyViolation("refused: cost_engine block is not observe_only")
    if ceb.get("tradable_claim_allowed") is not False or ceb.get("annualized") is not False:
        raise ExpansionSchemaViolation("refused: cost_engine block claims tradable / annualised")
    if ceb.get("edge_basis") != ce.EDGE_BASIS or ceb.get("calibrated") is not False:
        raise ExpansionSchemaViolation(
            "refused: cost_engine block must be hold-horizon and uncalibrated"
        )
    if abs(ceb["all_in_cost_bps"] - rec["costs_bps"]["total"]) > 1e-6:
        raise ExpansionSchemaViolation("cost_engine.all_in_cost_bps must equal costs_bps.total")
    if abs(ceb["net_edge_bps"] - rec["net_edge_bps"]) > 1e-6:
        raise ExpansionSchemaViolation("net_edge_bps must equal cost_engine.net_edge_bps")
    if abs(ceb["gross_edge_bps"] - rec["gross_edge_bps"]) > 1e-6:
        raise ExpansionSchemaViolation("gross_edge_bps must equal cost_engine.gross_edge_bps")

    text = json.dumps(rec, ensure_ascii=False).lower()
    for bad in FORBIDDEN_LABELS:
        if bad in text:
            raise ForbiddenLabelViolation(f"refused: forbidden label {bad!r} in record")
    return rec


# --------------------------------------------------------------------------- scanner


class ExpansionScanner:
    def __init__(self, cfg: ExpansionConfig, tracker: PersistenceTracker | None = None):
        cfg.validate()
        self.cfg = cfg
        self.tracker = tracker or PersistenceTracker(
            cfg.persistence_min_samples, cfg.persistence_min_sec
        )
        self.skipped: Counter = Counter()
        self.coverage: Counter = Counter()  # family_key → emitted expansion records
        self.coverage_baseline: Counter = Counter()  # B1 / B3 under Phase B defaults
        self._a = pas.ArbScanner(cfg.phase_a_config())
        b_base = cfg.phase_b_config()
        self._b_baseline = pcs.ComboScanner(b_base)
        relaxed, self.relaxation = relax_phase_b_config(b_base, cfg.c3_band_step)
        self._b_relaxed = pcs.ComboScanner(relaxed)
        self.relaxed_phase_b_config = relaxed

    # ---- shared

    def _assert_underlying(self, snap: Snapshot) -> None:
        want = self.cfg.underlying.upper()
        insts = [b.inst_id for b in (snap.spot, snap.perp, *snap.options, *snap.futures) if b]
        bad = [i for i in insts if i.split("-")[0].upper() != want]
        if bad:
            raise UnderlyingNotApproved(
                f"refused: snapshot carries non-{want} instruments {bad[:3]} "
                "(default ETH; BTC needs --allow-btc)"
            )

    def scan(self, snap: Snapshot) -> list[dict]:
        self._assert_underlying(snap)
        out: list[dict] = []
        if EXP_C1 in self.cfg.expansions:
            out.extend(self.scan_c1(snap))
        if EXP_C2 in self.cfg.expansions:
            out.extend(self.scan_c2(snap))
        if EXP_C3 in self.cfg.expansions:
            out.extend(self.scan_c3(snap))
        if EXP_C4 in self.cfg.expansions:
            out.extend(self.scan_c4(snap))
        return out

    # ---- C1 spot vs dated future basis / term-structure curve

    def _c1_curve(self, snap: Snapshot, futs: list[Book], spot: Book) -> list[dict]:
        cfg = self.cfg
        s_mid = _spot_mid(spot)
        curve: list[dict] = []
        for idx, f in enumerate(futs):
            T = years_between(snap.ts_ms, f.expiry_ms)
            mid = (f.best_bid + f.best_ask) / 2.0 if f.two_sided else None
            fair = s_mid * math.exp(cfg.ref_rate_apr * T)
            basis_bps = bps(mid - s_mid, s_mid) if mid is not None else None
            curve.append(
                {
                    "instrument": f.inst_id,
                    "kind": "dated_future",
                    "expiry": datetime.fromtimestamp(f.expiry_ms / 1000, UTC).isoformat(),
                    "T_years": round(T, 6),
                    "mid_ref": round(mid, 6) if mid is not None else None,
                    "mark_ref": f.mark_px,
                    "fair_value_ref": round(fair, 6),
                    "basis_mid_ref_bps": round(basis_bps, 4) if basis_bps is not None else None,
                    "mid_minus_fair_ref_bps": round(bps(mid - fair, s_mid), 4)
                    if mid is not None
                    else None,
                    "basis_apr_ref": ce.apy_ref(basis_bps, T)
                    if (basis_bps is not None and T > 0)
                    else None,
                    "role": "L2_tradeable" if idx < cfg.c1_tradeable_expiries else "hedge_note",
                    "tradeable_default": idx < cfg.c1_tradeable_expiries,
                }
            )
        perp = snap.perp
        if perp is not None and perp.two_sided:
            p_mid = (perp.best_bid + perp.best_ask) / 2.0
            curve.append(
                {
                    "instrument": perp.inst_id,
                    "kind": "perp_proxy",
                    "expiry": None,
                    "T_years": 0.0,
                    "mid_ref": round(p_mid, 6),
                    "mark_ref": perp.mark_px,
                    "fair_value_ref": round(s_mid, 6),
                    "basis_mid_ref_bps": round(bps(p_mid - s_mid, s_mid), 4),
                    "mid_minus_fair_ref_bps": round(bps(p_mid - s_mid, s_mid), 4),
                    "basis_apr_ref": None,
                    "role": "hedge_note",
                    "tradeable_default": False,
                }
            )
        return curve

    def scan_c1(self, snap: Snapshot) -> list[dict]:
        spot = snap.spot
        if spot is None or not spot.two_sided:
            self.skipped["c1_missing_spot"] += 1
            return []
        futs = sorted(
            (
                f
                for f in snap.futures
                if f.expiry_ms is not None and years_between(snap.ts_ms, f.expiry_ms) > 0
            ),
            key=lambda b: b.expiry_ms,
        )
        if not futs:
            self.skipped["c1_no_dated_futures"] += 1
            return []
        curve = self._c1_curve(snap, futs, spot)
        out: list[dict] = []
        for idx, f in enumerate(futs[: self.cfg.c1_tradeable_expiries]):
            if not f.two_sided:
                self.skipped["c1_future_one_sided"] += 1
                continue
            rec = self._c1_record(snap, spot, f, curve, idx)
            if rec is not None:
                out.append(rec)
        return out

    def _c1_record(
        self, snap: Snapshot, spot: Book, fut: Book, curve: list[dict], idx: int
    ) -> dict | None:
        cfg = self.cfg
        qty = cfg.qty
        T = years_between(snap.ts_ms, fut.expiry_ms)
        s_mid, f_mid = _spot_mid(spot), (fut.best_bid + fut.best_ask) / 2.0
        carry = f_mid >= s_mid
        residual: list[str] = []
        flags = [
            "basis_risk",
            "settlement_index_mismatch_possible",
            "leverage_1x_margin_still_applies",
            "fair_value_model_stub_uncalibrated",
            "no_perp_leg_no_funding_exposure",
        ]
        borrow_q = 0.0
        if carry:
            spot_leg = make_leg(spot, "buy", qty, "spot")
            fut_leg = make_leg(fut, "sell", qty, "future")
            direction = "long_spot_short_future_cash_and_carry"
            gross_q = (fut.best_bid - spot.best_ask) * qty
        else:
            spot_leg = make_leg(spot, "sell", qty, "spot")
            fut_leg = make_leg(fut, "buy", qty, "future")
            direction = "short_spot_long_future_reverse_cash_and_carry"
            gross_q = (spot.best_bid - fut.best_ask) * qty
            flags.append("requires_spot_borrow")
            residual.extend(SHORT_SPOT_RESIDUALS)
        if spot_leg is None or fut_leg is None:
            return None
        legs = [spot_leg, fut_leg]
        N = spot.best_ask * qty
        interval_sec = (
            snap.funding.interval_sec if snap.funding else ce.DEFAULT_FUNDING_INTERVAL_SEC
        )
        horizon_years = cfg.horizon_years(interval_sec)
        to_expiry = cfg.c1_hold == "to_expiry" or T <= horizon_years
        hold_years = T if to_expiry else horizon_years
        if not carry:
            if snap.spot_borrow_apr is None:
                flags.append("borrow_unavailable")
            else:
                borrow_q = N * snap.spot_borrow_apr * hold_years
        if bps(gross_q, N) < 0:
            flags.append("adverse_entry_basis")
        fees = cfg.fees
        fut_fee_bps = fees.perp_taker_bps  # dated-future taker assumed equal to perp taker
        fees_q = N * (2 * fees.spot_taker_bps + fut_fee_bps) / 1e4
        half_spread_q = qty * (spot.best_ask - spot.best_bid) / 2.0  # spot exit crossing
        if to_expiry:
            fees_q += N * cfg.c1_future_settlement_bps / 1e4
            exit_rule = "future_settles_at_expiry_spot_exits_crossing_spread"
        else:
            fees_q += N * fut_fee_bps / 1e4
            half_spread_q += qty * (fut.best_ask - fut.best_bid) / 2.0
            exit_rule = "both_legs_exit_at_horizon_crossing_spread"
        margin = 2 * N  # spot notional + future margin at 1x
        costs = {
            "fees": fees_q,
            "half_spread_slip": half_spread_q,
            "hedge_rebalance": 0.0,
            "borrow": borrow_q,
            "transfer": N * cfg.transfer_bps / 1e4,
            "capital_opp": margin * cfg.ref_rate_apr * hold_years,
            "funding_expected": 0.0,
            "funding_uncertainty": 0.0,
        }
        fair = s_mid * math.exp(cfg.ref_rate_apr * T)
        extra = {
            "direction": direction,
            "future": {
                "instrument": fut.inst_id,
                "expiry_ms": fut.expiry_ms,
                "expiry": datetime.fromtimestamp(fut.expiry_ms / 1000, UTC).isoformat(),
                "T_years": round(T, 6),
                "bid": fut.best_bid,
                "ask": fut.best_ask,
                "mark_ref_only": fut.mark_px,
                "curve_index": idx,
                "settle_ccy": fut.settle_ccy,
            },
            "basis": {
                "exec_bps": round(bps(gross_q, N), 4),
                "exec_definition": "sell@bid − buy@ask across the two legs, of spot ask notional",
                "mid_ref_bps": round(bps(f_mid - s_mid, s_mid), 4),
                "mid_ref_only": True,
                "basis_apr_ref": (
                    ce.apy_ref(bps(gross_q, N), hold_years) if hold_years > 0 else None
                ),
            },
            "fair_value": {
                "model": MODEL_STUB,
                "calibrated": False,
                "formula": "S_mid × exp(ref_rate_apr × T)",
                "ref_rate_apr": cfg.ref_rate_apr,
                "anchor_ref": "spot_mid",
                "S_mid_ref": round(s_mid, 6),
                "fair_ref": round(fair, 6),
                "F_mid_ref": round(f_mid, 6),
                "F_mid_minus_fair_ref_bps": round(bps(f_mid - fair, s_mid), 4),
                "used_in_net_edge": False,
                "note": "stub carry model, anchor_ref only; replace with the Fair-value / basis "
                "module (roadmap P1) under its own 04-risk",
            },
            "term_structure_curve_ref": curve,
            "hold": {
                "rule": cfg.c1_hold,
                "hold_to_expiry": to_expiry,
                "hold_years": round(hold_years, 6),
                "horizon_years": round(horizon_years, 6),
                "exit_rule": exit_rule,
                "future_taker_bps_assumed": fut_fee_bps,
                "future_settlement_bps": cfg.c1_future_settlement_bps if to_expiry else 0.0,
            },
            "invalidation": [
                "settlement_index_mismatch",
                "near_far_liquidity_asymmetry",
                "roll_cost_underestimated",
                "borrow_unavailable_or_spike",
                "gap",
                "depth_collapse",
            ],
        }
        hedge_note = [c for c in curve if not c["tradeable_default"]]
        return build_expansion_record(
            snap=snap,
            expansion_id=EXP_C1,
            family=FAMILY_C1,
            legs=legs,
            books={spot.inst_id: spot, fut.inst_id: fut},
            gross_quote=gross_q,
            notional_quote=N,
            costs_quote=costs,
            margin_capital=margin,
            risk_flags=flags,
            residual_risks=residual,
            model=MODEL_STUB,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_C1}|{spot.inst_id}|{fut.inst_id}|{direction}",
            coverage=self.coverage,
            extra=extra,
            hold_years=hold_years,
            hedge_note=hedge_note,
        )

    # ---- C2 funding expectation path + basis confirmation

    def scan_c2(self, snap: Snapshot) -> list[dict]:
        cfg = self.cfg
        spot, perp, fr = snap.spot, snap.perp, snap.funding
        if spot is None or perp is None or not (spot.two_sided and perp.two_sided):
            self.skipped["c2_missing_spot_or_perp"] += 1
            return []
        if fr is None:
            self.skipped["c2_no_funding"] += 1
            return []
        qty = cfg.qty
        H = cfg.horizon_intervals
        model = funding_expectation_stub(fr, H, cfg)
        expected = model["funding_expected_rate_hold_horizon"]
        short_perp = model["receiver"] == "short_perp"
        residual: list[str] = []
        flags = [
            "funding_path_risk",
            "basis_risk",
            "leverage_1x_margin_still_applies",
            "funding_model_stub_uncalibrated",
        ] + list(model["flags"])
        if short_perp:
            spot_leg = make_leg(spot, "buy", qty, "spot")
            perp_leg = make_leg(perp, "sell", qty, "perp")
            direction = "long_spot_short_perp"
            basis_exec_bps = bps(perp.best_bid - spot.best_ask, spot.best_ask)
        else:
            spot_leg = make_leg(spot, "sell", qty, "spot")
            perp_leg = make_leg(perp, "buy", qty, "perp")
            direction = "short_spot_long_perp"
            basis_exec_bps = bps(spot.best_bid - perp.best_ask, spot.best_ask)
            flags.append("requires_spot_borrow")
            residual.extend(SHORT_SPOT_RESIDUALS)
        if spot_leg is None or perp_leg is None:
            return []
        legs = [spot_leg, perp_leg]
        N = spot.best_ask * qty
        hold_years = cfg.horizon_years(fr.interval_sec)
        funding_gross_q = max(expected, 0.0) * N
        funding_cost_q = max(-expected, 0.0) * N
        if expected < 0:
            flags.append("funding_expected_path_negative_for_position")
        if basis_exec_bps < 0:
            flags.append("adverse_entry_basis")
        basis_credit_bps = basis_exec_bps if basis_exec_bps < 0 else 0.0  # adverse only
        gross_q = funding_gross_q + basis_credit_bps / 1e4 * N
        expected_published = expected if short_perp else -expected
        conf = basis_confirmation(spot, perp, expected_published, cfg.c2_basis_conflict_bps)
        if conf["conflict"]:
            flags.append(FLAG_BASIS_CONFLICT)
        borrow_q = 0.0
        if not short_perp:
            if snap.spot_borrow_apr is None:
                flags.append("borrow_unavailable")
            else:
                borrow_q = N * snap.spot_borrow_apr * hold_years
        fees = cfg.fees
        fees_q = N * (2 * fees.spot_taker_bps + 2 * fees.perp_taker_bps) / 1e4
        half_spread_q = qty * (
            (spot.best_ask - spot.best_bid) / 2 + (perp.best_ask - perp.best_bid) / 2
        )
        margin = 2 * N
        costs = {
            "fees": fees_q,
            "half_spread_slip": half_spread_q,
            "hedge_rebalance": N * cfg.hedge_rebalance_bps / 1e4,
            "borrow": borrow_q,
            "transfer": N * cfg.transfer_bps / 1e4,
            "capital_opp": margin * cfg.ref_rate_apr * hold_years,
            "funding_expected": funding_cost_q,
            "funding_uncertainty": model["funding_uncertainty_rate_hold_horizon"] * N,
        }
        extra = {
            "direction": direction,
            "funding_model": model,
            "funding_expected": model["funding_expected_bps"],
            "funding_uncertainty": model["funding_uncertainty_bps"],
            "basis_confirmation": conf,
            "basis_entry_bps": round(basis_exec_bps, 4),
            "basis_credited_bps": round(basis_credit_bps, 4),
            "hold": {
                "horizon_intervals": H,
                "interval_sec": fr.interval_sec,
                "hold_years": round(hold_years, 6),
                "exit_rule": "both_legs_exit_at_horizon_crossing_spread",
            },
            "a1_relationship": "narrative correction of A1 (path expectation), not a size-up",
            "invalidation": [
                "funding_sign_flip",
                "basis_conflict_with_funding_expectation",
                "basis_blowout",
                "holding_window_vs_settlement_misalignment",
                "margin_stress",
                "depth_collapse",
            ],
        }
        rec = build_expansion_record(
            snap=snap,
            expansion_id=EXP_C2,
            family=FAMILY_C2,
            legs=legs,
            books={spot.inst_id: spot, perp.inst_id: perp},
            gross_quote=gross_q,
            notional_quote=N,
            costs_quote=costs,
            margin_capital=margin,
            risk_flags=flags,
            residual_risks=residual,
            model=MODEL_STUB,
            cfg=cfg,
            tracker=self.tracker,
            persistence_key=f"{FAMILY_C2}|{spot.inst_id}|{perp.inst_id}|{direction}",
            coverage=self.coverage,
            extra=extra,
            funding_ctx=ce.FundingContext(
                intervals=float(H),
                interval_sec=fr.interval_sec,
                funding_gross_quote=funding_gross_q,
                funding_cost_quote=funding_cost_q,
                rate_used_per_interval=expected / H,
            ),
            hold_years=hold_years,
        )
        return [rec]

    # ---- C3 B1 / B3 sampler band relaxed by one step → Cost Engine (inherited Phase B records)

    def scan_c3(self, snap: Snapshot) -> list[dict]:
        books = _books_of(snap)
        spot = snap.spot
        if spot is None or not spot.two_sided:
            self.skipped["c3_missing_spot"] += 1
            return []
        # coverage baseline under the Phase B default bands (same snapshot, counts only)
        for r in self._b_baseline.scan_b1(snap) + self._b_baseline.scan_b3(snap):
            self.coverage_baseline[BASE_COMBO_ID[r["family"]]] += 1
        a1 = self._b_relaxed.a1_reference(snap)
        out: list[dict] = []
        for r in self._b_relaxed.scan_b1(snap, a1) + self._b_relaxed.scan_b3(snap):
            if r["family"] not in C3_TARGET_FAMILIES:
                continue
            out.append(
                wrap_inherited_record(
                    r,
                    expansion_id=EXP_C3,
                    books=books,
                    spot_ref=_spot_mid(spot),
                    cfg=self.cfg,
                    coverage=self.coverage,
                    extra={
                        "mechanism": self.cfg.c3_mechanism,
                        "band_step": self.cfg.c3_band_step,
                        "relaxation": self.relaxation,
                        "coverage_gate_n": self.cfg.coverage_n,
                        "selected_by_moneyness_fallback": "otm_selection_by_moneyness_fallback"
                        in r["risk_flags"],
                        "delta_off_target": "delta_off_target" in r["risk_flags"],
                        "note": "Phase B record under the one-step relaxed band; cover rule, "
                        "costs and cost_engine block inherited unchanged",
                    },
                )
            )
        return out

    # ---- C4 A2 / A3 through the high-liquidity filter (inherited Phase A records)

    def scan_c4(self, snap: Snapshot) -> list[dict]:
        books = _books_of(snap)
        spot = snap.spot
        if spot is None or not spot.two_sided:
            self.skipped["c4_missing_spot"] += 1
            return []
        out: list[dict] = []
        for r in self._a.scan_a2(snap) + self._a.scan_a3(snap):
            ok, _ = liquidity_filter(r["legs"], books, _spot_mid(spot), self.cfg.liquidity)
            if not ok and self.cfg.liquidity.mode == "skip":
                self.skipped["c4_illiquid_skipped"] += 1
                continue
            out.append(
                wrap_inherited_record(
                    r,
                    expansion_id=EXP_C4,
                    books=books,
                    spot_ref=_spot_mid(spot),
                    cfg=self.cfg,
                    coverage=self.coverage,
                    extra={
                        "filter": self.cfg.liquidity.to_dict(),
                        "extra_optimising_leg": "refused",
                        "note": "Phase A record; costs / cost_engine inherited; C4 only adds the "
                        "liquidity gate — it cannot create a positive net edge",
                    },
                )
            )
        return out


# --------------------------------------------------------------------------- summary


def metrics_block(rows: list[dict]) -> dict:
    n = len(rows)
    gross_pos = [r for r in rows if r["gross_edge_bps"] > 0]
    net_pos = [r for r in rows if r["net_edge_bps"] > 0]
    kill = [r for r in gross_pos if r["net_edge_bps"] <= 0]
    nets = [r["net_edge_bps"] for r in rows]
    all_in = [r["cost_engine"]["all_in_cost_bps"] for r in rows]
    liq = [r for r in rows if r["liquidity_ok"]]
    return {
        "records": n,
        "gross_gt_0": len(gross_pos),
        "gross_gt_0_rate": round(len(gross_pos) / n, 4) if n else None,
        "net_gt_0": len(net_pos),
        "net_edge_bps_gt_0_rate": round(len(net_pos) / n, 4) if n else None,
        "cost_kill": len(kill),
        "cost_kill_rate_vs_all": round(len(kill) / n, 4) if n else None,
        "cost_kill_rate_vs_gross_gt_0": round(len(kill) / len(gross_pos), 4) if gross_pos else None,
        "cost_kill_definition": "gross_edge_bps > 0 AND net_edge_bps <= 0; two bases reported",
        "liquidity_ok": len(liq),
        "liquidity_ok_rate": round(len(liq) / n, 4) if n else None,
        "invalidated": sum(1 for r in rows if r["invalidated_by"]),
        "edge_exceeds_buffer": sum(1 for r in rows if r["edge_exceeds_buffer"]),
        "passes_threshold": sum(1 for r in rows if r["passes_threshold"]),
        "median_net_edge_bps": round(statistics.median(nets), 4) if nets else None,
        "max_net_edge_bps": round(max(nets), 4) if nets else None,
        "median_all_in_cost_bps": round(statistics.median(all_in), 4) if all_in else None,
        "calibrated": False,
        "tradable_claim_allowed": False,
    }


def coverage_verdict(n: int, gate_n: int, net_gt_0: int) -> str:
    if n < gate_n:
        return VERDICT_COVERAGE_INSUFFICIENT
    if net_gt_0 == 0:
        return VERDICT_COST_VETO
    return VERDICT_NET_POSITIVE


def coverage_block(rows: list[dict], gate_n: int, baseline_n: int | None = None) -> dict:
    n = len(rows)
    net_pos = sum(1 for r in rows if r["net_edge_bps"] > 0)
    verdict = coverage_verdict(n, gate_n, net_pos)
    for bad in BANNED_VERDICT_WORDS:
        if bad in verdict:
            raise ExpansionSchemaViolation(f"verdict wording {verdict!r} is banned")
    block = {
        "n": n,
        "gate_n": gate_n,
        "gate_met": n >= gate_n,
        "net_gt_0": net_pos,
        "liquidity_ok": sum(1 for r in rows if r["liquidity_ok"]),
        "verdict": verdict,
        "verdict_rule": "n < N → coverage_insufficient (no strategy verdict either way); "
        "n ≥ N and net>0 == 0 → cost veto on this window; net>0 > 0 → observed, uncalibrated, "
        "not tradable",
    }
    if baseline_n is not None:
        block["baseline_n_phase_b_defaults"] = baseline_n
        block["delta_n_from_relaxation"] = n - baseline_n
    return block


def summarize(
    records: list[dict],
    snapshots: int,
    cfg: ExpansionConfig,
    source: dict,
    scanner: ExpansionScanner | None = None,
) -> dict:
    skipped = scanner.skipped if scanner else Counter()
    baseline = scanner.coverage_baseline if scanner else Counter()
    relaxation = scanner.relaxation if scanner else relax_phase_b_config(cfg.phase_b_config())[1]
    by_exp = {e: [r for r in records if r["expansion_id"] == e] for e in EXPANSIONS}
    exps: dict[str, dict] = {}
    for e in EXPANSIONS:
        rows = by_exp[e]
        block = {
            "name": EXPANSION_NAME[e],
            "hypothesis_id": HYPOTHESIS[e],
            "enabled": e in cfg.expansions,
            **metrics_block(rows),
            "taxonomies": sorted({r["taxonomy"] for r in rows}),
            "combo_ids": sorted({r["combo_id"] for r in rows}),
            "models": sorted({r["model"] for r in rows}),
        }
        if e in (EXP_C3, EXP_C4):
            block["by_base_family"] = {}
            for key in sorted({r["base_combo_id"] for r in rows}):
                fam_rows = [r for r in rows if r["base_combo_id"] == key]
                block["by_base_family"][key] = {
                    **metrics_block(fam_rows),
                    "coverage": coverage_block(
                        fam_rows, cfg.coverage_n, baseline.get(key) if e == EXP_C3 else None
                    ),
                }
        if e == EXP_C3:
            block["mechanism"] = relaxation
            block["baseline_phase_b_defaults_counts"] = dict(sorted(baseline.items()))
        if e == EXP_C4:
            block["filter"] = cfg.liquidity.to_dict()
            block["illiquid_flagged"] = sum(1 for r in rows if not r["liquidity_ok"])
            block["illiquid_skipped"] = skipped.get("c4_illiquid_skipped", 0)
        exps[e] = block

    c3_rows = by_exp[EXP_C3]
    cov_b1 = coverage_block(
        [r for r in c3_rows if r["base_combo_id"] == "B1"], cfg.coverage_n, baseline.get("B1", 0)
    )
    cov_b3 = coverage_block(
        [r for r in c3_rows if r["base_combo_id"] == "B3"], cfg.coverage_n, baseline.get("B3", 0)
    )
    overall = metrics_block(records)
    falsifiable = {
        "net_edge_bps_gt_0_rate": overall["net_edge_bps_gt_0_rate"],
        "net_gt_0": overall["net_gt_0"],
        "gross_gt_0_rate": overall["gross_gt_0_rate"],
        "cost_kill_rate": {
            "vs_all": overall["cost_kill_rate_vs_all"],
            "vs_gross_gt_0": overall["cost_kill_rate_vs_gross_gt_0"],
            "definition": overall["cost_kill_definition"],
        },
        "sample_coverage_B1": cov_b1,
        "sample_coverage_B3": cov_b3,
        "median_all_in_cost_bps": overall["median_all_in_cost_bps"],
        "max_net_edge_bps_by_expansion": {e: exps[e]["max_net_edge_bps"] for e in EXPANSIONS},
        "calibrated": False,
        "tradable_claim_allowed": False,
        "note": "paper / read-only window; calibrated=false → no tradable wording; coverage "
        "below N → no strategy verdict either way",
    }
    hyp: dict[str, dict] = {}
    for e in EXPANSIONS:
        rows = by_exp[e]
        hid = HYPOTHESIS[e]
        n_pass = sum(1 for r in rows if r["passes_threshold"])
        n_net = sum(1 for r in rows if r["net_edge_bps"] > 0)
        if e not in cfg.expansions:
            status = "disabled"
        elif not rows:
            status = "no_samples"
        elif len(rows) < cfg.coverage_n:
            status = VERDICT_COVERAGE_INSUFFICIENT
        elif snapshots < cfg.persistence_min_samples:
            status = "insufficient_samples_for_persistence"
        elif n_net == 0:
            status = VERDICT_COST_VETO
        elif n_pass == 0:
            status = VERDICT_NET_POSITIVE + "_no_pass"
        else:
            status = VERDICT_NET_POSITIVE + "_pass_observed"
        hyp[hid] = {
            "records": len(rows),
            "net_gt_0": n_net,
            "passes_threshold": n_pass,
            "pass_rate": round(n_pass / len(rows), 4) if rows else None,
            "status": status,
            "falsify_if": FALSIFY_IF[hid],
            "note": "paper/read-only sample; no return claim; stub models uncalibrated",
        }
    return {
        "mode": MODE,
        "phase": PHASE,
        "action": ACTION,
        "will_send_http": WILL_SEND_HTTP,
        "trading_http": {"order": False, "amend": False, "withdraw": False, "transfer": False},
        "live_execution": False,
        "live_delta_hedge": False,
        "venue": records[0]["venue"] if records else DEFAULT_VENUE,
        "underlying": cfg.underlying,
        "snapshots": snapshots,
        "records": len(records),
        "expansions": exps,
        "falsifiable_metrics": falsifiable,
        "hypotheses": hyp,
        "skipped": dict(sorted(skipped.items())),
        "cost_engine": {
            **ce.summarize_blocks([r["cost_engine"] for r in records], enabled=True),
            "mandatory": True,
            "net_edge_source": NET_EDGE_SOURCE,
        },
        "config": cfg.to_dict(),
        "relaxed_phase_b_config": scanner.relaxed_phase_b_config.to_dict() if scanner else None,
        "data_source": source,
        "policy": POLICY,
        "okx_client_policy": okx.POLICY,
        "taxonomy_rule": {
            "all_expansion_records": TAXONOMY,
            "inherited_A2_A3_original_in": "taxonomy_base",
            "forbidden_label_gate": "enforced_in_finalize_expansion_record",
        },
        "mainline_unchanged": "spot_grid_local_paper",
        "related_scanners": RELATED_SCANNERS,
        "approvals": APPROVALS,
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- okx public source


class ExpansionSnapshotSource(pas.OkxPublicSnapshotSource):
    """Phase A public GET snapshot source + dated futures books (`GET /public/instruments`
    FUTURES metadata once, then `GET /market/books` per future). No auth, no POST."""

    def __init__(self, *args, fut_family: str | None = "ETH-USDT", max_futures: int = 2, **kw):
        super().__init__(*args, **kw)
        self.fut_family = fut_family
        self.max_futures = max_futures
        self._futures_meta: list[dict] | None = None

    def _load_futures_meta(self) -> None:
        if self._futures_meta is None and self.fut_family:
            self._futures_meta = [
                r
                for r in self.client.get_instruments("FUTURES", inst_family=self.fut_family)
                if r.get("state", "live") == "live"
                and r.get("expTime_ms")
                and not r.get("optType")
                and r.get("instType") in (None, "FUTURES")
            ]

    def fetch(self) -> Snapshot:
        snap = super().fetch()
        self._load_futures_meta()
        futures: list[Book] = []
        if self._futures_meta:
            min_exp = snap.ts_ms + self.min_days_to_expiry * 86_400_000
            rows = sorted(
                (r for r in self._futures_meta if r["expTime_ms"] >= min_exp),
                key=lambda r: r["expTime_ms"],
            )
            for meta in rows[: self.max_futures]:
                futures.append(
                    self._book(
                        meta["instId"],
                        "future",
                        ct_val=meta.get("ctVal") or 1.0,
                        expiry_ms=meta["expTime_ms"],
                        settle_ccy=meta.get("settleCcy"),
                    )
                )
        source = dict(snap.source)
        source["future_books"] = len(futures)
        source["funding_history"] = "not fetched (C2 stub anchors on --c2-long-run-rate)"
        return replace(snap, futures=tuple(futures), source=source)


# --------------------------------------------------------------------------- cli

DEFAULT_FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-expansion-books-sample.json"
COMMANDS = ("scan", "evaluate-fixture", "policy")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper combo expansion scanner (C1 spot vs dated-future basis, C2 funding "
        "expectation path + basis confirmation, C3 one-step relaxed B1/B3 sampler → Cost Engine, "
        "C4 A2/A3 high-liquidity filter). Read-only, observe_only, will_send_http=false, "
        "taxonomy=relative_value, net edge only from tools/cost_engine.py evaluate, "
        "calibrated=false, ETH only by default. Emits JSON lines.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "cmd",
        nargs="?",
        choices=COMMANDS,
        default="scan",
        help="scan (fixture | okx-public) | evaluate-fixture (offline replay + falsifiable "
        "metrics to stderr) | policy (print the gates JSON and exit)",
    )
    p.add_argument("--source", choices=["fixture", "okx-public"], default="fixture")
    p.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="fixture JSON (offline)")
    p.add_argument("--venue", default=DEFAULT_VENUE)
    p.add_argument("--underlying", default=DEFAULT_UNDERLYING, help="ETH (BTC needs --allow-btc)")
    p.add_argument(
        "--allow-btc",
        action="store_true",
        help="named flag, default off: BTC is NOT cleared by 04-risk for this branch",
    )
    p.add_argument(
        "--expansions", default=",".join(EXPANSIONS), help="comma list subset of C1,C2,C3,C4"
    )
    p.add_argument("--spot", default="ETH-USDT")
    p.add_argument("--perp", default="ETH-USDT-SWAP")
    p.add_argument("--opt-family", default="ETH-USD", help="'' to skip options (C3/C4 empty)")
    p.add_argument("--fut-family", default="ETH-USDT", help="okx-public: dated futures family")
    p.add_argument("--max-futures", type=int, default=2, help="okx-public: dated futures to book")
    p.add_argument("--n-strikes", type=int, default=8)
    p.add_argument("--max-expiries", type=int, default=2)
    p.add_argument("--min-days-to-expiry", type=float, default=2.0)
    p.add_argument("--book-depth", type=int, default=5)
    p.add_argument("--samples", type=int, default=1, help="okx-public: snapshots to take")
    p.add_argument("--interval-sec", type=float, default=20.0, help="okx-public: seconds between")
    p.add_argument("--base-url", default=okx.OKX_PUBLIC_BASE)
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--spot-borrow-apr", type=float, default=None, help="unset → shorts invalidated")

    p.add_argument("--qty", type=float, default=1.0)
    p.add_argument("--horizon-intervals", type=int, default=3)
    p.add_argument("--ref-rate-apr", type=float, default=0.0)
    p.add_argument("--funding-sigma-bps", type=float, default=2.0)
    p.add_argument("--hedge-rebalance-bps", type=float, default=2.0)
    p.add_argument("--transfer-bps", type=float, default=0.0)
    p.add_argument("--depth-mult", type=float, default=2.0)
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--persistence-min-samples", type=int, default=3)
    p.add_argument("--persistence-min-sec", type=float, default=60.0)

    p.add_argument("--c1-tradeable-expiries", type=int, default=1, help="nearest N futures as L2")
    p.add_argument("--c1-hold", choices=["to_expiry", "horizon"], default="to_expiry")
    p.add_argument("--c1-future-settlement-bps", type=float, default=2.0)
    p.add_argument("--c2-decay", type=float, default=0.7)
    p.add_argument("--c2-long-run-rate", type=float, default=0.0001)
    p.add_argument("--c2-history-window", type=int, default=12)
    p.add_argument("--c2-basis-conflict-bps", type=float, default=5.0)
    p.add_argument("--coverage-n", type=int, default=C3_COVERAGE_N, help="per-family gate N")
    p.add_argument("--c4-max-half-spread-bps", type=float, default=C4_MAX_HALF_SPREAD_BPS)
    p.add_argument("--c4-min-size-contracts", type=float, default=C4_MIN_SIZE_CONTRACTS)
    p.add_argument("--c4-contract-size-base", type=float, default=1.0)
    p.add_argument("--c4-min-notional-quote", type=float, default=C4_MIN_NOTIONAL_QUOTE)
    p.add_argument("--c4-mode", choices=["flag", "skip"], default="flag")

    p.add_argument("--fee-spot-bps", type=float, default=10.0)
    p.add_argument("--fee-perp-bps", type=float, default=5.0)
    p.add_argument("--fee-option-bps", type=float, default=3.0)
    p.add_argument("--fee-option-cap-pct", type=float, default=12.5)
    p.add_argument("--fee-option-settle-bps", type=float, default=2.0)
    p.add_argument("--buffer-fee-roundtrip-bps", type=float, default=None)
    p.add_argument("--buffer-slip-bps", type=float, default=5.0)
    p.add_argument("--buffer-funding-uncert-bps", type=float, default=5.0)
    p.add_argument("--buffer-haircut-bps", type=float, default=10.0)

    p.add_argument("--paper-fills", action="store_true")
    p.add_argument("--paper-extra-slip-bps", type=float, default=5.0)
    p.add_argument("--out", default="", help="JSONL records file (default stdout)")
    p.add_argument("--summary-out", default="", help="summary JSON file")
    p.add_argument("--print-summary", action="store_true", help="summary JSON to stderr")
    p.add_argument("--only-exceeding", action="store_true", help="emit only edge_exceeds_buffer")
    p.add_argument("--quiet", action="store_true")
    return p


def config_from_args(args: argparse.Namespace) -> ExpansionConfig:
    return ExpansionConfig(
        underlying=args.underlying.upper(),
        allow_btc=args.allow_btc,
        expansions=tuple(x.strip().upper() for x in args.expansions.split(",") if x.strip()),
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
        paper_fills=args.paper_fills,
        paper_extra_slip_bps=args.paper_extra_slip_bps,
        c1_tradeable_expiries=args.c1_tradeable_expiries,
        c1_hold=args.c1_hold,
        c1_future_settlement_bps=args.c1_future_settlement_bps,
        c2_decay=args.c2_decay,
        c2_long_run_rate=args.c2_long_run_rate,
        c2_history_window=args.c2_history_window,
        c2_basis_conflict_bps=args.c2_basis_conflict_bps,
        coverage_n=args.coverage_n,
        liquidity=LiquidityFilter(
            max_half_spread_bps=args.c4_max_half_spread_bps,
            min_size_contracts=args.c4_min_size_contracts,
            contract_size_base=args.c4_contract_size_base,
            min_notional_quote=args.c4_min_notional_quote,
            mode=args.c4_mode,
        ),
        fees=FeeSchedule(
            spot_taker_bps=args.fee_spot_bps,
            perp_taker_bps=args.fee_perp_bps,
            option_taker_bps=args.fee_option_bps,
            option_fee_cap_pct_premium=args.fee_option_cap_pct,
            option_settlement_bps=args.fee_option_settle_bps,
        ),
        buffer=pas.SafetyBuffer(
            fee_roundtrip_bps=args.buffer_fee_roundtrip_bps,
            slip_buffer_bps=args.buffer_slip_bps,
            funding_uncert_bps=args.buffer_funding_uncert_bps,
            model_haircut_bps=args.buffer_haircut_bps,
            calibrated=False,
        ),
    )


def run(args: argparse.Namespace) -> tuple[list[dict], dict]:
    cfg = config_from_args(args)
    scanner = ExpansionScanner(cfg)
    records: list[dict] = []
    source_kind = "fixture" if args.cmd == "evaluate-fixture" else args.source
    if source_kind == "fixture":
        snaps = load_fixture(args.fixture)
        if args.spot_borrow_apr is not None:
            snaps = [replace(s, spot_borrow_apr=args.spot_borrow_apr) for s in snaps]
        for s in snaps:
            records.extend(scanner.scan(s))
        source = snaps[0].source if snaps else {"kind": "fixture", "http_fetch": False}
        n = len(snaps)
    else:
        client = okx.OkxPublicClient(base_url=args.base_url, timeout=args.timeout)
        src = ExpansionSnapshotSource(
            client,
            spot_inst=args.spot,
            perp_inst=args.perp,
            opt_family=args.opt_family or None,
            n_strikes=args.n_strikes,
            max_expiries=args.max_expiries,
            min_days_to_expiry=args.min_days_to_expiry,
            book_depth=args.book_depth,
            spot_borrow_apr=args.spot_borrow_apr,
            fut_family=args.fut_family or None,
            max_futures=args.max_futures,
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
    return records, summarize(records, n, cfg, source, scanner)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "policy":
        print(json.dumps(POLICY, ensure_ascii=False, indent=2))
        return 0
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
    if args.cmd == "evaluate-fixture" and not args.print_summary:
        print(
            json.dumps(
                {
                    "cmd": "evaluate-fixture",
                    "action": ACTION,
                    "will_send_http": WILL_SEND_HTTP,
                    "records": summary["records"],
                    "snapshots": summary["snapshots"],
                    "falsifiable_metrics": summary["falsifiable_metrics"],
                    "hypotheses": summary["hypotheses"],
                    "skipped": summary["skipped"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
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
        ExpansionSchemaViolation,
        UnderlyingNotApproved,
        ForbiddenLabelViolation,
        ce.CostEngineError,
        RuntimeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
