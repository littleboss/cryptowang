"""Offline unit tests for strategies/paper_combo_scanner.py (Phase B combos; no network).

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
import paper_combo_scanner as pcs  # noqa: E402
from test_paper_arb_scanner import FakeOkx  # noqa: E402

FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-combo-books-sample.json"
FIXTURE_A = ROOT / "fixtures" / "arb_books" / "2026-09-16-eth-books-sample.json"
SCRIPT = ROOT / "strategies" / "paper_combo_scanner.py"

T0 = 1_789_573_200_000
YEAR_MS = int(365 * 86_400_000)
NEAR = T0 + YEAR_MS // 4  # T = 0.25y
FAR = T0 + YEAR_MS // 2  # T = 0.50y
F_REF = 2002.5  # perp mid of spot_perp() → reference forward
SIGMA = 0.6


def book(inst, kind, bid, ask, sz=10.0, levels=3, tick=1.0, **kw) -> pas.Book:
    bids = tuple(pas.Level(bid - i * tick, sz) for i in range(levels))
    asks = tuple(pas.Level(ask + i * tick, sz) for i in range(levels))
    return pas.Book(inst_id=inst, kind=kind, bids=bids, asks=asks, ts_ms=T0, **kw)


def spot_perp(spot_bid=1999.0, spot_ask=2001.0, perp_bid=2002.0, perp_ask=2003.0, sz=10.0):
    s = book("ETH-USDT", "spot", spot_bid, spot_ask, sz=sz)
    p = book("ETH-USDT-SWAP", "perp", perp_bid, perp_ask, sz=sz * 5, mark_px=2002.5)
    return s, p


def opt(strike, typ, expiry=NEAR, sigma=SIGMA, mult=1.0, half=1.0, bid=None, ask=None) -> pas.Book:
    """Quote-priced option book. Default: Black-76 fair on F_REF at `sigma`, ±`half` spread."""
    T = (expiry - T0) / pcs.MS_PER_YEAR
    mid = pcs.black76(F_REF, strike, T, sigma, typ == "C").price * mult
    b = mid - half if bid is None else bid
    a = mid + half if ask is None else ask
    tag = "N" if expiry == NEAR else "F"
    return book(
        f"OPT-{tag}-{strike:g}-{typ}",
        "option",
        b,
        a,
        sz=10.0,
        tick=0.5,
        opt_type=typ,
        strike=float(strike),
        expiry_ms=expiry,
        premium_ccy="quote",
    )


def snap(spot=None, perp=None, funding=None, options=(), ts=T0) -> pas.Snapshot:
    return pas.Snapshot(
        ts_ms=ts, venue="okx", spot=spot, perp=perp, funding=funding, options=tuple(options)
    )


def cfg(**kw) -> pcs.ComboConfig:
    base = dict(persistence_min_samples=1, persistence_min_sec=0.0)
    base.update(kw)
    return pcs.ComboConfig(**base)


def leg(inst, side, px, role, qty=1.0, expiry=NEAR, strike=None) -> pas.Leg:
    return pas.Leg(
        inst,
        side,
        qty,
        px,
        pas.SIDE_TO_PRICE_TYPE[side],
        role,
        expiry_ms=expiry if role in ("call", "put") else None,
        strike=strike,
    )


FR = pas.Funding(rate=0.0003, next_rate=0.00025, interval_sec=28_800)


def atm_chain():
    return [opt(2000, "C"), opt(2000, "P")]


# --------------------------------------------------------------------------- hard gates


class HardGateTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(pcs.ACTION, "observe_only")
        self.assertIs(pcs.WILL_SEND_HTTP, False)
        self.assertEqual(pcs.PHASE, "B")
        self.assertEqual(pcs.TAXONOMY, "relative_value")
        self.assertEqual(pcs.HEDGE_MODES[pcs.FAMILY_B2], ("paper_delta_sim_only",))
        self.assertEqual(
            pcs.COMBO_ID, {pcs.FAMILY_B1: "B1", pcs.FAMILY_B2: "B2", pcs.FAMILY_B3: "B3"}
        )
        for k in pas.REQUIRED_FIELDS:
            self.assertIn(k, pcs.REQUIRED_FIELDS)  # Phase A schema inherited
        for k in ("phase", "combo_id", "hedge_mode", "residual_risks"):
            self.assertIn(k, pcs.REQUIRED_FIELDS)
        self.assertIn("vol_path_haircut", pcs.COST_KEYS)

    def test_module_has_no_trading_http_path_and_leaves_mainline_alone(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in ("/api/v5/trade", "urlopen", 'method="POST"', "place_order(", "amend_algo("):
            self.assertNotIn(needle, src, needle)
        self.assertNotIn("import local_paper_grid", src)
        self.assertNotIn("import grid_ab_compare", src)

    def test_b1_short_call_must_be_covered_by_spot_notional(self):
        call = leg("OPT-N-2700-C", "sell", 40.0, "call")
        spot_ok = leg("ETH-USDT", "buy", 2001.0, "spot")
        perp = leg("ETH-USDT-SWAP", "sell", 2002.0, "perp")
        covers = pcs.assert_covered_short_options([spot_ok, perp, call], b1_spot_cover=True)
        self.assertEqual(covers[0]["cover_type"], "spot_notional")
        # spot qty below the call qty → naked residual
        with self.assertRaises(pas.NakedShortOptionRefused):
            pcs.assert_covered_short_options(
                [leg("ETH-USDT", "buy", 2001.0, "spot", qty=0.5), perp, call], b1_spot_cover=True
            )
        # a long perp is delta cover but not spot notional → refused for B1
        with self.assertRaises(pas.NakedShortOptionRefused):
            pcs.assert_covered_short_options(
                [leg("ETH-USDT-SWAP", "buy", 2003.0, "perp"), call], b1_spot_cover=True
            )
        # naked short call
        with self.assertRaises(pas.NakedShortOptionRefused):
            pcs.assert_covered_short_options([perp, call])

    def test_calendar_cover_rule_refuses_reverse_calendar(self):
        long_far = leg("OPT-F-2000-C", "buy", 100.0, "call", expiry=FAR)
        short_near = leg("OPT-N-2000-C", "sell", 80.0, "call", expiry=NEAR)
        covers = pcs.assert_covered_short_options([long_far, short_near])
        self.assertEqual(covers[0]["cover_type"], "same_type_long_option_expiry_gte")
        with self.assertRaises(pas.NakedShortOptionRefused):  # short far, long near → naked later
            pcs.assert_covered_short_options(
                [
                    leg("OPT-N-2000-C", "buy", 80.0, "call", expiry=NEAR),
                    leg("OPT-F-2000-C", "sell", 100.0, "call", expiry=FAR),
                ]
            )
        with self.assertRaises(pas.NakedShortOptionRefused):  # put short vs call long: no cover
            pcs.assert_covered_short_options(
                [long_far, leg("OPT-N-2000-P", "sell", 80.0, "put", expiry=NEAR)]
            )

    def test_rr_without_wing_is_naked_and_refused(self):
        buy_call = leg("OPT-N-2550-C", "buy", 50.0, "call")
        sell_put = leg("OPT-N-1700-P", "sell", 30.0, "put")
        with self.assertRaises(pas.NakedShortOptionRefused):
            pcs.assert_covered_short_options([buy_call, sell_put])
        wing = leg("OPT-N-1400-P", "buy", 8.0, "put")
        covers = pcs.assert_covered_short_options([buy_call, sell_put, wing])
        self.assertEqual(covers[0]["covered_by"], "OPT-N-1400-P")

    def test_finalize_refuses_schema_breaks(self):
        s, p = spot_perp()
        rec = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, FR, atm_chain() + [opt(2700, "C")]))[0]
        for bad in ({"action": "place_order"}, {"will_send_http": True}):
            with self.assertRaises(pas.ObserveOnlyViolation):
                pcs.finalize_combo_record({**rec, **bad})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "taxonomy": pas.TAXONOMY_IDENTITY})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "taxonomy": "risk_free_arbitrage"})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "phase": "A"})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "combo_id": "B2"})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "hedge_mode": "live_delta_hedge"})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "hedge_mode": pcs.HEDGE_PAPER_DELTA})  # B1 ≠ B2 mode
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record({**rec, "residual_risks": ["gamma"]})
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record(
                {
                    **rec,
                    "risk_flags": [
                        f for f in rec["risk_flags"] if f != "relative_value_not_riskless"
                    ],
                }
            )
        for label in ("risk_free", "无风险套利", "稳赚", "guaranteed"):
            with self.assertRaises(pcs.ForbiddenLabelViolation):
                pcs.finalize_combo_record({**rec, "note": f"this is {label}"})
        with self.assertRaises(pas.ObserveOnlyViolation):
            pcs.finalize_combo_record(
                {**rec, "paper_delta_sim": {**rec["paper_delta_sim"], "live_hedge_http": True}}
            )
        with self.assertRaises(pas.NakedShortOptionRefused):  # drop the spot leg → naked call
            pcs.finalize_combo_record({**rec, "legs": rec["legs"][1:]})
        with self.assertRaises(pas.ExecutablePriceViolation):
            pcs.finalize_combo_record({**rec, "legs": [{**rec["legs"][0], "price_type": "mark"}]})
        missing = {k: v for k, v in rec.items() if k != "hedge_mode"}
        with self.assertRaises(pcs.ComboSchemaViolation):
            pcs.finalize_combo_record(missing)
        pcs.finalize_combo_record(rec)  # unchanged record is valid

    def test_config_validation(self):
        for bad in (
            dict(qty=0),
            dict(b1_delta_min=0.3, b1_delta_max=0.2),
            dict(b2_opt_type="X"),
            dict(hedge_instrument="live"),
            dict(b3_rr_ref_mode="oracle"),
            dict(b3_wing_delta=0.3),
        ):
            with self.assertRaises(ValueError):
                pcs.ComboConfig(**bad).validate()
        d = pcs.ComboConfig().to_dict()
        self.assertEqual(d["leverage_concept"], 1)
        self.assertIs(d["b2"]["live_delta_hedge"], False)
        self.assertIs(d["safety_buffer"]["calibrated"], False)
        self.assertIn("vol_path_haircut_bps", d["safety_buffer"])


# --------------------------------------------------------------------------- black-76 reference


class Black76Test(unittest.TestCase):
    def test_parity_delta_and_implied_vol_roundtrip(self):
        c = pcs.black76(2000.0, 2100.0, 0.25, 0.6, True)
        p = pcs.black76(2000.0, 2100.0, 0.25, 0.6, False)
        self.assertAlmostEqual(c.price - p.price, 2000.0 - 2100.0, places=8)
        self.assertAlmostEqual(c.delta - p.delta, 1.0, places=10)
        self.assertGreater(c.gamma, 0)
        self.assertAlmostEqual(c.vega, p.vega, places=10)
        for sigma in (0.2, 0.6, 1.5):
            px = pcs.black76(2000.0, 1800.0, 0.5, sigma, False).price
            self.assertAlmostEqual(pcs.implied_vol(px, 2000.0, 1800.0, 0.5, False), sigma, places=6)
        self.assertIsNone(pcs.implied_vol(50.0, 2000.0, 2100.0, 0.25, False))  # below intrinsic
        self.assertIsNone(pcs.implied_vol(1.0, 2000.0, 2100.0, 0.0, True))  # expired
        self.assertEqual(pcs.black76(2000.0, 1900.0, 0.0, 0.6, True).price, 100.0)

    def test_chain_analytics_reference_only(self):
        s, p = spot_perp()
        an = pcs.ChainAnalytics(snap(s, p, FR, atm_chain()), 0.0)
        self.assertEqual(an.F_ref, F_REF)
        self.assertEqual(an.forward_source, "perp_mid")
        r = an.analyze(atm_chain()[0])
        self.assertAlmostEqual(r.iv_mid, SIGMA, places=4)
        self.assertLess(r.iv_bid, r.iv_mid)
        self.assertLess(r.iv_mid, r.iv_ask)
        sig, src = an.atm_iv(NEAR)
        self.assertAlmostEqual(sig, SIGMA, places=4)
        self.assertTrue(src.startswith("atm_mid_iv_K2000"))
        an2 = pcs.ChainAnalytics(snap(s, None, FR, atm_chain()), 0.0)
        self.assertEqual(an2.forward_source, "spot_mid")


# --------------------------------------------------------------------------- B1


class B1CoveredCallCarryTest(unittest.TestCase):
    def chain(self, mult=1.0):
        return atm_chain() + [opt(2700, "C", mult=mult)]  # Δ ≈ 0.20 at σ=0.6, T=0.25

    def test_flat_vol_numbers(self):
        s, p = spot_perp()
        recs = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, FR, self.chain()))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["family"], pcs.FAMILY_B1)
        self.assertEqual(r["combo_id"], "B1")
        self.assertEqual(r["phase"], "B")
        self.assertEqual(r["taxonomy"], "relative_value")
        self.assertEqual(r["hedge_mode"], "static_combo")
        self.assertEqual(r["hypothesis_id"], "H-B1")
        self.assertEqual(r["strike"], 2700.0)
        self.assertTrue(0.15 <= r["call"]["delta_mid_ref"] <= 0.25)
        self.assertEqual(
            [(leg["role"], leg["side"], leg["price_type"]) for leg in r["legs"]],
            [("spot", "buy", "ask"), ("perp", "sell", "bid"), ("call", "sell", "bid")],
        )
        self.assertEqual(r["cover"][0]["cover_type"], "spot_notional")
        self.assertEqual(
            r["legs"][2]["executable_price"],
            r["executable_prices"][r["legs"][2]["instrument"]]["bid"],
        )
        # premium edge = bid − fair(ATM σ) = −half spread (flat vol) → −1 / 2001 → ≈ −5 bp
        self.assertAlmostEqual(r["call"]["premium_edge_bps"], -1.0 / 2001 * 1e4, places=1)
        self.assertAlmostEqual(
            r["funding"]["expected_funding_bps"], 7.5, places=6
        )  # min(3, 2.5) × 3
        self.assertAlmostEqual(
            r["gross_edge_bps"],
            7.5 + r["call"]["premium_edge_bps"] + r["basis_credited_bps"],
            places=3,
        )
        self.assertEqual(r["basis_credited_bps"], 0.0)
        c = r["costs_bps"]
        self.assertAlmostEqual(c["fees"], 2 * 10 + 2 * 5 + 3 + 3, places=6)  # opt entry + buy-back
        self.assertAlmostEqual(c["half_spread_slip"], (1.0 + 0.5 + 2.0) / 2001 * 1e4, places=3)
        self.assertEqual(c["hedge_rebalance"], 0.0)  # static_combo: no rebalance modelled
        self.assertAlmostEqual(c["funding_uncertainty"], 2.0 * 3 + 0.5 * 3, places=4)
        self.assertEqual(c["vol_path_haircut"], 10.0)
        self.assertAlmostEqual(c["total"], sum(c[k] for k in pcs.COST_KEYS), places=6)
        self.assertAlmostEqual(r["net_edge_bps"], r["gross_edge_bps"] - c["total"], places=4)
        self.assertEqual(r["margin_capital_required"], 2 * 2001.0)
        self.assertIs(r["hold"]["hold_to_expiry"], False)
        for rr in ("gamma", "funding_flip", "gap", "margin", "basis", "capped_upside"):
            self.assertIn(rr, r["residual_risks"])
        self.assertIn("relative_value_not_riskless", r["risk_flags"])
        self.assertIs(r["paper_delta_sim"]["enabled"], False)
        self.assertIs(r["paper_delta_sim"]["live_hedge_http"], False)
        self.assertFalse(r["passes_threshold"])
        self.assertEqual(r["action"], "observe_only")
        self.assertIs(r["will_send_http"], False)
        self.assertIsNone(r["a1_reference_same_window"])  # scan_b1 called directly

    def test_rich_call_edge_and_a1_comparator(self):
        s, p = spot_perp()
        scanner = pcs.ComboScanner(cfg())
        sn = snap(s, p, FR, self.chain(mult=1.5))
        recs = scanner.scan(sn)
        b1 = [r for r in recs if r["family"] == pcs.FAMILY_B1]
        self.assertEqual(len(b1), 1)
        r = b1[0]
        self.assertGreater(r["call"]["premium_edge_bps"], 0)
        self.assertGreater(r["call"]["iv_mid_ref"], r["call"]["sigma_ref"])
        a1 = r["a1_reference_same_window"]
        self.assertEqual(a1["family"], pas.FAMILY_A1)
        self.assertAlmostEqual(
            r["b1_minus_a1_net_edge_bps"], r["net_edge_bps"] - a1["net_edge_bps"], places=4
        )
        self.assertEqual(len(scanner.a1_reference_records), 1)
        # the A1 comparator is a Phase A record, not a Phase B row
        self.assertNotIn(pas.FAMILY_A1, {x["family"] for x in recs})

    def test_negative_funding_is_a_cost_and_flip_invalidates(self):
        s, p = spot_perp()
        neg = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, pas.Funding(-0.0004), self.chain()))[0]
        self.assertIn("funding_negative_for_short_perp", neg["risk_flags"])
        self.assertEqual(neg["funding"]["expected_funding_bps"], 0.0)
        self.assertAlmostEqual(neg["costs_bps"]["funding_expected"], 4.0 * 3, places=4)
        flip = pcs.ComboScanner(cfg()).scan_b1(
            snap(s, p, pas.Funding(0.0003, next_rate=-0.0001), self.chain())
        )[0]
        self.assertIn("funding_sign_flip_predicted", flip["invalidated_by"])
        self.assertEqual(flip["funding"]["expected_funding_bps"], 0.0)
        self.assertFalse(flip["passes_threshold"])

    def test_moneyness_fallback_and_band_cap(self):
        s, p = spot_perp()
        # no call in the Δ band: 2150 (Δ≈0.46) and 2400 (Δ≈0.30) → moneyness fallback band 5–30%
        chain = atm_chain() + [opt(2150, "C"), opt(2400, "C")]
        recs = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, FR, chain))
        self.assertEqual([r["strike"] for r in recs], [2400.0, 2150.0])
        self.assertTrue(all("otm_selection_by_moneyness_fallback" in r["risk_flags"] for r in recs))
        recs1 = pcs.ComboScanner(cfg(b1_max_calls_per_expiry=1)).scan_b1(snap(s, p, FR, chain))
        self.assertEqual(len(recs1), 1)
        sc = pcs.ComboScanner(cfg())
        self.assertEqual(sc.scan_b1(snap(s, p, FR, atm_chain())), [])  # no OTM call at all
        self.assertEqual(sc.skipped["b1_no_call_in_band"], 1)
        self.assertEqual(
            pcs.ComboScanner(cfg()).scan_b1(snap(s, p, None, chain)), []
        )  # needs funding

    def test_persistence_then_pass(self):
        s, p = spot_perp()
        scanner = pcs.ComboScanner(cfg(persistence_min_samples=3, persistence_min_sec=60))
        outs = []
        for i in range(3):
            r = scanner.scan_b1(snap(s, p, FR, self.chain(mult=1.5), ts=T0 + i * 30_000))[0]
            outs.append(
                (r["edge_exceeds_buffer"], r["persistence"]["samples_ok"], r["passes_threshold"])
            )
        self.assertEqual(outs, [(True, 1, False), (True, 2, False), (True, 3, True)])


# --------------------------------------------------------------------------- B2


class B2CalendarTest(unittest.TestCase):
    def chain(self, far_sigma=0.5):
        return atm_chain() + [
            opt(2000, "C", expiry=FAR, sigma=far_sigma),
            opt(2000, "P", expiry=FAR, sigma=far_sigma),
        ]

    def test_far_cheap_numbers_and_paper_delta_sim(self):
        s, p = spot_perp()
        recs = pcs.ComboScanner(cfg()).scan_b2(snap(s, p, FR, self.chain()))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["family"], pcs.FAMILY_B2)
        self.assertEqual(r["combo_id"], "B2")
        self.assertEqual(r["hedge_mode"], "paper_delta_sim_only")
        self.assertEqual(r["hypothesis_id"], "H-B2")
        self.assertEqual(
            [(leg["role"], leg["side"], leg["price_type"], leg["expiry_ms"]) for leg in r["legs"]],
            [("call", "buy", "ask", FAR), ("call", "sell", "bid", NEAR)],
        )
        self.assertEqual(r["cover"][0]["cover_type"], "same_type_long_option_expiry_gte")
        ts_ = r["term_structure"]
        self.assertAlmostEqual(ts_["iv_near_mid"], 0.6, places=4)
        self.assertAlmostEqual(ts_["iv_far_mid"], 0.5, places=4)
        self.assertLess(ts_["slope_mid"], 0)
        cal = r["calendar"]
        far_ask = pcs.black76(F_REF, 2000, 0.5, 0.5, True).price + 1.0
        near_bid = pcs.black76(F_REF, 2000, 0.25, 0.6, True).price - 1.0
        self.assertAlmostEqual(cal["exec_debit_quote"], far_ask - near_bid, places=6)
        fair = (
            pcs.black76(F_REF, 2000, 0.5, ts_["sigma_ref"], True).price
            - pcs.black76(F_REF, 2000, 0.25, ts_["sigma_ref"], True).price
        )
        self.assertAlmostEqual(cal["fair_debit_ref_quote"], fair, places=3)
        self.assertAlmostEqual(
            r["gross_edge_bps"], (fair - (far_ask - near_bid)) / 2001 * 1e4, places=2
        )
        self.assertGreater(r["gross_edge_bps"], 0)
        sim = r["paper_delta_sim"]
        self.assertIs(sim["enabled"], True)
        self.assertIs(sim["live_hedge_http"], False)
        self.assertIs(sim["order_sent"], False)
        self.assertEqual(sim["hedge_kind"], "perp")
        self.assertEqual(sim["hedge_instrument"], "ETH-USDT-SWAP")
        self.assertAlmostEqual(sim["hedge_units"], -sim["initial_net_delta"], places=8)
        self.assertEqual(sim["rebalances"], 3)  # 1 day × 3/day
        self.assertGreater(sim["cost_quote"], 0)
        self.assertAlmostEqual(
            r["costs_bps"]["hedge_rebalance"], sim["cost_quote"] / 2001 * 1e4, places=3
        )
        self.assertEqual(r["costs_bps"]["vol_path_haircut"], 10.0)
        # far leg: entry + buy-back fee; near leg: entry + buy-back (1d horizon < 0.25y)
        self.assertAlmostEqual(r["costs_bps"]["fees"], 4 * 3.0, places=4)
        self.assertGreater(r["costs_bps"]["half_spread_slip"], 0)
        self.assertAlmostEqual(
            r["margin_capital_required"],
            max(cal["exec_debit_quote"], 0) + sim["hedge_notional_quote"],
            places=4,
        )
        for rr in (
            "gamma",
            "vega_term_structure",
            "gap",
            "margin",
            "hedge_slippage_underestimation",
            "model_risk",
        ):
            self.assertIn(rr, r["residual_risks"])
        self.assertIs(r["calendar"]["theta_carry_credited"], False)

    def test_far_rich_is_negative_and_flagged(self):
        s, p = spot_perp()
        r = pcs.ComboScanner(cfg()).scan_b2(snap(s, p, FR, self.chain(far_sigma=0.7)))[0]
        self.assertLess(r["gross_edge_bps"], 0)
        self.assertIn("term_structure_not_cheap_vs_thesis", r["risk_flags"])
        self.assertFalse(r["passes_threshold"])

    def test_put_calendar_spot_hedge_and_single_expiry(self):
        s, p = spot_perp()
        r = pcs.ComboScanner(cfg(b2_opt_type="P", hedge_instrument="spot")).scan_b2(
            snap(s, p, FR, self.chain())
        )[0]
        self.assertEqual([leg["role"] for leg in r["legs"]], ["put", "put"])
        self.assertEqual(r["paper_delta_sim"]["hedge_kind"], "spot")
        self.assertEqual(r["paper_delta_sim"]["funding_paid_quote"], 0.0)
        sc = pcs.ComboScanner(cfg())
        self.assertEqual(sc.scan_b2(snap(s, p, FR, atm_chain())), [])
        self.assertEqual(sc.skipped["b2_single_expiry"], 1)

    def test_paper_delta_sim_funding_side(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.0003, interval_sec=28_800)
        long_hedge = pcs.paper_delta_hedge_sim(
            net_delta=-0.5,
            net_gamma=-0.001,
            hedge_book=p,
            hedge_kind="perp",
            funding=fr,
            sigma_ref=0.6,
            hold_years=1 / 365,
            cfg=cfg(),
        )
        self.assertEqual(long_hedge["hedge_units"], 0.5)  # long perp hedge pays positive funding
        self.assertAlmostEqual(
            long_hedge["funding_paid_quote"], 0.0003 * 3 * 0.5 * 2002.5, places=6
        )
        short_hedge = pcs.paper_delta_hedge_sim(
            net_delta=0.5,
            net_gamma=-0.001,
            hedge_book=p,
            hedge_kind="perp",
            funding=fr,
            sigma_ref=0.6,
            hold_years=1 / 365,
            cfg=cfg(),
        )
        self.assertEqual(short_hedge["funding_paid_quote"], 0.0)  # receiving side is not credited
        self.assertGreater(short_hedge["funding_uncert_quote"], 0)
        self.assertEqual(short_hedge["rebalances"], 3)
        self.assertGreater(short_hedge["rebalance_cost_quote"], 0)
        self.assertIs(short_hedge["live_hedge_http"], False)


# --------------------------------------------------------------------------- B3


class B3RiskReversalTest(unittest.TestCase):
    def chain(self, put_mult=1.0, with_put_wing=True, with_call_wing=True):
        c = atm_chain() + [opt(1700, "P", mult=put_mult), opt(2550, "C")]
        if with_put_wing:
            c.append(opt(1400, "P"))
        if with_call_wing:
            c.append(opt(3100, "C"))
        return c

    def test_selection_wing_cover_and_fixed_reference(self):
        s, p = spot_perp()
        recs = pcs.ComboScanner(cfg(b3_rr_ref_mode="fixed", b3_rr_ref_fixed_volpts=5.0)).scan_b3(
            snap(s, p, FR, self.chain())
        )
        self.assertEqual({r["direction"] for r in recs}, {"long_rr", "short_rr"})
        long_rr = next(r for r in recs if r["direction"] == "long_rr")
        short_rr = next(r for r in recs if r["direction"] == "short_rr")
        for r in recs:
            self.assertEqual(r["family"], pcs.FAMILY_B3)
            self.assertEqual(r["combo_id"], "B3")
            self.assertEqual(r["hedge_mode"], "options_rr_static")
            self.assertEqual(r["hypothesis_id"], "H-B3")
            self.assertEqual(r["strikes"]["call"], 2550.0)
            self.assertEqual(r["strikes"]["put"], 1700.0)
            self.assertAlmostEqual(abs(r["deltas_mid_ref"]["call"]), 0.25, delta=0.03)
            self.assertAlmostEqual(abs(r["deltas_mid_ref"]["put"]), 0.25, delta=0.03)
            self.assertNotIn("delta_off_target", r["risk_flags"])
            self.assertAlmostEqual(r["rr"]["rr_mid_ref"], 0.0, places=3)  # flat vol → RR ≈ 0
            self.assertEqual(r["rr"]["rr_ref_info"]["mode"], "fixed")
            self.assertAlmostEqual(
                r["gross_edge_bps"],
                (r["rr"]["edge_volpts"] / 100) * r["rr"]["vega_scale_quote_per_vol"] / 2001 * 1e4,
                places=2,
            )
            self.assertIs(r["paper_delta_sim"]["enabled"], False)
            for rr in ("skew_trend", "gamma", "gap", "margin", "spot_direction_bleed", "liquidity"):
                self.assertIn(rr, r["residual_risks"])
        self.assertEqual(
            [(leg["role"], leg["side"], leg["price_type"]) for leg in long_rr["legs"]],
            [("call", "buy", "ask"), ("put", "sell", "bid"), ("put", "buy", "ask")],
        )
        self.assertEqual(long_rr["strikes"]["wing"], 1400.0)
        self.assertEqual(long_rr["cover"][0]["covered_by"], "OPT-N-1400-P")
        self.assertEqual(short_rr["strikes"]["wing"], 3100.0)
        self.assertEqual(short_rr["cover"][0]["covered_by"], "OPT-N-3100-C")
        # ref = +5 vol pts, exec ≈ 0 ± spread → long RR cheap vs ref (edge > 0), short RR negative
        self.assertGreater(long_rr["rr"]["edge_volpts"], 0)
        self.assertLess(short_rr["rr"]["edge_volpts"], 0)
        # long RR max loss = (K_put − K_wing) + premiums paid
        self.assertGreater(long_rr["margin_capital_required"], 1700 - 1400)
        self.assertEqual(long_rr["costs_bps"]["vol_path_haircut"], 10.0)
        self.assertAlmostEqual(
            long_rr["costs_bps"]["fees"], 6 * 3.0, places=3
        )  # 3 legs × (entry + buy-back)

    def test_no_wing_means_no_record_never_naked(self):
        s, p = spot_perp()
        sc = pcs.ComboScanner(cfg(b3_rr_ref_mode="fixed"))
        recs = sc.scan_b3(snap(s, p, FR, self.chain(with_put_wing=False)))
        self.assertEqual([r["direction"] for r in recs], ["short_rr"])
        self.assertEqual(sc.skipped["b3_no_wing_cover_long_rr"], 1)
        for r in recs:
            pcs.assert_covered_short_options(r["legs"])

    def test_rolling_reference_needs_prior_samples(self):
        s, p = spot_perp()
        scanner = pcs.ComboScanner(cfg(b3_rr_ref_min_samples=2))
        seen = []
        for i, mult in enumerate((1.0, 1.0, 1.6)):
            recs = scanner.scan_b3(snap(s, p, FR, self.chain(put_mult=mult), ts=T0 + i * 30_000))
            r = next(x for x in recs if x["direction"] == "long_rr")
            seen.append(r)
        self.assertIn("rr_reference_unavailable", seen[0]["invalidated_by"])
        self.assertIn("rr_reference_unavailable", seen[1]["invalidated_by"])
        self.assertFalse(seen[0]["passes_threshold"] or seen[1]["passes_threshold"])
        self.assertLess(seen[0]["gross_edge_bps"], 0)  # vs own mid → minus the spread crossing
        self.assertEqual(seen[2]["invalidated_by"], [])
        self.assertEqual(seen[2]["rr"]["rr_ref_info"]["samples"], 2)
        self.assertAlmostEqual(seen[2]["rr"]["rr_ref"], 0.0, places=3)
        self.assertLess(seen[2]["rr"]["rr_mid_ref"], -0.05)  # rich put → RR down
        self.assertGreater(seen[2]["rr"]["edge_volpts"], 0)  # long RR cheap vs rolling reference

    def test_paper_delta_alt_mode(self):
        s, p = spot_perp()
        recs = pcs.ComboScanner(cfg(b3_rr_ref_mode="fixed", b3_paper_delta=True)).scan_b3(
            snap(s, p, FR, self.chain())
        )
        for r in recs:
            self.assertEqual(r["hedge_mode"], "options_rr_plus_paper_delta")
            self.assertIs(r["paper_delta_sim"]["enabled"], True)
            self.assertIs(r["paper_delta_sim"]["live_hedge_http"], False)
            self.assertGreater(r["costs_bps"]["hedge_rebalance"], 0)


# --------------------------------------------------------------------------- filters / buffer


class BufferAndFilterTest(unittest.TestCase):
    def test_buffer_includes_vol_path_haircut(self):
        r = pcs.ComboSafetyBuffer().resolve(fees_bps=30.0)
        self.assertEqual(r["components"]["fee_roundtrip_bps"], 30.0)
        self.assertEqual(r["components"]["vol_path_haircut_bps"], 10.0)
        self.assertEqual(r["total_bps"], 30 + 5 + 5 + 10 + 10)
        self.assertIs(r["calibrated"], False)
        r2 = pcs.ComboSafetyBuffer(fee_roundtrip_bps=12.0, calibrated=True).resolve(30.0)
        self.assertEqual(r2["total_bps"], 12 + 5 + 5 + 10 + 10)
        self.assertIs(r2["calibrated"], True)

    def test_record_buffer_matches_fees(self):
        s, p = spot_perp()
        r = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, FR, atm_chain() + [opt(2700, "C")]))[0]
        self.assertEqual(
            r["safety_buffer"]["components"]["fee_roundtrip_bps"], r["costs_bps"]["fees"]
        )
        self.assertEqual(r["safety_buffer_bps"], sum(r["safety_buffer"]["components"].values()))
        self.assertEqual(r["edge_exceeds_buffer"], r["net_edge_bps"] > r["safety_buffer_bps"])

    def test_thin_option_book_flags_illiquid(self):
        s, p = spot_perp()
        thin = opt(2700, "C")
        thin = pas.Book(
            thin.inst_id,
            "option",
            bids=(pas.Level(thin.best_bid, 0.4),),
            asks=thin.asks,
            ts_ms=T0,
            opt_type="C",
            strike=2700.0,
            expiry_ms=NEAR,
            premium_ccy="quote",
        )
        r = pcs.ComboScanner(cfg()).scan_b1(snap(s, p, FR, atm_chain() + [thin]))[0]
        self.assertFalse(r["liquidity"]["ok"])
        self.assertIn("insufficient_depth", r["invalidated_by"])
        self.assertFalse(r["passes_threshold"])

    def test_paper_fill_block(self):
        s, p = spot_perp()
        r = pcs.ComboScanner(cfg(paper_fills=True)).scan_b1(
            snap(s, p, FR, atm_chain() + [opt(2700, "C")])
        )[0]
        pf = r["paper_fill"]
        self.assertIs(pf["order_sent"], False)
        self.assertIs(pf["will_send_http"], False)
        self.assertAlmostEqual(pf["net_edge_after_fill_bps"], r["net_edge_bps"] - 5.0 * 3, places=4)
        self.assertEqual(len(pf["legs"]), 3)

    def test_phase_a_config_mirror(self):
        a = cfg(qty=2.0, horizon_intervals=6, ref_rate_apr=0.05).phase_a_config()
        self.assertEqual((a.qty, a.horizon_intervals, a.ref_rate_apr), (2.0, 6, 0.05))
        self.assertIs(a.buffer.calibrated, False)


# --------------------------------------------------------------------------- fixture replay


class FixtureReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = pcs.load_fixture(FIXTURE)
        cls.scanner = pcs.ComboScanner(pcs.ComboConfig())
        cls.records = [r for s in cls.snaps for r in cls.scanner.scan(s)]

    def test_fixture_shape(self):
        self.assertEqual(len(self.snaps), 4)
        self.assertTrue(all(s.source["http_fetch"] is False for s in self.snaps))
        self.assertEqual(len(self.snaps[0].options), 28)
        self.assertEqual(len(self.snaps[0].option_chain()), 2)  # two expiries
        self.assertIn("SYNTHETIC", self.snaps[0].source["note"])

    def test_every_record_is_observe_only_relative_value_and_schema_complete(self):
        self.assertGreater(len(self.records), 0)
        for r in self.records:
            for k in pcs.REQUIRED_FIELDS:
                self.assertIn(k, r)
            self.assertEqual(r["action"], "observe_only")
            self.assertIs(r["will_send_http"], False)
            self.assertEqual(r["phase"], "B")
            self.assertEqual(r["taxonomy"], "relative_value")
            self.assertIn(r["family"], pcs.FAMILIES)
            self.assertEqual(r["combo_id"], pcs.COMBO_ID[r["family"]])
            self.assertIn(r["hedge_mode"], pcs.HEDGE_MODES[r["family"]])
            self.assertTrue(set(pcs.RESIDUAL_RISKS[r["family"]]) <= set(r["residual_risks"]))
            self.assertIn("relative_value_not_riskless", r["risk_flags"])
            self.assertIn("vol_path_haircut", r["costs_bps"])
            self.assertIn("vol_path_haircut_bps", r["safety_buffer"]["components"])
            self.assertIs(r["paper_delta_sim"]["live_hedge_http"], False)
            self.assertEqual(r["leverage_concept"], 1)
            self.assertTrue(r["timestamp"].endswith("+08:00"))
            for leg in r["legs"]:
                self.assertIn(leg["price_type"], ("bid", "ask"))
                self.assertEqual(leg["price_type"], "ask" if leg["side"] == "buy" else "bid")
            pcs.assert_covered_short_options(
                r["legs"], b1_spot_cover=(r["family"] == pcs.FAMILY_B1)
            )
            self.assertAlmostEqual(
                r["net_edge_bps"], r["gross_edge_bps"] - r["costs_bps"]["total"], places=3
            )
            text = json.dumps(r, ensure_ascii=False).lower()
            for bad in pcs.FORBIDDEN_LABELS:
                self.assertNotIn(bad, text)

    def test_families_and_dislocation_paths(self):
        self.assertEqual({r["family"] for r in self.records}, set(pcs.FAMILIES))
        self.assertEqual(dict(self.scanner.skipped), {})
        b2 = [r for r in self.records if r["family"] == pcs.FAMILY_B2]
        self.assertTrue(all(r["hedge_mode"] == "paper_delta_sim_only" for r in b2))
        self.assertTrue(all(r["paper_delta_sim"]["enabled"] for r in b2))
        # B1: near 2800 call is marked rich from snapshot 2 → exceeds ×3, passes on the 3rd streak
        b1 = [
            r
            for r in self.records
            if r["family"] == pcs.FAMILY_B1
            and r["strike"] == 2800.0
            and r["expiry_ms"] == 1_793_347_200_000
        ]
        self.assertEqual([r["edge_exceeds_buffer"] for r in b1], [False, True, True, True])
        self.assertEqual([r["passes_threshold"] for r in b1], [False, False, False, True])
        self.assertLess(b1[0]["b1_minus_a1_net_edge_bps"], 0)  # flat skew: covered call < plain A1
        # B2: far vol drops from snapshot 2 → ATM calendar exceeds 3× and passes on the 3rd
        cal = [r for r in b2 if r["strike"] == 2400.0]
        self.assertEqual([r["edge_exceeds_buffer"] for r in cal], [False, True, True, True])
        self.assertEqual([r["passes_threshold"] for r in cal], [False, False, False, True])
        # B3: rolling reference unavailable on the first two snapshots; rich put later exceeds but
        # cannot reach persistence on a 4-snapshot window
        b3 = [r for r in self.records if r["family"] == pcs.FAMILY_B3]
        self.assertTrue(
            all(
                "rr_reference_unavailable" in r["invalidated_by"]
                for r in b3
                if r["ts_ms"] < T0 + 60_000
            )
        )
        self.assertGreater(sum(1 for r in b3 if r["edge_exceeds_buffer"]), 0)
        self.assertFalse(any(r["passes_threshold"] for r in b3))
        self.assertTrue(all(len(r["legs"]) == 3 and len(r["cover"]) == 1 for r in b3))

    def test_summary(self):
        s = pcs.summarize(
            self.records,
            len(self.snaps),
            pcs.ComboConfig(),
            self.snaps[0].source,
            self.scanner.skipped,
            self.scanner.a1_reference_records,
        )
        self.assertEqual(s["action"], "observe_only")
        self.assertIs(s["will_send_http"], False)
        self.assertIs(s["live_delta_hedge"], False)
        self.assertEqual(s["phase"], "B")
        self.assertTrue(all(v is False for v in s["trading_http"].values()))
        self.assertEqual(set(s["hypotheses"]), {"H-B1", "H-B2", "H-B3"})
        self.assertEqual(s["hypotheses"]["H-B1"]["status"], "not_yet_falsified_on_window")
        self.assertEqual(s["hypotheses"]["H-B2"]["status"], "not_yet_falsified_on_window")
        self.assertEqual(s["hypotheses"]["H-B3"]["status"], "no_pass_on_window")
        self.assertEqual(s["a1_reference_same_window"]["records"], 4)
        self.assertEqual(s["a1_reference_same_window"]["passes_threshold"], 0)
        self.assertIn("a1_pass_rate_same_window", s["hypotheses"]["H-B1"])
        self.assertEqual(s["mainline_unchanged"], "spot_grid_local_paper")
        self.assertEqual(s["related_phase_A"], "A1_A2_A3_scanners_remain")
        self.assertIs(s["config"]["safety_buffer"]["calibrated"], False)
        self.assertEqual(s["config"]["leverage_concept"], 1)
        for v in s["families"].values():
            self.assertEqual(v["taxonomies"], ["relative_value"])
        text = json.dumps(s, ensure_ascii=False)
        for banned in ("无风险", "risk_free", "guaranteed", "稳赚"):
            self.assertNotIn(banned, text)

    def test_phase_a_fixture_loads_but_is_too_narrow(self):
        sc = pcs.ComboScanner(pcs.ComboConfig())
        recs = [r for s in pcs.load_fixture(FIXTURE_A) for r in sc.scan(s)]
        self.assertEqual(recs, [])
        self.assertEqual(sc.skipped["b2_single_expiry"], 4)
        self.assertGreater(sc.skipped["b3_no_wing_cover_long_rr"], 0)


# --------------------------------------------------------------------------- cli


class CliTest(unittest.TestCase):
    def test_cli_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ = Path(d) / "combo.jsonl", Path(d) / "summary.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--source",
                    "fixture",
                    "--fixture",
                    str(FIXTURE),
                    "--paper-fills",
                    "--out",
                    str(out),
                    "--summary-out",
                    str(summ),
                    "--quiet",
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
                env={"PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
            self.assertGreater(len(rows), 0)
            self.assertTrue(all(r["action"] == "observe_only" for r in rows))
            self.assertTrue(all(r["will_send_http"] is False for r in rows))
            self.assertTrue(all(r["taxonomy"] == "relative_value" for r in rows))
            self.assertTrue(all(r["paper_fill"]["order_sent"] is False for r in rows))
            s = json.loads(summ.read_text())
            self.assertEqual(s["records"], len(rows))
            self.assertEqual(s["snapshots"], 4)
            self.assertIs(s["trading_http"]["order"], False)
            self.assertIs(s["live_delta_hedge"], False)

    def test_cli_only_exceeding_and_help(self):
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--only-exceeding",
                "--print-summary",
                "--b3-paper-delta",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(rows)
        self.assertTrue(all(r["edge_exceeds_buffer"] for r in rows))
        summary = json.loads(proc.stderr)
        self.assertEqual(summary["action"], "observe_only")
        self.assertEqual(summary["config"]["b3"]["hedge_mode"], "options_rr_plus_paper_delta")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe_only", proc.stdout)
        self.assertIn("relative_value", proc.stdout)


# --------------------------------------------------------------------------- okx public (fake, GET)


class OkxPublicSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOkx)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOkx.requests.clear()

    def test_combo_scan_on_public_source_is_get_only(self):
        c = okx.OkxPublicClient(base_url=self.base)
        src = pas.OkxPublicSnapshotSource(
            c, n_strikes=5, max_expiries=2, min_days_to_expiry=2.0, book_depth=1
        )
        s = src.fetch()
        recs = pcs.ComboScanner(cfg()).scan(s)
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
                "--source",
                "okx-public",
                "--base-url",
                self.base,
                "--samples",
                "1",
                "--n-strikes",
                "5",
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
        self.assertIs(s["will_send_http"], False)
        self.assertIs(s["live_delta_hedge"], False)
        self.assertFalse(any(r["method"] == "POST" for r in FakeOkx.requests))


if __name__ == "__main__":
    unittest.main()
