"""Offline unit tests for strategies/paper_combo_expansion.py (C1–C4 expansion; no network).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import cost_engine as ce  # noqa: E402
import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
import paper_combo_expansion as pce  # noqa: E402
import paper_combo_scanner as pcs  # noqa: E402
from test_paper_arb_scanner import EXP_MS, FakeOkx  # noqa: E402

FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-expansion-books-sample.json"
FIXTURE_B = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-combo-books-sample.json"
SCRIPT = ROOT / "strategies" / "paper_combo_expansion.py"

T0 = 1_789_573_200_000
YEAR_MS = int(365 * 86_400_000)
NEAR = T0 + YEAR_MS // 4  # T = 0.25y
FAR = T0 + YEAR_MS // 2  # T = 0.50y
FR = pas.Funding(rate=0.0003, next_rate=0.00025, interval_sec=28_800)


def book(inst, kind, bid, ask, sz=10.0, levels=3, tick=1.0, **kw) -> pas.Book:
    bids = tuple(pas.Level(bid - i * tick, sz) for i in range(levels))
    asks = tuple(pas.Level(ask + i * tick, sz) for i in range(levels))
    return pas.Book(inst_id=inst, kind=kind, bids=bids, asks=asks, ts_ms=T0, **kw)


def spot_perp(spot_bid=2399.0, spot_ask=2401.0, perp_bid=2402.0, perp_ask=2403.0, sz=10.0):
    s = book("ETH-USDT", "spot", spot_bid, spot_ask, sz=sz)
    p = book("ETH-USDT-SWAP", "perp", perp_bid, perp_ask, sz=sz * 5, mark_px=2402.5)
    return s, p


def future(inst="ETH-USDT-261030", expiry=NEAR, bid=2415.0, ask=2415.4, sz=30.0) -> pas.Book:
    return book(inst, "future", bid, ask, sz=sz, tick=0.2, expiry_ms=expiry, settle_ccy="USDT")


def snap(spot=None, perp=None, funding=None, options=(), futures=(), ts=T0, borrow=None):
    return pas.Snapshot(
        ts_ms=ts,
        venue="okx",
        spot=spot,
        perp=perp,
        funding=funding,
        options=tuple(options),
        futures=tuple(futures),
        spot_borrow_apr=borrow,
    )


def cfg(**kw) -> pce.ExpansionConfig:
    base = dict(persistence_min_samples=1, persistence_min_sec=0.0)
    base.update(kw)
    return pce.ExpansionConfig(**base)


def leg(inst, side, px, role, qty=1.0, expiry=None, strike=None, premium_ccy="quote") -> dict:
    return pas.Leg(
        inst,
        side,
        qty,
        px,
        pas.SIDE_TO_PRICE_TYPE[side],
        role,
        premium_ccy=premium_ccy,
        expiry_ms=expiry,
        strike=strike,
    ).to_dict()


# --------------------------------------------------------------------------- hard gates


class HardGateTest(unittest.TestCase):
    def test_constants_and_policy(self):
        self.assertEqual(pce.ACTION, "observe_only")
        self.assertIs(pce.WILL_SEND_HTTP, False)
        self.assertEqual(pce.PHASE, "C")
        self.assertEqual(pce.TAXONOMY, "relative_value")
        self.assertEqual(pce.EXPANSIONS, ("C1", "C2", "C3", "C4"))
        self.assertEqual(pce.DEFAULT_UNDERLYING, "ETH")
        self.assertEqual(pce.NET_EDGE_SOURCE, "tools/cost_engine.py:evaluate")
        for k in pas.REQUIRED_FIELDS:
            self.assertIn(k, pce.REQUIRED_FIELDS)  # Phase A schema inherited
        for k in (
            "phase",
            "expansion_id",
            "combo_id",
            "residual_risks",
            "liquidity_ok",
            "model",
            "calibrated",
            "sample_coverage",
            "cost_engine",
            "net_edge_source",
        ):
            self.assertIn(k, pce.REQUIRED_FIELDS)
        pol = pce.POLICY
        self.assertIs(pol["will_send_http"], False)
        self.assertIs(pol["live_execution"], False)
        self.assertTrue(all(v is False for v in pol["trading_http"].values()))
        self.assertEqual(pol["leverage_concept_max"], 1)
        self.assertEqual(pol["C4"]["max_half_spread_bps"], 25.0)
        self.assertEqual(pol["C4"]["min_size_contracts"], 1.0)
        self.assertEqual(pol["C4"]["min_notional_quote"], 50.0)
        self.assertEqual(pol["C3"]["coverage_gate_n"], 5)
        self.assertIn("current_funding_times_365_as_net_edge", pol["rejected"])
        self.assertIn("naked_short_vol", pol["rejected"])
        self.assertIn("cross_venue_latency", pol["rejected"])
        self.assertIn("multi_leg_live_ioc", pol["rejected"])

    def test_module_has_no_trading_http_path_and_uses_cost_engine_only(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in (
            "/api/v5/trade",
            "urlopen",
            'method="POST"',
            "place_order(",
            "amend_algo(",
            "withdraw(",
            "transfer(",
        ):
            self.assertNotIn(needle, src, needle)
        self.assertNotIn("import local_paper_grid", src)
        self.assertNotIn("import grid_ab_compare", src)
        # net edge comes from the engine — no inline gross − cost arithmetic in this module
        self.assertIn("ce.evaluate(", src)
        self.assertIn("gross_basis=ce.EDGE_BASIS", src)
        self.assertNotIn("gross_bps - ", src)
        self.assertNotIn("- costs_bps", src)
        self.assertNotIn("* 365", src)
        self.assertNotIn("* 1095", src)

    def test_underlying_gate_eth_default_btc_needs_named_flag(self):
        pce.ExpansionConfig().validate()
        with self.assertRaises(pce.UnderlyingNotApproved):
            pce.ExpansionConfig(underlying="BTC").validate()
        pce.ExpansionConfig(underlying="BTC", allow_btc=True).validate()  # named flag only
        with self.assertRaises(pce.UnderlyingNotApproved):
            pce.ExpansionConfig(underlying="SOL", allow_btc=True).validate()
        s = book("BTC-USDT", "spot", 60000.0, 60010.0)
        p = book("BTC-USDT-SWAP", "perp", 60020.0, 60030.0)
        with self.assertRaises(pce.UnderlyingNotApproved):
            pce.ExpansionScanner(cfg()).scan(snap(s, p, FR))

    def test_config_validation_pins_risk_calibrations(self):
        for bad in (
            dict(liquidity=pce.LiquidityFilter(max_half_spread_bps=50.0)),  # > 04-risk cap
            dict(liquidity=pce.LiquidityFilter(max_half_spread_bps=0.0)),
            dict(liquidity=pce.LiquidityFilter(min_size_contracts=0.5)),
            dict(liquidity=pce.LiquidityFilter(min_notional_quote=10.0)),
            dict(liquidity=pce.LiquidityFilter(mode="pass")),
            dict(coverage_n=3),
            dict(c3_mechanism="strike_rung"),
            dict(c3_band_step=0.10),
            dict(c1_hold="forever"),
            dict(c2_decay=1.0),
            dict(expansions=("C5",)),
            dict(expansions=()),
            dict(qty=0),
        ):
            with self.assertRaises(ValueError):
                pce.ExpansionConfig(**bad).validate()
        with self.assertRaises(ce.CalibrationClaimRefused):
            pce.ExpansionConfig(buffer=pas.SafetyBuffer(calibrated=True)).validate()
        d = pce.ExpansionConfig().to_dict()
        self.assertEqual(d["leverage_concept"], 1)
        self.assertIs(d["cost_engine"]["mandatory"], True)
        self.assertIs(d["safety_buffer"]["calibrated"], False)
        self.assertIs(d["c2"]["annualized_used_in_net_edge"], False)
        self.assertEqual(d["c1"]["hedge_note_only"], ["far_month_futures", "perp"])

    def test_relaxation_is_exactly_one_symmetric_step(self):
        base = pcs.ComboConfig()
        relaxed, deltas = pce.relax_phase_b_config(base)
        self.assertEqual((base.b1_delta_min, base.b1_delta_max), (0.15, 0.25))
        self.assertAlmostEqual(relaxed.b1_delta_min, 0.10)
        self.assertAlmostEqual(relaxed.b1_delta_max, 0.30)
        self.assertAlmostEqual(relaxed.b1_moneyness_min, 0.0)
        self.assertAlmostEqual(relaxed.b1_moneyness_max, 0.35)
        self.assertAlmostEqual(relaxed.b3_delta_tol, 0.15)
        # everything else untouched: cover rule knobs, targets, hedge modes, costs
        for k in (
            "b1_max_calls_per_expiry",
            "b3_target_delta",
            "b3_wing_delta",
            "b2_opt_type",
            "vol_path_haircut_bps",
            "hedge_instrument",
            "qty",
        ):
            self.assertEqual(getattr(relaxed, k), getattr(base, k), k)
        self.assertEqual(deltas["mechanism"], pce.C3_MECHANISM)
        self.assertEqual(deltas["strike_rung_mechanism"], "not_used")
        self.assertEqual(deltas["expiry_pm1_mechanism"], "not_used")
        self.assertEqual(deltas["b1_delta_band"]["relaxed"], [relaxed.b1_delta_min, 0.30])


# --------------------------------------------------------------------------- C4 liquidity filter


class LiquidityFilterTest(unittest.TestCase):
    def setUp(self):
        self.spot, self.perp = spot_perp()
        self.lf = pce.LiquidityFilter()

    def opt_book(self, inst, bid, ask, sz=6.0):
        return pas.Book(
            inst_id=inst,
            kind="option",
            bids=(pas.Level(bid, sz),),
            asks=(pas.Level(ask, sz),),
            opt_type="C",
            strike=2600.0,
            expiry_ms=NEAR,
            premium_ccy="base",
        )

    def test_tight_legs_pass_and_basis_is_underlying_notional(self):
        tight = self.opt_book("OPT-TIGHT", 0.0400, 0.0410)  # half 0.0005 ETH ≈ 2.1 bps
        books = {self.spot.inst_id: self.spot, tight.inst_id: tight}
        legs = [leg("ETH-USDT", "buy", 2401.0, "spot"), leg("OPT-TIGHT", "sell", 0.04, "call")]
        ok, block = pce.liquidity_filter(legs, books, 2400.0, self.lf)
        self.assertTrue(ok)
        self.assertEqual(block["failed_legs"], [])
        opt_row = block["legs"][1]
        self.assertAlmostEqual(opt_row["half_spread_bps"], 0.0005 * 2400.0 / 2400.0 * 1e4, 3)
        self.assertEqual(opt_row["size_contracts"], 6.0)
        self.assertEqual(block["half_spread_basis"], "bps_of_underlying_notional_per_unit")

    def test_wide_spread_leg_is_dropped(self):
        wide = self.opt_book("OPT-WIDE", 0.030, 0.050)  # half 0.010 ETH = 100 bps of notional
        books = {self.spot.inst_id: self.spot, wide.inst_id: wide}
        legs = [leg("ETH-USDT", "buy", 2401.0, "spot"), leg("OPT-WIDE", "buy", 0.05, "call")]
        ok, block = pce.liquidity_filter(legs, books, 2400.0, self.lf)
        self.assertFalse(ok)
        self.assertEqual(block["failed_legs"], ["OPT-WIDE"])
        self.assertIn("half_spread_gt_max", block["legs"][1]["reasons"])
        self.assertGreater(block["legs"][1]["half_spread_bps"], 25.0)

    def test_min_size_and_notional_and_one_sided(self):
        tiny = self.opt_book("OPT-TINY", 0.0400, 0.0410, sz=0.5)  # < 1 contract (1.0 base)
        books = {self.spot.inst_id: self.spot, tiny.inst_id: tiny}
        ok, block = pce.liquidity_filter(
            [leg("OPT-TINY", "buy", 0.041, "call")], books, 2400.0, self.lf
        )
        self.assertFalse(ok)
        self.assertIn("size_lt_min_contracts", block["legs"][0]["reasons"])
        # a 0.5-base top size is still > 50 USDT notional at 2400 → notional alone passes
        self.assertNotIn("notional_lt_min", block["legs"][0]["reasons"])
        # contract_size_base = 0.1 (OKX ETH option ctVal style) → 0.5 base = 5 contracts → ok
        ok2, _ = pce.liquidity_filter(
            [leg("OPT-TINY", "buy", 0.041, "call")],
            books,
            2400.0,
            pce.LiquidityFilter(contract_size_base=0.1),
        )
        self.assertTrue(ok2)
        # tiny notional: 0.01 base × 2400 = 24 USDT < 50
        micro = self.opt_book("OPT-MICRO", 0.0400, 0.0410, sz=0.01)
        ok3, block3 = pce.liquidity_filter(
            [leg("OPT-MICRO", "buy", 0.041, "call")],
            {**books, "OPT-MICRO": micro},
            2400.0,
            pce.LiquidityFilter(contract_size_base=0.01),
        )
        self.assertFalse(ok3)
        self.assertEqual(block3["legs"][0]["reasons"], ["notional_lt_min"])
        one_sided = pas.Book("OPT-ONE", "option", (pas.Level(0.04, 6.0),), (), premium_ccy="base")
        ok4, block4 = pce.liquidity_filter(
            [leg("OPT-ONE", "sell", 0.04, "call")], {"OPT-ONE": one_sided}, 2400.0, self.lf
        )
        self.assertFalse(ok4)
        self.assertEqual(block4["legs"][0]["reasons"], ["one_sided_book"])


# --------------------------------------------------------------------------- C2 stub funding path


class FundingStubTest(unittest.TestCase):
    def test_path_sums_over_horizon_never_times_365(self):
        c = cfg()
        m = pce.funding_expectation_stub(FR, 3, c)
        self.assertEqual(m["model"], "stub")
        self.assertIs(m["calibrated"], False)
        self.assertIs(m["annualized_used_in_net_edge"], False)
        self.assertEqual(len(m["path_per_interval_received"]), 3)
        self.assertAlmostEqual(
            m["funding_expected_bps"], sum(m["path_per_interval_received"]) * 1e4, places=3
        )
        self.assertEqual(m["rate_used_per_interval_received"], 0.00025)  # min(|now|, |next|)
        self.assertEqual(m["anchor_source"], "config_long_run_rate")  # no history on FR
        naive = m["current_funding_annualized_ref"]
        self.assertAlmostEqual(naive["bps"], 0.0003 * 1095 * 1e4, places=3)
        self.assertIs(naive["display_only"], True)
        self.assertIs(naive["is_net_edge"], False)
        self.assertIs(naive["banned_from_net_edge"], True)
        # the hold-horizon expectation is ~3 intervals, three orders of magnitude below × 1095
        self.assertLess(m["funding_expected_bps"], naive["bps"] / 100)
        # decay pulls the path toward the anchor
        path = m["path_per_interval_received"]
        self.assertTrue(path[0] > path[1] > path[2] > c.c2_long_run_rate)

    def test_history_anchor_sigma_and_sign_flip(self):
        fr = pas.Funding(rate=0.0003, next_rate=0.00025, history=(0.0002, 0.0003, 0.0004))
        m = pce.funding_expectation_stub(fr, 3, cfg())
        self.assertEqual(m["anchor_source"], "history_mean_last_3")
        self.assertAlmostEqual(m["anchor_rate_published"], 0.0003)
        self.assertEqual(m["sigma_source"], "history_pstdev")
        self.assertGreater(m["funding_uncertainty_bps"], 0)
        flip = pce.funding_expectation_stub(pas.Funding(rate=0.0003, next_rate=-0.0001), 3, cfg())
        self.assertEqual(flip["rate_used_per_interval_received"], 0.0)
        self.assertIn("funding_sign_flip_predicted", flip["flags"])
        neg = pce.funding_expectation_stub(pas.Funding(rate=-0.0002), 3, cfg())
        self.assertEqual(neg["receiver"], "long_perp")
        self.assertGreater(neg["funding_expected_rate_hold_horizon"], 0)  # received by long perp
        with self.assertRaises(ce.NaiveAnnualizationRefused):  # a year of intervals is not a path
            pce.funding_expectation_stub(FR, 2000, cfg())

    def test_basis_confirmation(self):
        s, p = spot_perp()  # perp mid 2402.5 vs spot mid 2400 → +10.4 bps
        c = pce.basis_confirmation(s, p, expected_published=0.0005, tol_bps=5.0)
        self.assertEqual(c["status"], "confirmed_same_direction")
        self.assertFalse(c["conflict"])
        c2 = pce.basis_confirmation(s, p, expected_published=-0.0005, tol_bps=5.0)
        self.assertTrue(c2["conflict"])
        self.assertEqual(c2["status"], "conflict_opposite_direction")
        c3 = pce.basis_confirmation(s, p, expected_published=-0.0005, tol_bps=20.0)
        self.assertEqual(c3["status"], "within_tolerance_neutral")
        self.assertIs(c["basis_mid_ref_only"], True)


# --------------------------------------------------------------------------- C1


class C1Test(unittest.TestCase):
    def test_cash_and_carry_two_legs_bid_ask_and_engine_net(self):
        s, p = spot_perp()
        near = future(bid=2415.0, ask=2415.4)
        far = future("ETH-USDT-261225", FAR, 2430.0, 2430.6)
        sc = pce.ExpansionScanner(cfg())
        recs = sc.scan_c1(snap(s, p, FR, futures=[far, near]))
        self.assertEqual(len(recs), 1)  # nearest expiry only; far is hedge_note
        r = recs[0]
        self.assertEqual(r["combo_id"], "C1")
        self.assertEqual(r["expansion_id"], "C1")
        self.assertEqual(r["phase"], "C")
        self.assertEqual(r["taxonomy"], "relative_value")
        self.assertEqual(r["direction"], "long_spot_short_future_cash_and_carry")
        self.assertEqual(
            [
                (x["instrument"], x["side"], x["price_type"], x["executable_price"])
                for x in r["legs"]
            ],
            [("ETH-USDT", "buy", "ask", 2401.0), ("ETH-USDT-261030", "sell", "bid", 2415.0)],
        )
        self.assertEqual(len(r["legs"]), 2)
        N = 2401.0
        self.assertAlmostEqual(r["gross_edge_bps"], (2415.0 - 2401.0) / N * 1e4, places=3)
        # fees: spot in + out (2 × 10) + future entry (5, perp taker assumed) + settlement 2
        self.assertAlmostEqual(r["costs_bps"]["fees"], 27.0, places=3)
        self.assertAlmostEqual(r["costs_bps"]["half_spread_slip"], 1.0 / N * 1e4, places=3)
        self.assertEqual(r["costs_bps"]["funding_uncertainty"], 0.0)
        ceb = r["cost_engine"]
        self.assertEqual(r["net_edge_bps"], ceb["net_edge_bps"])
        self.assertEqual(r["gross_edge_bps"], ceb["gross_edge_bps"])
        self.assertEqual(r["costs_bps"]["total"], ceb["all_in_cost_bps"])
        self.assertEqual(ceb["breakeven_funding"]["reason"], "no_funding_context")
        self.assertIs(ceb["calibrated"], False)
        self.assertEqual(r["net_edge_source"], pce.NET_EDGE_SOURCE)
        notes = {h["instrument"]: h for h in r["hedge_note"]}
        self.assertEqual(set(notes), {"ETH-USDT-261225", "ETH-USDT-SWAP"})
        self.assertTrue(all(h["tradeable_default"] is False for h in notes.values()))
        self.assertEqual(notes["ETH-USDT-261225"]["role"], "hedge_note")
        curve = r["term_structure_curve_ref"]
        self.assertEqual(curve[0]["instrument"], "ETH-USDT-261030")
        self.assertIs(curve[0]["tradeable_default"], True)
        self.assertIs(curve[0]["basis_apr_ref"]["display_only"], True)
        fv = r["fair_value"]
        self.assertEqual(fv["model"], "stub")
        self.assertIs(fv["calibrated"], False)
        self.assertIs(fv["used_in_net_edge"], False)
        self.assertEqual(fv["fair_ref"], 2400.0)  # r = 0 → S_mid
        self.assertEqual(r["model"], "stub")
        self.assertIs(r["calibrated"], False)
        for rr in pce.RESIDUAL_RISKS[pce.FAMILY_C1]:
            self.assertIn(rr, r["residual_risks"])
        self.assertNotIn("spot_borrow", r["residual_risks"])
        self.assertTrue(r["liquidity_ok"])
        self.assertEqual(r["sample_coverage"]["family_key"], "C1")

    def test_two_tradeable_expiries_flag_and_horizon_hold(self):
        s, p = spot_perp()
        near, far = future(), future("ETH-USDT-261225", FAR, 2430.0, 2430.6)
        sc = pce.ExpansionScanner(cfg(c1_tradeable_expiries=2, c1_hold="horizon"))
        recs = sc.scan_c1(snap(s, p, FR, futures=[near, far]))
        self.assertEqual(
            [r["future"]["instrument"] for r in recs], ["ETH-USDT-261030", "ETH-USDT-261225"]
        )
        self.assertTrue(all(len(r["legs"]) == 2 for r in recs))
        r = recs[0]
        self.assertIs(r["hold"]["hold_to_expiry"], False)
        self.assertAlmostEqual(r["costs_bps"]["fees"], 30.0, places=3)  # both legs exit crossing
        self.assertGreater(r["costs_bps"]["half_spread_slip"], 1.0 / 2401.0 * 1e4)
        self.assertEqual([h["instrument"] for h in r["hedge_note"]], ["ETH-USDT-SWAP"])

    def test_reverse_requires_borrow_residuals_and_is_invalidated_without_borrow(self):
        s, p = spot_perp()
        back = future(bid=2390.0, ask=2390.4)  # backwardation → short spot / long future
        r = pce.ExpansionScanner(cfg()).scan_c1(snap(s, p, FR, futures=[back]))[0]
        self.assertEqual(r["direction"], "short_spot_long_future_reverse_cash_and_carry")
        self.assertEqual(r["legs"][0]["side"], "sell")
        self.assertEqual(r["legs"][0]["price_type"], "bid")
        self.assertTrue({"spot_borrow", "spot_borrow_fee"} <= set(r["residual_risks"]))
        self.assertIn("borrow_unavailable", r["invalidated_by"])
        self.assertIs(r["passes_threshold"], False)
        r2 = pce.ExpansionScanner(cfg()).scan_c1(snap(s, p, FR, futures=[back], borrow=0.05))[0]
        self.assertNotIn("borrow_unavailable", r2["invalidated_by"])
        self.assertGreater(r2["costs_bps"]["borrow"], 0.0)

    def test_no_futures_or_expired_yields_no_record(self):
        s, p = spot_perp()
        sc = pce.ExpansionScanner(cfg())
        self.assertEqual(sc.scan_c1(snap(s, p, FR)), [])
        self.assertEqual(sc.skipped["c1_no_dated_futures"], 1)
        expired = future(expiry=T0 - 1000)
        self.assertEqual(sc.scan_c1(snap(s, p, FR, futures=[expired])), [])


# --------------------------------------------------------------------------- C2


class C2Test(unittest.TestCase):
    def test_gross_is_the_path_sum_and_engine_gives_breakeven(self):
        s, p = spot_perp()
        r = pce.ExpansionScanner(cfg()).scan_c2(snap(s, p, FR))[0]
        self.assertEqual(r["combo_id"], "C2")
        self.assertEqual(r["direction"], "long_spot_short_perp")
        self.assertEqual(len(r["legs"]), 2)
        m = r["funding_model"]
        self.assertEqual(m["model"], "stub")
        self.assertIs(m["annualized_used_in_net_edge"], False)
        self.assertAlmostEqual(r["gross_edge_bps"], m["funding_expected_bps"], places=3)
        self.assertEqual(r["funding_expected"], m["funding_expected_bps"])
        self.assertEqual(r["funding_uncertainty"], m["funding_uncertainty_bps"])
        self.assertNotAlmostEqual(r["gross_edge_bps"], 0.0003 * 1095 * 1e4, places=0)
        ceb = r["cost_engine"]
        self.assertIsNotNone(ceb["breakeven_funding_rate"])
        self.assertEqual(r["net_edge_bps"], ceb["net_edge_bps"])
        self.assertAlmostEqual(
            ceb["breakeven_funding"]["headroom_bps_per_interval"] * 3, r["net_edge_bps"], places=2
        )
        self.assertEqual(r["basis_confirmation"]["status"], "confirmed_same_direction")
        self.assertIs(r["passes_threshold"], False)  # 30 bps fees vs ~8 bps path expectation
        for rr in pce.RESIDUAL_RISKS[pce.FAMILY_C2]:
            self.assertIn(rr, r["residual_risks"])
        self.assertEqual(
            r["cost_engine"]["components_bps"]["funding_uncertainty"],
            r["costs_bps"]["funding_uncertainty"],
        )

    def test_basis_conflict_invalidates(self):
        s, p = spot_perp(perp_bid=2395.0, perp_ask=2396.0)  # perp below spot, funding positive
        r = pce.ExpansionScanner(cfg()).scan_c2(snap(s, p, FR))[0]
        self.assertTrue(r["basis_confirmation"]["conflict"])
        self.assertIn("basis_conflict_with_funding_expectation", r["invalidated_by"])
        self.assertIs(r["passes_threshold"], False)

    def test_negative_funding_short_spot_needs_borrow(self):
        s, p = spot_perp(perp_bid=2396.0, perp_ask=2397.0)
        neg = pas.Funding(rate=-0.0003, next_rate=-0.0002, interval_sec=28_800)
        r = pce.ExpansionScanner(cfg()).scan_c2(snap(s, p, neg))[0]
        self.assertEqual(r["direction"], "short_spot_long_perp")
        self.assertTrue({"spot_borrow", "spot_borrow_fee"} <= set(r["residual_risks"]))
        self.assertIn("borrow_unavailable", r["invalidated_by"])
        self.assertEqual(r["basis_confirmation"]["status"], "confirmed_same_direction")

    def test_missing_inputs(self):
        s, p = spot_perp()
        sc = pce.ExpansionScanner(cfg())
        self.assertEqual(sc.scan_c2(snap(s, p, None)), [])
        self.assertEqual(sc.skipped["c2_no_funding"], 1)
        self.assertEqual(sc.scan_c2(snap(s, None, FR)), [])


# --------------------------------------------------------------------------- finalize gates


class FinalizeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s, p = spot_perp()
        sc = pce.ExpansionScanner(cfg())
        cls.c1 = sc.scan_c1(snap(s, p, FR, futures=[future()]))[0]
        cls.c2 = sc.scan_c2(snap(s, p, FR))[0]
        snaps = pce.load_fixture(FIXTURE)
        sc2 = pce.ExpansionScanner(cfg())
        rows = sc2.scan(snaps[0])
        cls.c3_b1 = next(r for r in rows if r["combo_id"] == "C3-B1")
        cls.c4 = next(r for r in rows if r["combo_id"] == "C4-A2" and r["liquidity_ok"])

    def test_observe_only_and_schema(self):
        rec = self.c1
        for bad in ({"action": "place_order"}, {"will_send_http": True}):
            with self.assertRaises(pas.ObserveOnlyViolation):
                pce.finalize_expansion_record({**rec, **bad})
        with self.assertRaises(pas.ObserveOnlyViolation):
            pce.finalize_expansion_record(
                {**rec, "cost_engine": {**rec["cost_engine"], "will_send_http": True}}
            )
        for bad in (
            {"taxonomy": "identity_approx"},
            {"taxonomy": "risk_free"},
            {"phase": "B"},
            {"combo_id": "B1"},
            {"expansion_id": "C9"},
            {"hypothesis_id": "H-C2"},
            {"net_edge_source": "inline"},
            {"leverage_concept": 2},
            {"residual_risks": []},
            {"residual_risks": ["gap"]},
            {"risk_flags": [f for f in rec["risk_flags"] if f != "relative_value_not_riskless"]},
            {"fair_value": {**rec["fair_value"], "model": "calibrated_curve"}},
            {"fair_value": {**rec["fair_value"], "used_in_net_edge": True}},
            {"hedge_note": [{"instrument": "X", "tradeable_default": True}]},
            {"legs": rec["legs"] + [rec["legs"][0]]},  # 3 legs on C1
            {"cost_engine": {**rec["cost_engine"], "net_edge_bps": rec["net_edge_bps"] + 1}},
            {"cost_engine": {**rec["cost_engine"], "tradable_claim_allowed": True}},
            {"cost_engine": {**rec["cost_engine"], "annualized": True}},
            {"cost_engine": {**rec["cost_engine"], "calibrated": True}},
        ):
            with self.assertRaises(ValueError, msg=str(bad)[:60]):
                pce.finalize_expansion_record({**rec, **bad})
        with self.assertRaises(ce.CalibrationClaimRefused):
            pce.finalize_expansion_record({**rec, "calibrated": True})
        with self.assertRaises(pas.ExecutablePriceViolation):
            pce.finalize_expansion_record(
                {**rec, "legs": [{**rec["legs"][0], "price_type": "mark"}]}
            )
        for label in ("risk_free", "无风险套利", "稳赚", "guaranteed"):
            with self.assertRaises(pce.ForbiddenLabelViolation):
                pce.finalize_expansion_record({**rec, "note": f"this is {label}"})
        missing = {k: v for k, v in rec.items() if k != "liquidity_ok"}
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record(missing)
        pce.finalize_expansion_record(rec)  # unchanged record is valid

    def test_short_spot_without_borrow_residuals_refused(self):
        rec = dict(self.c1)
        rec["legs"] = [
            {**rec["legs"][0], "side": "sell", "price_type": "bid"},
            {**rec["legs"][1], "side": "buy", "price_type": "ask"},
        ]
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record(rec)
        rec["residual_risks"] = sorted(
            set(rec["residual_risks"]) | {"spot_borrow", "spot_borrow_fee"}
        )
        pce.finalize_expansion_record(rec)

    def test_c2_annualised_path_refused_and_conflict_must_invalidate(self):
        rec = self.c2
        with self.assertRaises(ce.NaiveAnnualizationRefused):
            pce.finalize_expansion_record(
                {
                    **rec,
                    "funding_model": {**rec["funding_model"], "annualized_used_in_net_edge": True},
                }
            )
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record(
                {**rec, "basis_confirmation": {**rec["basis_confirmation"], "conflict": True}}
            )
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record(
                {**rec, "funding_model": {**rec["funding_model"], "model": "calibrated"}}
            )

    def test_illiquid_can_never_pass_and_cover_rules_reasserted(self):
        rec = self.c4
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record({**rec, "liquidity_ok": False, "passes_threshold": True})
        with self.assertRaises(pce.ExpansionSchemaViolation):  # illiquid must carry the flag
            pce.finalize_expansion_record({**rec, "liquidity_ok": False, "passes_threshold": False})
        with self.assertRaises(pce.ExpansionSchemaViolation):
            pce.finalize_expansion_record(
                {**rec, "passes_threshold": True, "invalidated_by": ["x"]}
            )
        with self.assertRaises(pce.ExpansionSchemaViolation):  # C4 does not target B1
            pce.finalize_expansion_record({**rec, "base_family": pcs.FAMILY_B1})
        with self.assertRaises(pce.ExpansionSchemaViolation):  # a stacked "optimising" 4th leg
            pce.finalize_expansion_record({**rec, "legs": rec["legs"] + [rec["legs"][0]]})
        b1 = self.c3_b1
        with self.assertRaises(pas.NakedShortOptionRefused):  # drop spot → naked call
            pce.finalize_expansion_record({**b1, "legs": b1["legs"][1:]})
        with self.assertRaises(pas.NakedShortOptionRefused):  # perp cover is not spot cover for B1
            pce.finalize_expansion_record(
                {
                    **b1,
                    "legs": [{**b1["legs"][1], "side": "buy", "price_type": "ask"}, b1["legs"][2]],
                }
            )
        with self.assertRaises(pce.ExpansionSchemaViolation):  # C3 does not target A2
            pce.finalize_expansion_record({**b1, "base_family": pas.FAMILY_A2_CONV})
        pce.finalize_expansion_record(b1)


# --------------------------------------------------------------------------- fixture replay


class FixtureReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = pce.load_fixture(FIXTURE)
        cls.scanner = pce.ExpansionScanner(pce.ExpansionConfig())
        cls.records = [r for s in cls.snaps for r in cls.scanner.scan(s)]
        cls.summary = pce.summarize(
            cls.records, len(cls.snaps), pce.ExpansionConfig(), cls.snaps[0].source, cls.scanner
        )

    def test_fixture_shape(self):
        self.assertEqual(len(self.snaps), 6)
        self.assertTrue(all(s.source["http_fetch"] is False for s in self.snaps))
        self.assertEqual(
            [f.inst_id for f in self.snaps[0].futures], ["ETH-USDT-261030", "ETH-USDT-261225"]
        )
        self.assertEqual(self.snaps[0].futures[0].kind, "future")
        self.assertEqual(len(self.snaps[0].funding.history), 12)
        self.assertEqual(len(self.snaps[0].options), 24)
        self.assertIn("SYNTHETIC", self.snaps[0].source["note"])
        # Phase A / B scanners still load and run on the expansion fixture (futures ignored)
        self.assertTrue(pas.ArbScanner(pas.ScanConfig()).scan(self.snaps[0]))
        self.assertTrue(pcs.ComboScanner(pcs.ComboConfig()).scan(self.snaps[0]))

    def test_every_record_passes_all_gates(self):
        self.assertGreater(len(self.records), 0)
        self.assertEqual({r["expansion_id"] for r in self.records}, set(pce.EXPANSIONS))
        for r in self.records:
            for k in pce.REQUIRED_FIELDS:
                self.assertIn(k, r)
            self.assertEqual(r["action"], "observe_only")
            self.assertIs(r["will_send_http"], False)
            self.assertEqual(r["phase"], "C")
            self.assertEqual(r["taxonomy"], "relative_value")
            self.assertTrue(r["combo_id"].startswith(r["expansion_id"]))
            self.assertIs(r["calibrated"], False)
            self.assertEqual(r["net_edge_source"], pce.NET_EDGE_SOURCE)
            self.assertEqual(r["leverage_concept"], 1)
            self.assertIn("relative_value_not_riskless", r["risk_flags"])
            self.assertTrue(r["residual_risks"])
            ceb = r["cost_engine"]
            self.assertEqual(ceb["action"], "observe_only")
            self.assertIs(ceb["will_send_http"], False)
            self.assertIs(ceb["calibrated"], False)
            self.assertIs(ceb["tradable_claim_allowed"], False)
            self.assertIs(ceb["annualized"], False)
            self.assertAlmostEqual(ceb["net_edge_bps"], r["net_edge_bps"], places=6)
            self.assertAlmostEqual(ceb["all_in_cost_bps"], r["costs_bps"]["total"], places=6)
            self.assertLessEqual(len(r["legs"]), pce.MAX_LEGS[r["family"]])
            for lg in r["legs"]:
                self.assertIn(lg["price_type"], ("bid", "ask"))
                self.assertEqual(lg["price_type"], "ask" if lg["side"] == "buy" else "bid")
            if not r["liquidity_ok"]:
                self.assertIs(r["passes_threshold"], False)
                self.assertIn("c4_illiquid_leg", r["invalidated_by"])
            self.assertTrue(r["timestamp"].endswith("+08:00"))
            text = json.dumps(r, ensure_ascii=False).lower()
            for bad in pce.FORBIDDEN_LABELS:
                self.assertNotIn(bad, text)
        self.assertEqual(dict(self.scanner.skipped), {})

    def test_c1_paths(self):
        c1 = [r for r in self.records if r["expansion_id"] == "C1"]
        self.assertEqual(len(c1), 6)  # one tradeable future per snapshot
        self.assertTrue(all(len(r["legs"]) == 2 for r in c1))
        self.assertTrue(all(r["future"]["instrument"] == "ETH-USDT-261030" for r in c1))
        self.assertTrue(
            all(
                {h["instrument"] for h in r["hedge_note"]} == {"ETH-USDT-261225", "ETH-USDT-SWAP"}
                for r in c1
            )
        )
        # 1–3 gross>0 cost-killed; 4–5 net>0 but under buffer; 6 reverse → borrow invalidated
        self.assertTrue(all(r["gross_edge_bps"] > 0 and r["net_edge_bps"] < 0 for r in c1[:3]))
        self.assertTrue(
            all(r["net_edge_bps"] > 0 and not r["edge_exceeds_buffer"] for r in c1[3:5])
        )
        self.assertEqual(c1[5]["direction"], "short_spot_long_future_reverse_cash_and_carry")
        self.assertIn("borrow_unavailable", c1[5]["invalidated_by"])
        self.assertFalse(any(r["passes_threshold"] for r in c1))

    def test_c2_paths(self):
        c2 = [r for r in self.records if r["expansion_id"] == "C2"]
        self.assertEqual(len(c2), 6)
        self.assertTrue(
            all(r["funding_model"]["anchor_source"] == "history_mean_last_12" for r in c2)
        )
        self.assertTrue(all(r["funding_model"]["annualized_used_in_net_edge"] is False for r in c2))
        self.assertTrue(all(r["cost_engine"]["breakeven_funding_rate"] is not None for r in c2))
        self.assertEqual([r["basis_confirmation"]["conflict"] for r in c2], [False] * 5 + [True])
        self.assertIn("basis_conflict_with_funding_expectation", c2[5]["invalidated_by"])
        self.assertTrue(all(r["net_edge_bps"] < 0 for r in c2))

    def test_c3_relaxation_yields_coverage_where_defaults_had_none(self):
        c3 = [r for r in self.records if r["expansion_id"] == "C3"]
        b1 = [r for r in c3 if r["combo_id"] == "C3-B1"]
        b3 = [r for r in c3 if r["combo_id"] == "C3-B3"]
        self.assertEqual(len(b1), 18)  # 3 per snapshot × 6
        self.assertEqual(len(b3), 12)
        self.assertEqual(self.scanner.coverage_baseline.get("B1", 0), 0)  # Phase B defaults: none
        self.assertEqual(self.scanner.coverage_baseline["B3"], 12)  # Δ step does not add B3 rows
        self.assertEqual({r["base_family"] for r in b1}, {pcs.FAMILY_B1})
        self.assertEqual({r["base_phase"] for r in c3}, {"B"})
        self.assertEqual({r["taxonomy_base"] for r in c3}, {"relative_value"})
        self.assertEqual({r["hedge_mode"] for r in b1}, {"static_combo"})
        self.assertTrue(all(r["cover"][0]["cover_type"] == "spot_notional" for r in b1))
        self.assertTrue(all(len(r["legs"]) == 3 for r in c3))
        self.assertEqual({r["strike"] for r in b1}, {2500.0, 3200.0, 3400.0})
        self.assertTrue(all(r["expansion"]["mechanism"] == pce.C3_MECHANISM for r in c3))
        self.assertTrue(all("vol_path_haircut" in r["costs_bps"] for r in c3))  # Phase B costs kept
        self.assertTrue(all(r["liquidity_ok"] for r in c3))
        last = [r for r in b1 if r["ts_ms"] == self.snaps[-1].ts_ms][-1]
        self.assertEqual(
            last["sample_coverage"],
            {"family_key": "B1", "n_in_window_so_far": 18, "gate_n": 5, "gate_met_so_far": True},
        )

    def test_c4_filter_flags_wide_or_tiny_legs_and_never_passes_them(self):
        c4 = [r for r in self.records if r["expansion_id"] == "C4"]
        self.assertEqual({r["combo_id"] for r in c4}, {"C4-A2", "C4-A3"})
        self.assertEqual({r["taxonomy_base"] for r in c4}, {"identity_approx"})
        self.assertEqual({r["base_phase"] for r in c4}, {"A"})
        bad = [r for r in c4 if not r["liquidity_ok"]]
        self.assertTrue(bad)
        self.assertTrue(
            all(
                "ETH-USD-261030-1800-C" in r["liquidity_filter"]["failed_legs"]
                or "ETH-USD-261225-1800-C" in r["liquidity_filter"]["failed_legs"]
                or "ETH-USD-261225-3400-P" in r["liquidity_filter"]["failed_legs"]
                for r in bad
            )
        )
        wide = next(
            r for r in bad if "ETH-USD-261030-1800-C" in r["liquidity_filter"]["failed_legs"]
        )
        row = next(
            x
            for x in wide["liquidity_filter"]["legs"]
            if x["instrument"] == "ETH-USD-261030-1800-C"
        )
        self.assertIn("half_spread_gt_max", row["reasons"])
        self.assertIn("size_lt_min_contracts", row["reasons"])
        self.assertGreater(row["half_spread_bps"], 25.0)
        self.assertTrue(all(r["passes_threshold"] is False for r in bad))
        good = [r for r in c4 if r["liquidity_ok"]]
        self.assertTrue(good)
        self.assertTrue(
            all(x["half_spread_bps"] <= 25.0 for r in good for x in r["liquidity_filter"]["legs"])
        )
        self.assertTrue(all(len(r["legs"]) <= 4 for r in c4))
        # skip mode drops them instead of flagging
        sc = pce.ExpansionScanner(
            pce.ExpansionConfig(expansions=("C4",), liquidity=pce.LiquidityFilter(mode="skip"))
        )
        rows = [r for s in self.snaps for r in sc.scan(s)]
        self.assertEqual(len(rows), len(good))
        self.assertEqual(sc.skipped["c4_illiquid_skipped"], len(bad))
        self.assertTrue(all(r["liquidity_ok"] for r in rows))

    def test_summary_and_falsifiable_metrics(self):
        s = self.summary
        self.assertEqual(s["action"], "observe_only")
        self.assertIs(s["will_send_http"], False)
        self.assertIs(s["live_execution"], False)
        self.assertEqual(s["phase"], "C")
        self.assertTrue(all(v is False for v in s["trading_http"].values()))
        self.assertEqual(s["underlying"], "ETH")
        self.assertEqual(set(s["hypotheses"]), {"H-C1", "H-C2", "H-C3", "H-C4"})
        fm = s["falsifiable_metrics"]
        for k in (
            "net_edge_bps_gt_0_rate",
            "cost_kill_rate",
            "sample_coverage_B1",
            "sample_coverage_B3",
        ):
            self.assertIn(k, fm)
        self.assertEqual(fm["sample_coverage_B1"]["n"], 18)
        self.assertEqual(fm["sample_coverage_B1"]["baseline_n_phase_b_defaults"], 0)
        self.assertEqual(fm["sample_coverage_B1"]["delta_n_from_relaxation"], 18)
        self.assertIs(fm["sample_coverage_B1"]["gate_met"], True)
        self.assertEqual(fm["sample_coverage_B1"]["verdict"], pce.VERDICT_COST_VETO)
        self.assertEqual(fm["sample_coverage_B3"]["n"], 12)
        self.assertIn("vs_all", fm["cost_kill_rate"])
        self.assertIn("vs_gross_gt_0", fm["cost_kill_rate"])
        self.assertIs(fm["calibrated"], False)
        self.assertIs(fm["tradable_claim_allowed"], False)
        self.assertEqual(s["expansions"]["C1"]["net_gt_0"], 3)
        self.assertEqual(s["expansions"]["C3"]["mechanism"]["mechanism"], pce.C3_MECHANISM)
        self.assertIn("B1", s["expansions"]["C3"]["by_base_family"])
        self.assertGreater(s["expansions"]["C4"]["illiquid_flagged"], 0)
        self.assertEqual(s["cost_engine"]["records"], len(self.records))
        self.assertIs(s["cost_engine"]["mandatory"], True)
        self.assertEqual(s["mainline_unchanged"], "spot_grid_local_paper")
        self.assertEqual(s["related_scanners"], "A1_A2_A3_B1_B2_B3_scanners_remain")
        self.assertIs(s["config"]["safety_buffer"]["calibrated"], False)
        self.assertEqual(s["relaxed_phase_b_config"]["b1"]["delta_band"], [0.1, 0.3])
        text = json.dumps(s, ensure_ascii=False).lower()
        for banned in pce.FORBIDDEN_LABELS + ("falsified", "strategy is dead"):
            self.assertNotIn(banned, text)

    def test_coverage_gate_blocks_verdict_below_n(self):
        self.assertEqual(pce.coverage_verdict(3, 5, 0), pce.VERDICT_COVERAGE_INSUFFICIENT)
        self.assertEqual(pce.coverage_verdict(3, 5, 2), pce.VERDICT_COVERAGE_INSUFFICIENT)
        self.assertEqual(pce.coverage_verdict(5, 5, 0), pce.VERDICT_COST_VETO)
        self.assertEqual(pce.coverage_verdict(5, 5, 1), pce.VERDICT_NET_POSITIVE)
        for v in (
            pce.VERDICT_COVERAGE_INSUFFICIENT,
            pce.VERDICT_COST_VETO,
            pce.VERDICT_NET_POSITIVE,
        ):
            for bad in pce.BANNED_VERDICT_WORDS:
                self.assertNotIn(bad, v)
        # a single snapshot: B1 n = 3 < 5 → no verdict either way
        sc = pce.ExpansionScanner(pce.ExpansionConfig())
        rows = sc.scan(self.snaps[0])
        s = pce.summarize(rows, 1, pce.ExpansionConfig(), self.snaps[0].source, sc)
        cov = s["falsifiable_metrics"]["sample_coverage_B1"]
        self.assertEqual(
            (cov["n"], cov["gate_met"], cov["verdict"]),
            (3, False, pce.VERDICT_COVERAGE_INSUFFICIENT),
        )
        self.assertEqual(s["hypotheses"]["H-C1"]["status"], pce.VERDICT_COVERAGE_INSUFFICIENT)
        # a higher N pushes even the full window under the gate
        big = pce.ExpansionConfig(coverage_n=100)
        s2 = pce.summarize(self.records, 6, big, self.snaps[0].source, pce.ExpansionScanner(big))
        self.assertEqual(
            s2["falsifiable_metrics"]["sample_coverage_B3"]["verdict"],
            pce.VERDICT_COVERAGE_INSUFFICIENT,
        )

    def test_phase_b_fixture_has_no_futures_so_c1_skips_but_c2_c3_c4_run(self):
        sc = pce.ExpansionScanner(pce.ExpansionConfig())
        rows = [r for s in pce.load_fixture(FIXTURE_B) for r in sc.scan(s)]
        self.assertEqual(sc.skipped["c1_no_dated_futures"], 4)
        self.assertEqual({r["expansion_id"] for r in rows}, {"C2", "C3", "C4"})
        b1 = [r for r in rows if r["combo_id"] == "C3-B1"]
        self.assertEqual(len(b1), sc.coverage_baseline["B1"])  # both bands find the same 2 calls


# --------------------------------------------------------------------------- cli


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )

    def test_evaluate_fixture_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ = Path(d) / "exp.jsonl", Path(d) / "summary.json"
            proc = self.run_cli(
                "evaluate-fixture",
                "--fixture",
                str(FIXTURE),
                "--paper-fills",
                "--out",
                str(out),
                "--summary-out",
                str(summ),
                "--quiet",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
            self.assertGreater(len(rows), 0)
            self.assertTrue(all(r["action"] == "observe_only" for r in rows))
            self.assertTrue(all(r["will_send_http"] is False for r in rows))
            self.assertTrue(all(r["combo_id"][:2] in ("C1", "C2", "C3", "C4") for r in rows))
            self.assertTrue(all("cost_engine" in r for r in rows))
            self.assertTrue(all(r["paper_fill"]["order_sent"] is False for r in rows))
            s = json.loads(summ.read_text())
            self.assertEqual(s["records"], len(rows))
            self.assertEqual(s["snapshots"], 6)
            self.assertIs(s["trading_http"]["order"], False)
            metrics = json.loads(proc.stderr)
            self.assertEqual(metrics["cmd"], "evaluate-fixture")
            self.assertIn("sample_coverage_B1", metrics["falsifiable_metrics"])

    def test_scan_default_only_exceeding_print_summary_and_policy(self):
        proc = self.run_cli("--only-exceeding", "--print-summary")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(all(r["edge_exceeds_buffer"] for r in rows))
        summary = json.loads(proc.stderr)
        self.assertEqual(summary["action"], "observe_only")
        self.assertEqual(summary["phase"], "C")
        proc = self.run_cli("policy")
        self.assertEqual(proc.returncode, 0)
        pol = json.loads(proc.stdout)
        self.assertIs(pol["will_send_http"], False)
        self.assertIs(pol["live_execution"], False)
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe_only", proc.stdout)
        self.assertIn("relative_value", proc.stdout)

    def test_cli_refuses_btc_without_flag_and_looser_than_risk_filters(self):
        proc = self.run_cli("--underlying", "BTC", "--quiet")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("allow-btc", proc.stderr)
        proc = self.run_cli("--c4-max-half-spread-bps", "50", "--quiet")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("04-risk", proc.stderr)
        proc = self.run_cli("--coverage-n", "2", "--quiet")
        self.assertEqual(proc.returncode, 1)


# --------------------------------------------------------------------------- okx public (fake, GET)

FUT_EXP_MS = EXP_MS


class FakeOkxWithFutures(FakeOkx):
    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == okx.PATH_INSTRUMENTS and q.get("instType") == "FUTURES":
            FakeOkx.requests.append({"method": "GET", "path": u.path, "query": q})
            rows = [
                {
                    "instId": "ETH-USDT-261030",
                    "instType": "FUTURES",
                    "instFamily": "ETH-USDT",
                    "settleCcy": "USDT",
                    "ctVal": "0.1",
                    "ctValCcy": "ETH",
                    "expTime": str(FUT_EXP_MS),
                    "state": "live",
                },
                {
                    "instId": "ETH-USDT-260918",
                    "instType": "FUTURES",
                    "instFamily": "ETH-USDT",
                    "settleCcy": "USDT",
                    "ctVal": "0.1",
                    "expTime": str(1_789_600_000_000),
                    "state": "live",
                },  # too near → excluded
            ]
            return self._send({"code": "0", "msg": "", "data": rows})
        if u.path == okx.PATH_BOOKS and q.get("instId") == "ETH-USDT-261030":
            FakeOkx.requests.append({"method": "GET", "path": u.path, "query": q})
            return self._send(
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "bids": [["2412", "300", "0", "1"]],
                            "asks": [["2412.4", "280", "0", "1"]],
                            "ts": "1789573200000",
                        }
                    ],
                }
            )
        return super().do_GET()


class OkxPublicSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOkxWithFutures)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOkx.requests.clear()

    def test_source_adds_dated_futures_and_scan_is_get_only(self):
        c = okx.OkxPublicClient(base_url=self.base)
        src = pce.ExpansionSnapshotSource(
            c,
            n_strikes=5,
            max_expiries=1,
            min_days_to_expiry=2.0,
            book_depth=1,
            fut_family="ETH-USDT",
        )
        s = src.fetch()
        self.assertEqual([f.inst_id for f in s.futures], ["ETH-USDT-261030"])
        self.assertEqual(s.futures[0].kind, "future")
        self.assertEqual(s.futures[0].bids[0].sz, 30.0)  # 300 contracts × ctVal 0.1
        self.assertEqual(s.source["future_books"], 1)
        recs = pce.ExpansionScanner(cfg()).scan(s)
        self.assertIn("C1", {r["expansion_id"] for r in recs})
        self.assertTrue(all(r["will_send_http"] is False for r in recs))
        self.assertTrue(all(r["method"] == "GET" for r in FakeOkx.requests))
        self.assertEqual(
            {r["path"] for r in FakeOkx.requests},
            {okx.PATH_BOOKS, okx.PATH_FUNDING_RATE, okx.PATH_INSTRUMENTS},
        )
        self.assertFalse(any(r["method"] == "POST" for r in FakeOkx.requests))

    def test_cli_okx_public_against_fake_server(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "scan",
                "--source",
                "okx-public",
                "--base-url",
                self.base,
                "--samples",
                "1",
                "--n-strikes",
                "5",
                "--max-expiries",
                "1",
                "--print-summary",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        s = json.loads(proc.stderr)
        self.assertEqual(s["data_source"]["kind"], "okx_public_books")
        self.assertEqual(s["data_source"]["future_books"], 1)
        self.assertIs(s["will_send_http"], False)
        self.assertIs(s["live_execution"], False)
        self.assertGreater(s["expansions"]["C1"]["records"], 0)
        self.assertFalse(any(r["method"] == "POST" for r in FakeOkx.requests))


if __name__ == "__main__":
    unittest.main()
