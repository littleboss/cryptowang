"""Unit tests for strategies/e2_demo_oms_v2.py — E2 OMS-v2 (gated OKX-demo sender).

Everything runs offline: the armed path is exercised against a local fake OKX demo server on
127.0.0.1 that verifies the HMAC signature and the forced ``x-simulated-trading: 1`` header,
and against ``unittest.mock`` to prove the disarmed / refused paths never open a socket.

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import json
import os
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

import e2_demo_oms as phase_a  # noqa: E402
import e2_demo_oms_v2 as v2  # noqa: E402
from okx_readonly_client import sign  # noqa: E402

SRC_V2 = ROOT / "strategies" / "e2_demo_oms_v2.py"
SRC_A = ROOT / "strategies" / "e2_demo_oms.py"

NO_ENV = {
    v2.ENV_KEY: "",
    v2.ENV_SECRET: "",
    v2.ENV_PASSPHRASE: "",
    v2.ENV_SIMULATED: "",
    v2.ENV_FLAG: "",
}
DEMO_ENV = {
    v2.ENV_KEY: "test-key-not-real",
    v2.ENV_SECRET: "test-secret-not-real",
    v2.ENV_PASSPHRASE: "test-pass-not-real",
    v2.ENV_SIMULATED: "1",
    v2.ENV_FLAG: "",
}
BAD_HOSTS = (
    "https://aws.okx.com",
    "https://eea.okx.com",
    "https://www.okx.com/api",
    "http://www.okx.com",
    "https://127.0.0.1:9",
    "https://evil.example",
    "https://www.okx.com.evil.example",
)

# Eight buy limits placed *outside* this module (connectivity trial): no OMS-v2 prefix.
FOREIGN_PENDING = [
    {
        "ordId": f"trial-{i:02d}",
        "clOrdId": "" if i % 2 else f"trialbuy{i:02d}",
        "instId": "ETH-USDT",
        "side": "buy",
        "ordType": "limit",
        "px": str(2200 + 10 * i),
        "sz": "0.004",
        "state": "live",
    }
    for i in range(8)
]
FAIL_SUFFIX = "deadbeef"


def cfg(**over) -> phase_a.GridIntentConfig:
    return phase_a.GridIntentConfig(**over)


def own_body(**over) -> dict:
    plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)
    body = v2.order_body_from_intent(plan["plan_id"], plan["orders"][0])
    body.update(over)
    return body


# --------------------------------------------------------------------------- fake demo server


class FakeDemoHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []
    placed: dict[str, dict] = {}
    fail_cl_ord_ids: set[str] = set()

    def log_message(self, *_a):  # silence
        pass

    def _send(self, status: int, doc: dict) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, body: dict | None) -> dict:
        rec = {
            "method": method,
            "path": urlparse(self.path).path,
            "query": {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()},
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        FakeDemoHandler.requests.append(rec)
        return rec

    def _auth_ok(self, method: str, raw_body: str) -> bool:
        h = self.headers
        if h.get("x-simulated-trading") != "1":
            return False
        if h.get("OK-ACCESS-KEY") != DEMO_ENV[v2.ENV_KEY]:
            return False
        if h.get("OK-ACCESS-PASSPHRASE") != DEMO_ENV[v2.ENV_PASSPHRASE]:
            return False
        expected = sign(
            DEMO_ENV[v2.ENV_SECRET], h.get("OK-ACCESS-TIMESTAMP", ""), method, self.path, raw_body
        )
        return h.get("OK-ACCESS-SIGN") == expected

    def do_GET(self):  # noqa: N802
        rec = self._record("GET", None)
        if not self._auth_ok("GET", ""):
            return self._send(401, {"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []})
        if rec["path"] == v2.PATH_PENDING:
            own = [
                {
                    "ordId": row["ordId"],
                    "clOrdId": cid,
                    "instId": "ETH-USDT",
                    "side": "buy",
                    "ordType": "limit",
                    "px": row["px"],
                    "sz": row["sz"],
                    "state": "live",
                }
                for cid, row in FakeDemoHandler.placed.items()
            ]
            return self._send(200, {"code": "0", "msg": "", "data": [*FOREIGN_PENDING, *own]})
        return self._send(404, {"code": "404", "msg": "not found", "data": []})

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        body = json.loads(raw) if raw else {}
        rec = self._record("POST", body)
        if not self._auth_ok("POST", raw):
            return self._send(401, {"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []})
        cid = body.get("clOrdId", "")
        if rec["path"] == v2.PATH_ORDER:
            if cid in FakeDemoHandler.fail_cl_ord_ids:
                return self._send(
                    200,
                    {
                        "code": "1",
                        "msg": "",
                        "data": [
                            {"clOrdId": cid, "ordId": "", "sCode": "51008", "sMsg": "Insufficient"}
                        ],
                    },
                )
            ord_id = f"demo-{len(FakeDemoHandler.placed) + 1:04d}"
            FakeDemoHandler.placed[cid] = {"ordId": ord_id, "px": body["px"], "sz": body["sz"]}
            return self._send(
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [{"clOrdId": cid, "ordId": ord_id, "sCode": "0", "sMsg": ""}],
                },
            )
        if rec["path"] == v2.PATH_CANCEL:
            row = FakeDemoHandler.placed.pop(cid, None)
            if row is None:
                return self._send(
                    200,
                    {
                        "code": "1",
                        "msg": "",
                        "data": [{"clOrdId": cid, "sCode": "51400", "sMsg": "order not exist"}],
                    },
                )
            return self._send(
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [{"clOrdId": cid, "ordId": row["ordId"], "sCode": "0", "sMsg": ""}],
                },
            )
        if rec["path"] == v2.PATH_AMEND:
            row = FakeDemoHandler.placed.get(cid)
            if row is None:
                return self._send(
                    200,
                    {
                        "code": "1",
                        "msg": "",
                        "data": [{"clOrdId": cid, "sCode": "51400", "sMsg": "order not exist"}],
                    },
                )
            row["px"] = body.get("newPx", row["px"])
            row["sz"] = body.get("newSz", row["sz"])
            return self._send(
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [{"clOrdId": cid, "ordId": row["ordId"], "sCode": "0", "sMsg": ""}],
                },
            )
        return self._send(404, {"code": "404", "msg": "not found", "data": []})


class FakeServerMixin:
    server: HTTPServer
    base_url: str

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeDemoHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeDemoHandler.requests.clear()
        FakeDemoHandler.placed.clear()
        FakeDemoHandler.fail_cl_ord_ids.clear()

    def armed(self, env: dict | None = None, **kw) -> v2.DemoOmsV2:
        return v2.DemoOmsV2(
            will_send_http=True, env=DEMO_ENV if env is None else env, base_url=self.base_url, **kw
        )


# --------------------------------------------------------------------------- defaults / policy


class DefaultsAndPolicy(unittest.TestCase):
    def test_defaults_are_dry_run(self):
        self.assertIs(v2.DemoOmsV2().will_send_http, False)
        self.assertIs(v2.POLICY["will_send_http"], False)
        self.assertIs(v2.POLICY["merge_is_arming"], False)
        self.assertEqual(v2.POLICY["mode_default"], "dry_run")
        self.assertTrue(all(val is False for val in v2.POLICY["trading_http"].values()))
        armed = v2.POLICY["trading_http_when_armed_demo"]
        self.assertIs(armed["withdraw"], False)
        self.assertIs(armed["transfer"], False)
        self.assertEqual(v2.POLICY["sides_sendable"], ["buy"])
        self.assertEqual(v2.POLICY["lever"], 1)
        self.assertIs(v2.POLICY["auto_reband"], False)
        self.assertEqual(v2.POLICY["official_bots_untouched"], ["A", "B′", "C"])
        self.assertEqual(v2.POLICY["allowed_base_urls"], ["https://www.okx.com"])
        self.assertEqual(v2.POLICY["forced_headers"], {"x-simulated-trading": "1"})
        for bad in ("market", "ioc", "fok", "optimal_limit_ioc"):
            self.assertIn(bad, v2.POLICY["ord_types_forbidden"])
        self.assertEqual(v2.POLICY["max_total_notional_usdt"], 100.0)
        self.assertEqual(v2.POLICY["max_levels"], 10)

    def test_prefix_is_distinct_from_phase_a_and_sell_intents(self):
        # Phase A intent ids (`e2g1a…`) and G1b sell intents (`e2g1s…`) are foreign to OMS-v2.
        self.assertFalse(phase_a.CL_ORD_ID_PREFIX.startswith(v2.OMS_CL_ORD_ID_PREFIX))
        self.assertFalse(v2.SELL_INTENT_PREFIX.startswith(v2.OMS_CL_ORD_ID_PREFIX))
        self.assertNotEqual(v2.ORDER_TAG, phase_a.ORDER_TAG)
        self.assertTrue(v2.OMS_CL_ORD_ID_PREFIX.isalnum() and v2.ORDER_TAG.isalnum())
        cid = v2.v2_cl_ord_id("p" * 64, {"level": 1, "px": "2300", "sz": "0.004347"})
        self.assertTrue(cid.startswith("e2g1v2") and cid.isalnum() and len(cid) <= 32)

    def test_env_check_accepts_flag_alias_and_never_leaks(self):
        self.assertTrue(v2.check_send_env(DEMO_ENV).ok)
        self.assertTrue(v2.check_send_env({**DEMO_ENV, v2.ENV_SIMULATED: "", v2.ENV_FLAG: "1"}).ok)
        self.assertFalse(v2.check_send_env({**DEMO_ENV, v2.ENV_SIMULATED: "0"}).ok)
        self.assertFalse(v2.check_send_env(NO_ENV).ok)
        self.assertNotIn("not-real", json.dumps(v2.check_send_env(DEMO_ENV).to_dict()))
        self.assertNotIn("not-real", repr(v2.DemoCredentials.from_env(DEMO_ENV)))


# --------------------------------------------------------------------------- disarmed == Phase A


class DisarmedBehavesLikePhaseA(unittest.TestCase):
    def setUp(self):
        # Even with a valid demo env: without the flag nothing is armed.
        self.oms = v2.DemoOmsV2(env=DEMO_ENV)
        self.urlopen = mock.patch("urllib.request.urlopen")
        self.urlopen_mock = self.urlopen.start()
        self.addCleanup(self.urlopen.stop)

    def test_plan_is_identical_to_phase_a(self):
        plan = self.oms.build_plan(cfg())
        self.assertEqual(plan, phase_a.build_intent_plan(cfg(), env=NO_ENV))
        report = self.oms.submit(plan)
        self.assertIs(report.sent, False)
        self.assertIs(report.http_sent, False)
        self.assertIs(report.will_send_http, False)
        self.assertEqual(report.orders_placed, 0)
        self.assertEqual(self.oms.submitted, [plan["plan_id"]])
        self.urlopen_mock.assert_not_called()

    def test_place_amend_cancel_use_phase_a_codes(self):
        for name, code, args in (
            ("place_order", "PlaceOrderRefused", (own_body(),)),
            ("amend_order", "AmendRefused", (own_body()["clOrdId"],)),
            ("cancel_order", "CancelRefused", (own_body()["clOrdId"],)),
            ("cancel_tagged", "CancelRefused", ()),
        ):
            with self.assertRaises(phase_a.SendRefused) as ctx:
                getattr(self.oms, name)(*args)
            self.assertEqual(ctx.exception.code, code, name)
        self.assertEqual(self.oms.place_order_calls, 1)
        self.urlopen_mock.assert_not_called()

    def test_config_or_plan_flag_cannot_arm(self):
        with self.assertRaises(phase_a.SendRefused) as ctx:
            self.oms.build_plan(cfg(will_send_http=True))
        self.assertEqual(ctx.exception.code, "SendNotArmed")
        plan = self.oms.build_plan(cfg())
        with self.assertRaises(phase_a.SendRefused) as ctx:
            self.oms.submit(dict(plan, will_send_http=True))
        self.assertEqual(ctx.exception.code, "SendNotArmed")
        self.urlopen_mock.assert_not_called()

    def test_sender_never_constructed_or_called_when_disarmed(self):
        with mock.patch.object(v2.DemoHttpSender, "_request", autospec=True) as req:
            plan = self.oms.build_plan(cfg())
            self.oms.submit(plan)
            for name in ("place_order", "amend_order", "cancel_order"):
                with self.assertRaises(phase_a.SendRefused):
                    getattr(self.oms, name)(own_body()["clOrdId"])
            req.assert_not_called()
        self.assertIsNone(self.oms._sender)


# --------------------------------------------------------------------------- armed gate


class ArmedGateRefusals(unittest.TestCase):
    def setUp(self):
        self.urlopen = mock.patch("urllib.request.urlopen")
        self.urlopen_mock = self.urlopen.start()
        self.addCleanup(self.urlopen.stop)

    def assert_refused(self, oms: v2.DemoOmsV2, code: str, op, *args):
        with self.assertRaises(phase_a.RiskPrecheckRefused) as ctx:
            op(oms, *args)
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn("not-real", str(ctx.exception))
        self.urlopen_mock.assert_not_called()
        return ctx.exception

    def test_true_plus_bad_env_refuses_everywhere(self):
        for env in (
            NO_ENV,
            {**DEMO_ENV, v2.ENV_SIMULATED: "0"},
            {**DEMO_ENV, v2.ENV_SIMULATED: "", v2.ENV_FLAG: "0"},
            {**DEMO_ENV, v2.ENV_SECRET: ""},
            {**DEMO_ENV, v2.ENV_PASSPHRASE: ""},
        ):
            oms = v2.DemoOmsV2(will_send_http=True, env=env)
            exc = self.assert_refused(oms, "DemoEnvCheckFailed", lambda o: o.build_plan(cfg()))
            self.assertIsInstance(exc, phase_a.SendRefused)
            plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)
            self.assert_refused(oms, "DemoEnvCheckFailed", lambda o: o.submit(plan))
            self.assert_refused(oms, "DemoEnvCheckFailed", lambda o: o.place_order(own_body()))
            cid = own_body()["clOrdId"]
            self.assert_refused(oms, "DemoEnvCheckFailed", lambda o: o.cancel_order(cid))
            self.assert_refused(
                oms, "DemoEnvCheckFailed", lambda o: o.amend_order(cid, new_px="2310")
            )
            self.assert_refused(oms, "DemoEnvCheckFailed", lambda o: o.cancel_tagged())
            self.assertIsNone(oms._sender)

    def test_flag_alias_arms_the_env_gate(self):
        oms = v2.DemoOmsV2(
            will_send_http=True, env={**DEMO_ENV, v2.ENV_SIMULATED: "", v2.ENV_FLAG: "1"}
        )
        self.assertTrue(oms.arm_check().ok)
        # gate ok ≠ plan prechecks skipped
        self.assert_refused(
            oms, "NotionalOverCap", lambda o: o.build_plan(cfg(total_notional_usdt="500"))
        )

    def test_true_plus_live_host_refuses(self):
        plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)
        for host in BAD_HOSTS:
            oms = v2.DemoOmsV2(will_send_http=True, env=DEMO_ENV, base_url=host)
            self.assert_refused(oms, "LiveHostRefused", lambda o: o.build_plan(cfg()))
            self.assert_refused(oms, "LiveHostRefused", lambda o: o.submit(plan))
            self.assert_refused(oms, "LiveHostRefused", lambda o: o.cancel_tagged())
            self.assertIsNone(oms._sender)
        # the demo host itself passes the *host* gate (nothing is sent in this test)
        self.assertEqual(v2.assert_demo_base_url("https://www.okx.com"), "https://www.okx.com")
        self.assertEqual(v2.assert_demo_base_url("https://WWW.okx.com/"), "https://www.okx.com")
        self.assertEqual(v2.assert_demo_base_url("http://localhost:1"), "http://localhost:1")

    def test_live_flag_wins_before_env(self):
        oms = v2.DemoOmsV2(will_send_http=True, env=NO_ENV)
        self.assert_refused(oms, "LiveFlagRefused", lambda o: o.build_plan(cfg(live=True)))

    def test_sender_requires_simulated_creds_and_demo_host(self):
        creds = v2.DemoCredentials("k", "s", "p", simulated=False)
        with self.assertRaises(phase_a.SendRefused) as ctx:
            v2.DemoHttpSender(creds)
        self.assertEqual(ctx.exception.code, "DemoEnvCheckFailed")
        good = v2.DemoCredentials.from_env(DEMO_ENV)
        for host in BAD_HOSTS:
            with self.assertRaises(phase_a.SendRefused) as ctx:
                v2.DemoHttpSender(good, base_url=host)
            self.assertEqual(ctx.exception.code, "LiveHostRefused")
        self.urlopen_mock.assert_not_called()


# --------------------------------------------------------------------------- endpoint / body gates


class EndpointAndBodyGates(unittest.TestCase):
    def test_endpoint_allow_list(self):
        for m, p in v2.ALLOWED_ENDPOINTS:
            v2.assert_endpoint_allowed(m, p)
        for method, path in (
            ("POST", "/api/v5/trade/batch-orders"),
            ("POST", "/api/v5/trade/cancel-batch-orders"),
            ("POST", "/api/v5/trade/order-algo"),
            ("POST", "/api/v5/trade/cancel-algos"),
            ("POST", "/api/v5/trade/close-position"),
            ("POST", "/api/v5/tradingBot/grid/order-algo"),
            ("POST", "/api/v5/tradingBot/grid/stop-order-algo"),
            ("POST", "/api/v5/tradingBot/grid/amend-order-algo"),
            ("GET", "/api/v5/tradingBot/grid/orders-algo-pending"),
            ("POST", "/api/v5/asset/withdrawal"),
            ("POST", "/api/v5/asset/transfer"),
            ("GET", "/api/v5/asset/balances"),
            ("POST", "/api/v5/account/set-leverage"),
            ("POST", "/api/v5/users/subaccount/transfer"),
            ("GET", "/api/v5/trade/cancel-order"),
            ("DELETE", v2.PATH_ORDER),
            ("PUT", v2.PATH_ORDER),
            ("POST", v2.PATH_ORDER + "?x=1"),
            ("POST", "/api/v5/market/ticker"),
        ):
            with self.assertRaises(phase_a.SendRefused, msg=(method, path)) as ctx:
                v2.assert_endpoint_allowed(method, path)
            self.assertEqual(ctx.exception.code, "EndpointForbidden")

    def test_order_body_prechecks(self):
        base = own_body()
        self.assertGreater(v2.assert_order_body_allowed(base, ref_px="2500"), 0)
        cases = (
            ("OrderTypeForbidden", {"ordType": "market"}),
            ("OrderTypeForbidden", {"ordType": "ioc"}),
            ("OrderTypeForbidden", {"ordType": "fok"}),
            ("OrderTypeForbidden", {"ordType": "optimal_limit_ioc"}),
            ("SellSendNotArmed", {"side": "sell"}),
            ("SideForbidden", {"side": "short"}),
            ("InstrumentNotApproved", {"instId": "BTC-USDT"}),
            ("InstrumentNotApproved", {"instId": "ETH-USDT-SWAP"}),
            ("TdModeForbidden", {"tdMode": "cross"}),
            ("TdModeForbidden", {"tdMode": "isolated"}),
            ("BuyLevelAboveReference", {"px": "2500"}),
            ("BuyLevelAboveReference", {"px": "2600"}),
            ("ForeignOrderRefused", {"clOrdId": "e2g1a0123456789abcdef"}),
            ("ForeignOrderRefused", {"clOrdId": "trialbuy01"}),
            ("ForeignOrderRefused", {"clOrdId": ""}),
            ("OrderTagMismatch", {"tag": phase_a.ORDER_TAG}),
            ("UnknownOrderField", {"attachAlgoOrds": [{"slTriggerPx": "1"}]}),
            ("UnknownOrderField", {"reduceOnly": True}),
            ("NotionalOverCap", {"sz": "1"}),
            ("SizeBelowMin", {"sz": "0.0001"}),
            ("BadNumber", {"px": "-1"}),
            ("BadNumber", {"sz": "abc"}),
        )
        for code, over in cases:
            with self.assertRaises(phase_a.RiskPrecheckRefused, msg=over) as ctx:
                v2.assert_order_body_allowed({**base, **over}, ref_px="2500")
            self.assertEqual(ctx.exception.code, code, over)
        with self.assertRaises(phase_a.SendRefused) as ctx:
            v2.assert_order_body_allowed({k: val for k, val in base.items() if k != "tag"})
        self.assertEqual(ctx.exception.code, "OrderFieldMissing")

    def test_amend_prechecks(self):
        cid = own_body()["clOrdId"]
        body = v2.assert_amend_allowed(cid, new_px="2310.5", new_sz=None, ref_px="2500")
        self.assertEqual(body, {"instId": "ETH-USDT", "clOrdId": cid, "newPx": "2310.5"})
        body = v2.assert_amend_allowed(cid, new_px=None, new_sz="0.005")
        self.assertEqual(body["newSz"], "0.005")
        for code, kw in (
            ("ForeignOrderRefused", {"cl_ord_id": "trialbuy01", "new_px": "2310"}),
            ("ForeignOrderRefused", {"cl_ord_id": "", "new_px": "2310"}),
            ("AmendNoop", {"cl_ord_id": cid}),
            ("BuyLevelAboveReference", {"cl_ord_id": cid, "new_px": "2500", "ref_px": "2500"}),
            ("SizeBelowMin", {"cl_ord_id": cid, "new_sz": "0.0001"}),
            ("NotionalOverCap", {"cl_ord_id": cid, "new_px": "2400", "new_sz": "1"}),
            ("BadNumber", {"cl_ord_id": cid, "new_px": "0"}),
        ):
            kw = {"new_px": None, "new_sz": None, **kw}
            with self.assertRaises(phase_a.RiskPrecheckRefused, msg=kw) as ctx:
                v2.assert_amend_allowed(kw.pop("cl_ord_id"), **kw)
            self.assertEqual(ctx.exception.code, code, kw)

    def test_verify_plan_rejects_tampering(self):
        plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)
        v2.verify_plan(plan)
        tampered = json.loads(json.dumps(plan))
        tampered["orders"][0]["sz"] = "5"
        with self.assertRaises(phase_a.SendRefused) as ctx:
            v2.verify_plan(tampered)
        self.assertEqual(ctx.exception.code, "PlanRejected")
        for over in ({"schema": "other"}, {"strategy_id": "T1"}, {"will_send_http": True}):
            with self.assertRaises(phase_a.SendRefused):
                v2.verify_plan({**plan, **over})


# --------------------------------------------------------------------------- armed on fake demo


class ArmedAgainstFakeDemo(FakeServerMixin, unittest.TestCase):
    def test_submit_places_ladder_with_forced_header_and_valid_signature(self):
        oms = self.armed()
        plan = oms.build_plan(cfg())
        self.assertEqual(plan, phase_a.build_intent_plan(cfg(), env=NO_ENV), "intent unchanged")
        report = oms.submit(plan)
        self.assertIs(report.sent, True)
        self.assertIs(report.http_sent, True)
        self.assertIs(report.will_send_http, True)
        self.assertEqual((report.orders_placed, report.orders_failed), (10, 0))
        self.assertEqual(report.requests_made, 10)
        self.assertEqual(report.base_url, self.base_url)
        self.assertEqual(len(FakeDemoHandler.requests), 10)
        seen_ids = set()
        for req, order in zip(FakeDemoHandler.requests, plan["orders"], strict=True):
            self.assertEqual((req["method"], req["path"]), ("POST", v2.PATH_ORDER))
            self.assertEqual(req["headers"]["x-simulated-trading"], "1")
            self.assertIn("ok-access-sign", req["headers"])  # server verified it (else 401)
            body = req["body"]
            self.assertEqual(set(body), set(v2.ALLOWED_ORDER_FIELDS))
            self.assertEqual(body["instId"], "ETH-USDT")
            self.assertEqual(body["tdMode"], "cash")
            self.assertEqual(body["side"], "buy")
            self.assertEqual(body["ordType"], "limit")
            self.assertEqual((body["px"], body["sz"]), (order["px"], order["sz"]))
            self.assertTrue(body["clOrdId"].startswith(v2.OMS_CL_ORD_ID_PREFIX))
            self.assertNotEqual(body["clOrdId"], order["clOrdId"], "v2 ids ≠ Phase A intent ids")
            self.assertEqual(body["tag"], v2.ORDER_TAG)
            self.assertLess(float(body["px"]), float(plan["grid"]["ref_px"]))
            seen_ids.add(body["clOrdId"])
        self.assertEqual(len(seen_ids), 10)
        self.assertEqual(oms.placed_cl_ord_ids, [r["clOrdId"] for r in report.results])
        self.assertLessEqual(oms.placed_notional_usdt, v2.MAX_TOTAL_NOTIONAL_USDT)
        self.assertTrue(all(r["ok"] and r["ordId"].startswith("demo-") for r in report.results))
        # instance caps: same plan again → duplicate; another plan → over the per-instance cap
        with self.assertRaises(phase_a.SendRefused) as ctx:
            oms.submit(plan)
        self.assertEqual(ctx.exception.code, "DuplicateSubmit")
        other = oms.build_plan(cfg(levels=1, total_notional_usdt="10"))
        with self.assertRaises(phase_a.RiskPrecheckRefused) as ctx:
            oms.submit(other)
        self.assertEqual(ctx.exception.code, "LevelsOverCap")
        self.assertEqual(len(FakeDemoHandler.requests), 10, "refusals send nothing")
        # audit trail never contains headers or secrets
        audit = json.dumps(oms._sender.audit)
        self.assertNotIn("not-real", audit)
        self.assertNotIn("OK-ACCESS", audit)

    def test_cancel_tagged_skips_the_eight_foreign_orders(self):
        oms = self.armed()
        report = oms.submit(oms.build_plan(cfg(levels=3, total_notional_usdt="30")))
        self.assertEqual(report.orders_placed, 3)
        FakeDemoHandler.requests.clear()
        out = oms.cancel_tagged()
        self.assertEqual(out["pending_seen"], 8 + 3)
        self.assertEqual(out["own_seen"], 3)
        self.assertEqual(out["foreign_skipped"], 8)
        self.assertEqual(sorted(out["cancelled"]), sorted(oms.placed_cl_ord_ids))
        self.assertEqual(out["failed"], [])
        self.assertTrue(all(f["skipped"] for f in out["foreign"]))
        gets = [r for r in FakeDemoHandler.requests if r["method"] == "GET"]
        posts = [r for r in FakeDemoHandler.requests if r["method"] == "POST"]
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["path"], v2.PATH_PENDING)
        self.assertEqual(gets[0]["query"], {"instType": "SPOT", "instId": "ETH-USDT"})
        self.assertEqual(len(posts), 3)
        for r in posts:
            self.assertEqual(r["path"], v2.PATH_CANCEL)
            self.assertEqual(r["headers"]["x-simulated-trading"], "1")
            self.assertEqual(set(r["body"]), {"instId", "clOrdId"})
            self.assertTrue(r["body"]["clOrdId"].startswith(v2.OMS_CL_ORD_ID_PREFIX))
            self.assertNotIn("ordId", r["body"])
        touched = json.dumps([r["body"] for r in posts])
        for foreign in FOREIGN_PENDING:
            self.assertNotIn(foreign["ordId"], touched)
            if foreign["clOrdId"]:
                self.assertNotIn(foreign["clOrdId"], touched)
        # the fake server still holds all 8 foreign orders; our 3 are gone
        self.assertEqual(FakeDemoHandler.placed, {})
        again = oms.cancel_tagged()
        self.assertEqual(
            (again["own_seen"], again["foreign_skipped"], again["cancelled"]), (0, 8, [])
        )

    def test_amend_and_single_cancel_only_own_ids(self):
        oms = self.armed()
        report = oms.submit(oms.build_plan(cfg(levels=2, total_notional_usdt="20")))
        cid = report.results[0]["clOrdId"]
        FakeDemoHandler.requests.clear()
        row = oms.amend_order(cid, new_px="2310", ref_px="2500")
        self.assertEqual(row["clOrdId"], cid)
        self.assertEqual(FakeDemoHandler.placed[cid]["px"], "2310")
        req = FakeDemoHandler.requests[-1]
        self.assertEqual((req["method"], req["path"]), ("POST", v2.PATH_AMEND))
        self.assertEqual(req["body"], {"instId": "ETH-USDT", "clOrdId": cid, "newPx": "2310"})
        self.assertEqual(req["headers"]["x-simulated-trading"], "1")
        row = oms.replace_order(cid, new_sz="0.005")
        self.assertEqual(FakeDemoHandler.placed[cid]["sz"], "0.005")
        row = oms.cancel_order(cid)
        self.assertEqual(row["clOrdId"], cid)
        self.assertNotIn(cid, FakeDemoHandler.placed)
        n = len(FakeDemoHandler.requests)
        for foreign in ("trialbuy01", "e2g1a0123456789abcdef", "", "demo-0001"):
            for op in (
                lambda: oms.cancel_order(foreign),
                lambda: oms.amend_order(foreign, new_px="2310"),
            ):
                with self.assertRaises(phase_a.SendRefused) as ctx:
                    op()
                self.assertEqual(ctx.exception.code, "ForeignOrderRefused")
        self.assertEqual(len(FakeDemoHandler.requests), n, "foreign ids never reach the wire")

    def test_sell_market_and_forbidden_endpoints_never_reach_the_wire(self):
        oms = self.armed()
        oms.arm_check()
        for code, over in (
            ("SellSendNotArmed", {"side": "sell"}),
            ("OrderTypeForbidden", {"ordType": "market"}),
            ("OrderTypeForbidden", {"ordType": "ioc"}),
            ("InstrumentNotApproved", {"instId": "BTC-USDT"}),
            ("UnknownOrderField", {"attachAlgoOrds": []}),
        ):
            with self.assertRaises(phase_a.RiskPrecheckRefused) as ctx:
                oms.place_order(own_body(**over), ref_px="2500")
            self.assertEqual(ctx.exception.code, code)
        sender = oms._sender_or_refuse("PlaceOrderRefused", "test")
        for method, path in (
            ("POST", "/api/v5/asset/withdrawal"),
            ("POST", "/api/v5/asset/transfer"),
            ("POST", "/api/v5/tradingBot/grid/stop-order-algo"),
            ("POST", "/api/v5/trade/batch-orders"),
        ):
            with self.assertRaises(phase_a.SendRefused):
                sender._request(method, path, body={})
        self.assertEqual(FakeDemoHandler.requests, [])
        self.assertEqual(sender.requests_made, 0)

    def test_header_cannot_be_dropped(self):
        oms = self.armed()
        sender = oms._sender_or_refuse("PlaceOrderRefused", "test")
        real = sender._headers

        def without_header(*a, **k):
            h = real(*a, **k)
            h.pop(v2.SIMULATED_HEADER)
            return h

        with mock.patch.object(sender, "_headers", side_effect=without_header):
            with self.assertRaises(phase_a.SendRefused) as ctx:
                sender.place_limit_order(own_body(), ref_px="2500")
        self.assertEqual(ctx.exception.code, "SimulatedHeaderMissing")
        self.assertEqual(FakeDemoHandler.requests, [])

    def test_okx_error_stops_the_ladder_and_leaks_nothing(self):
        oms = self.armed()
        plan = oms.build_plan(cfg(levels=4, total_notional_usdt="40"))
        bodies = [v2.order_body_from_intent(plan["plan_id"], o) for o in plan["orders"]]
        FakeDemoHandler.fail_cl_ord_ids.add(bodies[2]["clOrdId"])
        report = oms.submit(plan)
        self.assertEqual((report.orders_placed, report.orders_failed), (2, 1))
        self.assertIs(report.sent, True)
        self.assertEqual(report.results[2]["sCode"], "51008")
        self.assertIn("partial", report.reason)
        self.assertEqual(len(FakeDemoHandler.requests), 3, "stops at first OKX error")
        with self.assertRaises(v2.DemoApiError) as ctx:
            oms.cancel_order(bodies[3]["clOrdId"])  # never placed → OKX 51400
        self.assertEqual(ctx.exception.code, "51400")
        self.assertNotIn("not-real", str(ctx.exception))

    def test_wrong_secret_is_rejected_by_server_without_echo(self):
        oms = self.armed(env={**DEMO_ENV, v2.ENV_SECRET: "wrong-secret-not-real"})
        report = oms.submit(oms.build_plan(cfg(levels=2, total_notional_usdt="20")))
        self.assertIs(report.sent, False)
        self.assertIs(report.http_sent, True)
        self.assertEqual((report.orders_placed, report.orders_failed), (0, 1))
        self.assertEqual(report.results[0]["sCode"], "50111")
        self.assertEqual(len(FakeDemoHandler.requests), 1, "stops at the first rejection")
        self.assertNotIn("not-real", json.dumps(report.to_dict()))
        with self.assertRaises(v2.DemoApiError) as ctx:
            oms.cancel_order(own_body()["clOrdId"])
        self.assertEqual(ctx.exception.code, "50111")
        self.assertNotIn("not-real", str(ctx.exception))


# --------------------------------------------------------------------------- G1b sell intents


class SellIntentsStub(unittest.TestCase):
    def setUp(self):
        self.plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)

    def test_paired_exits_above_buys_never_sendable(self):
        doc = v2.build_sell_intents(self.plan, [2, 1, 5])
        self.assertEqual(doc["schema"], "e2_demo_oms_v2_sell_intents_v1")
        self.assertIs(doc["will_send_http"], False)
        self.assertIs(doc["http_sent"], False)
        self.assertIs(doc["sendable"], False)
        self.assertEqual(doc["orders_placed"], 0)
        self.assertEqual(doc["filled_levels"], [1, 2, 5])
        self.assertEqual(len(doc["intents"]), 3)
        by_level = {o["level"]: o for o in self.plan["orders"]}
        for it in doc["intents"]:
            buy = by_level[it["level"]]
            self.assertEqual(it["side"], "sell")
            self.assertEqual(it["ordType"], "limit")
            self.assertEqual(it["tdMode"], "cash")
            self.assertEqual(it["px"], buy["paired_exit_px"])
            self.assertEqual(it["sz"], buy["sz"])
            self.assertGreater(float(it["px"]), float(buy["px"]))
            self.assertTrue(it["clOrdId"].startswith("e2g1s") and it["clOrdId"].isalnum())
            self.assertEqual(it["paired_buy_clOrdId"], v2.v2_cl_ord_id(self.plan["plan_id"], buy))
            self.assertIs(it["sendable"], False)
            self.assertGreater(it["gross_step_usdt"], 0)
        self.assertEqual(doc, v2.build_sell_intents(self.plan, [5, 2, 1]), "deterministic")
        for bad in ([], [0], [11], [1, 1]):
            with self.assertRaises(phase_a.RiskPrecheckRefused):
                v2.build_sell_intents(self.plan, bad)

    def test_armed_oms_refuses_to_send_a_sell_intent(self):
        doc = v2.build_sell_intents(self.plan, [1])
        it = doc["intents"][0]
        body = {k: it[k] for k in v2.ALLOWED_ORDER_FIELDS if k in it}
        body["tag"] = v2.ORDER_TAG
        body["clOrdId"] = own_body()["clOrdId"]  # even with an own id: side=sell is refused
        with mock.patch("urllib.request.urlopen") as urlopen:
            oms = v2.DemoOmsV2(will_send_http=True, env=DEMO_ENV, base_url="http://127.0.0.1:9")
            with self.assertRaises(phase_a.SendRefused) as ctx:
                oms.place_order(body)
            self.assertEqual(ctx.exception.code, "SellSendNotArmed")
            with self.assertRaises(phase_a.SendRefused) as ctx:
                oms.place_order({**body, "clOrdId": it["clOrdId"]})
            self.assertEqual(ctx.exception.code, "SellSendNotArmed")
            urlopen.assert_not_called()


# --------------------------------------------------------------------------- source audit


class SourceAudit(unittest.TestCase):
    @staticmethod
    def imports(src: str) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                out.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                out.add(node.module.split(".")[0])
        return out

    def test_phase_a_module_is_still_http_free(self):
        src = SRC_A.read_text(encoding="utf-8")
        for banned in ("urllib", "http", "socket", "httpx", "requests", "okx", "websockets"):
            self.assertNotIn(banned, self.imports(src), banned)
        self.assertNotIn("urlopen", src)
        self.assertNotIn("e2_demo_oms_v2", self.imports(src), "Phase A must not import OMS-v2")

    def test_v2_uses_only_urllib_inside_the_sender(self):
        src = SRC_V2.read_text(encoding="utf-8")
        mods = self.imports(src)
        self.assertIn("urllib", mods)
        for banned in ("httpx", "requests", "okx", "websockets", "socket", "http"):
            self.assertNotIn(banned, mods, banned)
        self.assertNotIn("okx_sdk_readonly", src)
        # every urlopen call sits inside DemoHttpSender._request — exactly one call site
        tree = ast.parse(src)
        sites = []
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
                for node in ast.walk(fn):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "urlopen"
                    ):
                        sites.append((cls.name, fn.name))
        self.assertEqual(sites, [("DemoHttpSender", "_request")])
        self.assertEqual(src.count("urlopen("), 1)
        for banned in ("algoId", "/tradingBot/", "withdrawal", "okx.Trade", "batch-orders"):
            self.assertNotIn(banned, src, banned)

    def test_forbidden_wording_absent_from_outputs(self):
        plan = phase_a.build_intent_plan(cfg(), env=NO_ENV)
        docs = [
            v2.POLICY,
            v2.RISK_NOTES,
            v2.build_sell_intents(plan, [1]),
            v2.DemoOmsV2(env=NO_ENV).submit(plan).to_dict(),
        ]
        text = json.dumps(docs, ensure_ascii=False).lower()
        for bad in ("risk_free", "无风险", "稳赚", "guaranteed", "algoid"):
            self.assertNotIn(bad, text, bad)


# --------------------------------------------------------------------------- cli


class Cli(FakeServerMixin, unittest.TestCase):
    def run_cli(self, *argv: str, env_over: dict | None = None) -> tuple[int, dict, str]:
        env = {**os.environ, **NO_ENV, **(env_over or {})}
        proc = subprocess.run(
            [sys.executable, str(SRC_V2), *argv],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, json.loads(proc.stdout), proc.stdout

    def test_default_plan_is_dry_run_and_matches_phase_a(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "run.json"
            rc, doc, text = self.run_cli("plan", "--out", str(out), env_over=DEMO_ENV)
            self.assertEqual(rc, 0)
            self.assertEqual(doc["schema"], "e2_demo_oms_v2_run_v1")
            self.assertEqual(doc["mode"], "dry_run")
            self.assertIs(doc["will_send_http"], False)
            self.assertIs(doc["http_sent"], False)
            self.assertEqual(doc["orders_placed"], 0)
            self.assertIs(doc["execution"]["sent"], False)
            self.assertIs(doc["policy"]["will_send_http"], False)
            self.assertEqual(doc["plan"], phase_a.build_intent_plan(cfg(), env=NO_ENV))
            self.assertNotIn("not-real", text)
            self.assertEqual(json.loads(out.read_text())["plan"]["plan_id"], doc["plan"]["plan_id"])
        rc, bare, _ = self.run_cli()  # no subcommand → plan
        self.assertEqual(rc, 0)
        self.assertEqual(bare["plan"]["plan_id"], doc["plan"]["plan_id"])

    def test_send_flag_without_demo_env_exits_3(self):
        for env_over, code in (
            (None, "DemoEnvCheckFailed"),
            ({**DEMO_ENV, v2.ENV_SIMULATED: "0"}, "DemoEnvCheckFailed"),
            ({**DEMO_ENV, v2.ENV_KEY: ""}, "DemoEnvCheckFailed"),
        ):
            rc, doc, text = self.run_cli("plan", "--will-send-http", env_over=env_over)
            self.assertEqual(rc, 3)
            self.assertIs(doc["refused"], True)
            self.assertEqual(doc["code"], code)
            self.assertIs(doc["http_sent"], False)
            self.assertEqual(doc["orders_placed"], 0)
            self.assertNotIn("not-real", text)

    def test_send_flag_with_demo_env_but_live_host_exits_3(self):
        for host in ("https://aws.okx.com", "https://evil.example", "http://www.okx.com"):
            rc, doc, text = self.run_cli(
                "plan", "--will-send-http", "--base-url", host, env_over=DEMO_ENV
            )
            self.assertEqual(rc, 3, host)
            self.assertEqual(doc["code"], "LiveHostRefused")
            self.assertIs(doc["http_sent"], False)
            self.assertNotIn("not-real", text)
        for argv in (("--live",), ("--total-notional", "500"), ("--levels", "11")):
            rc, doc, _ = self.run_cli(
                "plan", "--will-send-http", "--base-url", self.base_url, *argv, env_over=DEMO_ENV
            )
            self.assertEqual(rc, 3, argv)
            self.assertIs(doc["http_sent"], False)
        self.assertEqual(FakeDemoHandler.requests, [])

    def test_full_loop_on_fake_demo(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "run.json"
            rc, doc, text = self.run_cli(
                "plan",
                "--will-send-http",
                "--base-url",
                self.base_url,
                "--levels",
                "3",
                "--total-notional",
                "30",
                "--out",
                str(out),
                env_over=DEMO_ENV,
            )
            self.assertEqual(rc, 0, text)
            self.assertEqual(doc["mode"], "demo_send")
            self.assertIs(doc["will_send_http"], True)
            self.assertIs(doc["http_sent"], True)
            self.assertEqual(doc["orders_placed"], 3)
            self.assertEqual(doc["execution"]["requests_made"], 3)
            self.assertNotIn("not-real", text)
            self.assertNotIn("OK-ACCESS", text)
            ids = [r["clOrdId"] for r in doc["execution"]["results"]]
            self.assertTrue(all(i.startswith("e2g1v2") for i in ids))
            self.assertEqual(set(FakeDemoHandler.placed), set(ids))
            # G1b sell intents from the artifact: offline, never sent
            rc, sells, _ = self.run_cli(
                "sell-intents", "--plan-json", str(out), "--filled-levels", "1,2"
            )
            self.assertEqual(rc, 0)
            self.assertIs(sells["will_send_http"], False)
            self.assertIs(sells["sendable"], False)
            self.assertEqual(len(sells["intents"]), 2)
        # amend one, cancel-tagged the rest; the 8 foreign orders stay
        rc, am, _ = self.run_cli(
            "amend",
            "--cl-ord-id",
            ids[0],
            "--new-px",
            "2305",
            "--ref-px",
            "2500",
            "--will-send-http",
            "--base-url",
            self.base_url,
            env_over=DEMO_ENV,
        )
        self.assertEqual(rc, 0)
        self.assertIs(am["http_sent"], True)
        self.assertEqual(FakeDemoHandler.placed[ids[0]]["px"], "2305")
        rc, ct, _ = self.run_cli(
            "cancel-tagged", "--will-send-http", "--base-url", self.base_url, env_over=DEMO_ENV
        )
        self.assertEqual(rc, 0)
        self.assertEqual(ct["foreign_skipped"], 8)
        self.assertEqual(sorted(ct["cancelled"]), sorted(ids))
        self.assertEqual(FakeDemoHandler.placed, {})

    def test_cancel_and_amend_dry_run_and_foreign_refusal(self):
        cid = own_body()["clOrdId"]
        rc, doc, _ = self.run_cli("cancel", "--cl-ord-id", cid, env_over=DEMO_ENV)
        self.assertEqual(rc, 0)
        self.assertEqual(doc["mode"], "dry_run")
        self.assertIs(doc["http_sent"], False)
        self.assertEqual(doc["body"], {"instId": "ETH-USDT", "clOrdId": cid})
        rc, doc, _ = self.run_cli("amend", "--cl-ord-id", cid, "--new-px", "2310")
        self.assertEqual(rc, 0)
        self.assertIs(doc["http_sent"], False)
        self.assertEqual(doc["body"]["newPx"], "2310")
        for argv in (
            ("cancel", "--cl-ord-id", "trialbuy01", "--will-send-http"),
            ("amend", "--cl-ord-id", "trialbuy01", "--new-px", "2310", "--will-send-http"),
            ("cancel", "--cl-ord-id", "trialbuy01"),
            ("amend", "--cl-ord-id", cid),
        ):
            rc, doc, _ = self.run_cli(*argv, "--base-url", self.base_url, env_over=DEMO_ENV)
            self.assertEqual(rc, 3, argv)
            self.assertIs(doc["refused"], True)
            self.assertIs(doc["http_sent"], False)
        rc, doc, _ = self.run_cli("cancel-tagged")
        self.assertEqual(rc, 0)
        self.assertEqual(doc["mode"], "dry_run")
        self.assertIs(doc["http_sent"], False)
        self.assertEqual(doc["cancelled"], [])
        self.assertEqual(FakeDemoHandler.requests, [])

    def test_policy_and_check_env(self):
        rc, doc, _ = self.run_cli("policy")
        self.assertEqual(rc, 0)
        self.assertIs(doc["policy"]["will_send_http"], False)
        self.assertIs(doc["policy"]["merge_is_arming"], False)
        rc, doc, text = self.run_cli("check-env", env_over=DEMO_ENV)
        self.assertEqual(rc, 0)
        self.assertIs(doc["demo_env"]["ok"], True)
        self.assertIs(doc["will_send_http"], False)
        self.assertNotIn("not-real", text)


if __name__ == "__main__":
    unittest.main()
