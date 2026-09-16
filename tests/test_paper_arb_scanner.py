"""Offline unit tests for strategies/paper_arb_scanner.py (fixture books, no network).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "tools"))

import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402

FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-16-eth-books-sample.json"
SCRIPT = ROOT / "strategies" / "paper_arb_scanner.py"

T0 = 1_789_573_200_000  # 2026-09-16T23:40:00+08:00
YEAR_MS = int(365 * 86_400_000)
EXPIRY = T0 + YEAR_MS // 4  # T = 0.25y


def book(inst, kind, bid, ask, sz=10.0, levels=3, tick=1.0, **kw) -> pas.Book:
    bids = tuple(pas.Level(bid - i * tick, sz) for i in range(levels))
    asks = tuple(pas.Level(ask + i * tick, sz) for i in range(levels))
    return pas.Book(inst_id=inst, kind=kind, bids=bids, asks=asks, ts_ms=T0, **kw)


def opt(strike, typ, bid, ask, sz=10.0, premium_ccy="quote", expiry=EXPIRY) -> pas.Book:
    return book(
        f"OPT-{strike:g}-{typ}",
        "option",
        bid,
        ask,
        sz=sz,
        tick=0.5,
        opt_type=typ,
        strike=float(strike),
        expiry_ms=expiry,
        premium_ccy=premium_ccy,
    )


def spot_perp(spot_bid=1999.0, spot_ask=2001.0, perp_bid=2002.0, perp_ask=2003.0, sz=10.0):
    s = book("ETH-USDT", "spot", spot_bid, spot_ask, sz=sz)
    p = book("ETH-USDT-SWAP", "perp", perp_bid, perp_ask, sz=sz * 5, mark_px=2002.5)
    return s, p


def snap(spot=None, perp=None, funding=None, options=(), borrow=None, ts=T0) -> pas.Snapshot:
    return pas.Snapshot(
        ts_ms=ts,
        venue="okx",
        spot=spot,
        perp=perp,
        funding=funding,
        options=tuple(options),
        spot_borrow_apr=borrow,
    )


def cfg(**kw) -> pas.ScanConfig:
    base = dict(persistence_min_samples=1, persistence_min_sec=0.0)
    base.update(kw)
    return pas.ScanConfig(**base)


def only(records, family):
    rows = [r for r in records if r["family"] == family]
    assert rows, family
    return rows


# --------------------------------------------------------------------------- hard gates


class HardGateTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(pas.ACTION, "observe_only")
        self.assertIs(pas.WILL_SEND_HTTP, False)

    def test_mark_mid_last_refused_as_executable(self):
        for pt in ("mark", "mid", "last", "index"):
            with self.assertRaises(pas.ExecutablePriceViolation):
                pas.Leg("ETH-USDT", "buy", 1.0, 2400.0, pt, "spot")

    def test_side_price_type_convention(self):
        with self.assertRaises(pas.ExecutablePriceViolation):
            pas.Leg("ETH-USDT", "buy", 1.0, 2400.0, "bid", "spot")
        with self.assertRaises(pas.ExecutablePriceViolation):
            pas.Leg("ETH-USDT", "sell", 1.0, 2400.0, "ask", "spot")
        pas.Leg("ETH-USDT", "buy", 1.0, 2400.0, "ask", "spot")
        pas.Leg("ETH-USDT", "sell", 1.0, 2400.0, "bid", "spot")

    def test_finalize_refuses_non_observe_only(self):
        s, p = spot_perp()
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003)))[0]
        for bad in ({"action": "place_order"}, {"will_send_http": True}, {"action": "execute"}):
            with self.assertRaises(pas.ObserveOnlyViolation):
                pas.finalize_record({**rec, **bad})
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "taxonomy": pas.TAXONOMY_IDENTITY})  # A1 must be RV
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "family": pas.FAMILY_A3})  # A3 cannot be RV
        with self.assertRaises(ValueError):
            pas.finalize_record({**rec, "taxonomy": "risk_free_arbitrage"})

    def test_naked_short_option_refused(self):
        with self.assertRaises(pas.NakedShortOptionRefused):
            pas.assert_no_naked_short_options(
                [pas.Leg("OPT-2000-C", "sell", 1.0, 100.0, "bid", "call", expiry_ms=EXPIRY)]
            )
        # short put "covered" by a long underlying is still naked on the downside
        with self.assertRaises(pas.NakedShortOptionRefused):
            pas.assert_no_naked_short_options(
                [
                    pas.Leg("ETH-USDT", "buy", 1.0, 2000.0, "ask", "spot"),
                    pas.Leg("OPT-2000-P", "sell", 1.0, 90.0, "bid", "put", expiry_ms=EXPIRY),
                ]
            )
        # covered call (conversion) and spread (box) are fine
        pas.assert_no_naked_short_options(
            [
                pas.Leg("ETH-USDT", "buy", 1.0, 2000.0, "ask", "spot"),
                pas.Leg("OPT-2000-C", "sell", 1.0, 100.0, "bid", "call", expiry_ms=EXPIRY),
            ]
        )
        pas.assert_no_naked_short_options(
            [
                pas.Leg("OPT-1900-C", "buy", 1.0, 150.0, "ask", "call", expiry_ms=EXPIRY),
                pas.Leg("OPT-2100-C", "sell", 1.0, 50.0, "bid", "call", expiry_ms=EXPIRY),
            ]
        )

    def test_module_has_no_trading_http_path(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in ("/api/v5/trade", "urlopen", 'method="POST"', "place_order(", "amend_algo("):
            self.assertNotIn(needle, src, needle)
        # the mainline is untouched: the scanner does not import the grid modules
        self.assertNotIn("import local_paper_grid", src)
        self.assertNotIn("import grid_ab_compare", src)

    def test_readonly_gate_on_new_public_paths(self):
        for path in (okx.PATH_BOOKS, okx.PATH_FUNDING_RATE, okx.PATH_INSTRUMENTS, okx.PATH_TICKERS):
            okx.assert_read_only("GET", path, private=False)
            with self.assertRaises(okx.ReadOnlyViolation):
                okx.assert_read_only("POST", path, private=False)


# --------------------------------------------------------------------------- A1


class A1FundingCarryTest(unittest.TestCase):
    def test_positive_carry_numbers(self):
        s, p = spot_perp()  # spot ask 2001, perp bid 2002
        fr = pas.Funding(rate=0.0003, next_rate=0.00025, interval_sec=28_800)
        rec = pas.ArbScanner(cfg(horizon_intervals=3)).scan_a1(snap(s, p, fr))[0]
        self.assertEqual(rec["family"], pas.FAMILY_A1)
        self.assertEqual(rec["taxonomy"], pas.TAXONOMY_RV)
        self.assertEqual(rec["hypothesis_id"], "H-A1")
        self.assertEqual(rec["direction"], "long_spot_short_perp")
        self.assertEqual([leg["price_type"] for leg in rec["legs"]], ["ask", "bid"])
        self.assertEqual(rec["legs"][0]["executable_price"], 2001.0)
        self.assertEqual(rec["legs"][1]["executable_price"], 2002.0)
        # funding: min(3.0, 2.5) bp × 3 intervals; favorable basis NOT credited by default
        self.assertAlmostEqual(rec["gross_edge_bps"], 7.5, places=4)
        self.assertAlmostEqual(rec["basis_entry_bps"], (2002 - 2001) / 2001 * 1e4, places=3)
        self.assertEqual(rec["basis_credited_bps"], 0.0)
        c = rec["costs_bps"]
        self.assertAlmostEqual(c["fees"], 2 * 10 + 2 * 5, places=6)
        self.assertAlmostEqual(c["half_spread_slip"], (1.0 + 0.5) / 2001 * 1e4, places=3)
        self.assertAlmostEqual(c["funding_uncertainty"], 2.0 * 3 + 0.5 * 3, places=4)
        self.assertEqual(c["borrow"], 0.0)
        self.assertAlmostEqual(c["total"], sum(c[k] for k in pas.COST_KEYS), places=6)
        self.assertAlmostEqual(rec["net_edge_bps"], rec["gross_edge_bps"] - c["total"], places=4)
        self.assertIn("relative_value_not_riskless", rec["risk_flags"])
        self.assertFalse(rec["passes_threshold"])
        self.assertEqual(rec["margin_capital_required"], 2 * 2001.0)
        self.assertEqual(rec["leverage_concept"], 1)

    def test_favorable_basis_credit_is_opt_in(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.0003)
        rec = pas.ArbScanner(cfg(credit_favorable_basis=True)).scan_a1(snap(s, p, fr))[0]
        self.assertAlmostEqual(rec["basis_credited_bps"], rec["basis_entry_bps"])
        self.assertAlmostEqual(rec["gross_edge_bps"], 9.0 + rec["basis_entry_bps"], places=3)

    def test_adverse_basis_always_charged(self):
        s, p = spot_perp(perp_bid=1995.0, perp_ask=1996.0)  # perp below spot: adverse for short
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003)))[0]
        self.assertLess(rec["basis_entry_bps"], 0)
        self.assertIn("adverse_entry_basis", rec["risk_flags"])
        self.assertAlmostEqual(rec["gross_edge_bps"], 9.0 + rec["basis_entry_bps"], places=3)

    def test_negative_funding_needs_borrow(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=-0.0004)
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, fr))[0]
        self.assertEqual(rec["direction"], "short_spot_long_perp")
        self.assertEqual([leg["side"] for leg in rec["legs"]], ["sell", "buy"])
        self.assertIn("requires_spot_borrow", rec["risk_flags"])
        self.assertIn("borrow_unavailable", rec["risk_flags"])
        self.assertIn("borrow_unavailable", rec["invalidated_by"])
        self.assertFalse(rec["passes_threshold"])
        rec2 = pas.ArbScanner(cfg()).scan_a1(snap(s, p, fr, borrow=0.10))[0]
        self.assertNotIn("borrow_unavailable", rec2["risk_flags"])
        self.assertGreater(rec2["costs_bps"]["borrow"], 0)

    def test_predicted_sign_flip_zeroes_funding(self):
        s, p = spot_perp()
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003, next_rate=-0.0001)))[0]
        self.assertIn("funding_sign_flip_predicted", rec["risk_flags"])
        self.assertEqual(rec["funding"]["expected_funding_bps"], 0.0)
        self.assertFalse(rec["passes_threshold"])

    def test_large_funding_can_exceed_buffer_but_not_pass_without_persistence(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.01)  # 100 bp / interval — synthetic stress, not a market claim
        rec = pas.ArbScanner(cfg(persistence_min_samples=3)).scan_a1(snap(s, p, fr))[0]
        self.assertTrue(rec["edge_exceeds_buffer"])
        self.assertFalse(rec["persistence"]["ok"])
        self.assertFalse(rec["passes_threshold"])
        rec1 = pas.ArbScanner(cfg()).scan_a1(snap(s, p, fr))[0]
        self.assertTrue(rec1["passes_threshold"])
        self.assertEqual(rec1["action"], "observe_only")
        self.assertIs(rec1["will_send_http"], False)


# --------------------------------------------------------------------------- A2


class A2PcpTest(unittest.TestCase):
    def chain(self):
        return [opt(2000, "C", 100.0, 102.0), opt(2000, "P", 90.0, 92.0)]

    def test_conversion_and_reversal_numbers_quote_priced(self):
        s, p = spot_perp()
        recs = pas.ArbScanner(cfg()).scan_a2(
            snap(s, p, pas.Funding(0.0), self.chain(), borrow=0.05)
        )
        conv = only(recs, pas.FAMILY_A2_CONV)[0]
        rev = only(recs, pas.FAMILY_A2_REV)[0]
        for r in (conv, rev):
            self.assertEqual(r["taxonomy"], pas.TAXONOMY_IDENTITY)
            self.assertEqual(r["hypothesis_id"], "H-A2")
            self.assertEqual(r["anchor"], "spot")
            self.assertAlmostEqual(r["T_years"], 0.25, places=5)
            self.assertEqual(r["discount_factor"], 1.0)
        # conversion: K·df − (S_ask + P_ask − C_bid) = 2000 − (2001 + 92 − 100) = 7
        self.assertAlmostEqual(conv["gross_edge_bps"], 7 / 2001 * 1e4, places=3)
        self.assertEqual(
            [(leg["side"], leg["price_type"], leg["role"]) for leg in conv["legs"]],
            [("buy", "ask", "spot"), ("buy", "ask", "put"), ("sell", "bid", "call")],
        )
        self.assertAlmostEqual(conv["margin_capital_required"], 2001 + 92 - 100, places=6)
        # reversal: (S_bid + P_bid − C_ask) − K·df = (1999 + 90 − 102) − 2000 = −13
        self.assertAlmostEqual(rev["gross_edge_bps"], -13 / 2001 * 1e4, places=3)
        self.assertEqual(
            [(leg["side"], leg["price_type"], leg["role"]) for leg in rev["legs"]],
            [("sell", "bid", "spot"), ("sell", "bid", "put"), ("buy", "ask", "call")],
        )
        self.assertIn("requires_spot_borrow", rev["risk_flags"])
        self.assertAlmostEqual(rev["costs_bps"]["borrow"], 0.05 * 0.25 * 1e4, places=3)
        self.assertEqual(conv["costs_bps"]["borrow"], 0.0)
        self.assertEqual(conv["costs_bps"]["half_spread_slip"], 0.0)
        # option fee: min(3 bp of notional, 12.5% premium) + 2 bp settlement, per option leg
        self.assertAlmostEqual(conv["costs_bps"]["fees"], 10 + 2 * (3 + 2), places=4)

    def test_discounting_uses_ref_rate(self):
        s, p = spot_perp()
        recs = pas.ArbScanner(cfg(ref_rate_apr=0.08)).scan_a2(snap(s, p, None, self.chain()))
        conv = only(recs, pas.FAMILY_A2_CONV)[0]
        self.assertAlmostEqual(conv["discount_factor"], math.exp(-0.08 * 0.25), places=6)
        self.assertLess(conv["gross_edge_bps"], 7 / 2001 * 1e4)
        self.assertGreater(conv["costs_bps"]["capital_opp"], 0)

    def test_base_ccy_premium_converted_conservatively(self):
        s, p = spot_perp()  # spot bid 1999 / ask 2001
        chain = [
            opt(2000, "C", 0.05, 0.051, premium_ccy="base"),
            opt(2000, "P", 0.045, 0.046, premium_ccy="base"),
        ]
        recs = pas.ArbScanner(cfg()).scan_a2(snap(s, p, None, chain))
        conv = only(recs, pas.FAMILY_A2_CONV)[0]
        # buy put: pay coin bought at spot ask; sell call: receive coin sold at spot bid
        self.assertAlmostEqual(conv["pcp"]["put_exec_quote"], 0.046 * 2001, places=6)
        self.assertAlmostEqual(conv["pcp"]["call_exec_quote"], 0.05 * 1999, places=6)
        self.assertIn("option_premium_in_base_ccy_converted_at_spot_bid_ask", conv["risk_flags"])

    def test_perp_anchor_downgrades_taxonomy_and_charges_funding(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.0002, interval_sec=28_800)
        recs = pas.ArbScanner(cfg(pcp_anchor="perp")).scan_a2(snap(s, p, fr, self.chain()))
        conv = only(recs, pas.FAMILY_A2_CONV)[0]
        rev = only(recs, pas.FAMILY_A2_REV)[0]
        for r in (conv, rev):
            self.assertEqual(r["taxonomy"], pas.TAXONOMY_IDENTITY_PERP_PROXY)
            self.assertEqual(r["taxonomy_base"], pas.TAXONOMY_IDENTITY)
            self.assertEqual(r["anchor"], "perp")
            self.assertEqual(r["legs"][0]["instrument"], "ETH-USDT-SWAP")
            self.assertIn("perp_forward_proxy_funding_in_costs", r["risk_flags"])
            self.assertGreater(r["costs_bps"]["funding_uncertainty"], 0)
            self.assertGreater(r["costs_bps"]["half_spread_slip"], 0)
        # long perp pays positive funding; short perp receives (not credited → 0)
        intervals = 0.25 * 365 * 86400 / 28_800
        self.assertAlmostEqual(
            conv["costs_bps"]["funding_expected"], 0.0002 * intervals * 1e4, places=2
        )
        self.assertEqual(rev["costs_bps"]["funding_expected"], 0.0)
        self.assertNotIn("requires_spot_borrow", rev["risk_flags"])

    def test_one_sided_option_book_is_skipped_not_guessed(self):
        s, p = spot_perp()
        call = pas.Book(
            "OPT-2000-C",
            "option",
            bids=(),
            asks=(pas.Level(102.0, 5),),
            ts_ms=T0,
            opt_type="C",
            strike=2000.0,
            expiry_ms=EXPIRY,
        )
        recs = pas.ArbScanner(cfg()).scan_a2(snap(s, p, None, [call, opt(2000, "P", 90, 92)]))
        # conversion needs the call bid → cannot be priced; reversal (buy call @ ask) can
        self.assertEqual({r["family"] for r in recs}, {pas.FAMILY_A2_REV})
        self.assertIn("one_sided_book", recs[0]["risk_flags"])
        self.assertFalse(recs[0]["liquidity"]["ok"])
        self.assertFalse(recs[0]["passes_threshold"])

    def test_expired_option_invalidated(self):
        s, p = spot_perp()
        chain = [opt(2000, "C", 100, 102, expiry=T0 - 1), opt(2000, "P", 90, 92, expiry=T0 - 1)]
        recs = pas.ArbScanner(cfg()).scan_a2(snap(s, p, None, chain))
        self.assertTrue(all("expired_or_no_time_to_expiry" in r["invalidated_by"] for r in recs))


# --------------------------------------------------------------------------- A3


class A3BoxTest(unittest.TestCase):
    def chain(self):
        return [
            opt(1900, "C", 150.0, 152.0),
            opt(1900, "P", 40.0, 42.0),
            opt(2100, "C", 50.0, 52.0),
            opt(2100, "P", 130.0, 132.0),
        ]

    def test_buy_and_sell_box_numbers(self):
        s, p = spot_perp()
        recs = pas.ArbScanner(cfg()).scan_a3(snap(s, p, None, self.chain()))
        self.assertEqual(len(recs), 2)
        buy = next(r for r in recs if r["direction"] == "buy_box")
        sell = next(r for r in recs if r["direction"] == "sell_box")
        for r in (buy, sell):
            self.assertEqual(r["family"], pas.FAMILY_A3)
            self.assertEqual(r["taxonomy"], pas.TAXONOMY_IDENTITY)
            self.assertEqual(r["hypothesis_id"], "H-A3")
            self.assertEqual(r["strikes"], [1900.0, 2100.0])
            self.assertEqual(len(r["legs"]), 4)
            self.assertEqual(r["box"]["payoff_quote"], 200.0)
        # buy box: C1_ask − C2_bid + P2_ask − P1_bid = 152 − 50 + 132 − 40 = 194 → gross 6
        self.assertAlmostEqual(buy["box"]["premium_exec_quote"], 194.0, places=6)
        self.assertAlmostEqual(buy["gross_edge_bps"], 6 / 2001 * 1e4, places=3)
        self.assertAlmostEqual(
            buy["box"]["implied_box_rate_apr"], -math.log(194 / 200) / 0.25, places=5
        )
        self.assertEqual(
            [(leg["role"], leg["side"], leg["price_type"]) for leg in buy["legs"]],
            [
                ("call", "buy", "ask"),
                ("call", "sell", "bid"),
                ("put", "buy", "ask"),
                ("put", "sell", "bid"),
            ],
        )
        self.assertEqual(buy["margin_capital_required"], 194.0)
        # sell box: credit 150 − 52 + 130 − 42 = 186 vs 200 owed → gross −14; policy-gated
        self.assertAlmostEqual(sell["box"]["premium_exec_quote"], -186.0, places=6)
        self.assertAlmostEqual(sell["gross_edge_bps"], -14 / 2001 * 1e4, places=3)
        self.assertIn("short_box_margin_not_approved", sell["risk_flags"])
        self.assertIn("short_box_margin_not_approved", sell["invalidated_by"])
        self.assertFalse(sell["passes_threshold"])
        self.assertEqual(sell["margin_capital_required"], 200.0)

    def test_sell_box_never_passes_even_with_edge(self):
        s, p = spot_perp()
        chain = [
            opt(1900, "C", 190.0, 192.0),  # rich K1 call → selling the box looks attractive
            opt(1900, "P", 40.0, 42.0),
            opt(2100, "C", 50.0, 52.0),
            opt(2100, "P", 130.0, 132.0),
        ]
        recs = pas.ArbScanner(cfg()).scan_a3(snap(s, p, None, chain))
        sell = next(r for r in recs if r["direction"] == "sell_box")
        self.assertTrue(sell["edge_exceeds_buffer"])
        self.assertFalse(sell["passes_threshold"])

    def test_missing_leg_skips_pair(self):
        s, p = spot_perp()
        recs = pas.ArbScanner(cfg()).scan_a3(snap(s, p, None, self.chain()[:3]))
        self.assertEqual(recs, [])

    def test_max_box_pairs(self):
        s, p = spot_perp()
        chain = self.chain() + [opt(2000, "C", 100, 102), opt(2000, "P", 90, 92)]
        recs = pas.ArbScanner(cfg(max_box_pairs=1)).scan_a3(snap(s, p, None, chain))
        self.assertEqual(len(recs), 2)  # one pair × buy/sell
        recs = pas.ArbScanner(cfg(max_box_pairs=6)).scan_a3(snap(s, p, None, chain))
        self.assertEqual(len(recs), 6)  # three pairs × buy/sell


# --------------------------------------------------------------------------- filters / buffer


class FiltersAndBufferTest(unittest.TestCase):
    def test_safety_buffer_is_sum_of_components(self):
        buf = pas.SafetyBuffer(
            fee_roundtrip_bps=None, slip_buffer_bps=5, funding_uncert_bps=5, model_haircut_bps=10
        )
        r = buf.resolve(fees_bps=30.0)
        self.assertEqual(r["components"]["fee_roundtrip_bps"], 30.0)
        self.assertEqual(r["total_bps"], 50.0)
        self.assertIs(r["calibrated"], False)
        r2 = pas.SafetyBuffer(fee_roundtrip_bps=12.0, calibrated=True).resolve(fees_bps=30.0)
        self.assertEqual(r2["components"]["fee_roundtrip_bps"], 12.0)
        self.assertEqual(r2["total_bps"], 12 + 5 + 5 + 10)
        self.assertIs(r2["calibrated"], True)

    def test_record_buffer_matches_config_and_fees(self):
        s, p = spot_perp()
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003)))[0]
        comps = rec["safety_buffer"]["components"]
        self.assertEqual(comps["fee_roundtrip_bps"], rec["costs_bps"]["fees"])
        self.assertEqual(rec["safety_buffer_bps"], sum(comps.values()))
        self.assertEqual(rec["edge_exceeds_buffer"], rec["net_edge_bps"] > rec["safety_buffer_bps"])

    def test_persistence_requires_consecutive_samples_and_duration(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.01)
        scanner = pas.ArbScanner(cfg(persistence_min_samples=3, persistence_min_sec=60))
        oks = []
        for i in range(3):
            rec = scanner.scan_a1(snap(s, p, fr, ts=T0 + i * 30_000))[0]
            oks.append(
                (
                    rec["persistence"]["samples_ok"],
                    rec["persistence"]["ok"],
                    rec["passes_threshold"],
                )
            )
        self.assertEqual(oks, [(1, False, False), (2, False, False), (3, True, True)])
        # an edge collapse resets the streak
        rec = scanner.scan_a1(snap(s, p, pas.Funding(0.0), ts=T0 + 90_000))[0]
        self.assertEqual(rec["persistence"]["samples_ok"], 0)
        rec = scanner.scan_a1(snap(s, p, fr, ts=T0 + 120_000))[0]
        self.assertEqual(rec["persistence"]["samples_ok"], 1)
        self.assertFalse(rec["passes_threshold"])

    def test_persistence_duration_gate(self):
        s, p = spot_perp()
        fr = pas.Funding(rate=0.01)
        scanner = pas.ArbScanner(cfg(persistence_min_samples=2, persistence_min_sec=100))
        scanner.scan_a1(snap(s, p, fr, ts=T0))
        rec = scanner.scan_a1(snap(s, p, fr, ts=T0 + 30_000))[0]
        self.assertEqual(rec["persistence"]["samples_ok"], 2)
        self.assertFalse(rec["persistence"]["ok"])  # 30s < 100s

    def test_thin_book_flags_illiquid_and_impact(self):
        s = book("ETH-USDT", "spot", 1999.0, 2001.0, sz=0.4, levels=3, tick=1.0)
        p = book("ETH-USDT-SWAP", "perp", 2002.0, 2003.0, sz=100.0)
        rec = pas.ArbScanner(cfg(qty=1.0, depth_mult=2.0)).scan_a1(snap(s, p, pas.Funding(0.01)))[0]
        # 3 levels × 0.4 = 1.2 filled of 1.0 → complete, but depth 1.2 < 2.0 required
        self.assertFalse(rec["liquidity"]["ok"])
        self.assertIn("illiquid", rec["risk_flags"])
        self.assertGreater(rec["costs_bps"]["impact"], 0)  # walked into 2002 / 2003 asks
        self.assertFalse(rec["passes_threshold"])
        rec2 = pas.ArbScanner(cfg(qty=5.0)).scan_a1(snap(s, p, pas.Funding(0.01)))[0]
        self.assertIn("insufficient_depth", rec2["risk_flags"])
        self.assertIn("insufficient_depth", rec2["invalidated_by"])

    def test_book_walk(self):
        b = book("X", "spot", 99.0, 100.0, sz=1.0, levels=3, tick=1.0)
        w = b.walk("buy", 2.5)
        self.assertTrue(w.complete)
        self.assertEqual(w.levels_used, 3)
        self.assertAlmostEqual(w.vwap, (100 + 101 + 0.5 * 102) / 2.5)
        w = b.walk("sell", 10.0)
        self.assertFalse(w.complete)
        self.assertEqual(w.filled_qty, 3.0)
        self.assertEqual(pas.Book("E", "spot", (), (), 0).walk("buy", 1.0).vwap, None)

    def test_paper_fill_block(self):
        s, p = spot_perp()
        rec = pas.ArbScanner(cfg(paper_fills=True, paper_extra_slip_bps=5.0)).scan_a1(
            snap(s, p, pas.Funding(0.0003))
        )[0]
        pf = rec["paper_fill"]
        self.assertIs(pf["order_sent"], False)
        self.assertIs(pf["will_send_http"], False)
        self.assertAlmostEqual(
            pf["net_edge_after_fill_bps"], rec["net_edge_bps"] - 5.0 * 2, places=4
        )
        self.assertGreater(pf["legs"][0]["fill_px_hypothetical"], 2001.0)  # buy: worse than ask
        self.assertLess(pf["legs"][1]["fill_px_hypothetical"], 2002.0)  # sell: worse than bid
        rec_off = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003)))[0]
        self.assertNotIn("paper_fill", rec_off)

    def test_mark_is_reference_only(self):
        s, p = spot_perp()
        rec = pas.ArbScanner(cfg()).scan_a1(snap(s, p, pas.Funding(0.0003)))[0]
        self.assertEqual(rec["legs"][1]["mark_price_ref"], 2002.5)
        self.assertEqual(rec["legs"][1]["executable_price"], 2002.0)
        self.assertEqual(rec["executable_prices"]["ETH-USDT-SWAP"]["mark_ref_only"], 2002.5)


# --------------------------------------------------------------------------- fixture replay


class FixtureReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = pas.load_fixture(FIXTURE)
        scanner = pas.ArbScanner(pas.ScanConfig())
        cls.records = [r for s in cls.snaps for r in scanner.scan(s)]

    def test_fixture_shape(self):
        self.assertEqual(len(self.snaps), 4)
        self.assertTrue(all(s.source["http_fetch"] is False for s in self.snaps))
        self.assertEqual(len(self.snaps[0].options), 6)

    def test_every_record_is_observe_only_and_schema_complete(self):
        self.assertGreater(len(self.records), 0)
        for r in self.records:
            for k in pas.REQUIRED_FIELDS:
                self.assertIn(k, r)
            self.assertEqual(r["action"], "observe_only")
            self.assertIs(r["will_send_http"], False)
            self.assertIn(r["family"], pas.FAMILIES)
            self.assertIn(r["taxonomy"], pas.TAXONOMIES)
            self.assertIn("total", r["costs_bps"])
            self.assertIsInstance(r["risk_flags"], list)
            self.assertEqual(r["venue"], "okx")
            self.assertTrue(r["timestamp"].endswith("+08:00"))
            for leg in r["legs"]:
                self.assertIn(leg["price_type"], ("bid", "ask"))
                self.assertEqual(leg["price_type"], "ask" if leg["side"] == "buy" else "bid")
            self.assertAlmostEqual(
                r["net_edge_bps"], r["gross_edge_bps"] - r["costs_bps"]["total"], places=3
            )

    def test_taxonomy_per_family(self):
        for r in self.records:
            if r["family"] == pas.FAMILY_A1:
                self.assertEqual(r["taxonomy"], pas.TAXONOMY_RV)
                self.assertIn("relative_value_not_riskless", r["risk_flags"])
            else:
                self.assertEqual(r["taxonomy"], pas.TAXONOMY_IDENTITY)
                self.assertIn("identity_approx_not_riskless", r["risk_flags"])

    def test_families_present_and_persistence_path_exercised(self):
        fams = {r["family"] for r in self.records}
        self.assertEqual(fams, set(pas.FAMILIES))
        # A1 realistic: 2.5bp × 3 funding cannot beat 30bp fees
        self.assertTrue(all(r["net_edge_bps"] < 0 for r in only(self.records, pas.FAMILY_A1)))
        # dislocated ATM call in snapshots 2–4 → conversion exceeds buffer 3×, passes on the 3rd
        conv = [r for r in only(self.records, pas.FAMILY_A2_CONV) if r["strike"] == 2400.0]
        self.assertEqual([r["edge_exceeds_buffer"] for r in conv], [False, True, True, True])
        self.assertEqual([r["persistence"]["samples_ok"] for r in conv], [0, 1, 2, 3])
        self.assertEqual([r["passes_threshold"] for r in conv], [False, False, False, True])
        # no sell box ever passes (margin approval gate)
        self.assertFalse(
            any(
                r["passes_threshold"]
                for r in self.records
                if r["family"] == pas.FAMILY_A3 and r["direction"] == "sell_box"
            )
        )

    def test_summary(self):
        s = pas.summarize(self.records, len(self.snaps), pas.ScanConfig(), self.snaps[0].source)
        self.assertEqual(s["action"], "observe_only")
        self.assertIs(s["will_send_http"], False)
        self.assertEqual(set(s["hypotheses"]), {"H-A1", "H-A2", "H-A3"})
        self.assertEqual(s["hypotheses"]["H-A1"]["status"], "no_pass_on_window")
        self.assertEqual(s["hypotheses"]["H-A2"]["status"], "not_yet_falsified_on_window")
        self.assertEqual(s["mainline_unchanged"], "spot_grid_local_paper")
        self.assertIs(s["config"]["safety_buffer"]["calibrated"], False)
        self.assertEqual(s["config"]["leverage_concept"], 1)
        for v in s["families"].values():
            self.assertNotIn("relative_value", v["taxonomies"]) if v is s["families"][
                pas.FAMILY_A3
            ] else None
        text = json.dumps(s, ensure_ascii=False)
        for banned in ("无风险", "risk_free", "guaranteed", "稳赚"):
            self.assertNotIn(banned, text)


# --------------------------------------------------------------------------- cli


class CliTest(unittest.TestCase):
    def test_cli_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ = Path(d) / "arb.jsonl", Path(d) / "summary.json"
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
            self.assertTrue(all(r["paper_fill"]["order_sent"] is False for r in rows))
            s = json.loads(summ.read_text())
            self.assertEqual(s["records"], len(rows))
            self.assertEqual(s["snapshots"], 4)
            self.assertIs(s["trading_http"]["order"], False)

    def test_cli_only_exceeding_and_stdout(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--only-exceeding", "--print-summary"],
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

    def test_help(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe_only", proc.stdout)


# --------------------------------------------------------------------------- okx public (fake)

EXP_MS = 1_793_347_200_000


class FakeOkx(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_a):
        pass

    def _send(self, doc):
        body = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        FakeOkx.requests.append({"method": "POST", "path": self.path})
        self._send({"code": "405", "msg": "never", "data": []})

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeOkx.requests.append({"method": "GET", "path": u.path, "query": q})
        if u.path == okx.PATH_BOOKS:
            inst = q["instId"]
            if inst == "ETH-USDT":
                bids, asks = [["2399.9", "3.2", "0", "1"]], [["2400.1", "2.8", "0", "1"]]
            elif inst == "ETH-USDT-SWAP":
                bids, asks = [["2401", "400", "0", "1"]], [["2401.2", "380", "0", "1"]]  # contracts
            elif inst.endswith("-C"):
                bids, asks = [["0.081", "60", "0", "1"]], [["0.085", "60", "0", "1"]]
            else:
                bids, asks = [["0.081", "60", "0", "1"]], [["0.085", "60", "0", "1"]]
            return self._send(
                {
                    "code": "0",
                    "msg": "",
                    "data": [{"bids": bids, "asks": asks, "ts": "1789573200000"}],
                }
            )
        if u.path == okx.PATH_FUNDING_RATE:
            return self._send(
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "instId": q["instId"],
                            "fundingRate": "0.0003",
                            "nextFundingRate": "",
                            "fundingTime": "1789574400000",
                            "nextFundingTime": "1789603200000",
                            "method": "current_period",
                        }
                    ],
                }
            )
        if u.path == okx.PATH_INSTRUMENTS:
            if q["instType"] == "SWAP":
                return self._send(
                    {
                        "code": "0",
                        "msg": "",
                        "data": [
                            {
                                "instId": "ETH-USDT-SWAP",
                                "instType": "SWAP",
                                "ctVal": "0.1",
                                "ctValCcy": "ETH",
                                "state": "live",
                            }
                        ],
                    }
                )
            rows = []
            for k in (2200, 2300, 2400, 2500, 2600):
                for t in ("C", "P"):
                    rows.append(
                        {
                            "instId": f"ETH-USD-261030-{k}-{t}",
                            "instType": "OPTION",
                            "instFamily": "ETH-USD",
                            "settleCcy": "ETH",
                            "ctVal": "0.1",
                            "ctValCcy": "ETH",
                            "optType": t,
                            "stk": str(k),
                            "expTime": str(EXP_MS),
                            "state": "live",
                        }
                    )
            rows.append(
                {
                    "instId": "ETH-USD-260917-2400-C",
                    "instType": "OPTION",
                    "instFamily": "ETH-USD",
                    "settleCcy": "ETH",
                    "ctVal": "0.1",
                    "optType": "C",
                    "stk": "2400",
                    "expTime": str(1_789_600_000_000),
                    "state": "live",
                }
            )  # too near → excluded
            return self._send({"code": "0", "msg": "", "data": rows})
        if u.path == okx.PATH_TICKERS:
            return self._send(
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "instId": "ETH-USD-261030-2400-C",
                            "bidPx": "0.081",
                            "askPx": "0.085",
                            "bidSz": "1",
                            "askSz": "1",
                            "last": "0.083",
                            "vol24h": "10",
                            "ts": "1",
                        }
                    ],
                }
            )
        return self._send({"code": "404", "msg": "nf", "data": []})


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

    def test_client_endpoints(self):
        c = okx.OkxPublicClient(base_url=self.base)
        b = c.get_books("ETH-USDT-SWAP", sz=1)
        self.assertEqual(b["bids"], [(2401.0, 400.0)])
        fr = c.get_funding_rate("ETH-USDT-SWAP")
        self.assertEqual(fr["rate"], 0.0003)
        self.assertIsNone(fr["next_rate"])
        self.assertEqual(fr["interval_sec"], 28_800)
        inst = c.get_instruments("SWAP", inst_id="ETH-USDT-SWAP")
        self.assertEqual(inst[0]["ctVal"], 0.1)
        t = c.get_tickers("OPTION", inst_family="ETH-USD")
        self.assertEqual(t[0]["bidPx"], 0.081)
        with self.assertRaises(ValueError):
            c.get_books("ETH-USDT", sz=0)

    def test_snapshot_source_scales_contracts_and_selects_strikes(self):
        c = okx.OkxPublicClient(base_url=self.base)
        src = pas.OkxPublicSnapshotSource(
            c, n_strikes=3, max_expiries=1, min_days_to_expiry=2.0, book_depth=1
        )
        s = src.fetch()
        self.assertEqual(s.venue, "okx")
        self.assertEqual(s.spot.best_ask, 2400.1)
        self.assertEqual(s.perp.bids[0].sz, 400 * 0.1)  # contracts × ctVal
        self.assertEqual(s.funding.rate, 0.0003)
        strikes = sorted({o.strike for o in s.options})
        self.assertEqual(strikes, [2300.0, 2400.0, 2500.0])  # nearest 3 to spot
        self.assertEqual(len(s.options), 6)
        self.assertTrue(all(o.premium_ccy == "base" and o.expiry_ms == EXP_MS for o in s.options))
        self.assertEqual(s.options[0].bids[0].sz, 60 * 0.1)
        self.assertTrue(s.source["read_only"] and s.source["auth"] == "none")
        self.assertTrue(all(r["method"] == "GET" for r in FakeOkx.requests))
        paths = {r["path"] for r in FakeOkx.requests}
        self.assertEqual(paths, {okx.PATH_BOOKS, okx.PATH_FUNDING_RATE, okx.PATH_INSTRUMENTS})
        # 2 instruments + spot + perp + funding + 6 option books
        self.assertEqual(len(FakeOkx.requests), 11)
        recs = pas.ArbScanner(cfg()).scan(s)
        self.assertEqual({r["family"] for r in recs}, set(pas.FAMILIES))
        self.assertTrue(all(r["will_send_http"] is False for r in recs))

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
                "2",
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
        self.assertEqual(s["data_source"]["option_books"], 4)
        self.assertIs(s["will_send_http"], False)
        self.assertFalse(any(r["method"] == "POST" for r in FakeOkx.requests))


if __name__ == "__main__":
    unittest.main()
