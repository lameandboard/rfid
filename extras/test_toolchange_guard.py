"""Tests for the AFC/RFID toolchange busy guard.

Run with:
    python3 extras/test_toolchange_guard.py
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

_EXTRAS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_EXTRAS_DIR)
for _p in (_REPO_ROOT, _EXTRAS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_BUS_MOD = MagicMock()
_BUS_MOD.MCU_SPI_from_config = MagicMock(return_value=MagicMock())
sys.modules["extras.bus"] = _BUS_MOD

import extras.rfid as _rfid_module  # noqa: E402


class _FakeReactor:
    NEVER = float("inf")

    def __init__(self, monotonic_value=1000.0):
        self._monotonic = monotonic_value
        self.timers = []
        self.async_callbacks = []

    def monotonic(self):
        return self._monotonic

    def register_timer(self, callback, when):
        handle = {"callback": callback, "when": when, "cancelled": False}
        self.timers.append(handle)
        return handle

    def unregister_timer(self, handle):
        handle["cancelled"] = True

    def register_async_callback(self, callback):
        self.async_callbacks.append(callback)

    def run_async_callbacks(self):
        while self.async_callbacks:
            cb = self.async_callbacks.pop(0)
            cb(self._monotonic)


class _FakeGCmd:
    def __init__(self, commandline):
        self._commandline = commandline

    def get_commandline(self):
        return self._commandline

    def get_raw_command_parameters(self):
        parts = self._commandline.split(None, 1)
        return parts[1] if len(parts) > 1 else ""


class _FakeGCode:
    def __init__(self):
        self.ready_gcode_handlers = {}
        self.base_gcode_handlers = {}
        self.gcode_help = {}
        self.executed_scripts = []
        self.messages = []

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        if func is None:
            old = self.ready_gcode_handlers.pop(cmd, None)
            self.base_gcode_handlers.pop(cmd, None)
            self.gcode_help.pop(cmd, None)
            return old
        self.ready_gcode_handlers[cmd] = func
        if not when_not_ready:
            self.base_gcode_handlers[cmd] = func
        if desc is not None:
            self.gcode_help[cmd] = desc
        return None

    def register_mux_command(self, cmd, _key, _value, func, desc=None):
        self.register_command(f"{cmd}:mux", func, desc=desc)

    def respond_info(self, msg):
        self.messages.append(msg)

    def run_script_from_command(self, script):
        self.executed_scripts.append(script)
        cmd = script.split(None, 1)[0]
        handler = self.ready_gcode_handlers.get(cmd)
        if handler is not None:
            handler(_FakeGCmd(script))


def _make_rfid(lanes="lane1", reactor=None):
    if reactor is None:
        reactor = _FakeReactor()

    gcode = _FakeGCode()
    printer = MagicMock()
    printer.get_reactor.return_value = reactor
    printer.lookup_objects.return_value = []

    def _lookup_object(name, default=None):
        if name == "gcode":
            return gcode
        return default

    printer.lookup_object.side_effect = _lookup_object

    config = MagicMock()
    config.get_name.return_value = "rfid mfrc522_0"
    config.get_printer.return_value = printer
    config.getint.return_value = 100000
    config.getfloat.return_value = 10.0

    def _cfg_getboolean(key, default=False, **_kw):
        if key == "messages":
            return True
        return default

    config.getboolean.side_effect = _cfg_getboolean

    def _cfg_get(key, default=None, **_kw):
        return {
            "driver": "mfrc522",
            "lanes": lanes,
            "spoolman_url": "",
            "spoolman_api_key": None,
        }.get(key, default)

    config.get.side_effect = _cfg_get

    rfid_obj = _rfid_module.Rfid(config)
    rfid_obj.reactor = reactor
    rfid_obj.gcode = gcode
    return rfid_obj, gcode, reactor


class TestToolchangeGuard(unittest.TestCase):
    def setUp(self):
        self.rfid, self.gcode, self.reactor = _make_rfid()
        self.original_calls = []

        def _original_t0(gcmd):
            self.original_calls.append(gcmd.get_commandline())

        self.gcode.register_command("T0", _original_t0, desc="toolchange T0")
        self.rfid._assign_spool_to_gate = MagicMock()
        self.rfid._handle_klippy_connect()

    def test_t0_runs_normally_when_not_busy(self):
        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))
        self.assertEqual(self.original_calls, ["T0"])

    def test_t0_deferred_until_commit_finishes(self):
        lane = "lane1"
        self.rfid._start_scan_timer(lane)

        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))

        self.assertEqual(self.original_calls, [])
        self.assertEqual(self.rfid._toolchange_guard["deferred_toolchanges"], ["T0"])

        self.rfid._pending[lane] = {
            "lane": lane,
            "spoolman_id": 42,
            "uid_hex": "ABCD",
            "ts": time.time(),
            "timeout": 60.0,
        }
        self.assertTrue(self.rfid._event_scan_commit(lane))

        self.reactor.run_async_callbacks()

        self.assertEqual(self.original_calls, ["T0"])
        self.assertFalse(self.rfid._rfid_busy)

    def test_expired_pending_commit_clears_busy_and_replays_t0(self):
        lane = "lane1"
        self.rfid._start_scan_timer(lane)
        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))

        self.rfid._pending[lane] = {
            "lane": lane,
            "spoolman_id": 42,
            "uid_hex": "ABCD",
            "ts": time.time() - 30.0,
            "timeout": 1.0,
        }
        self.assertFalse(self.rfid._event_scan_commit(lane))

        self.reactor.run_async_callbacks()

        self.assertEqual(self.original_calls, ["T0"])
        self.assertFalse(self.rfid._rfid_busy)

    def test_busy_timeout_clears_guard_and_replays_t0(self):
        lane = "lane1"
        self.rfid._start_scan_timer(lane)
        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))

        handle = self.rfid._toolchange_guard["busy_timers"][lane]
        handle["callback"](handle["when"])
        self.reactor.run_async_callbacks()

        self.assertEqual(self.original_calls, ["T0"])
        self.assertFalse(self.rfid._rfid_busy)

    def test_replay_requeues_remaining_toolchanges_if_busy_restarts(self):
        lane = "lane1"

        def _restart_busy_on_first_call(gcmd):
            self.original_calls.append(gcmd.get_commandline())
            if len(self.original_calls) == 1:
                self.rfid._start_scan_timer(lane)

        self.gcode.register_command("T0", _restart_busy_on_first_call, desc="toolchange T0")
        self.rfid._toolchange_guard["installed"] = False
        self.rfid._handle_klippy_connect()
        self.rfid._start_scan_timer(lane)
        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))
        self.gcode.ready_gcode_handlers["T0"](_FakeGCmd("T0"))

        self.rfid._clear_rfid_busy(lane, reason="commit_complete")
        self.reactor.run_async_callbacks()

        self.assertEqual(self.original_calls, ["T0"])
        self.assertEqual(self.rfid._toolchange_guard["deferred_toolchanges"], ["T0"])
        self.assertTrue(self.rfid._rfid_busy)

        self.rfid._clear_rfid_busy(lane, reason="commit_complete")
        self.reactor.run_async_callbacks()

        self.assertEqual(self.original_calls, ["T0", "T0"])
        self.assertFalse(self.rfid._rfid_busy)


if __name__ == "__main__":
    unittest.main()
