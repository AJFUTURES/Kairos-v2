"""Broker-free regression checks for KAIROS execution protection.

Selected functions are loaded with AST; importing ``main`` is intentionally
avoided because it initializes runtime configuration. External boundaries are
replaced with in-memory fakes.
"""

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).resolve().parents[1] / "main.py"


def load_function(name, namespace):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    node = next(
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    node.decorator_list = []
    code = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(code), str(SOURCE), "exec"), namespace)
    return namespace[name]


@asynccontextmanager
async def fake_client():
    yield object()


class EndMonitor(BaseException):
    """Stop a production infinite loop after exactly one evaluation."""


async def phase_transition(be_triggered=False):
    orders = {"protective-stop"}
    sleeps = 0

    async def sleep(_):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 2:
            raise EndMonitor()

    ns = {
        "asyncio": SimpleNamespace(sleep=sleep),
        "time": SimpleNamespace(time=lambda: 120.1),
        "structural_pos": {
            "NQ": {
                "level": 100.0,
                "side": 0,
                "tf": "1m",
                "entry_bucket": 1,
                "two_phase": True,
                "phase": 1,
            }
        },
        "quotes": {"NQ": {"last": 101.0}},
        "positions": {"NQ": {"direction": "LONG", "side": 0, "size": 1}},
        "contracts": {"NQ": {"tick": 0.25}},
        "be_state": {"NQ": {"be_triggered": be_triggered}},
        "_candle_bucket": lambda *_: 2,
        "_stamp_open_rows": lambda *args, **kwargs: None,
        "alog": AsyncMock(),
    }
    run = load_function("structural_monitor_loop", ns)
    try:
        await run()
    except EndMonitor:
        pass
    if ns["structural_pos"]["NQ"]["phase"] != 2:
        raise AssertionError("Fixture never reached phase 2")
    return orders


async def attempt_flatten(confirmed):
    orders = {"protective-stop"}

    async def cancel(*args):
        orders.clear()

    ns = {
        "trade_lock": asyncio.Lock(),
        "api_client": fake_client,
        "get_auth": AsyncMock(return_value=("fake-token", "fake-account")),
        "refresh_positions": AsyncMock(),
        "positions": {"NQ": {"direction": "LONG", "side": 0, "size": 1}},
        "blank_pos": lambda: {"direction": "FLAT", "side": None, "size": 0},
        "cancel_all_orders": cancel,
        "flatten_position": AsyncMock(return_value=confirmed),
        "confirm_flat": AsyncMock(return_value=confirmed),
        "structural_pos": {"NQ": {"level": 100.0, "exiting": True}},
        "be_state": {"NQ": {"be_triggered": False}},
        "current_bracket_ids": {"NQ": {"protective-stop"}},
        "_stamp_open_rows": lambda *args, **kwargs: None,
        "alog": AsyncMock(),
    }
    result = await load_function("flatten_symbol", ns)("NQ", require_side=0)
    return {
        "result": result,
        "orders": orders,
        "tracking": "NQ" in ns["structural_pos"],
        "exiting": ns["structural_pos"].get("NQ", {}).get("exiting"),
    }


async def exit_alert(timeframe):
    flatten = AsyncMock(return_value=True)
    ns = {
        "structural_pos": {"NQ": {"tf": "1m", "side": 0}},
        "alog": AsyncMock(),
        "flatten_symbol": flatten,
    }
    await load_function("handle_structural_exit", ns)(
        "NQ", reason="bullish IFVG invalidated", exit_tf=timeframe
    )
    return flatten.await_args_list


async def rejected_close_response():
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "success": False,
            "orderId": None,
            "errorCode": 1,
            "errorMessage": "fixture rejection",
        },
        text="fixture rejection",
    )
    client = SimpleNamespace(post=AsyncMock(return_value=response))
    ns = {
        "contracts": {"NQ": {"id": "fake-contract"}},
        "positions": {"NQ": {"direction": "LONG", "side": 0, "size": 1}},
        "blank_pos": lambda: {"direction": "FLAT", "side": None, "size": 0},
        "alog": AsyncMock(),
    }
    ns["_order_response_ok"] = load_function("_order_response_ok", {})
    result = await load_function("flatten_position", ns)(
        client, "fake-token", "fake-account", "NQ", 1, 0
    )
    return result, ns["positions"]["NQ"]["direction"]


class ExecutionSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.phase_without_be = asyncio.run(phase_transition(False))
        cls.phase_with_be = asyncio.run(phase_transition(True))
        cls.failed_flatten = asyncio.run(attempt_flatten(False))
        cls.successful_flatten = asyncio.run(attempt_flatten(True))
        cls.wrong_tf_exit = asyncio.run(exit_alert("5m"))
        cls.matching_tf_exit = asyncio.run(exit_alert("1m"))
        cls.rejected_close = asyncio.run(rejected_close_response())

    def test_phase2_retains_broker_protective_stop(self):
        self.assertEqual(self.phase_without_be, {"protective-stop"})

    def test_existing_break_even_stop_survives_phase_transition(self):
        self.assertEqual(self.phase_with_be, {"protective-stop"})

    def test_unconfirmed_flatten_retains_protection_and_tracking(self):
        self.assertEqual(self.failed_flatten["orders"], {"protective-stop"})
        self.assertTrue(self.failed_flatten["tracking"])
        self.assertFalse(self.failed_flatten["exiting"])
        self.assertFalse(self.failed_flatten["result"])

    def test_confirmed_flatten_clears_protection_and_tracking(self):
        self.assertEqual(
            self.successful_flatten,
            {"result": True, "orders": set(), "tracking": False, "exiting": None},
        )

    def test_http200_business_rejection_does_not_report_closed(self):
        result, direction = self.rejected_close
        self.assertFalse(result)
        self.assertEqual(direction, "LONG")

    def test_entry_and_flatten_share_business_success_gate(self):
        bot = SOURCE.read_text(encoding="utf-8")
        self.assertGreaterEqual(bot.count("if _order_response_ok(response):"), 2)
        gate = load_function("_order_response_ok", {})
        self.assertFalse(gate(SimpleNamespace(status_code=500, json=lambda: {"success": True})))
        self.assertFalse(gate(SimpleNamespace(status_code=200, json=lambda: {"success": False})))
        self.assertTrue(gate(SimpleNamespace(status_code=200, json=lambda: {"success": True})))

    def test_wrong_timeframe_exit_is_ignored(self):
        self.assertEqual(self.wrong_tf_exit, [])

    def test_matching_bullish_exit_requires_long_position(self):
        self.assertEqual(len(self.matching_tf_exit), 1)
        self.assertEqual(self.matching_tf_exit[0].kwargs["require_side"], 0)


if __name__ == "__main__":
    unittest.main()
