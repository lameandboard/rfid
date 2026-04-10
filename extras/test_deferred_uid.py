"""Tests for the deferred-UID scan-window-expiry fix.

Validates that when the scan window timer expires with a seen UID (but no
resolved spoolman_id) and lane_loaded fires *after* the scan state has
already been cleared, the system still performs the Spoolman fallback lookup
instead of silently skipping the commit.

Run with:
    python3 extras/test_deferred_uid.py
"""

import ast
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path and module stubs: must happen before importing extras.rfid
# ---------------------------------------------------------------------------
_EXTRAS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_EXTRAS_DIR)
for _p in (_REPO_ROOT, _EXTRAS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub out the Klipper bus module which is unavailable outside Klipper
_BUS_MOD = MagicMock()
_BUS_MOD.MCU_SPI_from_config = MagicMock(return_value=MagicMock())
sys.modules["extras.bus"] = _BUS_MOD

_RFID_PY = os.path.join(_EXTRAS_DIR, "rfid.py")

import extras.rfid as _rfid_module  # noqa: E402  (after sys.modules stubs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_reactor(monotonic_value=1000.0):
    reactor = MagicMock()
    reactor.monotonic.return_value = monotonic_value
    reactor.NEVER = float("inf")
    reactor.register_timer.return_value = MagicMock()
    reactor.register_async_callback = MagicMock()
    return reactor


def _make_rfid(lanes="lane3", spoolman_url="", reactor=None):
    """Instantiate a minimal Rfid instance with enough mocking to work."""
    if reactor is None:
        reactor = _make_reactor()

    printer = MagicMock()
    printer.get_reactor.return_value = reactor
    gcode = MagicMock()
    printer.lookup_object.return_value = gcode
    printer.lookup_objects.return_value = []

    config = MagicMock()
    config.get_name.return_value = "rfid mfrc522_1"
    config.get_printer.return_value = printer
    config.getint.return_value = 100000
    config.getboolean.return_value = False
    config.getfloat.return_value = 10.0

    def _cfg_get(key, default=None, **kw):
        return {
            "driver": "mfrc522",
            "lanes": lanes,
            "spoolman_url": spoolman_url,
            "spoolman_api_key": None,
        }.get(key, default)

    config.get.side_effect = _cfg_get

    rfid_obj = _rfid_module.Rfid(config)
    rfid_obj.reactor = reactor
    rfid_obj.gcode = gcode
    return rfid_obj


# ---------------------------------------------------------------------------
# AST-level checks (no Klipper runtime needed)
# ---------------------------------------------------------------------------

class TestDeferredUidConstantAndInit(unittest.TestCase):
    """Verify the constant and dict are present at the source level."""

    @classmethod
    def setUpClass(cls):
        with open(_RFID_PY, encoding="utf-8") as fh:
            src = fh.read()
        cls.tree = ast.parse(src, filename=_RFID_PY)

    def test_deferred_uid_ttl_constant_defined(self):
        """_DEFERRED_UID_TTL_S must be a module-level constant with a positive value."""
        self.assertTrue(
            hasattr(_rfid_module, "_DEFERRED_UID_TTL_S"),
            "_DEFERRED_UID_TTL_S not found in rfid module",
        )
        self.assertGreater(
            _rfid_module._DEFERRED_UID_TTL_S, 0,
            "_DEFERRED_UID_TTL_S must be positive",
        )

    def test_deferred_uid_initialized_in_init(self):
        """self._deferred_uid must be initialized in Rfid.__init__."""
        rfid_class = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ClassDef) and node.name == "Rfid":
                rfid_class = node
                break
        self.assertIsNotNone(rfid_class, "class Rfid not found")

        found = False
        for method in rfid_class.body:
            if isinstance(method, ast.FunctionDef) and method.name == "__init__":
                for node in ast.walk(method):
                    # Check both plain (ast.Assign) and annotated (ast.AnnAssign)
                    # assignments since the code uses type hints.
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if (
                                isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"
                                and t.attr == "_deferred_uid"
                            ):
                                found = True
                                break
                    elif isinstance(node, ast.AnnAssign):
                        tgt = node.target
                        if (
                            isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                            and tgt.attr == "_deferred_uid"
                        ):
                            found = True
                    if found:
                        break
                break
        self.assertTrue(found, "self._deferred_uid must be initialized in Rfid.__init__")


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------

class TestDeferredUidSavedOnWindowExpiry(unittest.TestCase):
    """_deferred_uid is populated when the scan window expires with a seen UID."""

    def setUp(self):
        self.reactor = _make_reactor(monotonic_value=1000.0)
        self.rfid = _make_rfid(lanes="lane3", reactor=self.reactor)
        self.lane = "lane3"
        # Mock _scan_once so no real SPI access is needed
        self.rfid._scan_once = MagicMock(return_value={"uid_hex": None})

    def _arm_expired_window(self, uid="04684502C62A81"):
        """Set up scan state as if the window ran, saw a tag, but no spoolman_id."""
        lane = self.lane
        gen = 1
        self.rfid._scan_gen[lane] = gen
        self.rfid._scan_seen_uids[lane] = {uid}
        self.rfid._scan_last_uid[lane] = uid
        self.rfid._scan_tick_count[lane] = 5
        self.rfid._scan_no_tag_streak[lane] = 2
        self.rfid._scan_blocked_uids[lane] = set()
        self.rfid._scan_candidates.pop(lane, None)
        # Deadline in the past so the timer fires the expiry branch
        self.rfid._scan_deadlines[lane] = self.reactor.monotonic.return_value - 1.0
        self.rfid._scan_timers[lane] = MagicMock()
        return gen

    def test_deferred_uid_set_on_window_expiry(self):
        """When scan window expires with a seen UID and no pending spoolman_id,
        _deferred_uid[lane] must be populated."""
        uid = "04684502C62A81"
        gen = self._arm_expired_window(uid=uid)

        self.rfid._scan_timer_callback(self.lane, self.reactor.monotonic.return_value, gen)

        self.assertIn(self.lane, self.rfid._deferred_uid,
                      "_deferred_uid not set after scan window expiry")
        entry = self.rfid._deferred_uid[self.lane]
        self.assertEqual(entry["last_uid"], uid)
        self.assertIn(uid, entry["seen_uids"])
        self.assertAlmostEqual(entry["ts"], time.time(), delta=2.0)

    def test_deferred_uid_not_set_when_spoolman_id_already_pending(self):
        """If _pending already has a spoolman_id, _deferred_uid should NOT be set."""
        uid = "04684502C62A81"
        gen = self._arm_expired_window(uid=uid)
        self.rfid._pending[self.lane] = {
            "spoolman_id": 42, "uid_hex": uid, "ts": time.time(), "timeout": 60.0,
        }

        self.rfid._scan_timer_callback(self.lane, self.reactor.monotonic.return_value, gen)

        self.assertNotIn(self.lane, self.rfid._deferred_uid,
                         "_deferred_uid should not be set when pending already has spoolman_id")

    def test_deferred_uid_not_set_when_no_uids_seen(self):
        """If no UIDs were seen during the window, _deferred_uid should NOT be set."""
        gen = self._arm_expired_window(uid="AABBCCDD")
        self.rfid._scan_seen_uids[self.lane] = set()
        self.rfid._scan_last_uid[self.lane] = None

        self.rfid._scan_timer_callback(self.lane, self.reactor.monotonic.return_value, gen)

        self.assertNotIn(self.lane, self.rfid._deferred_uid,
                         "_deferred_uid should not be set when no UIDs were seen")

    def test_scan_state_cleared_after_window_expiry(self):
        """After window expiry, per-lane scan state (seen_uids, last_uid, etc.) is cleared."""
        uid = "04684502C62A81"
        gen = self._arm_expired_window(uid=uid)

        self.rfid._scan_timer_callback(self.lane, self.reactor.monotonic.return_value, gen)

        self.assertNotIn(self.lane, self.rfid._scan_seen_uids)
        self.assertNotIn(self.lane, self.rfid._scan_last_uid)


class TestDeferredUidClearedOnNewWindow(unittest.TestCase):
    """_deferred_uid is cleared when a new scan window starts."""

    def setUp(self):
        self.reactor = _make_reactor(monotonic_value=1000.0)
        self.rfid = _make_rfid(lanes="lane3", reactor=self.reactor)
        self.lane = "lane3"

    def test_new_scan_window_clears_deferred_uid(self):
        """Starting a new scan window must discard the previous deferred UID."""
        self.rfid._deferred_uid[self.lane] = {
            "last_uid": "STALEUID",
            "seen_uids": {"STALEUID"},
            "ts": time.time() - 10.0,
        }

        self.rfid._start_scan_timer(self.lane)

        self.assertNotIn(self.lane, self.rfid._deferred_uid,
                         "_deferred_uid must be cleared when a new scan window starts")


class TestHandleLaneLoadedDeferredFallback(unittest.TestCase):
    """_handle_lane_loaded uses _deferred_uid when scan state was already cleared."""

    def setUp(self):
        self.reactor = _make_reactor(monotonic_value=1000.0)
        self.rfid = _make_rfid(lanes="lane3", reactor=self.reactor)
        self.lane = "lane3"
        # Patch _find_reader_for_lane so it always returns self (this reader)
        self.rfid._find_reader_for_lane = lambda l: (self.rfid, self.lane)

    def _make_lane_obj(self, name="lane3"):
        lane_obj = MagicMock()
        lane_obj.name = name
        return lane_obj

    def _prime_deferred_uid(self, uid="04684502C62A81", age_s=5.0):
        """Pre-populate _deferred_uid as if the scan window just expired."""
        self.rfid._deferred_uid[self.lane] = {
            "last_uid": uid,
            "seen_uids": {uid},
            "ts": time.time() - age_s,
        }

    def test_deferred_uid_recovered_in_lane_loaded(self):
        """lane_loaded must recover seen_uids from _deferred_uid when scan state is empty.

        This is the core regression: previously, if the scan window timer expired
        before lane_loaded arrived, seen_uids_snapshot was empty and no Spoolman
        lookup was triggered.
        """
        uid = "04684502C62A81"
        self._prime_deferred_uid(uid=uid, age_s=5.0)

        # Scan state is already cleared (timer expired before lane_loaded)
        self.assertNotIn(self.lane, self.rfid._scan_seen_uids)
        self.assertNotIn(self.lane, self.rfid._scan_last_uid)
        self.assertNotIn(self.lane, self.rfid._pending)

        # Configure a mock Spoolman client/executor so the fallback path fires
        self.rfid._spoolman = MagicMock()
        self.rfid._spoolman_executor = MagicMock()
        dispatched = []
        self.rfid._spoolman_run_async = lambda fn: dispatched.append(fn)

        self.rfid._handle_lane_loaded(self._make_lane_obj("lane3"))

        self.assertEqual(len(dispatched), 1,
                         "Expected exactly one Spoolman fallback lookup to be dispatched")
        # _deferred_uid should be consumed
        self.assertNotIn(self.lane, self.rfid._deferred_uid)

    def test_deferred_uid_ignored_if_scan_state_still_fresh(self):
        """If scan state snapshots are non-empty, _deferred_uid is discarded."""
        uid_fresh = "FRESHUID1234"
        uid_stale = "STALEUID5678"

        # Both fresh scan state AND a stale deferred entry exist
        self.rfid._scan_seen_uids[self.lane] = {uid_fresh}
        self.rfid._scan_last_uid[self.lane] = uid_fresh
        self.rfid._deferred_uid[self.lane] = {
            "last_uid": uid_stale,
            "seen_uids": {uid_stale},
            "ts": time.time() - 2.0,
        }

        self.rfid._spoolman = MagicMock()
        self.rfid._spoolman_executor = MagicMock()
        self.rfid._spoolman_run_async = MagicMock()

        self.rfid._handle_lane_loaded(self._make_lane_obj("lane3"))

        # The stale deferred entry should be discarded after lane_loaded
        self.assertNotIn(self.lane, self.rfid._deferred_uid)

    def test_deferred_uid_ignored_when_ttl_expired(self):
        """A deferred UID older than _DEFERRED_UID_TTL_S must not be used."""
        uid = "04684502C62A81"
        # Make the entry older than the TTL
        self._prime_deferred_uid(uid=uid, age_s=_rfid_module._DEFERRED_UID_TTL_S + 10.0)

        self.rfid._spoolman = MagicMock()
        self.rfid._spoolman_executor = MagicMock()
        dispatched = []
        self.rfid._spoolman_run_async = lambda fn: dispatched.append(fn)

        self.rfid._handle_lane_loaded(self._make_lane_obj("lane3"))

        self.assertEqual(len(dispatched), 0,
                         "Expired deferred UID must not trigger a Spoolman lookup")
        # Expired entry should be discarded
        self.assertNotIn(self.lane, self.rfid._deferred_uid)

    def test_deferred_uid_consumed_only_once(self):
        """A deferred UID must be consumed (popped) on first use and not reused."""
        uid = "04684502C62A81"
        self._prime_deferred_uid(uid=uid, age_s=2.0)

        self.rfid._spoolman = MagicMock()
        self.rfid._spoolman_executor = MagicMock()
        dispatched = []
        self.rfid._spoolman_run_async = lambda fn: dispatched.append(fn)

        self.rfid._handle_lane_loaded(self._make_lane_obj("lane3"))

        # After first lane_loaded, _deferred_uid must be consumed
        self.assertNotIn(self.lane, self.rfid._deferred_uid)
        self.assertEqual(len(dispatched), 1)

        # A second lane_loaded must not trigger another lookup
        dispatched.clear()
        self.rfid._handle_lane_loaded(self._make_lane_obj("lane3"))
        self.assertEqual(len(dispatched), 0,
                         "Second lane_loaded must not dispatch another lookup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
