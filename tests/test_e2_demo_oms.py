"""Unit tests for strategies/e2_demo_oms.py — E2 Phase A demo OMS (dry-run only, no network).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies"))

import e2_demo_oms as oms  # noqa: E402

SRC = ROOT / "strategies" / "e2_demo_oms.py"
NO_ENV = {
    oms.ENV_KEY: "",
    oms.ENV_SECRET: "",
    oms.ENV_PASSPHRASE: "",
    oms.ENV_SIMULATED: "",
}
DEMO_ENV = {
    oms.ENV_KEY: "test-key-not-real",
    oms.ENV_SECRET: "test-secret-not-real",
    oms.ENV_PASSPHRASE: "test-pass-not-real",
    oms.ENV_SIMULATED: "1",
}


def cfg(**over) -> oms.GridIntentConfig:
    return oms.GridIntentConfig(**over)


class DefaultsAndPolicy(unittest.TestCase):
    def test_will_send_http_defaults_false_everywhere(self):
        self.assertIs(oms.GridIntentConfig().will_send_http, False)
        self.assertIs(oms.GridIntentConfig().live, False)
        self.assertIs(oms.DemoOms().will_send_http, False)
        self.assertIs(oms.POLICY["will_send_http"], False)
        self.assertIs(oms.POLICY["http_code_present"], False)
        self.assertTrue(all(v is False for v in oms.POLICY["trading_http"].values()))
        self.assertEqual(oms.POLICY["lever"], 1)
        self.assertIs(oms.POLICY["auto_reband"], False)

    def test_policy_forbids_market_ioc_and_keeps_bots_untouched(self):
        for bad in ("market", "ioc", "fok", "optimal_limit_ioc"):
            self.assertIn(bad, oms.POLICY["ord_types_forbidden"])
        self.assertEqual(sorted(oms.POLICY["ord_types_allowed"]), ["limit", "post_only"])
        self.assertEqual(oms.POLICY["official_bots_untouched"], ["A", "B′", "C"])
        self.assertEqual(oms.POLICY["approved_inst_ids"], ["ETH-USDT"])
        self.assertEqual(oms.POLICY["max_levels"], 10)
        self.assertEqual(oms.POLICY["max_total_notional_usdt"], 100.0)


class Refusals(unittest.TestCase):
    def assert_refused(self, code: str, **over):
        with self.assertRaises(oms.RiskPrecheckRefused) as ctx:
            oms.build_intent_plan(cfg(**over), env=NO_ENV)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception

    def test_refuse_live(self):
        self.assert_refused("LiveFlagRefused", live=True)
        # live wins even when combined with a send request: nothing else is evaluated
        self.assert_refused("LiveFlagRefused", live=True, will_send_http=True)

    def test_refuse_send_without_demo_env(self):
        exc = self.assert_refused("DemoEnvCheckFailed", will_send_http=True)
        self.assertIsInstance(exc, oms.SendRefused)
        self.assertNotIn("not-real", str(exc))

    def test_refuse_send_even_with_demo_env(self):
        with self.assertRaises(oms.SendRefused) as ctx:
            oms.build_intent_plan(cfg(will_send_http=True), env=DEMO_ENV)
        self.assertEqual(ctx.exception.code, "PhaseASendNotImplemented")
        self.assertNotIn("not-real", str(ctx.exception))

    def test_refuse_oversize_notional(self):
        self.assert_refused("NotionalOverCap", total_notional_usdt="100.01")
        self.assert_refused("NotionalOverCap", total_notional_usdt="150")
        self.assert_refused("NotionalOverCap", total_notional_usdt="0")
        # exactly at the cap is allowed
        plan = oms.build_intent_plan(cfg(total_notional_usdt="100"), env=NO_ENV)
        self.assertLessEqual(plan["totals"]["notional_usdt"], 100.0)

    def test_refuse_too_many_levels(self):
        self.assert_refused("LevelsOverCap", levels=11)
        self.assert_refused("LevelsOverCap", levels=0)

    def test_refuse_missing_or_misplaced_stop_loss(self):
        self.assert_refused("StopLossMissing", sl_trigger_px=None)
        self.assert_refused("StopLossMissing", sl_trigger_px="")
        self.assert_refused("StopLossPlacement", sl_trigger_px="2300")  # == lower_px
        self.assert_refused("StopLossPlacement", sl_trigger_px="2400")  # inside the ladder
        self.assert_refused("StopLossPlacement", sl_trigger_px="0")

    def test_refuse_non_eth_usdt(self):
        for inst in ("BTC-USDT", "ETH-USDC", "ETH-USDT-SWAP", "eth-usdt"):
            self.assert_refused("InstrumentNotApproved", inst_id=inst)

    def test_refuse_missing_simulated_metadata(self):
        self.assert_refused("SimulatedIntentMetadataMissing", intent_metadata={})
        self.assert_refused(
            "SimulatedIntentMetadataMissing",
            intent_metadata={
                "x-simulated-trading": "0",
                "simulated_trading": True,
                "venue": "okx_demo",
                "demo_only": True,
            },
        )
        self.assert_refused(
            "SimulatedIntentMetadataMissing",
            intent_metadata={
                "x-simulated-trading": "1",
                "simulated_trading": True,
                "venue": "okx_live",
                "demo_only": True,
            },
        )

    def test_refuse_market_ioc_leverage_reband_and_sweep(self):
        for bad in ("market", "ioc", "fok", "optimal_limit_ioc", "weird"):
            self.assert_refused("OrderTypeForbidden", ord_type=bad)
        self.assert_refused("TdModeForbidden", td_mode="cross")
        self.assert_refused("TdModeForbidden", td_mode="isolated")
        self.assert_refused("TdModeForbidden", lever=2)
        self.assert_refused("AutoRebandRefused", auto_reband=True)
        # highest buy level (2480) at / above the reference would cross the book
        self.assert_refused("BuyLevelAboveReference", ref_px="2480")
        self.assert_refused("BuyLevelAboveReference", ref_px="2400")
        self.assert_refused("BadRange", lower_px="2500", upper_px="2300")
        self.assert_refused("BadNumber", ref_px="abc")

    def test_refuse_size_below_min(self):
        # 10 levels × 0.5 USDT at 2300 → 0.000217 ETH < minSz 0.001
        self.assert_refused("SizeBelowMin", total_notional_usdt="5")

    def test_prechecks_order_send_gates_first(self):
        # will_send_http is judged before any sizing / instrument checks
        with self.assertRaises(oms.SendRefused):
            oms.run_prechecks(cfg(will_send_http=True, inst_id="BTC-USDT"), env=NO_ENV)


class DryRunPlan(unittest.TestCase):
    def setUp(self):
        self.plan = oms.build_intent_plan(cfg(), env=NO_ENV)

    def test_shape_and_gates(self):
        p = self.plan
        self.assertEqual(p["schema"], "e2_demo_oms_intent_plan_v1")
        self.assertEqual((p["strategy_id"], p["phase"], p["mode"]), ("E2-G1", "A", "dry_run"))
        self.assertEqual(p["action"], "build_intent_only")
        self.assertIs(p["will_send_http"], False)
        self.assertIs(p["http_sent"], False)
        self.assertEqual(p["orders_placed"], 0)
        self.assertEqual(p["venue"], "okx_demo")
        self.assertEqual(p["intent_metadata"]["x-simulated-trading"], "1")
        self.assertIs(p["intent_metadata"]["demo_only"], True)
        self.assertEqual(p["instrument"]["source"], "static_default_not_fetched")
        self.assertNotIn("generated_at", p)
        for key in (
            "policy",
            "config",
            "grid",
            "orders",
            "totals",
            "stop_loss",
            "prechecks",
            "plan_id",
            "phase_b_requires",
            "risk_notes",
        ):
            self.assertIn(key, p, key)
        self.assertTrue(all(c["ok"] for c in p["prechecks"]))
        self.assertEqual(
            [c["code"] for c in p["prechecks"]],
            [
                "live_flag",
                "will_send_http",
                "simulated_intent_metadata",
                "instrument",
                "td_mode",
                "ord_type",
                "auto_reband",
                "levels",
                "total_notional",
                "range",
                "stop_loss",
            ],
        )

    def test_orders_are_resting_buy_limits_below_reference(self):
        p = self.plan
        orders = p["orders"]
        self.assertEqual(len(orders), 10)
        self.assertEqual(p["totals"]["orders"], 10)
        self.assertLessEqual(p["totals"]["notional_usdt"], 100.0)
        self.assertGreater(p["totals"]["notional_usdt"], 99.0)
        ref = float(p["grid"]["ref_px"])
        sl = float(p["stop_loss"]["slTriggerPx"])
        prev_px = 0.0
        for i, o in enumerate(orders, start=1):
            self.assertEqual(o["level"], i)
            self.assertEqual(o["instId"], "ETH-USDT")
            self.assertEqual(o["tdMode"], "cash")
            self.assertEqual(o["side"], "buy")
            self.assertEqual(o["ordType"], "limit")
            px, sz = float(o["px"]), float(o["sz"])
            self.assertLess(px, ref, "resting buy must sit below the reference")
            self.assertGreater(px, sl, "stop-loss must be below every level")
            self.assertGreater(px, prev_px, "levels ascend")
            prev_px = px
            self.assertGreaterEqual(sz, float(p["instrument"]["minSz"]))
            self.assertAlmostEqual(px * sz, o["notional_usdt"], places=3)
            self.assertLessEqual(o["notional_usdt"], 10.0)
            self.assertGreater(float(o["paired_exit_px"]), px)
            self.assertTrue(o["clOrdId"].startswith("e2g1a") and len(o["clOrdId"]) <= 32)
            self.assertTrue(o["clOrdId"].isalnum())
        self.assertEqual(orders[0]["px"], "2300")
        self.assertEqual(orders[-1]["px"], "2480")
        self.assertEqual(orders[-1]["paired_exit_px"], "2500")
        self.assertEqual(p["grid"]["step_px"], "20")
        self.assertEqual(p["stop_loss"]["basis"], "absolute_px")
        self.assertEqual(len({o["clOrdId"] for o in orders}), 10)

    def test_deterministic(self):
        again = oms.build_intent_plan(cfg(), env=NO_ENV)
        self.assertEqual(again, self.plan)
        self.assertEqual(again["plan_id"], self.plan["plan_id"])
        self.assertEqual(len(self.plan["plan_id"]), 64)
        # a different ladder yields a different plan_id, same shape
        other = oms.build_intent_plan(cfg(levels=5), env=NO_ENV)
        self.assertNotEqual(other["plan_id"], self.plan["plan_id"])
        self.assertEqual(set(other), set(self.plan))
        self.assertEqual(len(other["orders"]), 5)

    def test_timestamp_only_when_asked_and_does_not_change_plan_id(self):
        now = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
        stamped = oms.build_intent_plan(cfg(), env=NO_ENV, now=now)
        self.assertEqual(stamped["generated_at"], "2026-09-19T10:00:00Z")
        self.assertEqual(stamped["plan_id"], self.plan["plan_id"])

    def test_no_forbidden_wording(self):
        text = json.dumps(self.plan, ensure_ascii=False).lower()
        for bad in ("risk_free", "无风险", "稳赚", "guaranteed", "withdraw_ok", "algoid"):
            self.assertNotIn(bad, text, bad)


class OmsNeverSends(unittest.TestCase):
    def test_build_and_submit_never_call_place_order(self):
        client = oms.DemoOms(env=NO_ENV)
        with mock.patch.object(oms.DemoOms, "place_order", autospec=True) as po:
            plan = client.build_plan(cfg())
            result = client.submit(plan)
            po.assert_not_called()
        self.assertIs(result.sent, False)
        self.assertEqual(result.orders_placed, 0)
        self.assertEqual(result.plan_id, plan["plan_id"])
        self.assertEqual(client.submitted, [plan["plan_id"]])
        self.assertEqual(client.place_order_calls, 0)

    def test_place_amend_cancel_are_loud_refusals(self):
        client = oms.DemoOms(env=NO_ENV)
        for name in ("place_order", "amend_order", "cancel_order"):
            with self.assertRaises(oms.SendRefused):
                getattr(client, name)({"instId": "ETH-USDT"})
        self.assertEqual(client.place_order_calls, 1)

    def test_oms_with_send_flag_refuses_before_plan(self):
        client = oms.DemoOms(will_send_http=True, env=DEMO_ENV)
        with self.assertRaises(oms.SendRefused) as ctx:
            client.build_plan(cfg())
        self.assertEqual(ctx.exception.code, "PhaseASendNotImplemented")
        plan = oms.build_intent_plan(cfg(), env=NO_ENV)
        with self.assertRaises(oms.SendRefused):
            client.submit(plan)
        tampered = dict(plan, will_send_http=True)
        with self.assertRaises(oms.SendRefused):
            oms.DemoOms(env=NO_ENV).submit(tampered)

    def test_source_has_no_http_or_sdk_path(self):
        src = SRC.read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("urllib", "http", "socket", "httpx", "requests", "okx", "websockets"):
            self.assertNotIn(banned, imported, banned)
        for banned in ("urlopen", "httpx", "requests.", "okx.Trade", "algoId", "withdraw("):
            self.assertNotIn(banned, src, banned)
        self.assertNotIn("okx_readonly_client", src)
        self.assertNotIn("okx_sdk_readonly", src)

    def test_env_check_never_leaks_values(self):
        check = oms.check_demo_env(DEMO_ENV)
        self.assertTrue(check.ok and check.simulated and check.keys_present)
        self.assertNotIn("not-real", json.dumps(check.to_dict()))
        self.assertFalse(oms.check_demo_env(NO_ENV).ok)
        self.assertFalse(oms.check_demo_env({**DEMO_ENV, oms.ENV_SIMULATED: "0"}).ok)


class Cli(unittest.TestCase):
    def run_cli(self, *argv: str, env_over: dict | None = None) -> tuple[int, dict]:
        env = {**os.environ, **NO_ENV, **(env_over or {})}
        proc = subprocess.run(
            [sys.executable, str(SRC), *argv],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, json.loads(proc.stdout)

    def test_default_run_is_dry_run_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "e2-g1-phase-a.json"
            rc, doc = self.run_cli("plan", "--out", str(out))
            self.assertEqual(rc, 0)
            self.assertIs(doc["will_send_http"], False)
            self.assertIs(doc["submit_result"]["sent"], False)
            self.assertEqual(doc["submit_result"]["orders_placed"], 0)
            on_disk = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["plan_id"], doc["plan_id"])
        rc, bare = self.run_cli()  # no subcommand → plan
        self.assertEqual(rc, 0)
        self.assertEqual(bare["plan_id"], doc["plan_id"])

    def test_refusals_exit_3(self):
        for argv, code in (
            (("plan", "--will-send-http"), "DemoEnvCheckFailed"),
            (("plan", "--live"), "LiveFlagRefused"),
            (("plan", "--total-notional", "500"), "NotionalOverCap"),
            (("plan", "--levels", "12"), "LevelsOverCap"),
            (("plan", "--sl-trigger-px", ""), "StopLossMissing"),
            (("plan", "--inst-id", "BTC-USDT"), "InstrumentNotApproved"),
            (("plan", "--auto-reband"), "AutoRebandRefused"),
        ):
            rc, doc = self.run_cli(*argv)
            self.assertEqual(rc, 3, argv)
            self.assertIs(doc["refused"], True)
            self.assertEqual(doc["code"], code, argv)
            self.assertIs(doc["will_send_http"], False)
            self.assertEqual(doc["orders_placed"], 0)

    def test_send_flag_with_demo_env_still_exit_3_and_no_echo(self):
        rc, doc = self.run_cli("plan", "--will-send-http", env_over=DEMO_ENV)
        self.assertEqual(rc, 3)
        self.assertEqual(doc["code"], "PhaseASendNotImplemented")
        self.assertNotIn("not-real", json.dumps(doc))

    def test_policy_and_check_env(self):
        rc, doc = self.run_cli("policy")
        self.assertEqual(rc, 0)
        self.assertIs(doc["policy"]["will_send_http"], False)
        rc, doc = self.run_cli("check-env", env_over=DEMO_ENV)
        self.assertEqual(rc, 0)
        self.assertIs(doc["demo_env"]["ok"], True)
        self.assertIs(doc["will_send_http"], False)
        self.assertNotIn("not-real", json.dumps(doc))


if __name__ == "__main__":
    unittest.main()
