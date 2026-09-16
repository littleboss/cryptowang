"""Unit tests for tools/okx_readonly_client.py against a local fake OKX server (no internet).

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import okx_readonly_client as okx  # noqa: E402

FAKE_KEY = "test-key-not-real"
FAKE_SECRET = "test-hmac-not-real"
FAKE_PASSPHRASE = "test-pass-not-real"

# newest first, like OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
PAGE1 = [
    ["1789535400000", "2400.9", "2401", "2400", "2400.5", "2.0", "0", "0", "0"],
    ["1789535100000", "2398.2", "2400.9", "2393.4", "2400.9", "1434.6", "0", "0", "1"],
    ["1789534800000", "2403.9", "2404.9", "2395.4", "2398.2", "1027.2", "0", "0", "1"],
]
PAGE2 = [
    ["1789534500000", "2405.0", "2406.0", "2403.0", "2403.9", "900.0", "0", "0", "1"],
]


class FakeOkxHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *_a):  # silence
        pass

    def _send(self, status: int, doc: dict) -> None:
        body = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeOkxHandler.requests.append(
            {
                "method": "GET",
                "path": u.path,
                "query": q,
                # urllib normalises header casing; compare lower-cased
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )
        if u.path == okx.PATH_TICKER:
            return self._send(
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "instType": "SPOT",
                            "instId": q.get("instId"),
                            "last": "2401.67",
                            "bidPx": "2401.61",
                            "askPx": "2401.62",
                            "high24h": "2500.92",
                            "low24h": "2358.1",
                            "vol24h": "253424.4",
                            "ts": "1789534948967",
                        }
                    ],
                },
            )
        if u.path == okx.PATH_CANDLES:
            data = PAGE2 if q.get("after") == PAGE1[-1][0] else PAGE1
            return self._send(200, {"code": "0", "msg": "", "data": data})
        if u.path == okx.PATH_ACCOUNT_BALANCE:
            if (
                "OK-ACCESS-SIGN" not in self.headers
                or self.headers.get("x-simulated-trading") != "1"
            ):
                return self._send(
                    401, {"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []}
                )
            return self._send(200, {"code": "0", "msg": "", "data": [{"totalEq": "1000"}]})
        if u.path == okx.PATH_GRID_DETAILS:
            return self._send(
                200,
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        {
                            "algoId": q.get("algoId"),
                            "instId": "ETH-USDT",
                            "state": "running",
                            "minPx": "2200",
                            "maxPx": "3200",
                            "gridNum": "30",
                            "investment": "1000",
                            "lever": "1",
                            "slTriggerPx": "2150",
                            "tpTriggerPx": "",
                            "totalPnl": "-27.38",
                            "totalPnlRatio": "-0.0273",
                            "gridProfit": "0.93",
                            "floatProfit": "-28.31",
                            "arbitrageNum": "2",
                        }
                    ],
                },
            )
        return self._send(404, {"code": "404", "msg": "not found", "data": []})

    def do_POST(self):  # noqa: N802
        FakeOkxHandler.requests.append({"method": "POST", "path": self.path})
        self._send(405, {"code": "405", "msg": "POST must never happen", "data": []})


class FakeServerMixin:
    server: HTTPServer
    base_url: str

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOkxHandler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOkxHandler.requests.clear()


class GateTest(unittest.TestCase):
    def test_non_get_refused_before_io(self):
        for method in ("POST", "PUT", "DELETE", "post"):
            with self.assertRaises(okx.ReadOnlyViolation):
                okx.assert_read_only(method, okx.PATH_TICKER, private=False)

    def test_trade_paths_refused(self):
        bad = [
            "/api/v5/trade/order",
            "/api/v5/trade/amend-order",
            "/api/v5/tradingBot/grid/amend-order-algo",
            "/api/v5/tradingBot/grid/order-algo",
            "/api/v5/tradingBot/grid/stop-order-algo",
            "/api/v5/asset/withdrawal",
            "/api/v5/asset/transfer",
            "/api/v5/account/set-leverage",
        ]
        for path in bad:
            for private in (False, True):
                with self.assertRaises(okx.ReadOnlyViolation, msg=path):
                    okx.assert_read_only("GET", path, private=private)

    def test_allow_lists_are_disjoint_and_get_only(self):
        for path in okx.PUBLIC_READ_PATHS:
            okx.assert_read_only("GET", path, private=False)
            with self.assertRaises(okx.ReadOnlyViolation):
                okx.assert_read_only("GET", path, private=True)
        for path in okx.PRIVATE_READ_PATHS:
            okx.assert_read_only("GET", path, private=True)
            with self.assertRaises(okx.ReadOnlyViolation):
                okx.assert_read_only("GET", path, private=False)

    def test_forbidden_methods_raise(self):
        creds = okx.ReadOnlyCredentials(FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE, simulated=True)
        client = okx.OkxReadOnlyPrivateClient(creds, base_url="http://127.0.0.1:9")
        for name in ("place_order", "amend_algo", "transfer", "withdraw"):
            with self.assertRaises(okx.ReadOnlyViolation):
                getattr(client, name)()

    def test_policy_flags(self):
        self.assertIs(okx.POLICY["will_send_http"], False)
        for k in ("order", "amend", "transfer", "withdraw"):
            self.assertIs(okx.POLICY[k], False)
        self.assertEqual(okx.POLICY["methods_allowed"], ["GET"])


class CredentialsTest(unittest.TestCase):
    def test_absent_keys_return_none(self):
        self.assertIsNone(okx.ReadOnlyCredentials.from_env({}))
        self.assertIsNone(okx.ReadOnlyCredentials.from_env({okx.ENV_KEY: "k"}))
        self.assertIsNone(
            okx.ReadOnlyCredentials.from_env(
                {okx.ENV_KEY: "k", okx.ENV_SECRET: "s", okx.ENV_SIMULATED: "1"}
            )
        )

    def test_live_keys_refused(self):
        env = {okx.ENV_KEY: "k", okx.ENV_SECRET: "s", okx.ENV_PASSPHRASE: "p"}
        with self.assertRaises(okx.ReadOnlyViolation):
            okx.ReadOnlyCredentials.from_env(env)
        with self.assertRaises(okx.ReadOnlyViolation):
            okx.ReadOnlyCredentials.from_env({**env, okx.ENV_SIMULATED: "0"})
        with self.assertRaises(okx.ReadOnlyViolation):
            okx.ReadOnlyCredentials.from_env({**env, okx.ENV_SIMULATED: "true"})
        creds = okx.ReadOnlyCredentials.from_env({**env, okx.ENV_SIMULATED: "1"})
        self.assertIsNotNone(creds)
        self.assertTrue(creds.simulated)

    def test_repr_never_leaks(self):
        creds = okx.ReadOnlyCredentials(FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE, simulated=True)
        text = repr(creds) + str(creds) + repr(okx.OkxReadOnlyPrivateClient(creds))
        for value in (FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE):
            self.assertNotIn(value, text)
        self.assertIn("<set:", text)
        self.assertEqual(okx.redact(""), "<unset>")

    def test_signature_matches_okx_scheme(self):
        ts = "2026-09-16T05:00:00.000Z"
        path = "/api/v5/account/balance?ccy=USDT"
        expected = base64.b64encode(
            hmac.new(FAKE_SECRET.encode(), f"{ts}GET{path}".encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(okx.sign(FAKE_SECRET, ts, "GET", path), expected)
        self.assertRegex(okx.okx_timestamp(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class PublicClientTest(FakeServerMixin, unittest.TestCase):
    def test_ticker(self):
        client = okx.OkxPublicClient(base_url=self.base_url)
        t = client.get_ticker("ETH-USDT")
        self.assertEqual(t["instId"], "ETH-USDT")
        self.assertEqual(t["last"], 2401.67)
        self.assertEqual(t["ts_ms"], 1789534948967)
        req = FakeOkxHandler.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], okx.PATH_TICKER)
        # public path: no auth headers at all
        self.assertNotIn("ok-access-key", req["headers"])
        self.assertNotIn("ok-access-sign", req["headers"])
        self.assertEqual(client.requests_made, 1)

    def test_candles_paginate_and_sort_oldest_first(self):
        client = okx.OkxPublicClient(base_url=self.base_url)
        rows = client.get_candles("ETH-USDT", bar="5m", limit=3, pages=2)
        self.assertEqual(
            [c.ts_ms for c in rows], [1789534500000, 1789534800000, 1789535100000, 1789535400000]
        )
        self.assertEqual(rows[0].open, 2405.0)
        self.assertEqual(rows[-1].confirm, 0)
        self.assertEqual(len(FakeOkxHandler.requests), 2)
        self.assertEqual(FakeOkxHandler.requests[1]["query"]["after"], PAGE1[-1][0])
        meta = client.data_source_meta("ETH-USDT", "5m", 3, 2)
        self.assertEqual(meta["auth"], "none")
        self.assertTrue(meta["read_only"])
        self.assertEqual(meta["requests_made"], 2)

    def test_candles_limit_validation(self):
        client = okx.OkxPublicClient(base_url=self.base_url)
        with self.assertRaises(ValueError):
            client.get_candles("ETH-USDT", limit=301)
        with self.assertRaises(ValueError):
            client.get_candles("ETH-USDT", limit=101, history=True)

    def test_api_error_surfaces(self):
        client = okx.OkxPublicClient(base_url=self.base_url)
        # 404 payload carries code != "0" → OkxApiError
        with self.assertRaises(okx.OkxApiError):
            client._get(okx.PATH_HISTORY_CANDLES, {"instId": "X"})


class PrivateReadOnlyClientTest(FakeServerMixin, unittest.TestCase):
    def _client(self) -> okx.OkxReadOnlyPrivateClient:
        creds = okx.ReadOnlyCredentials(FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE, simulated=True)
        return okx.OkxReadOnlyPrivateClient(creds, base_url=self.base_url)

    def test_balance_sends_signed_get_with_simulated_header(self):
        rows = self._client().get_balance("USDT")
        self.assertEqual(rows, [{"totalEq": "1000"}])
        req = FakeOkxHandler.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["query"], {"ccy": "USDT"})
        h = req["headers"]
        self.assertEqual(h["x-simulated-trading"], "1")
        self.assertEqual(h["ok-access-key"], FAKE_KEY)
        self.assertEqual(h["ok-access-passphrase"], FAKE_PASSPHRASE)
        expected = okx.sign(
            FAKE_SECRET, h["ok-access-timestamp"], "GET", okx.PATH_ACCOUNT_BALANCE + "?ccy=USDT"
        )
        self.assertEqual(h["ok-access-sign"], expected)
        self.assertNotIn(FAKE_SECRET, json.dumps(h))  # secret itself never travels

    def test_grid_details_summary(self):
        details = self._client().get_grid_details("demo-algo-1")
        summary = okx.bot_status_summary(details)
        self.assertEqual(summary["algoId"], "demo-algo-1")
        self.assertEqual(summary["venue"], "okx_demo")
        self.assertEqual(summary["okx_bot_total_pnl_ratio"], -0.0273)
        self.assertEqual(summary["arbitrage_num"], 2)
        self.assertEqual(summary["gridNum"], 30)
        self.assertIsNone(summary["tpTriggerPx"])
        self.assertEqual(FakeOkxHandler.requests[0]["query"]["algoOrdType"], "grid")

    def test_no_post_ever_reaches_server(self):
        c = self._client()
        c.get_balance()
        c.get_grid_details("x")
        with self.assertRaises(okx.ReadOnlyViolation):
            c._get("/api/v5/tradingBot/grid/amend-order-algo", {"algoId": "x"})
        self.assertTrue(all(r["method"] == "GET" for r in FakeOkxHandler.requests))
        self.assertEqual(len(FakeOkxHandler.requests), 2)


class CliStubTest(unittest.TestCase):
    def test_private_status_without_keys_is_skipped(self):
        script = ROOT / "tools" / "okx_readonly_client.py"
        env = {"PATH": "/usr/bin:/bin"}  # no OKX_* at all
        proc = subprocess.run(
            [sys.executable, str(script), "private-status"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["skipped"])
        self.assertEqual(doc["requires"], {okx.ENV_SIMULATED: "1"})

    def test_private_status_with_live_keys_refused(self):
        script = ROOT / "tools" / "okx_readonly_client.py"
        env = {
            "PATH": "/usr/bin:/bin",
            okx.ENV_KEY: "k",
            okx.ENV_SECRET: "s",
            okx.ENV_PASSPHRASE: "p",
        }
        proc = subprocess.run(
            [sys.executable, str(script), "private-status"],
            capture_output=True,
            text=True,
            env=env,
            cwd=ROOT,
        )
        self.assertEqual(proc.returncode, 3)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["skipped"])
        self.assertIn("OKX_SIMULATED=1", doc["reason"])
        for v in ("k", "s", "p"):
            self.assertNotIn(f'"{v}"', proc.stdout)

    def test_policy_command_offline(self):
        script = ROOT / "tools" / "okx_readonly_client.py"
        proc = subprocess.run(
            [sys.executable, str(script), "policy"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", okx.ENV_SECRET: "should-not-print"},
        )
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("should-not-print", proc.stdout)
        self.assertIn("<set:16 chars>", proc.stdout)


if __name__ == "__main__":
    unittest.main()
