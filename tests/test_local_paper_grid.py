"""Unit / smoke tests for tools/local_paper_grid.py (paper only, no HTTP).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import local_paper_grid as lpg  # noqa: E402

SCHEMA_METRIC_KEYS = {
    "okx_bot_total_pnl",
    "okx_bot_total_pnl_ratio",
    "realized_pnl",
    "unrealized_pnl",
    "grid_profit",
    "float_profit",
    "fees_paid_est",
    "fee_after_pnl_est",
    "arbitrage_num",
    "buy_count",
    "sell_count",
    "sl_hit_count",
    "invalidation_hit_count",
    "max_drawdown_ratio",
    "last_px",
}
SCHEMA_FLAG_KEYS = {
    "below_min_px",
    "sl_triggered",
    "pnl_ratio_lte_minus_8pct",
    "fee_gte_per_grid",
}
SCHEMA_TOP_KEYS = {
    "pulled_at",
    "source",
    "mode",
    "venue",
    "runId",
    "algoId",
    "strategy",
    "instId",
    "params",
    "window",
    "metrics",
    "invalidation_flags",
    "notes",
}

DEMO = lpg.GridParams(
    min_px=2200.0, max_px=3200.0, grid_num=30, investment=1000.0, sl_trigger_px=2150.0
)
FEES = lpg.FeeModel(maker=0.0008, taker=0.0010, worst_case=True)


def flat_candles(prices: list[float], start_ts: int = 1_789_488_000_000) -> list[lpg.Candle]:
    """Build candles whose open==close==high==low=price (pure close-to-close path)."""
    out = []
    prev = prices[0]
    for i, px in enumerate(prices):
        out.append(lpg.Candle(start_ts + i * 60_000, prev, max(prev, px), min(prev, px), px))
        prev = px
    return out


def run_engine(candles, params=DEMO, fees=FEES, **kw) -> lpg.LocalPaperGrid:
    eng = lpg.LocalPaperGrid(params, fees, **kw)
    eng.run(candles)
    return eng


class GridLevelsTest(unittest.TestCase):
    def test_arithmetic_levels(self):
        levels = lpg.build_levels(DEMO)
        self.assertEqual(len(levels), 31)
        self.assertEqual(levels[0], 2200.0)
        self.assertEqual(levels[-1], 3200.0)
        step = DEMO.step
        for a, b in zip(levels, levels[1:]):
            self.assertAlmostEqual(b - a, step, places=9)
        # matches bootstrap fixture per_grid_pct_paper ≈ 1.2345679%
        self.assertAlmostEqual(DEMO.per_grid_pct, 0.012345679012345678, places=12)

    def test_validation(self):
        with self.assertRaises(ValueError):
            lpg.GridParams(2200, 2100, 30, 1000).validate()
        with self.assertRaises(ValueError):
            lpg.GridParams(2200, 3200, 30, 1000, lever=2).validate()
        with self.assertRaises(ValueError):
            lpg.GridParams(2200, 3200, 30, 1000, sl_trigger_px=2250).validate()


class FeeModelTest(unittest.TestCase):
    def test_worst_case_round_trip(self):
        self.assertEqual(FEES.rate("maker"), 0.0010)
        self.assertEqual(FEES.round_trip, 0.002)
        mt = lpg.FeeModel(0.0008, 0.0010, worst_case=False)
        self.assertEqual(mt.rate("maker"), 0.0008)
        self.assertEqual(mt.rate("taker"), 0.0010)


class FillEngineTest(unittest.TestCase):
    def test_synthetic_oscillation_produces_fills(self):
        candles = lpg.gen_synthetic(2700.0, 250.0, 240, 1440, 3.0, seed=20260916)
        eng = run_engine(candles)
        s = eng.s
        self.assertGreater(s.buy_count, 0)
        self.assertGreater(s.sell_count, 0)
        self.assertGreater(s.arbitrage_num, 0)
        self.assertEqual(s.arbitrage_num, sum(1 for f in eng.fills if f.kind == "grid_sell"))
        self.assertEqual(s.sl_hit_count, 0)
        self.assertGreater(s.fees_quote, 0)

    def test_synthetic_is_deterministic(self):
        a = lpg.gen_synthetic(2700.0, 250.0, 240, 500, 3.0, seed=7)
        b = lpg.gen_synthetic(2700.0, 250.0, 240, 500, 3.0, seed=7)
        self.assertEqual(a, b)
        for c in a:
            self.assertGreaterEqual(c.high, max(c.open, c.close))
            self.assertLessEqual(c.low, min(c.open, c.close))

    def test_accounting_identity(self):
        candles = lpg.gen_synthetic(2700.0, 250.0, 240, 1440, 3.0, seed=1)
        eng = run_engine(candles)
        m = eng.metrics()
        # okx_bot_total_pnl (pre-fee) − fees == cash-accounting fee-after PnL
        self.assertAlmostEqual(
            m["okx_bot_total_pnl"] - m["fees_paid_est"], m["fee_after_pnl_est"], places=4
        )
        self.assertAlmostEqual(m["realized_pnl"] + m["unrealized_pnl"], m["okx_bot_total_pnl"], 4)
        self.assertAlmostEqual(m["okx_bot_total_pnl_ratio"], m["okx_bot_total_pnl"] / 1000.0, 6)
        self.assertGreaterEqual(m["max_drawdown_ratio"], 0.0)
        # base inventory equals the sum of held slots
        held = sum(sl.qty for sl in eng.slots if sl.holding)
        self.assertAlmostEqual(eng.s.base_qty, held, places=10)

    def test_single_round_trip_profit(self):
        # start at 2700 (a grid line), walk down one grid, back up two: expect 1 arbitrage.
        params = lpg.GridParams(2200.0, 3200.0, 30, 1000.0, sl_trigger_px=None)
        step = params.step
        candles = flat_candles([2700.0, 2700.0 - step, 2700.0 + step])
        eng = run_engine(candles, params=params)
        buys = [f for f in eng.fills if f.kind == "grid_buy"]
        sells = [f for f in eng.fills if f.kind == "grid_sell"]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys[0].px, 2700.0 - step)
        # the bought grid sells at 2700; initial-buy grid with upper 2700+step sells too
        self.assertEqual(len(sells), 2)
        pgq = params.per_grid_quote
        expected_gross = pgq * step / (2700.0 - step)
        self.assertAlmostEqual(sells[0].qty * (sells[0].px - buys[0].px), expected_gross, 9)
        self.assertEqual(eng.s.arbitrage_num, 2)

    def test_stop_loss_liquidates_and_stops(self):
        candles = flat_candles([2400.0, 2300.0, 2140.0, 2100.0, 2500.0, 2600.0])
        eng = run_engine(candles)
        s = eng.s
        self.assertEqual(s.sl_hit_count, 1)
        self.assertTrue(s.stopped)
        self.assertEqual(s.stop_reason, "sl_sell")
        self.assertAlmostEqual(s.base_qty, 0.0)
        sl_fills = [f for f in eng.fills if f.kind == "sl_sell"]
        self.assertEqual(len(sl_fills), 1)
        self.assertEqual(sl_fills[0].px, 2150.0)
        # no fills after the stop even though price bounces back into range
        self.assertTrue(all(f.ts_ms <= sl_fills[0].ts_ms for f in eng.fills))
        flags = eng.invalidation_flags()
        self.assertTrue(flags["sl_triggered"])
        self.assertTrue(flags["pnl_ratio_lte_minus_8pct"])
        self.assertGreaterEqual(eng.metrics()["invalidation_hit_count"], 2)
        self.assertLess(eng.metrics()["okx_bot_total_pnl_ratio"], -0.08)

    def test_out_of_band_start_waits_and_flags(self):
        candles = flat_candles([2100.0, 2120.0, 2150.0, 2180.0])
        eng = run_engine(candles)
        self.assertFalse(eng.s.started)
        self.assertEqual(len(eng.fills), 0)
        flags = eng.invalidation_flags()
        self.assertTrue(flags["below_min_px"])
        self.assertFalse(flags["price_in_band"])
        self.assertEqual(eng.metrics()["okx_bot_total_pnl"], 0.0)
        # SL must not fire before the grid has started (no inventory to protect)
        self.assertEqual(eng.s.sl_hit_count, 0)

    def test_fee_gte_per_grid_flag(self):
        params = lpg.GridParams(2200.0, 3200.0, 600, 1000.0, sl_trigger_px=None)
        eng = lpg.LocalPaperGrid(params, FEES)
        self.assertTrue(eng.invalidation_flags()["fee_gte_per_grid"])
        self.assertEqual(eng.metrics()["invalidation_hit_count"], 1)


class ReportTest(unittest.TestCase):
    def test_report_matches_schema_block(self):
        candles = lpg.gen_synthetic(2700.0, 250.0, 240, 300, 3.0, seed=3)
        eng = run_engine(candles)
        doc = lpg.build_report(
            eng,
            run_id="local-paper-eth-grid-001",
            inst_id="ETH-USDT",
            source_kind="synthetic",
            data_source={"kind": "synthetic_oscillating", "http_fetch": False},
            window_label="synthetic",
        )
        self.assertTrue(SCHEMA_TOP_KEYS <= set(doc))
        self.assertTrue(SCHEMA_METRIC_KEYS <= set(doc["metrics"]))
        self.assertTrue(SCHEMA_FLAG_KEYS <= set(doc["invalidation_flags"]))
        self.assertEqual(doc["venue"], "local_paper")
        self.assertEqual(doc["mode"], "模拟")
        self.assertEqual(doc["strategy"], "okx_spot_grid")
        self.assertIsNone(doc["algoId"])
        self.assertEqual(doc["params"]["lever"], 1)
        self.assertEqual(doc["params"]["slTriggerPx"], 2150.0)
        self.assertFalse(doc["will_send_http"])
        self.assertFalse(doc["trading_http"]["amend"])
        self.assertFalse(doc["data_source"]["http_fetch"])
        self.assertIsNotNone(doc["metrics"]["last_px"])
        self.assertIn("≠ OKX Bot", doc["disclaimer"])
        json.dumps(doc, ensure_ascii=False)  # serialisable
        self.assertTrue(lpg.default_out_name(doc).endswith("-local-paper-eth-grid-001.json"))


class CsvRoundTripTest(unittest.TestCase):
    def test_save_and_load_csv(self):
        candles = lpg.gen_synthetic(2700.0, 100.0, 60, 120, 1.0, seed=11)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.csv")
            lpg.save_csv(path, candles)
            loaded = lpg.load_csv(path)
        self.assertEqual(loaded, candles)

    def test_headerless_okx_row_shape(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "raw.csv")
            with open(path, "w", encoding="utf-8") as f:
                f.write("1789532280000,2403.41,2403.41,2402.89,2403.09,6.6,15923.7,15923.7,1\n")
                f.write("1789532220000,2402.22,2403.41,2402.22,2403.41,5.4,13047.2,13047.2,1\n")
            loaded = lpg.load_csv(path)
        self.assertEqual([c.ts_ms for c in loaded], [1789532220000, 1789532280000])
        self.assertEqual(loaded[1].close, 2403.09)

    def test_parse_cst(self):
        ms = lpg.parse_cst("2026-09-16 00:00")
        self.assertEqual(lpg.fmt_cst(ms), "2026-09-16 00:00:00 CST")
        self.assertEqual(lpg.parse_ts_cell("1789488000"), 1789488000000)


class CliSmokeTest(unittest.TestCase):
    def test_cli_synthetic_default_has_fills(self):
        script = ROOT / "tools" / "local_paper_grid.py"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out.json")
            fills = os.path.join(d, "fills.csv")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--quiet",
                    "--out",
                    out,
                    "--fills-out",
                    fills,
                    "--out-dir",
                    os.path.join(d, "sim"),
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=ROOT,
            )
            self.assertEqual(proc.stdout, "")
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertTrue(os.path.exists(fills))
            self.assertTrue(
                any(
                    n.endswith("-local-paper-eth-grid-001.json")
                    for n in os.listdir(os.path.join(d, "sim"))
                )
            )
        m = doc["metrics"]
        self.assertGreater(m["buy_count"], 0)
        self.assertGreater(m["sell_count"], 0)
        self.assertGreater(m["arbitrage_num"], 0)
        self.assertEqual(doc["venue"], "local_paper")
        self.assertFalse(doc["will_send_http"])


if __name__ == "__main__":
    unittest.main()
