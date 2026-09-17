"""Offline unit tests for tools/cost_engine.py (Cost Engine v0) and its optional wiring into the
Phase A / Phase B paper scanners. No network, no secrets.

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "strategies"))

import cost_engine as ce  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
import paper_combo_scanner as pcs  # noqa: E402

SCRIPT = ROOT / "tools" / "cost_engine.py"
CASES = ROOT / "fixtures" / "cost_engine" / "2026-09-17-cost-cases.json"
FIXTURE_A = ROOT / "fixtures" / "arb_books" / "2026-09-16-eth-books-sample.json"
FIXTURE_B = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-combo-books-sample.json"

N = 2001.0  # spot ask × qty 1 — the Phase A notional convention
H8 = 28_800
HOLD_1D = 3 * H8 / (365.0 * 86400.0)

# The A1 buckets of tests/test_paper_arb_scanner.py::test_positive_carry_numbers, in quote.
A1_BUCKETS = {
    "fees": N * (2 * 10 + 2 * 5) / 1e4,  # 6.003
    "half_spread_slip": 1.0 + 0.5,  # exit half-spreads: spot (2001-1999)/2 + perp (2003-2002)/2
    "impact": 0.0,
    "hedge_rebalance": N * 2 / 1e4,  # 0.4002
    "borrow": 0.0,
    "transfer": 0.0,
    "capital_opp": 0.0,
    "funding_expected": 0.0,
    "funding_uncertainty": N * (2 / 1e4 * 3 + 0.00005 * 3),  # 1.50075
}
A1_GROSS = 0.00025 * 3 * N  # min(3.0, 2.5) bp × 3 intervals = 1.50075


def spot_perp(spot_bid=1999.0, spot_ask=2001.0, perp_bid=2002.0, perp_ask=2003.0, sz=10.0):
    def book(inst, kind, bid, ask, size, **kw):
        bids = tuple(pas.Level(bid - i, size) for i in range(3))
        asks = tuple(pas.Level(ask + i, size) for i in range(3))
        return pas.Book(inst_id=inst, kind=kind, bids=bids, asks=asks, ts_ms=1_789_573_200_000, **kw)

    return (
        book("ETH-USDT", "spot", spot_bid, spot_ask, sz),
        book("ETH-USDT-SWAP", "perp", perp_bid, perp_ask, sz * 5, mark_px=2002.5),
    )


def a1_record(cost_engine=True, **funding_kw) -> dict:
    s, p = spot_perp()
    fr = pas.Funding(**{"rate": 0.0003, "next_rate": 0.00025, "interval_sec": H8, **funding_kw})
    cfg = pas.ScanConfig(persistence_min_samples=1, persistence_min_sec=0.0, cost_engine=cost_engine)
    snap = pas.Snapshot(ts_ms=1_789_573_200_000, venue="okx", spot=s, perp=p, funding=fr)
    return pas.ArbScanner(cfg).scan_a1(snap)[0]


# --------------------------------------------------------------------------- hard gates


class HardGateTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(ce.ACTION, "observe_only")
        self.assertIs(ce.WILL_SEND_HTTP, False)
        self.assertEqual(ce.VERSION, "v0")
        self.assertEqual(ce.EDGE_BASIS, "hold_horizon")
        self.assertEqual(
            ce.CORE_COMPONENTS,
            (
                "fees",
                "half_spread_slip",
                "impact",
                "borrow",
                "transfer",
                "capital_opp",
                "funding_uncertainty",
                "hedge_rebalance",
            ),
        )
        # every Phase A / B cost key is either a core bucket or a known carried extra
        for k in pas.COST_KEYS + pcs.COST_KEYS:
            self.assertIn(k, ce.CORE_COMPONENTS + ce.KNOWN_EXTRA_COMPONENTS, k)

    def test_module_has_no_network_or_trading_path_and_leaves_mainline_alone(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in (
            "/api/v5/trade",
            "urlopen",
            "urllib",
            "http.client",
            "socket",
            'method="POST"',
            "place_order(",
            "amend_algo(",
            "import local_paper_grid",
            "import grid_ab_compare",
            "import paper_arb_scanner",  # engine is upstream of the scanners
            "import okx_readonly_client",
        ):
            self.assertFalse(needle in src, f"{needle!r} found in tools/cost_engine.py")

    def test_result_is_observe_only_and_not_annualised(self):
        d = ce.evaluate(gross_quote=1.0, notional_quote=N, components_quote=A1_BUCKETS).to_dict()
        self.assertEqual(d["action"], "observe_only")
        self.assertIs(d["will_send_http"], False)
        self.assertIs(d["annualized"], False)
        self.assertEqual(d["edge_basis"], "hold_horizon")
        self.assertIs(d["calibrated"], False)
        self.assertIs(d["tradable_claim_allowed"], False)
        text = json.dumps(d, ensure_ascii=False).lower()
        for bad in ce.FORBIDDEN_LABELS:
            self.assertNotIn(bad, text)

    def test_finalize_refuses_tampered_results(self):
        d = ce.evaluate(gross_quote=1.0, notional_quote=N, components_quote=A1_BUCKETS).to_dict()
        with self.assertRaises(ce.ObserveOnlyViolation):
            ce.finalize({**d, "action": "execute"})
        with self.assertRaises(ce.ObserveOnlyViolation):
            ce.finalize({**d, "will_send_http": True})
        with self.assertRaises(ce.NaiveAnnualizationRefused):
            ce.finalize({**d, "annualized": True})
        with self.assertRaises(ce.NaiveAnnualizationRefused):
            ce.finalize({**d, "edge_basis": "apy"})
        with self.assertRaises(ce.CalibrationClaimRefused):
            ce.finalize({**d, "tradable_claim_allowed": True})
        with self.assertRaises(ce.CostEngineError):
            ce.finalize({**d, "notes": ["risk-free carry"]})
        with self.assertRaises(ce.CostEngineError):
            ce.evaluate(
                gross_quote=1.0, notional_quote=N, components_quote=A1_BUCKETS, notes=["稳赚"]
            ).to_dict()

    def test_mark_mid_last_refused_as_executable(self):
        for pt in ("mark", "mid", "last", "index"):
            with self.assertRaises(ce.ExecutablePriceViolation):
                ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, 1999.0, 2001.0, price_type=pt)
        # side / price_type convention: buy hits the ask, sell hits the bid
        with self.assertRaises(ce.ExecutablePriceViolation):
            ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, 1999.0, 2001.0, price_type="bid")
        with self.assertRaises(ce.ExecutablePriceViolation):
            ce.LegSpec("ETH-USDT", "spot", "sell", 1.0, 1999.0, 2001.0, price_type="ask")
        buy = ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, 1999.0, 2001.0, mark_ref=2000.0)
        sell = ce.LegSpec("ETH-USDT", "spot", "sell", 1.0, 1999.0, 2001.0)
        self.assertEqual((buy.price_type, buy.executable_price), ("ask", 2001.0))
        self.assertEqual((sell.price_type, sell.executable_price), ("bid", 1999.0))
        self.assertEqual(buy.to_dict()["mark_ref"], 2000.0)  # reference only, never priced
        self.assertEqual(buy.exec_quote(), 2001.0)

    def test_one_sided_book_is_refused_not_guessed(self):
        with self.assertRaises(ce.ExecutablePriceViolation):
            ce.LegSpec("OPT", "option", "sell", 1.0, None, 102.0, mark_ref=101.0)
        with self.assertRaises(ce.ExecutablePriceViolation):
            ce.half_spread_quote(None, 102.0, 1.0)
        # buying only needs the ask, and the leg is still bid/ask priced
        leg = ce.LegSpec("OPT", "option", "buy", 1.0, None, 102.0, crossings=1)
        self.assertFalse(leg.two_sided)
        self.assertEqual(leg.executable_price, 102.0)

    def test_annualised_gross_refused(self):
        for basis in ("annualized", "annualised", "apy", "x365", "per_year", "whatever"):
            with self.assertRaises(ce.NaiveAnnualizationRefused):
                ce.evaluate(
                    gross_quote=0.0003 * 1095 * N,
                    notional_quote=N,
                    components_quote=A1_BUCKETS,
                    gross_basis=basis,
                )
        # FundingLeg horizons longer than a year of intervals are refused too
        with self.assertRaises(ce.NaiveAnnualizationRefused):
            ce.FundingLeg(rate_now=0.0003, horizon_intervals=1096, interval_sec=H8)
        ce.FundingLeg(rate_now=0.0003, horizon_intervals=1095, interval_sec=H8)

    def test_funding_expectation_is_per_horizon_not_x365(self):
        fl = ce.FundingLeg(rate_now=0.0003, horizon_intervals=3, rate_next=0.00025)
        self.assertAlmostEqual(fl.expected_quote(N), 0.00025 * 3 * N)
        self.assertNotAlmostEqual(fl.expected_quote(N), 0.0003 * 365 * N)
        self.assertNotAlmostEqual(fl.expected_quote(N), 0.0003 * 1095 * N)
        self.assertAlmostEqual(fl.hold_years, HOLD_1D)
        self.assertEqual(fl.to_dict()["flags"], [])
        # a display annualisation exists but says what it is
        ref = ce.apy_ref(-39.4963, HOLD_1D)
        self.assertIs(ref["display_only"], True)
        self.assertIs(ref["return_promise"], False)
        self.assertIs(ref["is_net_edge"], False)
        self.assertAlmostEqual(ref["apy_ref_bps"], -39.4963 / HOLD_1D, places=3)
        with self.assertRaises(ce.CostEngineError):
            ce.apy_ref(10.0, 0.0)
        with self.assertRaises(ce.CostEngineError):
            ce.apy_ref(10.0, None)

    def test_calibrated_needs_reference_and_never_allows_tradable_claim(self):
        self.assertIs(ce.DEFAULT_CONFIG.calibrated, False)
        with self.assertRaises(ce.CalibrationClaimRefused):
            ce.CostEngineConfig(calibrated=True)
        with self.assertRaises(ce.CalibrationClaimRefused):
            ce.CostEngineConfig(calibrated=True, calibration_ref="   ")
        cfg = ce.CostEngineConfig(calibrated=True, calibration_ref="04-risk/example-note")
        d = ce.evaluate(
            gross_quote=1.0, notional_quote=N, components_quote=A1_BUCKETS, config=cfg
        ).to_dict()
        self.assertIs(d["calibrated"], True)
        self.assertEqual(d["calibration_ref"], "04-risk/example-note")
        self.assertIs(d["tradable_claim_allowed"], False)


# --------------------------------------------------------------------------- component primitives


class ComponentPrimitiveTest(unittest.TestCase):
    fees = ce.FeeSchedule()

    def test_fee_quote_spot_perp_option_cap_settlement(self):
        self.assertAlmostEqual(ce.fee_quote("spot", N, self.fees), N * 10 / 1e4)
        self.assertAlmostEqual(ce.fee_quote("perp", N, self.fees, crossings=2), N * 5 / 1e4 * 2)
        # option: min(3 bp of underlying notional = 0.6003, 12.5% of premium 92 = 11.5) + 2 bp settle
        self.assertAlmostEqual(
            ce.fee_quote("option", N, self.fees, premium_quote=92.0, settlement=True),
            0.6003 + 0.4002,
        )
        # cheap premium → cap binds: 12.5% of 1.0 = 0.125 < 0.6003
        self.assertAlmostEqual(
            ce.fee_quote("option", N, self.fees, premium_quote=1.0, crossings=2), 0.25
        )
        with self.assertRaises(ce.CostEngineError):
            ce.fee_quote("option", N, self.fees)  # premium required for the cap rule
        with self.assertRaises(ce.CostEngineError):
            ce.fee_quote("future", N, self.fees)
        with self.assertRaises(ce.CostEngineError):
            ce.fee_quote("spot", N, self.fees, crossings=-1)

    def test_half_spread_impact(self):
        self.assertAlmostEqual(ce.half_spread_quote(1999.0, 2001.0, 1.0), 1.0)
        self.assertAlmostEqual(ce.half_spread_quote(1999.0, 2001.0, 2.0, crossings=2), 4.0)
        self.assertEqual(ce.half_spread_quote(1999.0, 2001.0, 1.0, crossings=0), 0.0)
        self.assertEqual(ce.half_spread_quote(2001.0, 1999.0, 1.0), 0.0)  # crossed book → 0
        # coin-priced premium converted at quote_conv
        self.assertAlmostEqual(ce.half_spread_quote(0.05, 0.051, 1.0, quote_conv=2001.0), 1.0005)
        self.assertAlmostEqual(ce.impact_quote(2001.0, 2001.6, 1.0), 0.6)
        self.assertEqual(ce.impact_quote(2001.0, None, 1.0), 0.0)
        self.assertAlmostEqual(ce.impact_quote(2002.0, 2001.5, 2.0), 1.0)  # sell side: |vwap − bid|
        with self.assertRaises(ce.CostEngineError):
            ce.impact_quote(2001.0, 2001.6, 0.0)

    def test_borrow_transfer_capital_opp_funding_uncertainty_hedge(self):
        self.assertAlmostEqual(ce.borrow_quote(N, 0.10, HOLD_1D), N * 0.10 * HOLD_1D)
        self.assertAlmostEqual(ce.transfer_quote(N, 1.0), 0.2001)
        self.assertAlmostEqual(ce.capital_opp_quote(2 * N, 0.05, HOLD_1D), 2 * N * 0.05 * HOLD_1D)
        self.assertEqual(ce.capital_opp_quote(2 * N, 0.0, HOLD_1D), 0.0)  # no claim by default
        self.assertAlmostEqual(
            ce.funding_uncertainty_quote(N, 2.0, 3, pred_gap_rate=0.00005), A1_BUCKETS["funding_uncertainty"]
        )
        self.assertAlmostEqual(ce.hedge_rebalance_quote(N, 2.0), 0.4002)
        for bad in (
            lambda: ce.borrow_quote(-1.0, 0.1, HOLD_1D),
            lambda: ce.borrow_quote(N, -0.1, HOLD_1D),
            lambda: ce.transfer_quote(N, -1.0),
            lambda: ce.capital_opp_quote(N, 0.05, -0.1),
            lambda: ce.funding_uncertainty_quote(N, -2.0, 3),
            lambda: ce.hedge_rebalance_quote(N, -2.0),
        ):
            with self.assertRaises(ce.CostEngineError):
                bad()

    def test_leg_costs_from_bid_ask(self):
        legs = [
            ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, 1999.0, 2001.0, vwap=2001.0),
            ce.LegSpec("ETH-USDT-SWAP", "perp", "sell", 1.0, 2002.0, 2003.0, vwap=2001.8),
        ]
        c = ce.leg_costs(legs, self.fees, underlying_notional_quote=N)
        # fees on each leg's executable notional, 2 crossings each
        self.assertAlmostEqual(c["fees"], 2001.0 * 10 / 1e4 * 2 + 2002.0 * 5 / 1e4 * 2)
        # one exit half-spread per leg (entry spread lives inside the bid/ask leg price)
        self.assertAlmostEqual(c["half_spread_slip"], 1.0 + 0.5)
        self.assertAlmostEqual(c["impact"], 0.2)  # sell VWAP 2001.8 vs bid 2002
        # options: fee on underlying notional with the premium cap; held to expiry → settlement
        opt = [
            ce.LegSpec("OPT-P", "option", "buy", 1.0, 90.0, 92.0, crossings=1, settlement=True),
            ce.LegSpec("OPT-C", "option", "sell", 1.0, 100.0, 102.0, crossings=1, settlement=True),
        ]
        c2 = ce.leg_costs(opt, self.fees, underlying_notional_quote=N)
        self.assertAlmostEqual(c2["fees"], 2 * (0.6003 + 0.4002))
        self.assertEqual(c2["half_spread_slip"], 0.0)
        # coin-priced premium: quote_conv carries the spot bid/ask conversion
        coin = [ce.LegSpec("OPT-C", "option", "sell", 1.0, 0.05, 0.051, quote_conv=1999.0)]
        c3 = ce.leg_costs(coin, self.fees, underlying_notional_quote=N)
        self.assertAlmostEqual(c3["half_spread_slip"], 0.0005 * 1999.0)
        self.assertAlmostEqual(c3["fees"], min(0.6003, 0.05 * 1999.0 * 0.125) * 2)

    def test_evaluate_legs_refuses_double_counting(self):
        legs = [ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, 1999.0, 2001.0)]
        with self.assertRaises(ce.CostEngineError):
            ce.evaluate_legs(
                legs=legs,
                fees=self.fees,
                gross_quote=0.0,
                underlying_notional_quote=N,
                other_components_quote={"fees": 1.0},
            )
        res = ce.evaluate_legs(
            legs=legs,
            fees=self.fees,
            gross_quote=0.0,
            underlying_notional_quote=N,
            other_components_quote={"transfer": 0.2001},
        )
        self.assertAlmostEqual(res.components_bps["fees"], 20.0)
        self.assertAlmostEqual(res.components_bps["half_spread_slip"], 1.0 / N * 1e4, places=4)
        self.assertAlmostEqual(res.components_bps["transfer"], 1.0)


# --------------------------------------------------------------------------- evaluate / breakeven


class EvaluateTest(unittest.TestCase):
    def a1(self, **kw) -> ce.CostResult:
        base = dict(
            gross_quote=A1_GROSS,
            notional_quote=N,
            components_quote=A1_BUCKETS,
            hold_years=HOLD_1D,
            funding=ce.FundingContext(
                intervals=3, interval_sec=H8, funding_gross_quote=A1_GROSS, rate_used_per_interval=0.00025
            ),
        )
        base.update(kw)
        return ce.evaluate(**base)

    def test_all_in_is_sum_of_rounded_components_and_net_is_gross_minus_all_in(self):
        r = self.a1()
        self.assertAlmostEqual(r.components_bps["fees"], 30.0)
        self.assertAlmostEqual(r.components_bps["half_spread_slip"], 7.4963)
        self.assertAlmostEqual(r.components_bps["hedge_rebalance"], 2.0)
        self.assertAlmostEqual(r.components_bps["funding_uncertainty"], 7.5)
        self.assertAlmostEqual(r.all_in_cost_bps, 46.9963)
        self.assertAlmostEqual(r.all_in_cost_bps, round(sum(r.components_bps.values()), 4))
        self.assertAlmostEqual(r.gross_edge_bps, 7.5)
        self.assertAlmostEqual(r.net_edge_bps, 7.5 - 46.9963)
        self.assertEqual(r.flags, [])
        d = r.to_dict()
        self.assertEqual(set(d["components_bps"]), set(ce.CORE_COMPONENTS) | {"funding_expected"})
        self.assertEqual(d["components_extra"], ["funding_expected"])
        self.assertAlmostEqual(d["all_in_cost_quote"], sum(A1_BUCKETS.values()))

    def test_breakeven_funding_identity(self):
        r = self.a1()
        be = r.breakeven_funding
        # f* = other costs / (H × N): all costs here (basis not credited, no funding cost)
        self.assertAlmostEqual(be["rate_per_interval"], sum(A1_BUCKETS.values()) / (3 * N), places=9)
        self.assertAlmostEqual(be["bps_per_interval"], 15.6654)
        self.assertAlmostEqual(be["rate_implied_by_inputs_per_interval"], 0.00025)
        self.assertEqual(be["rate_used_per_interval"], 0.00025)
        # headroom × H reproduces net edge; used rate below breakeven → negative net
        self.assertAlmostEqual(be["headroom_bps_per_interval"] * 3, r.net_edge_bps, places=3)
        self.assertLess(0.00025, be["rate_per_interval"])
        self.assertLess(r.net_edge_bps, 0)
        self.assertEqual(r.breakeven_funding_rate, be["rate_per_interval"])
        self.assertIs(be["apr_ref"]["display_only"], True)
        self.assertIs(be["apr_ref"]["return_promise"], False)
        self.assertAlmostEqual(be["apr_ref"]["value"], be["rate_per_interval"] * 1095, places=6)

    def test_breakeven_is_a_threshold_that_flips_net_sign(self):
        # feed exactly the breakeven rate back in as the funding credit → net ≈ 0
        r = self.a1()
        f_star = r.breakeven_funding["rate_per_interval"]
        r0 = self.a1(
            gross_quote=f_star * 3 * N,
            funding=ce.FundingContext(intervals=3, funding_gross_quote=f_star * 3 * N),
        )
        self.assertAlmostEqual(r0.net_edge_bps, 0.0, places=3)
        r_plus = self.a1(
            gross_quote=(f_star + 0.0001) * 3 * N,
            funding=ce.FundingContext(intervals=3, funding_gross_quote=(f_star + 0.0001) * 3 * N),
        )
        self.assertAlmostEqual(r_plus.net_edge_bps, 3.0, places=3)  # 1 bp × 3 intervals

    def test_breakeven_with_funding_paid_as_cost_and_other_gross(self):
        # Phase B B1-like: negative funding paid sits in funding_expected; premium edge in gross
        comps = {**A1_BUCKETS, "funding_expected": 1.2006, "vol_path_haircut": 2.001}
        r = ce.evaluate(
            gross_quote=3.0,
            notional_quote=N,
            components_quote=comps,
            funding=ce.FundingContext(
                intervals=3, funding_gross_quote=0.0, funding_cost_quote=1.2006, rate_used_per_interval=-0.0002
            ),
        )
        be = r.breakeven_funding
        other_cost = sum(comps.values()) - 1.2006
        self.assertAlmostEqual(be["rate_per_interval"], (other_cost - 3.0) / (3 * N), places=9)
        self.assertAlmostEqual(be["rate_implied_by_inputs_per_interval"], -0.0002)
        self.assertAlmostEqual(be["headroom_bps_per_interval"] * 3, r.net_edge_bps, places=3)
        self.assertIn("vol_path_haircut", r.components_bps)
        self.assertAlmostEqual(r.components_bps["vol_path_haircut"], 10.0)
        self.assertEqual(r.flags, [])  # vol_path_haircut is a known extra

    def test_no_funding_context_or_zero_horizon_gives_none_with_reason(self):
        r = ce.evaluate(gross_quote=7.0, notional_quote=N, components_quote=A1_BUCKETS, hold_years=0.25)
        self.assertIsNone(r.breakeven_funding_rate)
        self.assertEqual(r.breakeven_funding["reason"], "no_funding_context")
        self.assertEqual(r.hold_years, 0.25)
        r0 = ce.evaluate(
            gross_quote=0.0, notional_quote=N, components_quote=A1_BUCKETS, funding=ce.FundingContext(intervals=0)
        )
        self.assertEqual(r0.breakeven_funding["reason"], "zero_horizon_intervals")
        self.assertIsNone(r0.hold_years)
        # hold_years derived from the funding context when not given
        r1 = self.a1(hold_years=None)
        self.assertAlmostEqual(r1.hold_years, HOLD_1D)

    def test_missing_unknown_negative_components_are_flagged_not_dropped(self):
        r = ce.evaluate(
            gross_quote=0.0,
            notional_quote=N,
            components_quote={"fees": 6.003, "mystery": 0.2001, "borrow": -0.1},
        )
        self.assertEqual(r.components_bps["fees"], 30.0)
        self.assertEqual(r.components_bps["mystery"], 1.0)
        self.assertEqual(r.components_bps["impact"], 0.0)
        self.assertAlmostEqual(r.all_in_cost_bps, 30.0 + 1.0 - 0.4998)
        flags = " ".join(r.flags)
        self.assertIn("core_components_defaulted_to_zero:", flags)
        self.assertIn("half_spread_slip", flags)
        self.assertIn("unknown_extra_component:mystery", flags)
        self.assertIn("negative_cost_component:borrow", flags)

    def test_input_validation(self):
        with self.assertRaises(ce.CostEngineError):
            ce.evaluate(gross_quote=0.0, notional_quote=0.0, components_quote={})
        with self.assertRaises(ce.CostEngineError):
            ce.evaluate(gross_quote=0.0, notional_quote=N, components_quote={"fees": 1, "total": 1})
        with self.assertRaises(ce.CostEngineError):
            ce.evaluate(gross_quote=0.0, notional_quote=N, components_quote={}, hold_years=-1.0)
        with self.assertRaises(ce.CostEngineError):
            ce.FundingContext(intervals=-1)
        with self.assertRaises(ce.CostEngineError):
            ce.FundingContext(intervals=3, funding_gross_quote=-1.0)
        with self.assertRaises(ce.CostEngineError):
            ce.FundingLeg(rate_now=0.0003, horizon_intervals=3, receiver="spot")

    def test_funding_leg_sign_conventions_and_flip(self):
        # short perp receives positive funding; min(|now|, |next|) same sign
        fl = ce.FundingLeg(rate_now=0.0003, horizon_intervals=3, rate_next=0.0004)
        self.assertEqual(fl.used_rate_per_interval(), (0.0003, []))
        # long perp receives negative funding
        fl2 = ce.FundingLeg(rate_now=-0.0004, horizon_intervals=3, receiver="long_perp")
        self.assertEqual(fl2.used_rate_per_interval(), (0.0004, []))
        ctx = fl2.context(N)
        self.assertAlmostEqual(ctx.funding_gross_quote, 0.0004 * 3 * N)
        self.assertEqual(ctx.funding_cost_quote, 0.0)
        # short perp with negative funding pays → cost bucket
        fl3 = ce.FundingLeg(rate_now=-0.0002, horizon_intervals=3)
        ctx3 = fl3.context(N)
        self.assertEqual(ctx3.funding_gross_quote, 0.0)
        self.assertAlmostEqual(ctx3.funding_cost_quote, 0.0002 * 3 * N)
        # predicted flip → 0 and flagged
        fl4 = ce.FundingLeg(rate_now=0.0003, horizon_intervals=3, rate_next=-0.0001)
        self.assertEqual(fl4.used_rate_per_interval(), (0.0, ["funding_sign_flip_predicted"]))
        self.assertEqual(fl4.expected_quote(N), 0.0)
        self.assertAlmostEqual(fl4.pred_gap_rate(), 0.0002)
        # next == 0 → 0 used, no flip flag
        fl5 = ce.FundingLeg(rate_now=0.0003, horizon_intervals=3, rate_next=0.0)
        self.assertEqual(fl5.used_rate_per_interval(), (0.0, []))

    def test_summarize_blocks(self):
        blocks = [self.a1().to_dict(), ce.evaluate(gross_quote=7.0, notional_quote=N, components_quote=A1_BUCKETS).to_dict()]
        s = ce.summarize_blocks(blocks)
        self.assertEqual((s["records"], s["records_with_breakeven"]), (2, 1))
        self.assertAlmostEqual(s["median_breakeven_funding_bps_per_interval"], 15.6654)
        self.assertAlmostEqual(s["median_all_in_cost_bps"], 46.9963)
        self.assertIs(s["calibrated"], False)
        self.assertIs(s["tradable_claim_allowed"], False)
        self.assertEqual(s["version"], "v0")
        empty = ce.summarize_blocks([], enabled=False)
        self.assertEqual((empty["records"], empty["median_all_in_cost_bps"]), (0, None))
        self.assertIs(empty["enabled"], False)


# --------------------------------------------------------------------------- fixture cases / cli


class FixtureCasesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = ce.run_cases(CASES)
        cls.by_id = {c["id"]: c for c in cls.report["cases"]}

    def test_all_cases_reproduce(self):
        failing = [c for c in self.report["cases"] if not c["ok"]]
        self.assertEqual(failing, [], json.dumps(failing, ensure_ascii=False, indent=1))
        self.assertIs(self.report["all_ok"], True)
        self.assertGreaterEqual(self.report["cases_total"], 15)
        self.assertEqual(self.report["action"], "observe_only")
        self.assertIs(self.report["will_send_http"], False)
        self.assertIs(self.report["calibrated"], False)
        self.assertIs(self.report["data_source"]["http_fetch"], False)
        self.assertTrue(all(v is False for v in self.report["trading_http"].values()))

    def test_refusal_cases_are_exercised(self):
        refused = {c["id"]: c["refused"] for c in self.report["cases"] if c.get("refused")}
        self.assertEqual(refused["refused_annualized_gross"], "NaiveAnnualizationRefused")
        self.assertEqual(
            refused["refused_funding_leg_horizon_over_one_year"], "NaiveAnnualizationRefused"
        )
        self.assertEqual(refused["refused_mark_price_as_executable"], "ExecutablePriceViolation")
        self.assertEqual(refused["refused_buy_at_bid"], "ExecutablePriceViolation")
        self.assertEqual(refused["refused_one_sided_book"], "ExecutablePriceViolation")
        self.assertEqual(refused["refused_calibrated_claim_without_ref"], "CalibrationClaimRefused")
        self.assertEqual(refused["refused_total_passed_as_component"], "CostEngineError")

    def test_a1_case_matches_hand_numbers_and_scanner_record(self):
        res = self.by_id["a1_like_positive_carry_buckets"]["result"]
        rec = a1_record()
        self.assertAlmostEqual(res["all_in_cost_bps"], rec["costs_bps"]["total"])
        self.assertAlmostEqual(res["net_edge_bps"], rec["net_edge_bps"])
        self.assertAlmostEqual(
            res["breakeven_funding_rate"], rec["cost_engine"]["breakeven_funding_rate"]
        )

    def test_case_mismatch_and_unexpected_result_are_reported(self):
        bad = ce.run_case(
            {"id": "x", "notional_quote": N, "components_quote": {"fees": 6.003}, "expect": {"all_in_cost_bps": 31.0}}
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["diffs"]["all_in_cost_bps"], {"expect": 31.0, "got": 30.0})
        surprise = ce.run_case(
            {"id": "y", "notional_quote": N, "components_quote": {}, "expect_error": "CostEngineError"}
        )
        self.assertFalse(surprise["ok"])
        wrong_kind = ce.run_case(
            {"id": "z", "notional_quote": 0.0, "components_quote": {}, "expect_error": "NaiveAnnualizationRefused"}
        )
        self.assertFalse(wrong_kind["ok"])
        self.assertEqual(wrong_kind["refused"], "CostEngineError")

    def test_cli_replays_cases_offline(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "report.json"
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--cases", str(CASES), "--out", str(out), "--full"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("observe_only", proc.stderr)
            self.assertIn("calibrated=false", proc.stderr)
            rep = json.loads(out.read_text(encoding="utf-8"))
            self.assertIs(rep["all_ok"], True)
            self.assertTrue(all("result" in c or c.get("refused") for c in rep["cases"]))
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT, env={"PATH": "/usr/bin:/bin"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertNotIn("result", rep["cases"][0])  # compact by default

    def test_cli_exit_1_on_mismatch_and_help(self):
        with tempfile.TemporaryDirectory() as d:
            cases = Path(d) / "bad.json"
            cases.write_text(
                json.dumps(
                    {"cases": [{"id": "bad", "notional_quote": N, "components_quote": {"fees": 6.003}, "expect": {"all_in_cost_bps": 1.0}}]}
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--cases", str(cases), "--quiet"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("0/1 cases ok", proc.stderr)
        proc = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe_only", proc.stdout)
        self.assertIn("calibrated=false", proc.stdout)


# --------------------------------------------------------------------------- scanner wiring


class PhaseAWiringTest(unittest.TestCase):
    def test_a1_record_carries_consistent_block_with_breakeven(self):
        rec = a1_record()
        blk = rec["cost_engine"]
        self.assertEqual(blk["version"], "v0")
        self.assertEqual(blk["action"], "observe_only")
        self.assertIs(blk["will_send_http"], False)
        self.assertIs(blk["calibrated"], False)
        self.assertIs(blk["tradable_claim_allowed"], False)
        self.assertIs(blk["annualized"], False)
        self.assertAlmostEqual(blk["all_in_cost_bps"], rec["costs_bps"]["total"])
        self.assertAlmostEqual(blk["net_edge_bps"], rec["net_edge_bps"])
        self.assertAlmostEqual(blk["gross_edge_bps"], rec["gross_edge_bps"])
        for k in pas.COST_KEYS:
            self.assertAlmostEqual(blk["components_bps"][k], rec["costs_bps"][k], msg=k)
        self.assertAlmostEqual(blk["hold_years"], HOLD_1D, places=8)
        be = blk["breakeven_funding"]
        self.assertAlmostEqual(be["rate_per_interval"], 0.0015665417)
        self.assertEqual(be["intervals"], 3.0)
        self.assertEqual(be["rate_used_per_interval"], 0.00025)
        self.assertAlmostEqual(be["headroom_bps_per_interval"] * 3, rec["net_edge_bps"], places=3)
        # the funding used is 2.5 bp/interval, well below the ~15.7 bp breakeven → negative net
        self.assertFalse(rec["passes_threshold"])
        # the record's own fields are untouched by the block
        self.assertAlmostEqual(rec["costs_bps"]["fees"], 30.0)
        self.assertEqual(rec["safety_buffer"]["calibrated"], False)

    def test_negative_funding_direction_and_sign_flip(self):
        rec = a1_record(rate=-0.0004, next_rate=-0.0003)
        be = rec["cost_engine"]["breakeven_funding"]
        self.assertEqual(rec["direction"], "short_spot_long_perp")
        self.assertAlmostEqual(be["rate_implied_by_inputs_per_interval"], 0.0003)  # received
        self.assertAlmostEqual(be["headroom_bps_per_interval"] * 3, rec["net_edge_bps"], places=3)
        flip = a1_record(rate=0.0003, next_rate=-0.0001)
        self.assertIn("funding_sign_flip_predicted", flip["risk_flags"])
        self.assertEqual(flip["cost_engine"]["breakeven_funding"]["rate_implied_by_inputs_per_interval"], 0.0)

    def test_no_cost_engine_flag_removes_block_only(self):
        on, off = a1_record(True), a1_record(False)
        self.assertIn("cost_engine", on)
        self.assertNotIn("cost_engine", off)
        for k in ("costs_bps", "net_edge_bps", "gross_edge_bps", "safety_buffer_bps", "passes_threshold"):
            self.assertEqual(on[k], off[k], k)

    def test_finalize_refuses_tampered_block(self):
        rec = a1_record()
        blk = rec["cost_engine"]
        with self.assertRaises(pas.ObserveOnlyViolation):
            pas.finalize_record({**rec, "cost_engine": {**blk, "will_send_http": True}})
        with self.assertRaises(pas.ObserveOnlyViolation):
            pas.finalize_record({**rec, "cost_engine": {**blk, "action": "execute"}})
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "cost_engine": {**blk, "tradable_claim_allowed": True}})
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "cost_engine": {**blk, "annualized": True}})
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "cost_engine": {**blk, "all_in_cost_bps": blk["all_in_cost_bps"] + 1}})
        with self.assertRaises(ValueError):
            pas.cost_engine_block(
                gross_quote=A1_GROSS,
                notional_quote=N,
                costs_quote=A1_BUCKETS,
                cost_keys=pas.COST_KEYS,
                costs_bps_total=1.0,  # inconsistent with the buckets → refused
                net_bps=0.0,
                hold_years=HOLD_1D,
                funding_ctx=None,
            )

    def test_fixture_replay_every_record_consistent_a2_a3_without_breakeven(self):
        snaps = pas.load_fixture(FIXTURE_A)
        scanner = pas.ArbScanner(pas.ScanConfig())
        records = [r for s in snaps for r in scanner.scan(s)]
        self.assertTrue(records)
        for r in records:
            blk = r["cost_engine"]
            self.assertAlmostEqual(blk["all_in_cost_bps"], r["costs_bps"]["total"], msg=r["family"])
            self.assertAlmostEqual(blk["net_edge_bps"], r["net_edge_bps"], msg=r["family"])
            self.assertIs(blk["calibrated"], False)
            self.assertEqual(blk["action"], "observe_only")
            if r["family"] == pas.FAMILY_A1:
                self.assertIsNotNone(blk["breakeven_funding_rate"])
            else:
                self.assertIsNone(blk["breakeven_funding_rate"])
                self.assertEqual(blk["breakeven_funding"]["reason"], "no_funding_context")
                self.assertAlmostEqual(blk["hold_years"], r["T_years"], places=5)
        s = pas.summarize(records, len(snaps), pas.ScanConfig(), snaps[0].source)
        self.assertIs(s["cost_engine"]["enabled"], True)
        self.assertEqual(s["cost_engine"]["records"], len(records))
        self.assertEqual(s["cost_engine"]["records_with_breakeven"], len(snaps))  # one A1 per snapshot
        self.assertIs(s["cost_engine"]["calibrated"], False)
        self.assertIs(s["config"]["cost_engine"]["enabled"], True)
        self.assertIs(s["config"]["cost_engine"]["calibrated"], False)
        text = json.dumps(s, ensure_ascii=False).lower()
        for bad in ce.FORBIDDEN_LABELS:
            self.assertNotIn(bad, text)

    def test_cli_no_cost_engine(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "strategies" / "paper_arb_scanner.py"), "--no-cost-engine", "--print-summary"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(rows)
        self.assertTrue(all("cost_engine" not in r for r in rows))
        summary = json.loads(proc.stderr)
        self.assertIs(summary["cost_engine"]["enabled"], False)
        self.assertEqual(summary["cost_engine"]["records"], 0)


class PhaseBWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = pcs.load_fixture(FIXTURE_B)
        cls.scanner = pcs.ComboScanner(pcs.ComboConfig())
        cls.records = [r for s in cls.snaps for r in cls.scanner.scan(s)]

    def test_every_record_consistent_and_carries_vol_path_haircut(self):
        self.assertTrue(self.records)
        fams = set()
        for r in self.records:
            blk = r["cost_engine"]
            self.assertAlmostEqual(blk["all_in_cost_bps"], r["costs_bps"]["total"], msg=r["family"])
            self.assertAlmostEqual(blk["net_edge_bps"], r["net_edge_bps"], msg=r["family"])
            for k in pcs.COST_KEYS:
                self.assertAlmostEqual(blk["components_bps"][k], r["costs_bps"][k], msg=k)
            self.assertEqual(blk["components_extra"], ["funding_expected", "vol_path_haircut"])
            self.assertEqual(blk["flags"], [])
            self.assertIs(blk["calibrated"], False)
            self.assertAlmostEqual(blk["hold_years"], r["hold"]["hold_years"], places=6)
            if r["family"] == pcs.FAMILY_B1:
                self.assertIsNotNone(blk["breakeven_funding_rate"])
                be = blk["breakeven_funding"]
                self.assertAlmostEqual(
                    be["headroom_bps_per_interval"] * be["intervals"], r["net_edge_bps"], places=2
                )
            else:
                self.assertIsNone(blk["breakeven_funding_rate"])
            fams.add(r["family"])
        self.assertEqual(fams, set(pcs.FAMILIES))

    def test_summary_and_a1_comparator_share_the_engine_flag(self):
        s = pcs.summarize(
            self.records,
            len(self.snaps),
            pcs.ComboConfig(),
            self.snaps[0].source,
            self.scanner.skipped,
            self.scanner.a1_reference_records,
        )
        self.assertIs(s["cost_engine"]["enabled"], True)
        self.assertEqual(s["cost_engine"]["records"], len(self.records))
        self.assertIs(s["cost_engine"]["calibrated"], False)
        self.assertIs(s["config"]["cost_engine"]["enabled"], True)
        # the A1 comparator runs with the same flag
        self.assertTrue(all("cost_engine" in r for r in self.scanner.a1_reference_records))
        off = pcs.ComboScanner(pcs.ComboConfig(cost_engine=False))
        recs = [r for s_ in self.snaps[:1] for r in off.scan(s_)]
        self.assertTrue(recs)
        self.assertTrue(all("cost_engine" not in r for r in recs))
        self.assertTrue(all("cost_engine" not in r for r in off.a1_reference_records))
        self.assertIs(pcs.ComboConfig(cost_engine=False).phase_a_config().cost_engine, False)

    def test_finalize_combo_refuses_tampered_block(self):
        rec = self.records[0]
        blk = rec["cost_engine"]
        with self.assertRaises(pas.ObserveOnlyViolation):
            pcs.finalize_combo_record({**rec, "cost_engine": {**blk, "will_send_http": True}})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "cost_engine": {**blk, "tradable_claim_allowed": True}})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "cost_engine": {**blk, "all_in_cost_bps": 0.0}})
        no_vph = {**blk, "components_bps": {k: v for k, v in blk["components_bps"].items() if k != "vol_path_haircut"}}
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "cost_engine": no_vph})


if __name__ == "__main__":
    unittest.main()
