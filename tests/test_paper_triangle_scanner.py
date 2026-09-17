"""Offline unit tests for strategies/paper_triangle_scanner.py (menu T1; no network).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies"))
sys.path.insert(0, str(ROOT / "tools"))

import cost_engine as ce  # noqa: E402
import okx_readonly_client as okx  # noqa: E402
import paper_arb_scanner as pas  # noqa: E402
import paper_triangle_scanner as pts  # noqa: E402

FIXTURE = ROOT / "fixtures" / "arb_books" / "2026-09-17-eth-btc-usdt-triangle-sample.json"
SCRIPT = ROOT / "strategies" / "paper_triangle_scanner.py"
T0 = 1_789_573_200_000
N = 100.0


def book(inst, bid, ask, tick, sz=10.0, levels=3, ts=T0, sizes_bid=None, sizes_ask=None):
    sb = sizes_bid or [sz] * levels
    sa = sizes_ask or [sz] * levels
    bids = tuple(pas.Level(round(bid - i * tick, 10), s) for i, s in enumerate(sb))
    asks = tuple(pas.Level(round(ask + i * tick, 10), s) for i, s in enumerate(sa))
    return pas.Book(inst_id=inst, kind="spot", bids=bids, asks=asks, ts_ms=ts)


def books(
    eth=(2399.9, 2400.1),
    btc=(63995.0, 64005.0),
    cross=(0.03749, 0.03751),
    ts_eth=T0,
    ts_btc=T0,
    ts_cross=T0,
    cross_sizes_bid=None,
    cross_sizes_ask=None,
):
    return {
        "ETH-USDT": book("ETH-USDT", *eth, 0.1, sz=5.0, ts=ts_eth),
        "BTC-USDT": book("BTC-USDT", *btc, 5.0, sz=0.5, ts=ts_btc),
        "ETH-BTC": book(
            "ETH-BTC",
            *cross,
            0.00001,
            sz=4.0,
            ts=ts_cross,
            sizes_bid=cross_sizes_bid,
            sizes_ask=cross_sizes_ask,
        ),
    }


def snap(bk=None, ts=T0) -> pts.TriangleSnapshot:
    return pts.TriangleSnapshot(ts_ms=ts, venue="okx", books=bk or books())


def cfg(**kw) -> pts.TriangleConfig:
    base = dict(persistence_min_samples=1, persistence_min_sec=0.0)
    base.update(kw)
    return pts.TriangleConfig(**base)


def by_dir(recs):
    return {r["direction"]: r for r in recs}


# --------------------------------------------------------------------------- hard gates


class HardGateTest(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(pts.ACTION, "observe_only")
        self.assertIs(pts.WILL_SEND_HTTP, False)
        self.assertEqual(pts.TAXONOMY, "same_venue_microstructure")
        self.assertNotIn("risk_free", pts.TAXONOMY)
        self.assertEqual(pts.MENU_ID, "T1")
        self.assertEqual(pts.WHITELIST_LEGS, ("ETH-USDT", "BTC-USDT", "ETH-BTC"))
        self.assertEqual(set(pts.DIRECTIONS), {"usdt_eth_btc_usdt", "usdt_btc_eth_usdt"})
        self.assertIn("non_atomic_three_leg", pts.RESIDUAL_RISKS)
        self.assertIn("leftover_inventory_on_partial_or_cancelled_leg", pts.RESIDUAL_RISKS)
        for k in ("residual_risks", "book_sync", "synthetic_fill", "cost_engine", "liquidity"):
            self.assertIn(k, pts.REQUIRED_FIELDS)
        self.assertEqual(pts.COST_KEYS, tuple(ce.CORE_COMPONENTS))
        self.assertIn("stale_book", pts.INVALIDATING_FLAGS)
        self.assertIn("illiquid", pts.INVALIDATING_FLAGS)

    def test_module_has_no_trading_http_path_and_leaves_mainline_alone(self):
        src = SCRIPT.read_text(encoding="utf-8")
        for needle in (
            "/api/v5/trade",
            "urlopen",
            'method="POST"',
            "place_order(",
            "amend_algo(",
            "withdraw(",
            "cancel_order(",
        ):
            self.assertNotIn(needle, src, needle)
        self.assertNotIn("import local_paper_grid", src)
        self.assertNotIn("import grid_ab_compare", src)
        self.assertNotIn("okx_grid_dry_run", src)

    def test_whitelist_is_enforced(self):
        pts.Triangle().validate()
        for bad in (
            pts.Triangle(home="USDT", a="SOL", b="BTC"),
            pts.Triangle(home="USDC", a="ETH", b="BTC"),
        ):
            with self.assertRaises(pts.TriangleNotWhitelisted):
                bad.validate()
            with self.assertRaises(pts.TriangleNotWhitelisted):
                pts.TriangleScanner(cfg(triangle=bad))
        # currency order inside the whitelist does not matter, instruments do
        pts.Triangle(home="USDT", a="ETH", b="BTC").validate()

    def test_config_validation_and_defaults_follow_04_risk(self):
        d = pts.TriangleConfig().to_dict()
        self.assertEqual(d["max_book_skew_ms"], 200)
        self.assertEqual(d["max_half_spread_bps"], 15.0)
        self.assertEqual(d["leverage_concept"], 1)
        self.assertIs(d["cost_engine"]["mandatory"], True)
        self.assertIs(d["cost_engine"]["calibrated"], False)
        self.assertIs(d["safety_buffer"]["calibrated"], False)
        for bad in (
            dict(notional_quote=0),
            dict(max_book_skew_ms=-1),
            dict(max_half_spread_bps=0),
            dict(nonatomic_slip_half_spreads=-1),
            dict(ref_rate_apr=-0.1),
            dict(persistence_min_samples=0),
        ):
            with self.assertRaises(ValueError):
                pts.TriangleConfig(**bad).validate()

    def test_finalize_refuses_schema_breaks(self):
        rec = pts.TriangleScanner(cfg()).scan(snap())[0]
        for bad in ({"action": "place_order"}, {"will_send_http": True}):
            with self.assertRaises(pas.ObserveOnlyViolation):
                pts.finalize_record({**rec, **bad})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "taxonomy": "relative_value"})
        with self.assertRaises((pts.TriangleSchemaViolation, pts.ForbiddenLabelViolation)):
            pts.finalize_record({**rec, "taxonomy": "risk_free"})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "residual_risks": ["gamma"]})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record(
                {
                    **rec,
                    "risk_flags": [f for f in rec["risk_flags"] if f != "non_atomic_three_leg"],
                }
            )
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "net_edge_bps": rec["net_edge_bps"] + 1.0})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "gross_edge_bps": rec["gross_edge_bps"] + 1.0})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record(
                {**rec, "costs_bps": {**rec["costs_bps"], "total": rec["costs_bps"]["total"] + 1}}
            )
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "leverage_concept": 2})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "direction": "usdt_sol_btc_usdt"})
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "legs": rec["legs"][:2]})
        with self.assertRaises(pas.ExecutablePriceViolation):
            pts.finalize_record(
                {**rec, "legs": [{**rec["legs"][0], "price_type": "mid"}] + rec["legs"][1:]}
            )
        with self.assertRaises(pas.ExecutablePriceViolation):  # buy at bid
            pts.finalize_record(
                {**rec, "legs": [{**rec["legs"][0], "price_type": "bid"}] + rec["legs"][1:]}
            )
        with self.assertRaises(pas.ObserveOnlyViolation):
            pts.finalize_record(
                {**rec, "synthetic_fill": {**rec["synthetic_fill"], "order_sent": True}}
            )
        with self.assertRaises(pas.ObserveOnlyViolation):
            pts.finalize_record(
                {**rec, "cost_engine": {**rec["cost_engine"], "will_send_http": True}}
            )
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record({**rec, "cost_engine": {**rec["cost_engine"], "calibrated": True}})
        with self.assertRaises(pts.TriangleSchemaViolation):  # a stale record can never pass
            pts.finalize_record({**rec, "passes_threshold": True, "invalidated_by": ["stale_book"]})
        for label in ("risk_free", "无风险套利", "稳赚", "guaranteed"):
            with self.assertRaises(pts.ForbiddenLabelViolation):
                pts.finalize_record({**rec, "note": f"this is {label}"})
        missing = {k: v for k, v in rec.items() if k != "residual_risks"}
        with self.assertRaises(pts.TriangleSchemaViolation):
            pts.finalize_record(missing)
        pts.finalize_record(rec)  # unchanged record is valid


# --------------------------------------------------------------------------- cycle arithmetic


class CycleMathTest(unittest.TestCase):
    def test_direction_a_hand_numbers(self):
        c = pts.build_cycle(pts.Triangle(), books(), pts.DIRECTION_A, N)
        qty_eth = N / 2400.1
        qty_btc = qty_eth * 0.03749
        out = qty_btc * 63995.0
        self.assertEqual(c.path, ("USDT", "ETH", "BTC", "USDT"))
        self.assertEqual(
            [(leg.instrument, leg.side, leg.price_type) for leg in c.legs],
            [("ETH-USDT", "buy", "ask"), ("ETH-BTC", "sell", "bid"), ("BTC-USDT", "sell", "bid")],
        )
        self.assertAlmostEqual(c.legs[0].qty, qty_eth, places=12)
        self.assertAlmostEqual(c.legs[1].qty, qty_eth, places=12)
        self.assertAlmostEqual(c.legs[2].qty, qty_btc, places=12)
        self.assertAlmostEqual(c.home_out, out, places=10)
        self.assertAlmostEqual(c.gross_quote, out - N, places=10)
        self.assertLess(c.gross_quote, 0)  # consistent books: spreads only
        self.assertEqual(c.legs[1].quote_conv_to_home, 64005.0)  # b-home ask, conservative
        self.assertAlmostEqual(c.cross_implied_from_directs, 2400.1 / 63995.0, places=12)

    def test_direction_b_hand_numbers(self):
        c = pts.build_cycle(pts.Triangle(), books(), pts.DIRECTION_B, N)
        qty_btc = N / 64005.0
        qty_eth = qty_btc / 0.03751
        out = qty_eth * 2399.9
        self.assertEqual(c.path, ("USDT", "BTC", "ETH", "USDT"))
        self.assertEqual(
            [(leg.instrument, leg.side, leg.price_type) for leg in c.legs],
            [("BTC-USDT", "buy", "ask"), ("ETH-BTC", "buy", "ask"), ("ETH-USDT", "sell", "bid")],
        )
        self.assertAlmostEqual(c.legs[0].qty, qty_btc, places=12)
        self.assertAlmostEqual(c.legs[1].qty, qty_eth, places=12)
        self.assertAlmostEqual(c.home_out, out, places=10)
        self.assertAlmostEqual(c.cross_implied_from_directs, 2399.9 / 64005.0, places=12)

    def test_rich_cross_favours_a_cheap_cross_favours_b(self):
        rich = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(cross=(0.03787, 0.03789)))))
        self.assertGreater(rich[pts.DIRECTION_A]["gross_edge_bps"], 0)
        self.assertLess(rich[pts.DIRECTION_B]["gross_edge_bps"], 0)
        self.assertGreater(rich[pts.DIRECTION_A]["triangle"]["cross_favourable_deviation_bps"], 0)
        cheap = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(cross=(0.03711, 0.03713)))))
        self.assertGreater(cheap[pts.DIRECTION_B]["gross_edge_bps"], 0)
        self.assertLess(cheap[pts.DIRECTION_A]["gross_edge_bps"], 0)
        self.assertGreater(cheap[pts.DIRECTION_B]["triangle"]["cross_favourable_deviation_bps"], 0)

    def test_one_sided_book_is_skipped_not_guessed(self):
        bk = books()
        bk["ETH-BTC"] = pas.Book("ETH-BTC", "spot", bids=(), asks=bk["ETH-BTC"].asks, ts_ms=T0)
        sc = pts.TriangleScanner(cfg())
        self.assertEqual(sc.scan(snap(bk)), [])
        self.assertEqual(sc.skipped["one_sided_book"], 2)
        self.assertIsNone(pts.build_cycle(pts.Triangle(), bk, pts.DIRECTION_A, N))
        del bk["BTC-USDT"]
        self.assertEqual(sc.scan(snap(bk)), [])
        self.assertEqual(sc.skipped["missing_book"], 1)

    def test_leg_refuses_non_bid_ask(self):
        with self.assertRaises(pas.ExecutablePriceViolation):
            pts.CycleLeg("ETH-USDT", "buy", 1.0, 2400.0, "mid", "ETH", "USDT", 1.0, T0)
        with self.assertRaises(pas.ExecutablePriceViolation):
            pts.CycleLeg("ETH-USDT", "buy", 1.0, 2400.0, "bid", "ETH", "USDT", 1.0, T0)
        with self.assertRaises(pas.ExecutablePriceViolation):
            pts.CycleLeg("ETH-USDT", "sell", 1.0, 2400.0, "ask", "ETH", "USDT", 1.0, T0)


# --------------------------------------------------------------------------- engine is the edge


class CostEngineIsTheEdgeTest(unittest.TestCase):
    def test_record_numbers_are_the_engine_numbers(self):
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        ceb = r["cost_engine"]
        self.assertEqual(ceb["engine"], "cost_engine")
        self.assertEqual(r["net_edge_source"], "tools/cost_engine.py:evaluate_legs")
        self.assertEqual(r["gross_edge_bps"], ceb["gross_edge_bps"])
        self.assertEqual(r["net_edge_bps"], ceb["net_edge_bps"])
        self.assertEqual(r["costs_bps"]["total"], ceb["all_in_cost_bps"])
        for k in ce.CORE_COMPONENTS:
            self.assertEqual(r["costs_bps"][k], ceb["components_bps"][k])
        self.assertAlmostEqual(
            r["net_edge_bps"], r["gross_edge_bps"] - r["costs_bps"]["total"], places=4
        )
        self.assertIs(ceb["calibrated"], False)
        self.assertIs(ceb["annualized"], False)
        self.assertIs(ceb["tradable_claim_allowed"], False)
        self.assertEqual(ceb["edge_basis"], "hold_horizon")
        self.assertIsNone(ceb["breakeven_funding_rate"])  # no funding thesis for a spot triangle
        self.assertEqual(ceb["hold_years"], 0.0)

    def test_evaluate_legs_is_actually_called(self):
        calls = []
        real = ce.evaluate_legs

        def spy(**kw):
            calls.append(kw)
            return real(**kw)

        with mock.patch.object(ce, "evaluate_legs", side_effect=spy):
            recs = pts.TriangleScanner(cfg()).scan(snap())
        self.assertEqual(len(calls), 2)
        for kw, r in zip(calls, recs, strict=True):
            self.assertEqual(len(kw["legs"]), 3)
            self.assertTrue(all(leg.kind == "spot" and leg.crossings == 1 for leg in kw["legs"]))
            self.assertTrue(all(leg.slip_crossings == 1 for leg in kw["legs"]))
            self.assertEqual(kw["underlying_notional_quote"], N)
            self.assertAlmostEqual(kw["gross_quote"], r["triangle"]["gross_quote"], places=6)

    def test_cost_components_hand_numbers(self):
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        c = pts.build_cycle(pts.Triangle(), books(), pts.DIRECTION_A, N)
        # fees: 10 bp of each leg's executable notional (cross leg converted at BTC-USDT ask)
        fees_q = sum(leg.exec_notional_home * 10 / 1e4 for leg in c.legs)
        self.assertAlmostEqual(r["costs_bps"]["fees"], fees_q / N * 1e4, places=3)
        # half_spread_slip: one extra half spread per leg (non-atomic re-quote haircut)
        hs_q = (
            (2400.1 - 2399.9) / 2 * c.legs[0].qty
            + (0.03751 - 0.03749) / 2 * c.legs[1].qty * 64005.0
            + (64005.0 - 63995.0) / 2 * c.legs[2].qty
        )
        self.assertAlmostEqual(r["costs_bps"]["half_spread_slip"], hs_q / N * 1e4, places=3)
        self.assertEqual(r["costs_bps"]["impact"], 0.0)  # qty sits inside the top level
        for k in ("borrow", "transfer", "capital_opp", "funding_uncertainty", "hedge_rebalance"):
            self.assertEqual(r["costs_bps"][k], 0.0)
        self.assertEqual(r["dominant_cost_component"], "fees")
        # gross must be the cycle gross in bps of the home notional
        self.assertAlmostEqual(r["gross_edge_bps"], c.gross_quote / N * 1e4, places=4)

    def test_impact_and_slip_knobs_flow_through_engine(self):
        # push qty beyond the top level so the walk has a VWAP worse than top → impact > 0
        bk = books(cross_sizes_bid=[0.02, 5.0, 5.0])
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap(bk)))[pts.DIRECTION_A]
        self.assertGreater(r["costs_bps"]["impact"], 0)
        self.assertGreater(r["cost_engine"]["components_bps"]["impact"], 0)
        self.assertTrue(r["synthetic_fill"]["ok"])  # still fills within three levels
        r0 = by_dir(pts.TriangleScanner(cfg(nonatomic_slip_half_spreads=0)).scan(snap()))[
            pts.DIRECTION_A
        ]
        self.assertEqual(r0["costs_bps"]["half_spread_slip"], 0.0)
        r2 = by_dir(pts.TriangleScanner(cfg(nonatomic_slip_half_spreads=2)).scan(snap()))[
            pts.DIRECTION_A
        ]
        r1 = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        self.assertAlmostEqual(
            r2["costs_bps"]["half_spread_slip"], 2 * r1["costs_bps"]["half_spread_slip"], places=3
        )
        self.assertEqual(r2["costs_bps"]["fees"], r1["costs_bps"]["fees"])  # fees unaffected
        rr = by_dir(pts.TriangleScanner(cfg(ref_rate_apr=0.05)).scan(snap()))[pts.DIRECTION_A]
        self.assertEqual(rr["costs_bps"]["capital_opp"], 0.0)  # zero hold horizon → no claim


# --------------------------------------------------------------------------- gates


class SyncGateTest(unittest.TestCase):
    def test_skew_within_200ms_ok_over_is_stale(self):
        ok = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(ts_cross=T0 - 200))))
        for r in ok.values():
            self.assertTrue(r["book_sync"]["ok"])
            self.assertEqual(r["book_sync"]["skew_ms"], 200)
            self.assertNotIn("stale_book", r["risk_flags"])
        stale = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(ts_cross=T0 - 201))))
        for r in stale.values():
            self.assertFalse(r["book_sync"]["ok"])
            self.assertEqual(r["book_sync"]["skew_ms"], 201)
            self.assertIn("stale_book", r["risk_flags"])
            self.assertIn("stale_book", r["invalidated_by"])
            self.assertFalse(r["passes_threshold"])
            self.assertFalse(r["synthetic_fill"]["ok"])
            self.assertIn("books_not_synced", r["synthetic_fill"]["reasons"])

    def test_stale_never_passes_even_with_edge(self):
        bk = books(cross=(0.03787, 0.03789), ts_btc=T0 + 500)
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap(bk)))[pts.DIRECTION_A]
        self.assertTrue(r["edge_exceeds_buffer"])
        self.assertTrue(r["persistence"]["ok"])
        self.assertIn("stale_book", r["invalidated_by"])
        self.assertFalse(r["passes_threshold"])

    def test_missing_timestamp_cannot_prove_sync(self):
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(ts_eth=0))))[pts.DIRECTION_A]
        self.assertFalse(r["book_sync"]["ok"])
        self.assertIsNone(r["book_sync"]["skew_ms"])
        self.assertIn("book_timestamp_missing", r["invalidated_by"])
        self.assertFalse(r["passes_threshold"])

    def test_skew_knob(self):
        r = by_dir(
            pts.TriangleScanner(cfg(max_book_skew_ms=500)).scan(snap(books(ts_cross=T0 - 400)))
        )
        self.assertTrue(all(x["book_sync"]["ok"] for x in r.values()))


class LiquidityGateTest(unittest.TestCase):
    def test_half_spread_cap_15bp(self):
        # ETH-BTC half spread = (ask − bid)/2 / bid: 0.0001/0.03749 ≈ 26.7 bp > 15
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(cross=(0.03749, 0.03769)))))
        for x in r.values():
            leg = next(g for g in x["liquidity"]["legs"] if g["instrument"] == "ETH-BTC")
            self.assertGreater(leg["half_spread_bps"], 15.0)
            self.assertFalse(leg["half_spread_ok"])
            self.assertIn("half_spread_over_cap", x["risk_flags"])
            self.assertIn("illiquid", x["invalidated_by"])
            self.assertFalse(x["liquidity"]["ok"])
            self.assertFalse(x["passes_threshold"])
        tight = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        self.assertTrue(tight["liquidity"]["ok"])
        for leg in tight["liquidity"]["legs"]:
            self.assertLessEqual(leg["half_spread_bps"], 15.0)
        self.assertAlmostEqual(
            pts.half_spread_bps(books()["ETH-BTC"]), 0.00002 / 2 / 0.03749 * 1e4, places=6
        )
        self.assertIsNone(pts.half_spread_bps(pas.Book("X", "spot", bids=(), asks=(), ts_ms=T0)))

    def test_thin_depth_fails_synthetic_fill(self):
        bk = books(cross_sizes_bid=[0.01])  # 100 USDT ≈ 0.0417 ETH; only 0.01 on the bid
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap(bk)))
        a = r[pts.DIRECTION_A]  # sells ETH on ETH-BTC → hits the thin bid
        self.assertIn("insufficient_depth", a["invalidated_by"])
        self.assertIn("illiquid", a["risk_flags"])
        self.assertFalse(a["synthetic_fill"]["ok"])
        self.assertEqual(a["synthetic_fill"]["reasons"], ["ETH-BTC:sell:partial_fill"])
        self.assertIs(a["synthetic_fill"]["order_sent"], False)
        b = r[pts.DIRECTION_B]  # buys ETH on ETH-BTC → ask side untouched
        self.assertTrue(b["synthetic_fill"]["ok"])
        self.assertEqual(b["invalidated_by"], [])

    def test_depth_mult_requirement(self):
        bk = books(cross_sizes_bid=[0.05, 0.01, 0.01])  # fills 0.0417 but top5 depth < 2× qty
        r = by_dir(pts.TriangleScanner(cfg(depth_mult=2.0)).scan(snap(bk)))[pts.DIRECTION_A]
        self.assertFalse(r["liquidity"]["ok"])
        self.assertIn("illiquid", r["invalidated_by"])
        self.assertTrue(r["synthetic_fill"]["ok"])  # the walk itself completes


# --------------------------------------------------------------------------- threshold/persistence


class ThresholdTest(unittest.TestCase):
    def test_buffer_is_sum_of_components_with_record_fees(self):
        r = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        sb = r["safety_buffer"]
        self.assertEqual(sb["components"]["fee_roundtrip_bps"], r["costs_bps"]["fees"])
        self.assertEqual(r["safety_buffer_bps"], round(sum(sb["components"].values()), 4))
        self.assertIs(sb["calibrated"], False)
        self.assertEqual(r["edge_exceeds_buffer"], r["net_edge_bps"] > r["safety_buffer_bps"])
        b = pts.TriangleSafetyBuffer(fee_roundtrip_bps=20.0, calibrated=True).resolve(30.0)
        self.assertEqual(b["total_bps"], 20 + 5 + 5 + 5)
        self.assertIs(b["calibrated"], True)

    def test_cost_kill_and_net_positive_flags(self):
        kill = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(cross=(0.037565, 0.037585)))))[
            pts.DIRECTION_A
        ]
        self.assertTrue(kill["gross_positive"])
        self.assertFalse(kill["net_positive"])
        self.assertTrue(kill["cost_killed"])
        self.assertFalse(kill["edge_exceeds_buffer"])
        pos = by_dir(pts.TriangleScanner(cfg()).scan(snap(books(cross=(0.03787, 0.03789)))))[
            pts.DIRECTION_A
        ]
        self.assertTrue(pos["net_positive"] and not pos["cost_killed"])
        neg = by_dir(pts.TriangleScanner(cfg()).scan(snap()))[pts.DIRECTION_A]
        self.assertFalse(neg["gross_positive"] or neg["cost_killed"])

    def test_persistence_then_pass(self):
        scanner = pts.TriangleScanner(cfg(persistence_min_samples=3, persistence_min_sec=60))
        seen = []
        for i in range(3):
            r = by_dir(scanner.scan(snap(books(cross=(0.03787, 0.03789)), ts=T0 + i * 30_000)))[
                pts.DIRECTION_A
            ]
            seen.append(
                (r["edge_exceeds_buffer"], r["persistence"]["samples_ok"], r["passes_threshold"])
            )
        self.assertEqual(seen, [(True, 1, False), (True, 2, False), (True, 3, True)])
        # the opposite direction never exceeded → never passes
        r = by_dir(scanner.scan(snap(books(cross=(0.03787, 0.03789)), ts=T0 + 90_000)))
        self.assertFalse(r[pts.DIRECTION_B]["passes_threshold"])
        self.assertEqual(r[pts.DIRECTION_B]["persistence"]["samples_ok"], 0)


# --------------------------------------------------------------------------- summary


class SummaryTest(unittest.TestCase):
    def test_rates(self):
        sc = pts.TriangleScanner(cfg())
        recs = []
        recs += sc.scan(snap())  # 2 × gross < 0
        recs += sc.scan(snap(books(cross=(0.037565, 0.037585)), ts=T0 + 1))  # A cost-killed
        recs += sc.scan(snap(books(cross=(0.03787, 0.03789)), ts=T0 + 2))  # A net > 0, pass
        recs += sc.scan(snap(books(cross=(0.03787, 0.03789), ts_cross=T0 - 999), ts=T0 + 3))
        s = pts.summarize(recs, 4, cfg(), {"kind": "fixture", "http_fetch": False}, sc.skipped)
        m = s["metrics"]
        self.assertEqual(m["records"], 8)
        self.assertEqual(m["gross_positive"], 3)
        self.assertEqual(m["net_positive"], 2)
        self.assertEqual(m["net_positive_rate"], 0.25)
        self.assertEqual(m["cost_killed"], 1)
        self.assertEqual(m["cost_kill_rate"], round(1 / 3, 4))
        self.assertEqual(m["passes_threshold"], 1)
        self.assertEqual(m["stale_book"], 2)
        self.assertEqual(m["synthetic_fill_ok"], 6)
        self.assertEqual(m["synthetic_fill_success_rate"], 0.75)
        self.assertEqual(m["dominant_cost_component"], {"fees": 8})
        self.assertEqual(s["directions"][pts.DIRECTION_A]["net_positive"], 2)
        self.assertEqual(s["directions"][pts.DIRECTION_B]["net_positive"], 0)
        h = s["hypotheses"]["H-T1"]
        self.assertEqual(h["status"], "not_yet_falsified_on_window")
        self.assertEqual(h["net_positive_rate"], 0.25)
        self.assertEqual(s["taxonomy"], "same_venue_microstructure")
        self.assertEqual(s["action"], "observe_only")
        self.assertIs(s["will_send_http"], False)
        self.assertTrue(all(v is False for v in s["trading_http"].values()))
        self.assertEqual(s["cost_engine"]["records"], 8)
        self.assertIs(s["cost_engine"]["calibrated"], False)
        self.assertEqual(s["mainline_unchanged"], "spot_grid_local_paper")
        text = json.dumps(s, ensure_ascii=False).lower()
        for bad in pts.FORBIDDEN_LABELS:
            self.assertNotIn(bad, text)

    def test_status_values(self):
        c = cfg()
        src = {"kind": "fixture", "http_fetch": False}
        self.assertEqual(pts.summarize([], 0, c, src)["hypotheses"]["H-T1"]["status"], "no_samples")
        recs = pts.TriangleScanner(c).scan(snap())
        self.assertEqual(
            pts.summarize(recs, 1, c, src)["hypotheses"]["H-T1"]["status"], "no_pass_on_window"
        )
        c3 = pts.TriangleConfig(persistence_min_samples=3)
        self.assertEqual(
            pts.summarize(recs, 1, c3, src)["hypotheses"]["H-T1"]["status"],
            "insufficient_samples_for_persistence",
        )
        self.assertIsNone(pts.summarize([], 0, c, src)["metrics"]["cost_kill_rate"])


# --------------------------------------------------------------------------- fixture replay


class FixtureReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = pts.load_fixture(FIXTURE)
        cls.scanner = pts.TriangleScanner(pts.TriangleConfig())
        cls.records = [r for s in cls.snaps for r in cls.scanner.scan(s)]

    def test_fixture_shape(self):
        self.assertEqual(len(self.snaps), 8)
        self.assertTrue(all(s.source["http_fetch"] is False for s in self.snaps))
        self.assertIn("SYNTHETIC", self.snaps[0].source["note"])
        for s in self.snaps:
            self.assertEqual(set(s.books), set(pts.WHITELIST_LEGS))
            self.assertTrue(all(b.ts_ms > 0 for b in s.books.values()))

    def test_every_record_is_observe_only_and_schema_complete(self):
        self.assertEqual(len(self.records), 16)
        self.assertEqual(dict(self.scanner.skipped), {})
        for r in self.records:
            for k in pts.REQUIRED_FIELDS:
                self.assertIn(k, r)
            self.assertEqual(r["action"], "observe_only")
            self.assertIs(r["will_send_http"], False)
            self.assertEqual(r["taxonomy"], "same_venue_microstructure")
            self.assertEqual(r["menu_id"], "T1")
            self.assertEqual(r["leverage_concept"], 1)
            self.assertIn(r["direction"], pts.DIRECTIONS)
            self.assertEqual(sorted(r["instruments"]), sorted(pts.WHITELIST_LEGS))
            self.assertTrue(set(pts.RESIDUAL_RISKS) <= set(r["residual_risks"]))
            self.assertIn("non_atomic_three_leg", r["risk_flags"])
            self.assertTrue(r["timestamp"].endswith("+08:00"))
            self.assertEqual(len(r["legs"]), 3)
            for leg in r["legs"]:
                self.assertIn(leg["price_type"], ("bid", "ask"))
                self.assertEqual(leg["price_type"], "ask" if leg["side"] == "buy" else "bid")
                self.assertEqual(leg["role"], "spot")
            ceb = r["cost_engine"]
            self.assertEqual(r["net_edge_bps"], ceb["net_edge_bps"])
            self.assertEqual(r["gross_edge_bps"], ceb["gross_edge_bps"])
            self.assertEqual(r["costs_bps"]["total"], ceb["all_in_cost_bps"])
            self.assertIs(ceb["calibrated"], False)
            self.assertIsNone(ceb["breakeven_funding_rate"])
            self.assertGreater(r["costs_bps"]["fees"], 0)
            self.assertGreater(r["costs_bps"]["half_spread_slip"], 0)
            self.assertIs(r["synthetic_fill"]["order_sent"], False)
            if r["passes_threshold"]:
                self.assertEqual(r["invalidated_by"], [])
                self.assertTrue(r["book_sync"]["ok"] and r["liquidity"]["ok"])
            text = json.dumps(r, ensure_ascii=False).lower()
            for bad in pts.FORBIDDEN_LABELS:
                self.assertNotIn(bad, text)

    def test_code_paths_by_snapshot(self):
        a = [r for r in self.records if r["direction"] == pts.DIRECTION_A]
        b = [r for r in self.records if r["direction"] == pts.DIRECTION_B]
        self.assertEqual(len(a), 8)
        self.assertEqual([r["gross_positive"] for r in a], [False, True] + [True] * 6)
        self.assertEqual([r["cost_killed"] for r in a], [False, True] + [False] * 6)
        self.assertEqual(
            [r["edge_exceeds_buffer"] for r in a[:5]], [False, False, True, True, True]
        )
        self.assertEqual([r["passes_threshold"] for r in a], [False] * 4 + [True] + [False] * 3)
        self.assertEqual(a[5]["invalidated_by"], ["stale_book"])
        self.assertEqual(a[5]["book_sync"]["skew_ms"], 350)
        self.assertIn("illiquid", a[6]["invalidated_by"])
        self.assertIn("half_spread_over_cap", a[6]["risk_flags"])
        self.assertEqual(a[7]["invalidated_by"], ["illiquid", "insufficient_depth"])
        self.assertFalse(a[7]["synthetic_fill"]["ok"])
        self.assertFalse(any(r["gross_positive"] for r in b))
        self.assertFalse(any(r["passes_threshold"] for r in b))

    def test_summary(self):
        s = pts.summarize(
            self.records,
            len(self.snaps),
            pts.TriangleConfig(),
            self.snaps[0].source,
            self.scanner.skipped,
        )
        m = s["metrics"]
        self.assertEqual((m["records"], m["gross_positive"], m["net_positive"]), (16, 7, 6))
        self.assertEqual(m["cost_killed"], 1)
        self.assertEqual(m["cost_kill_rate"], round(1 / 7, 4))
        self.assertEqual(m["net_positive_rate"], 0.375)
        self.assertEqual(m["passes_threshold"], 1)
        self.assertEqual(m["stale_book"], 2)
        self.assertEqual(m["illiquid"], 3)
        self.assertEqual(m["synthetic_fill_ok"], 13)
        self.assertEqual(m["synthetic_fill_success_rate"], 0.8125)
        self.assertEqual(s["snapshots"], 8)
        self.assertEqual(s["data_source"]["http_fetch"], False)
        self.assertEqual(s["hypotheses"]["H-T1"]["status"], "not_yet_falsified_on_window")


# --------------------------------------------------------------------------- cli


class CliTest(unittest.TestCase):
    def test_cli_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ = Path(d) / "t1.jsonl", Path(d) / "summary.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--source",
                    "fixture",
                    "--fixture",
                    str(FIXTURE),
                    "--venue",
                    "okx_demo",
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
            self.assertEqual(len(rows), 16)
            self.assertTrue(all(r["action"] == "observe_only" for r in rows))
            self.assertTrue(all(r["will_send_http"] is False for r in rows))
            self.assertTrue(all(r["venue"] == "okx_demo" for r in rows))
            self.assertTrue(all(r["taxonomy"] == "same_venue_microstructure" for r in rows))
            s = json.loads(summ.read_text())
            self.assertEqual(s["records"], 16)
            self.assertEqual(s["snapshots"], 8)
            self.assertEqual(s["venue"], "okx_demo")
            self.assertIs(s["trading_http"]["order"], False)

    def test_cli_only_exceeding_help_and_whitelist_refusal(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--only-exceeding", "--print-summary"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["edge_exceeds_buffer"] for r in rows))
        summary = json.loads(proc.stderr)
        self.assertEqual(summary["menu_id"], "T1")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe_only", proc.stdout)
        self.assertIn("same_venue_microstructure", proc.stdout)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--leg-a", "SOL", "--quiet"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("whitelist", proc.stderr)


# --------------------------------------------------------------------------- okx public (fake, GET)


class FakeTriangleOkx(BaseHTTPRequestHandler):
    requests: list[dict] = []
    books = {
        "ETH-USDT": ([["2399.9", "3.2", "0", "1"]], [["2400.1", "2.8", "0", "1"]], "1789573200000"),
        "BTC-USDT": ([["63995", "0.6", "0", "1"]], [["64005", "0.5", "0", "1"]], "1789573200040"),
        "ETH-BTC": ([["0.03749", "4", "0", "1"]], [["0.03751", "4", "0", "1"]], "1789573200090"),
    }

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
        FakeTriangleOkx.requests.append({"method": "POST", "path": self.path})
        self._send({"code": "405", "msg": "never", "data": []})

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeTriangleOkx.requests.append({"method": "GET", "path": u.path, "query": q})
        if u.path == okx.PATH_BOOKS and q["instId"] in self.books:
            bids, asks, ts = self.books[q["instId"]]
            return self._send(
                {"code": "0", "msg": "", "data": [{"bids": bids, "asks": asks, "ts": ts}]}
            )
        return self._send({"code": "51001", "msg": "unknown", "data": []})


class OkxPublicSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeTriangleOkx)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeTriangleOkx.requests.clear()

    def test_source_is_three_public_gets_with_book_timestamps(self):
        c = okx.OkxPublicClient(base_url=self.base)
        s = pts.OkxPublicTriangleSource(c, book_depth=1).fetch()
        self.assertEqual(set(s.books), set(pts.WHITELIST_LEGS))
        self.assertEqual(s.ts_ms, 1789573200090)
        self.assertEqual(s.source["kind"], "okx_public_books")
        self.assertEqual(s.source["requests_made"], 3)
        recs = pts.TriangleScanner(cfg()).scan(s)
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertIs(r["will_send_http"], False)
            self.assertTrue(r["book_sync"]["ok"])
            self.assertEqual(r["book_sync"]["skew_ms"], 90)
        self.assertTrue(all(r["method"] == "GET" for r in FakeTriangleOkx.requests))
        self.assertEqual({r["path"] for r in FakeTriangleOkx.requests}, {okx.PATH_BOOKS})
        self.assertFalse(any(r["method"] == "POST" for r in FakeTriangleOkx.requests))
        with self.assertRaises(pts.TriangleNotWhitelisted):
            pts.OkxPublicTriangleSource(c, triangle=pts.Triangle(a="SOL"))

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
        self.assertEqual(s["data_source"]["endpoints"], [okx.PATH_BOOKS])
        self.assertIs(s["will_send_http"], False)
        self.assertEqual(s["records"], 2)
        self.assertFalse(any(r["method"] == "POST" for r in FakeTriangleOkx.requests))


# --------------------------------------------------------------------------- slip_crossings


class CostEngineSlipCrossingsTest(unittest.TestCase):
    """The additive `slip_crossings` on LegSpec must leave A/B behaviour untouched."""

    def test_default_keeps_crossings_minus_one(self):
        fees = ce.FeeSchedule()
        rt = ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, bid=1999.0, ask=2001.0)  # crossings=2
        self.assertEqual(rt.spread_crossings, 1)
        self.assertIsNone(rt.slip_crossings)
        self.assertIsNone(rt.to_dict()["slip_crossings"])
        c = ce.leg_costs([rt], fees, underlying_notional_quote=2001.0)
        self.assertAlmostEqual(c["fees"], 2001.0 * 10 / 1e4 * 2, places=9)
        self.assertAlmostEqual(c["half_spread_slip"], 1.0, places=9)
        entry = ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, bid=1999.0, ask=2001.0, crossings=1)
        self.assertEqual(entry.spread_crossings, 0)
        self.assertEqual(
            ce.leg_costs([entry], fees, underlying_notional_quote=2001.0)["half_spread_slip"], 0.0
        )

    def test_explicit_slip_crossings_independent_of_fee_crossings(self):
        fees = ce.FeeSchedule()
        leg = ce.LegSpec(
            "ETH-USDT", "spot", "buy", 1.0, bid=1999.0, ask=2001.0, crossings=1, slip_crossings=1
        )
        c = ce.leg_costs([leg], fees, underlying_notional_quote=2001.0)
        self.assertAlmostEqual(c["fees"], 2001.0 * 10 / 1e4, places=9)  # one fee crossing
        self.assertAlmostEqual(c["half_spread_slip"], 1.0, places=9)  # one extra half spread
        rt = ce.LegSpec.from_dict({**leg.to_dict()})
        self.assertEqual(rt, leg)
        with self.assertRaises(ce.CostEngineError):
            ce.LegSpec("ETH-USDT", "spot", "buy", 1.0, bid=1999.0, ask=2001.0, slip_crossings=-1)


if __name__ == "__main__":
    unittest.main()
