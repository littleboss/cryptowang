"""Unit tests for strategies/grid_ab_compare.py — synthetic candles only, no network.

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
sys.path.insert(0, str(ROOT / "strategies"))

import grid_ab_compare as gab  # noqa: E402
import local_paper_grid as lpg  # noqa: E402

PROPOSAL = ROOT / "fixtures" / "proposals" / "2026-09-16-local-paper-eth-grid-001-v3.json"
FEES = lpg.FeeModel(maker=0.0008, taker=0.0010, worst_case=True)


def synthetic(seed: int = 20260916, steps: int = 1440, **kw) -> list[lpg.Candle]:
    return lpg.gen_synthetic(2450.0, 200.0, 240, steps, 3.0, seed=seed, **kw)


def write_proposal(d: str, **overrides) -> str:
    with open(PROPOSAL, encoding="utf-8") as f:
        doc = json.load(f)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(doc.get(k), dict):
            doc[k] = {**doc[k], **v}
        else:
            doc[k] = v
    path = os.path.join(d, "p.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return path


class ProposalTest(unittest.TestCase):
    def test_loads_v3_fixture(self):
        prop = gab.load_proposal(PROPOSAL)
        self.assertEqual(prop.version, "v3")
        self.assertEqual(prop.run_id, "local-paper-eth-grid-001")
        self.assertFalse(prop.will_send_http)
        self.assertEqual([a.name for a in prop.arms], ["baseline", "B1"])
        b, e = prop.baseline.params, prop.arm("B1").params
        self.assertEqual((b.min_px, b.max_px, b.grid_num), (2200.0, 3200.0, 30))
        self.assertEqual((e.min_px, e.max_px, e.grid_num), (2200.0, 2700.0, 20))
        # same min / SL / investment / lever between arms (proposal §3.2)
        self.assertEqual(e.min_px, b.min_px)
        self.assertEqual(e.sl_trigger_px, b.sl_trigger_px)
        self.assertEqual(e.investment, b.investment)
        self.assertEqual(e.lever, b.lever, 1)
        self.assertIn("user_confirm_before_adopt", prop.arm("B1").requires)
        self.assertEqual(prop.day1_reference["fee_after_pnl_est"], -28.321237)
        self.assertEqual(prop.hypotheses, ("H-A", "H-B", "H-C"))
        self.assertIn("buy_sell_ratio_gt_3_with_worsening_float", prop.invalidation)

    def test_refuses_will_send_http_true(self):
        with tempfile.TemporaryDirectory() as d:
            # negative test: a proposal flagged for trading HTTP must be REFUSED at load time
            path = write_proposal(d, will_send_http=True)
            with self.assertRaises(ValueError):
                gab.load_proposal(path)

    def test_refuses_lever_above_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_proposal(d, paper_experiment_B1={"lever": 2})
            with self.assertRaises(ValueError):
                gab.load_proposal(path)

    def test_refuses_add_position_arm(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_proposal(d, paper_experiment_B1={"investment": 2000})
            with self.assertRaisesRegex(ValueError, "H-C"):
                gab.load_proposal(path)


class CompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prop = gab.load_proposal(PROPOSAL)
        cls.candles = synthetic()
        cls.doc, cls.results = gab.compare(cls.prop, cls.candles, FEES)

    def test_both_arms_run_on_same_path_with_fills(self):
        self.assertEqual(set(self.results), {"baseline", "B1"})
        for name, res in self.results.items():
            m = res.metrics
            self.assertEqual(m["candles"], len(self.candles), name)
            self.assertGreater(m["arbitrage_num"], 0, name)
            self.assertEqual(res.report["venue"], "local_paper")
            self.assertEqual(res.report["params"]["lever"], 1)
            self.assertFalse(res.report["will_send_http"])
            self.assertEqual(res.report["runId"], f"local-paper-eth-grid-001-{name}")
        cp = self.doc["candle_path"]
        self.assertEqual(cp["candles"], len(self.candles))
        self.assertEqual(self.doc["arms"]["baseline"]["candles"], self.doc["arms"]["B1"]["candles"])

    def test_required_output_fields(self):
        required = {
            "fee_after_pnl_est",
            "okx_bot_total_pnl_ratio",
            "arbitrage_num",
            "max_drawdown_ratio",
            "fee_falsified",
            "invalidation",
        }
        for name, a in self.doc["arms"].items():
            self.assertTrue(required <= set(a), (name, required - set(a)))
            inv = a["invalidation"]
            for key in self.prop.invalidation:
                self.assertIn(key, inv)
            self.assertIn("any", inv)
        self.assertFalse(self.doc["will_send_http"])
        self.assertFalse(self.doc["trading_http"]["amend"])
        self.assertFalse(self.doc["adoption"]["adopted"])
        self.assertFalse(self.doc["adoption"]["bot_changed"])
        self.assertIn("≠ OKX Bot", self.doc["disclaimer"])
        self.assertEqual(set(self.doc["hypotheses"]), {"H-A", "H-B", "H-C"})
        json.dumps(self.doc, ensure_ascii=False)

    def test_deltas_match_arm_metrics(self):
        b, e = self.doc["arms"]["baseline"], self.doc["arms"]["B1"]
        d = self.doc["deltas"]["B1"]
        self.assertAlmostEqual(
            d["fee_after_pnl_est"], e["fee_after_pnl_est"] - b["fee_after_pnl_est"], 5
        )
        self.assertEqual(d["arbitrage_num"], e["arbitrage_num"] - b["arbitrage_num"])
        self.assertAlmostEqual(
            d["max_drawdown_ratio"], e["max_drawdown_ratio"] - b["max_drawdown_ratio"], 7
        )
        # per-grid pct: B1 = 500/20/2450, baseline = 1000/30/2700
        self.assertAlmostEqual(e["per_grid_pct"], 25.0 / 2450.0, 6)
        self.assertAlmostEqual(b["per_grid_pct"], (1000.0 / 30.0) / 2700.0, 6)

    def test_h_b_status_follows_rule(self):
        hb = self.doc["hypotheses"]["H-B"]["B1"]
        ev = hb["evidence"]
        expected_falsified = ev["delta_fee_after_pnl_est"] <= 0 or (
            ev["delta_max_drawdown_ratio"] > gab.HB_MDD_TOLERANCE
        )
        self.assertEqual(hb["status"] == "falsified_on_window", expected_falsified)
        self.assertEqual(bool(hb["falsified_by"]), expected_falsified)

    def test_h_c_policy_enforced_no_veto(self):
        hc = self.doc["hypotheses"]["H-C"]
        self.assertEqual(hc["status"], "policy_enforced")
        self.assertEqual(hc["vetoed_arms"], [])
        self.assertFalse(hc["add_position_proposals_allowed"])
        for a in hc["evidence"]["arms"].values():
            self.assertFalse(a["adds_capital"])
            self.assertFalse(a["adds_lever"])

    def test_markdown_table_renders(self):
        md = gab.render_markdown(self.doc)
        self.assertIn("| baseline | baseline | 2200–3200 / 30 |", md)
        self.assertIn("| B1 | experiment | 2200–2700 / 20 |", md)
        self.assertIn("| H-A |", md)
        self.assertIn("| H-B (B1) |", md)
        self.assertIn("| H-C |", md)
        self.assertIn("adopted: `false`", md)

    def test_subprocess_engine_matches_import(self):
        doc_sub, _ = gab.compare(self.prop, synthetic(steps=300), FEES, engine="subprocess")
        doc_imp, _ = gab.compare(self.prop, synthetic(steps=300), FEES, engine="import")
        for name in ("baseline", "B1"):
            for key in (
                "fee_after_pnl_est",
                "okx_bot_total_pnl_ratio",
                "arbitrage_num",
                "max_drawdown_ratio",
                "untouched_grid_fraction",
            ):
                self.assertEqual(
                    doc_sub["arms"][name][key], doc_imp["arms"][name][key], (name, key)
                )
        self.assertEqual(doc_sub["engine"], "subprocess")


class ScoringRulesTest(unittest.TestCase):
    def _res(self, **metrics) -> gab.ArmResult:
        prop = gab.load_proposal(PROPOSAL)
        base = {
            "fee_after_pnl_est": -30.0,
            "okx_bot_total_pnl": -29.0,
            "okx_bot_total_pnl_ratio": -0.029,
            "fees_paid_est": 1.0,
            "arbitrage_num": 0,
            "buy_count": 7,
            "sell_count": 2,
            "float_profit": -28.0,
            "max_drawdown_ratio": 0.04,
            "candles": 288,
            "fee_falsified": False,
        }
        base.update(metrics)
        report = {
            "runId": "x-baseline",
            "metrics": base,
            "invalidation_flags": {},
            "window": {"from": "2026-09-16 00:00:00 CST", "to": "2026-09-16 23:55:00 CST"},
            "data_source": {"candles": 288},
        }
        return gab.ArmResult(prop.baseline, report, 0.5)

    def test_h_a_falsified_after_seven_bad_eods(self):
        day1 = {"fee_after_pnl_est": -28.321237}
        bad_eod = {
            "metrics": {"fee_after_pnl_est": -30.0, "arbitrage_num": 0, "candles": 288},
            "window": {"from": "2026-09-16 00:00:00 CST", "to": "2026-09-16 23:55:00 CST"},
        }
        res = self._res()
        six = gab.score_h_a(res, day1, [dict(bad_eod) for _ in range(5)])
        self.assertEqual(six["status"], "not_yet_falsified")
        self.assertEqual(six["evidence"]["consecutive_bad_eods"], 6)
        self.assertEqual(six["evidence"]["days_until_falsifiable"], 1)
        seven = gab.score_h_a(res, day1, [dict(bad_eod) for _ in range(6)])
        self.assertEqual(seven["status"], "falsified")
        # a good day in between resets the streak
        good = {**bad_eod, "metrics": {**bad_eod["metrics"], "arbitrage_num": 5}}
        reset = gab.score_h_a(res, day1, [dict(bad_eod) for _ in range(6)] + [good])
        self.assertEqual(reset["evidence"]["consecutive_bad_eods"], 1)
        pos = gab.score_h_a(self._res(fee_after_pnl_est=3.0), day1, [])
        self.assertEqual(pos["status"], "supported_on_window")

    def test_h_b_falsified_when_mdd_up_more_than_2pct(self):
        base = self._res(fee_after_pnl_est=-30.0, max_drawdown_ratio=0.04)
        exp = self._res(fee_after_pnl_est=-20.0, max_drawdown_ratio=0.07)
        hb = gab.score_h_b(base, exp)
        self.assertEqual(hb["status"], "falsified_on_window")
        self.assertIn("mdd_up_gt_0.02", hb["falsified_by"])
        exp2 = self._res(fee_after_pnl_est=-20.0, max_drawdown_ratio=0.05)
        self.assertEqual(gab.score_h_b(base, exp2)["status"], "supported_on_window")
        exp3 = self._res(fee_after_pnl_est=-30.0, max_drawdown_ratio=0.03)
        hb3 = gab.score_h_b(base, exp3)
        self.assertEqual(hb3["status"], "falsified_on_window")
        self.assertIn("fee_after_not_better_than_baseline", hb3["falsified_by"])

    def test_inventory_risk_rule_matches_day1(self):
        # Day-1: buy 7 / sell 2 (ratio 3.5 > 3) with float −28 → inventory risk
        self.assertTrue(
            gab.inventory_risk({"buy_count": 7, "sell_count": 2, "float_profit": -28.0})
        )
        self.assertFalse(gab.inventory_risk({"buy_count": 7, "sell_count": 2, "float_profit": 1.0}))
        self.assertFalse(
            gab.inventory_risk({"buy_count": 6, "sell_count": 2, "float_profit": -1.0})
        )
        self.assertTrue(gab.inventory_risk({"buy_count": 1, "sell_count": 0, "float_profit": -1.0}))
        res = self._res()
        inv = gab.invalidation_matrix(res, gab.load_proposal(PROPOSAL).invalidation)
        self.assertTrue(inv["buy_sell_ratio_gt_3_with_worsening_float"])
        self.assertTrue(inv["any"])
        self.assertFalse(inv["sl_hit_2150"])


class CliSmokeTest(unittest.TestCase):
    def test_cli_writes_json_and_markdown(self):
        script = ROOT / "strategies" / "grid_ab_compare.py"
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "cmp.json")
            md = os.path.join(d, "cmp.md")
            arms = os.path.join(d, "arms")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--quiet",
                    "--out",
                    out,
                    "--md-out",
                    md,
                    "--arm-reports-dir",
                    arms,
                    "--synthetic-steps",
                    "600",
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=ROOT,
            )
            self.assertEqual(proc.stdout, "")
            with open(out, encoding="utf-8") as f:
                doc = json.load(f)
            with open(md, encoding="utf-8") as f:
                table = f.read()
            names = sorted(os.listdir(arms))
        self.assertEqual(set(doc["arms"]), {"baseline", "B1"})
        self.assertFalse(doc["will_send_http"])
        self.assertIn("| B1 | experiment |", table)
        self.assertTrue(any(n.endswith("-local-paper-eth-grid-001-B1.json") for n in names))
        self.assertTrue(any(n.endswith("-local-paper-eth-grid-001-baseline.json") for n in names))


if __name__ == "__main__":
    unittest.main()
