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


# ---------------------------------------------------------------------------
# Tests: pending spool assignment (race-proof lane_loaded / spool creation)
# ---------------------------------------------------------------------------

class TestPendingSpoolAssignment(unittest.TestCase):
    """_on_created must store spool_id in _pending and only commit once
    lane_loaded has also fired — regardless of which event comes first.

    The helper _run_do_auto_create_spool() exercises the real production
    code path: it stubs the Spoolman client, calls _do_auto_create_spool()
    synchronously (bypassing the thread-pool), captures the async callback
    that _do_auto_create_spool registers on the reactor, and then invokes it
    so tests can assert on _pending / run_script_from_command state.
    """

    def setUp(self):
        self.reactor = _make_reactor(monotonic_value=1000.0)
        self.rfid = _make_rfid(lanes="lane1", reactor=self.reactor)
        self.lane = "lane1"
        self.rfid._find_reader_for_lane = lambda l: (self.rfid, self.lane)

        # Stub Spoolman client so auto_create_spool returns a known spool_id
        # without making real HTTP calls.
        mock_client = MagicMock()
        mock_client.auto_create_spool.return_value = 42
        mock_client.find_spool_by_uid.return_value = None
        mock_client.add_uid_to_spool.return_value = True
        self.rfid._spoolman = mock_client
        self.rfid._spoolman_executor = MagicMock()  # present but not used below

    def _make_lane_obj(self, name="lane1"):
        lo = MagicMock()
        lo.name = name
        return lo

    def _run_do_auto_create_spool(self, spool_id=42, uid="AABB1122",
                                   tag_data=None):
        """Call _do_auto_create_spool() with the Spoolman mock returning
        *spool_id*, capture the reactor async callback it registers, invoke
        that callback synchronously, and return the captured spool_id."""
        if tag_data is None:
            tag_data = {"protocol": "openspool", "material": "PLA",
                        "weight_g": 1000.0}
        # Override auto_create_spool to return the requested spool_id
        self.rfid._spoolman.auto_create_spool.return_value = spool_id

        # Capture the callback registered with reactor.register_async_callback
        captured = []
        self.rfid.reactor.register_async_callback = lambda fn: captured.append(fn)

        # Run _do_auto_create_spool synchronously (it's normally called from a
        # thread-pool worker, but it is safe to call directly in tests).
        self.rfid._do_auto_create_spool(self.lane, uid, tag_data)

        # There must be exactly one registered callback (_on_created)
        self.assertEqual(len(captured), 1,
                         "_do_auto_create_spool must register exactly one "
                         "reactor async callback (_on_created)")

        # Invoke the callback as the reactor would
        captured[0](event_time=0.0)
        return spool_id

    # ------------------------------------------------------------------
    # Case A: spool creation finishes BEFORE lane_loaded fires
    # ------------------------------------------------------------------
    def test_on_created_before_lane_loaded_does_not_commit_immediately(self):
        """When lane_loaded has not yet fired, _on_created must NOT call
        RFID_SCAN_COMMIT — it stores the spool_id in _pending and waits."""
        # lane_loaded has not fired; _start_scan_timer sets the flag to False
        self.rfid._lane_loaded_seen[self.lane] = False

        self._run_do_auto_create_spool(spool_id=99)

        # spool_id must be in _pending
        self.assertIn(self.lane, self.rfid._pending)
        self.assertEqual(self.rfid._pending[self.lane]["spoolman_id"], 99)
        # RFID_SCAN_COMMIT must NOT have been called
        for call_args in self.rfid.gcode.run_script_from_command.call_args_list:
            self.assertNotIn("RFID_SCAN_COMMIT", str(call_args))

    def test_lane_loaded_after_on_created_triggers_commit(self):
        """When lane_loaded fires AFTER spool creation, _handle_lane_loaded
        must find the spool_id in _pending and schedule RFID_SCAN_COMMIT."""
        self.rfid._lane_loaded_seen[self.lane] = False
        self._run_do_auto_create_spool(spool_id=99)

        # Now lane_loaded fires — collect callbacks registered from here on
        callbacks_registered = []
        self.rfid.reactor.register_async_callback = lambda fn: callbacks_registered.append(fn)

        self.rfid._handle_lane_loaded(self._make_lane_obj("lane1"))

        # _lane_loaded_seen must now be True
        self.assertTrue(self.rfid._lane_loaded_seen.get(self.lane))
        # An async callback must have been registered to run RFID_SCAN_COMMIT
        self.assertGreater(len(callbacks_registered), 0,
                           "Expected an async commit callback to be registered")

    # ------------------------------------------------------------------
    # Case B: lane_loaded fires BEFORE spool creation finishes
    # ------------------------------------------------------------------
    def test_lane_loaded_before_on_created_sets_flag(self):
        """When lane_loaded fires before spool creation, _lane_loaded_seen
        must be set to True so _on_created can commit immediately afterward."""
        # Simulate lane_loaded firing with no pending entry yet
        self.rfid._handle_lane_loaded(self._make_lane_obj("lane1"))

        self.assertTrue(
            self.rfid._lane_loaded_seen.get(self.lane),
            "_lane_loaded_seen must be True after lane_loaded fires",
        )

    def test_on_created_after_lane_loaded_commits_immediately(self):
        """When _on_created fires after lane_loaded, it must call
        RFID_SCAN_COMMIT immediately because the lane is already loaded."""
        # lane_loaded fires first (no pending yet)
        self.rfid._handle_lane_loaded(self._make_lane_obj("lane1"))
        self.assertTrue(self.rfid._lane_loaded_seen.get(self.lane))

        # Reset gcode mock so we only see calls made by _on_created
        self.rfid.gcode.run_script_from_command.reset_mock()

        # Spool creation finishes — _on_created should see the flag and commit
        self._run_do_auto_create_spool(spool_id=77)

        calls = [str(c) for c in self.rfid.gcode.run_script_from_command.call_args_list]
        self.assertTrue(
            any("RFID_SCAN_COMMIT" in c for c in calls),
            "RFID_SCAN_COMMIT must be called when _on_created fires after lane_loaded",
        )

    # ------------------------------------------------------------------
    # GCode sync-scan path must not be gated on lane_loaded
    # ------------------------------------------------------------------
    def test_sync_scan_lane_commits_immediately(self):
        """For synchronous GCode scans (lane in _sync_scan_lanes) _on_created
        must commit immediately regardless of _lane_loaded_seen."""
        # lane_loaded has NOT fired
        self.rfid._lane_loaded_seen[self.lane] = False
        # Mark lane as a sync scan lane (as _run_scan_window_sync would do)
        self.rfid._sync_scan_lanes.add(self.lane)

        self.rfid.gcode.run_script_from_command.reset_mock()
        self._run_do_auto_create_spool(spool_id=55)

        calls = [str(c) for c in self.rfid.gcode.run_script_from_command.call_args_list]
        self.assertTrue(
            any("RFID_SCAN_COMMIT" in c for c in calls),
            "Sync-scan path must commit immediately without waiting for lane_loaded",
        )

    # ------------------------------------------------------------------
    # _start_scan_timer resets _lane_loaded_seen to False (not pop) for
    # the new cycle, so the guard can distinguish "mid-cycle" from "none".
    # ------------------------------------------------------------------
    def test_start_scan_timer_resets_lane_loaded_seen_to_false(self):
        """_start_scan_timer must set _lane_loaded_seen to False (not remove
        the key) so that the no-double-scan guard can distinguish an active
        AFC cycle (False) from no cycle tracking at all (key missing)."""
        self.rfid._lane_loaded_seen[self.lane] = True

        self.rfid._start_scan_timer(self.lane)

        self.assertIn(self.lane, self.rfid._lane_loaded_seen,
                      "_start_scan_timer must keep _lane_loaded_seen key present")
        self.assertIs(self.rfid._lane_loaded_seen[self.lane], False,
                      "_start_scan_timer must set _lane_loaded_seen to False")


# ---------------------------------------------------------------------------
# Tests: no-double-scan guard in _handle_lane_prep_start
# ---------------------------------------------------------------------------

class TestNoDoubleScanGuard(unittest.TestCase):
    """After a lane is committed within a load cycle, a second lane_prep_start
    must not restart the scan timer until the cycle resets."""

    def setUp(self):
        self.reactor = _make_reactor(monotonic_value=1000.0)
        self.rfid = _make_rfid(lanes="lane2", reactor=self.reactor)
        self.lane = "lane2"
        self.rfid._find_reader_for_lane = lambda l: (self.rfid, self.lane)

    def _make_lane_obj(self, name="lane2"):
        lo = MagicMock()
        lo.name = name
        return lo

    def test_prep_start_starts_scan_when_not_committed(self):
        """A normal lane_prep_start (no prior commit) must start the scan timer."""
        timer_started = []
        original = self.rfid._start_scan_timer
        self.rfid._start_scan_timer = lambda l: timer_started.append(l) or original(l)

        self.rfid._handle_lane_prep_start(self._make_lane_obj("lane2"))

        self.assertIn(self.lane, timer_started,
                      "_start_scan_timer must be called on the first prep_start")

    def test_prep_start_blocked_when_committed_before_lane_loaded(self):
        """If the lane was already committed this cycle (tag confirmed) but
        lane_loaded has not yet fired, a second prep_start must be ignored."""
        # Simulate a committed lane mid-cycle (tag found, but lane not yet loaded)
        self.rfid._lane_committed[self.lane] = True
        self.rfid._lane_loaded_seen[self.lane] = False  # load not finished yet

        timer_started = []
        self.rfid._start_scan_timer = lambda l: timer_started.append(l)

        self.rfid._handle_lane_prep_start(self._make_lane_obj("lane2"))

        self.assertNotIn(self.lane, timer_started,
                         "_start_scan_timer must NOT be called when lane is already "
                         "committed within the current load cycle")

    def test_prep_start_not_blocked_when_lane_loaded_seen_key_missing(self):
        """A committed lane whose _lane_loaded_seen key is missing (no active
        AFC cycle — e.g. committed via a standalone RFID_SCAN command) must
        NOT be blocked by the no-double-scan guard.  The guard only applies
        when the flag is explicitly False (set by _start_scan_timer), not when
        the key is absent."""
        self.rfid._lane_committed[self.lane] = True
        # Key is absent — simulates a commit that happened outside an AFC cycle
        self.rfid._lane_loaded_seen.pop(self.lane, None)

        timer_started = []
        original = self.rfid._start_scan_timer
        self.rfid._start_scan_timer = lambda l: timer_started.append(l) or original(l)

        self.rfid._handle_lane_prep_start(self._make_lane_obj("lane2"))

        self.assertIn(self.lane, timer_started,
                      "_start_scan_timer must be called when _lane_loaded_seen "
                      "key is missing (not an active AFC cycle)")



