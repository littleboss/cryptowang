"""Unit tests for tools/okx_sdk_readonly.py — official python-okx SDK behind a GET-only gate.

Everything here is offline: a local fake OKX http.server (shared with the stdlib client
tests) for wire-level assertions, and unittest.mock on httpx.Client.send to prove that
write paths raise *before* any transport call.

Run: uv run --no-dev python -m unittest discover -s tests -v
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import okx_readonly_client as okx  # noqa: E402
import okx_sdk_readonly as sdk  # noqa: E402
from test_okx_readonly_client import (  # noqa: E402
    FAKE_KEY,
    FAKE_PASSPHRASE,
    FAKE_SECRET,
    PAGE1,
    FakeOkxHandler,
    FakeServerMixin,
)

DEAD_BASE = "http://127.0.0.1:9"  # nothing listens; any accidental I/O fails loudly


def _sim_creds() -> okx.ReadOnlyCredentials:
    return okx.ReadOnlyCredentials(FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE, simulated=True)


def _dummy_args(fn) -> tuple[list, dict]:
    """Fill every required positional parameter with a harmless string."""
    args: list = []
    for p in list(inspect.signature(fn).parameters.values()):
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is inspect.Parameter.empty:
            args.append("x")
    return args, {}


def _sdk_methods(cls) -> list[str]:
    """Public SDK methods defined on the *SDK* class (not httpx, not our gate)."""
    out = []
    for base in cls.__mro__:
        if base.__module__.startswith("okx."):
            for name, member in vars(base).items():
                if not name.startswith("_") and callable(member):
                    out.append(name)
    return sorted(set(out))


class SdkModuleAuditTest(unittest.TestCase):
    def test_no_write_capable_sdk_module_is_imported(self):
        for name in sdk.FORBIDDEN_SDK_MODULES:
            self.assertNotIn(name, sys.modules, name)
        sdk.assert_no_write_sdk_modules_loaded()

    def test_static_imports_are_read_only_subset(self):
        found = sdk.imported_sdk_modules()
        self.assertTrue(found, "expected some okx imports")
        self.assertTrue(found <= sdk.ALLOWED_SDK_MODULES, found - sdk.ALLOWED_SDK_MODULES)
        self.assertFalse(found & sdk.FORBIDDEN_SDK_MODULES)

    def test_audit_detects_trade_module(self):
        fake = types.ModuleType("okx.Trade")
        with mock.patch.dict(sys.modules, {"okx.Trade": fake}):
            with self.assertRaises(okx.ReadOnlyViolation):
                sdk.assert_no_write_sdk_modules_loaded()
        sdk.assert_no_write_sdk_modules_loaded()

    def test_policy_flags(self):
        self.assertIs(sdk.POLICY["will_send_http"], False)
        for k in ("order", "amend", "transfer", "withdraw"):
            self.assertIs(sdk.POLICY[k], False)
        self.assertEqual(sdk.POLICY["methods_allowed"], ["GET"])
        self.assertIn("python-okx", sdk.POLICY["backend"])
        self.assertEqual(sdk.SIMULATED_FLAG, "1")

    def test_sdk_debug_logging_cannot_leak_headers(self):
        """Even if SDK debug logging were turned on, the `okx` logger namespace is disabled."""
        from loguru import logger
        from okx import utils as sdk_utils

        captured: list[str] = []
        sink_id = logger.add(captured.append, level="DEBUG")
        try:
            sdk_utils.get_header(FAKE_KEY, b"sig", "ts", FAKE_PASSPHRASE, "1", debug=True)
            sdk_utils.pre_hash("ts", "GET", "/api/v5/account/balance", "", debug=True)
        finally:
            logger.remove(sink_id)
        self.assertEqual(captured, [])


class GateTest(unittest.TestCase):
    """Write paths raise before httpx.Client.send is reached."""

    def setUp(self):
        self.pub = sdk.OkxSdkPublicClient(base_url=DEAD_BASE)
        self.priv = sdk.OkxSdkReadOnlyPrivateClient(_sim_creds(), base_url=DEAD_BASE)
        self.gated = [self.pub._market, self.priv._account, self.priv._grid]

    def tearDown(self):
        self.pub.close()
        self.priv.close()

    def test_httpx_write_verbs_raise_without_transport(self):
        with mock.patch.object(httpx.Client, "send") as send:
            for g in self.gated:
                for verb in ("post", "put", "patch", "delete"):
                    with self.assertRaises(okx.ReadOnlyViolation, msg=verb):
                        getattr(g, verb)("/api/v5/trade/order", json={})
                for method in ("POST", "PUT", "DELETE", "post"):
                    with self.assertRaises(okx.ReadOnlyViolation, msg=method):
                        g.request(method, "/api/v5/trade/order")
                    with self.assertRaises(okx.ReadOnlyViolation, msg=method):
                        with g.stream(method, "/api/v5/trade/order"):
                            pass
                    with self.assertRaises(okx.ReadOnlyViolation, msg=method):
                        g.send(httpx.Request(method, f"{DEAD_BASE}/api/v5/trade/order"))
            send.assert_not_called()

    def test_sdk_request_funnel_refuses_non_get_and_unlisted_paths(self):
        with mock.patch.object(httpx.Client, "send") as send:
            for g in self.gated:
                with self.assertRaises(okx.ReadOnlyViolation):
                    g._request("POST", okx.PATH_TICKER, {})
                with self.assertRaises(okx.ReadOnlyViolation):
                    g._request("GET", "/api/v5/trade/orders-pending", {})
                with self.assertRaises(okx.ReadOnlyViolation):
                    g._request("GET", "/api/v5/tradingBot/grid/amend-order-algo", {})
                with self.assertRaises(okx.ReadOnlyViolation):
                    g._request("POST", "/api/v5/asset/withdrawal", {"amt": "1"})
            send.assert_not_called()

    def test_every_sdk_post_method_raises_and_only_allowlisted_gets_reach_send(self):
        """Walk *all* SDK methods on the gated Grid / Account / Market classes.

        Methods whose source uses POST must raise ReadOnlyViolation; GET methods outside the
        allow-list must raise too; whatever reaches httpx.send must be a GET to an
        allow-listed path. This survives SDK upgrades that add new write methods.
        """
        seen_post = 0
        with mock.patch.object(httpx.Client, "send") as send:
            for g in self.gated:
                allow = okx.PRIVATE_READ_PATHS if g._private else okx.PUBLIC_READ_PATHS
                for name in _sdk_methods(type(g)):
                    fn = getattr(g, name)
                    src = inspect.getsource(fn)
                    is_post = "POST" in src
                    args, kwargs = _dummy_args(fn)
                    try:
                        fn(*args, **kwargs)
                    except okx.ReadOnlyViolation:
                        if is_post:
                            seen_post += 1
                        continue
                    except Exception as e:  # Mock response → SDK may choke on .json(); fine
                        self.assertFalse(is_post, f"{name}: POST path did not raise: {e!r}")
                        continue
                    self.assertFalse(is_post, f"{type(g).__name__}.{name} POST did not raise")
                    self.assertTrue(send.called, name)
                    req = send.call_args.args[0]
                    self.assertEqual(req.method, "GET", name)
                    self.assertIn(req.url.path, allow, name)
                    send.reset_mock()
        self.assertGreater(seen_post, 5, "expected several SDK write methods to be exercised")
        for call in send.call_args_list:
            self.assertEqual(call.args[0].method, "GET")

    def test_known_write_methods_by_name(self):
        for name in (
            "grid_order_algo",
            "grid_amend_order_algo",
            "grid_stop_order_algo",
            "grid_withdraw_income",
            "grid_adjust_margin_balance",
            "place_recurring_buy_order",
        ):
            with self.assertRaises(okx.ReadOnlyViolation, msg=name):
                getattr(self.priv._grid, name)()
        with self.assertRaises(okx.ReadOnlyViolation):
            self.priv._account.set_leverage("1", "cash")
        with self.assertRaises(okx.ReadOnlyViolation):
            self.priv._account.set_position_mode("net_mode")

    def test_facade_forbidden_methods_raise(self):
        for name in ("place_order", "amend_algo", "stop_algo", "transfer", "withdraw"):
            with self.assertRaises(okx.ReadOnlyViolation, msg=name):
                getattr(self.priv, name)()
        self.assertFalse(hasattr(self.pub, "place_order"))

    def test_facade_does_not_proxy_sdk_attributes(self):
        # The facade only exposes explicit read helpers, never the SDK surface.
        for name in ("grid_amend_order_algo", "post", "get_orderbook", "set_leverage"):
            self.assertFalse(hasattr(self.pub, name), name)
            self.assertFalse(hasattr(self.priv, name), name)


class CredentialsTest(unittest.TestCase):
    def test_live_credentials_refused_by_constructor(self):
        live = okx.ReadOnlyCredentials(FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE, simulated=False)
        with self.assertRaises(okx.ReadOnlyViolation):
            sdk.OkxSdkReadOnlyPrivateClient(live, base_url=DEAD_BASE)

    def test_from_env_none_without_keys(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(sdk.OkxSdkReadOnlyPrivateClient.from_env(base_url=DEAD_BASE))
        env = {okx.ENV_KEY: "k", okx.ENV_SECRET: "s", okx.ENV_PASSPHRASE: "p"}
        with mock.patch.dict("os.environ", env, clear=True):
            with self.assertRaises(okx.ReadOnlyViolation):
                sdk.OkxSdkReadOnlyPrivateClient.from_env(base_url=DEAD_BASE)
        with mock.patch.dict("os.environ", {**env, okx.ENV_SIMULATED: "1"}, clear=True):
            c = sdk.OkxSdkReadOnlyPrivateClient.from_env(base_url=DEAD_BASE)
            self.assertIsNotNone(c)
            c.close()

    def test_simulated_flag_is_forced(self):
        c = sdk.OkxSdkReadOnlyPrivateClient(_sim_creds(), base_url=DEAD_BASE)
        self.assertEqual(c._account.flag, "1")
        self.assertEqual(c._grid.flag, "1")
        self.assertFalse(c._account.debug)
        self.assertFalse(c._grid.debug)
        c.close()

    def test_repr_never_leaks(self):
        c = sdk.OkxSdkReadOnlyPrivateClient(_sim_creds(), base_url=DEAD_BASE)
        text = repr(c) + str(c) + repr(c._account) + str(c._grid) + repr(c._creds)
        for value in (FAKE_KEY, FAKE_SECRET, FAKE_PASSPHRASE):
            self.assertNotIn(value, text)
        self.assertIn("<set:", text)
        c.close()


class PublicClientTest(FakeServerMixin, unittest.TestCase):
    def test_ticker_is_keyless_get(self):
        client = sdk.OkxSdkPublicClient(base_url=self.base_url)
        t = client.get_ticker("ETH-USDT")
        self.assertEqual(t["instId"], "ETH-USDT")
        self.assertEqual(t["last"], 2401.67)
        self.assertEqual(t["ts_ms"], 1789534948967)
        req = FakeOkxHandler.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], okx.PATH_TICKER)
        self.assertNotIn("ok-access-key", req["headers"])
        self.assertNotIn("ok-access-sign", req["headers"])
        self.assertNotIn("ok-access-passphrase", req["headers"])
        self.assertEqual(client.requests_made, 1)
        client.close()

    def test_candles_paginate_and_sort_oldest_first(self):
        client = sdk.OkxSdkPublicClient(base_url=self.base_url)
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
        self.assertIn("python-okx", meta["backend"])
        client.close()

    def test_candles_limit_validation(self):
        client = sdk.OkxSdkPublicClient(base_url=self.base_url)
        with self.assertRaises(ValueError):
            client.get_candles("ETH-USDT", limit=301)
        with self.assertRaises(ValueError):
            client.get_candles("ETH-USDT", limit=101, history=True)
        self.assertEqual(FakeOkxHandler.requests, [])

    def test_api_error_surfaces(self):
        client = sdk.OkxSdkPublicClient(base_url=self.base_url)
        with self.assertRaises(okx.OkxApiError):
            client.get_candles("X", history=True, limit=5)

    def test_contract_matches_stdlib_client(self):
        """Same inputs → same dicts as tools/okx_readonly_client.py (drop-in backend)."""
        a = okx.OkxPublicClient(base_url=self.base_url)
        b = sdk.OkxSdkPublicClient(base_url=self.base_url)
        self.assertEqual(a.get_ticker("ETH-USDT"), b.get_ticker("ETH-USDT"))
        ca = [c.to_dict() for c in a.get_candles("ETH-USDT", bar="5m", limit=3, pages=2)]
        cb = [c.to_dict() for c in b.get_candles("ETH-USDT", bar="5m", limit=3, pages=2)]
        self.assertEqual(ca, cb)
        # Same CLI surface: identical subcommand names.
        self.assertEqual(
            set(okx.build_parser()._subparsers._group_actions[0].choices),
            set(sdk.build_parser()._subparsers._group_actions[0].choices),
        )
        b.close()


class PrivateReadOnlyClientTest(FakeServerMixin, unittest.TestCase):
    def _client(self) -> sdk.OkxSdkReadOnlyPrivateClient:
        return sdk.OkxSdkReadOnlyPrivateClient(_sim_creds(), base_url=self.base_url)

    def test_balance_sends_signed_get_with_simulated_header(self):
        c = self._client()
        rows = c.get_balance("USDT")
        self.assertEqual(rows, [{"totalEq": "1000"}])
        req = FakeOkxHandler.requests[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], okx.PATH_ACCOUNT_BALANCE)
        self.assertEqual(req["query"], {"ccy": "USDT"})
        h = req["headers"]
        self.assertEqual(h["x-simulated-trading"], "1")
        self.assertEqual(h["ok-access-key"], FAKE_KEY)
        self.assertEqual(h["ok-access-passphrase"], FAKE_PASSPHRASE)
        expected = okx.sign(
            FAKE_SECRET, h["ok-access-timestamp"], "GET", okx.PATH_ACCOUNT_BALANCE + "?ccy=USDT"
        )
        self.assertEqual(h["ok-access-sign"], expected)
        self.assertNotIn(FAKE_SECRET, json.dumps(h))
        c.close()

    def test_grid_details_summary(self):
        c = self._client()
        details = c.get_grid_details("demo-algo-1")
        summary = okx.bot_status_summary(details)
        self.assertEqual(summary["algoId"], "demo-algo-1")
        self.assertEqual(summary["venue"], "okx_demo")
        self.assertEqual(summary["okx_bot_total_pnl_ratio"], -0.0273)
        self.assertEqual(summary["arbitrage_num"], 2)
        q = FakeOkxHandler.requests[0]["query"]
        self.assertEqual(q, {"algoOrdType": "grid", "algoId": "demo-algo-1"})
        self.assertEqual(FakeOkxHandler.requests[0]["headers"]["x-simulated-trading"], "1")
        c.close()

    def test_no_post_ever_reaches_server(self):
        c = self._client()
        c.get_balance()
        c.get_grid_details("x")
        with self.assertRaises(okx.ReadOnlyViolation):
            c._grid.grid_amend_order_algo(algoId="x", instId="ETH-USDT", slTriggerPx="2150")
        with self.assertRaises(okx.ReadOnlyViolation):
            c._grid.grid_stop_order_algo(algoId="x", instId="ETH-USDT", algoOrdType="grid")
        with self.assertRaises(okx.ReadOnlyViolation):
            c._grid.grid_order_algo(instId="ETH-USDT", algoOrdType="grid")
        with self.assertRaises(okx.ReadOnlyViolation):
            c._account.set_leverage("2", "cross", instId="ETH-USDT")
        self.assertTrue(all(r["method"] == "GET" for r in FakeOkxHandler.requests))
        self.assertEqual(len(FakeOkxHandler.requests), 2)
        c.close()


class CliStubTest(unittest.TestCase):
    script = ROOT / "tools" / "okx_sdk_readonly.py"

    def _run(self, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.script), *args],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", **env},
            cwd=ROOT,
        )

    def test_private_status_without_keys_is_skipped(self):
        proc = self._run(["private-status"], {})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["skipped"])
        self.assertEqual(doc["requires"], {okx.ENV_SIMULATED: "1"})
        self.assertIs(doc["policy"]["will_send_http"], False)

    def test_private_status_with_live_keys_refused(self):
        proc = self._run(
            ["private-status"],
            {okx.ENV_KEY: "kk-live", okx.ENV_SECRET: "ss-live", okx.ENV_PASSPHRASE: "pp-live"},
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertTrue(doc["skipped"])
        self.assertIn("OKX_SIMULATED=1", doc["reason"])
        for v in ("kk-live", "ss-live", "pp-live"):
            self.assertNotIn(v, proc.stdout + proc.stderr)

    def test_policy_command_offline_and_audits_modules(self):
        proc = self._run(["policy"], {okx.ENV_SECRET: "should-not-print"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("should-not-print", proc.stdout)
        self.assertIn("<set:16 chars>", proc.stdout)
        doc = json.loads(proc.stdout)
        loaded = set(doc["sdk_modules_loaded"])
        self.assertFalse(loaded & sdk.FORBIDDEN_SDK_MODULES, loaded)
        self.assertEqual(set(doc["sdk_modules_in_source"]), sdk.ALLOWED_SDK_MODULES)


if __name__ == "__main__":
    unittest.main()
