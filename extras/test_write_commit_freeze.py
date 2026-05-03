"""Tests for the commit-freeze fix in RFID_WRITE and RFID_BAMBU_WRITE.

Validates that when ``_commit_in_progress`` is True for a lane (left over from
a previous scan/commit cycle), calling ``RFID_WRITE`` or ``RFID_BAMBU_WRITE``
clears the freeze and proceeds to call ``_scan_once``, rather than having
``_scan_once`` bail out silently.

Run with:
    python3 extras/test_write_commit_freeze.py
"""

import os
import sys
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


def _make_rfid(lanes="lane2", spoolman_url="", reactor=None):
    """Instantiate a minimal Rfid instance with enough mocking to work."""
    if reactor is None:
        reactor = _make_reactor()

    printer = MagicMock()
    printer.get_reactor.return_value = reactor
    gcode = MagicMock()
    printer.lookup_object.return_value = gcode
    printer.lookup_objects.return_value = []

    config = MagicMock()
    config.get_name.return_value = "rfid mfrc522_0"
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


def _make_gcmd(lane="2", spool_id=42, tray_uid=None):
    """Build a minimal GCode command mock for RFID_WRITE / RFID_BAMBU_WRITE."""
    gcmd = MagicMock()

    def _get(key, default=None, **kw):
        if key == "LANE":
            return lane
        if key == "SLOT":
            return None
        if key == "TRAY_UID":
            return tray_uid
        return default

    gcmd.get.side_effect = _get
    gcmd.get_int.return_value = spool_id
    gcmd.respond_info = MagicMock()
    gcmd.error.side_effect = lambda msg: Exception(msg)
    return gcmd


# ---------------------------------------------------------------------------
# Tests: _prepare_lane_for_explicit_write helper
# ---------------------------------------------------------------------------

class TestPrepareLaneForExplicitWrite(unittest.TestCase):
    """Unit tests for the _prepare_lane_for_explicit_write helper."""

    def setUp(self):
        self.reactor = _make_reactor()
        self.rfid = _make_rfid(lanes="lane2", reactor=self.reactor)
        self.lane = "lane2"

    def test_clears_commit_freeze(self):
        """_prepare_lane_for_explicit_write must clear _commit_in_progress for the lane."""
        self.rfid._commit_in_progress[self.lane] = True
        self.rfid._prepare_lane_for_explicit_write(self.lane, reason="test")
        self.assertFalse(
            self.rfid._commit_in_progress.get(self.lane, False),
            "_commit_in_progress must be cleared by _prepare_lane_for_explicit_write",
        )

    def test_stops_active_scan_timer(self):
        """_prepare_lane_for_explicit_write must cancel any active scan timer."""
        timer_handle = MagicMock()
        self.rfid._scan_timers[self.lane] = timer_handle

        self.rfid._prepare_lane_for_explicit_write(self.lane, reason="test")

        self.reactor.unregister_timer.assert_called_once_with(timer_handle)
        self.assertNotIn(self.lane, self.rfid._scan_timers,
                         "scan timer must be removed from _scan_timers")

    def test_noop_when_no_freeze_or_timer(self):
        """_prepare_lane_for_explicit_write must not crash when nothing needs clearing."""
        # Should not raise
        self.rfid._prepare_lane_for_explicit_write(self.lane, reason="test")

    def test_clears_stale_pending(self):
        """_prepare_lane_for_explicit_write must clear any stale _pending entry for the lane."""
        self.rfid._pending[self.lane] = {
            "lane": self.lane,
            "spoolman_id": 99,
            "uid_hex": "CAFEBABE",
        }
        self.rfid._prepare_lane_for_explicit_write(self.lane, reason="test")
        self.assertNotIn(
            self.lane,
            self.rfid._pending,
            "_pending must be cleared by _prepare_lane_for_explicit_write",
        )

    def test_clears_freeze_and_timer_together(self):
        """_prepare_lane_for_explicit_write must handle both freeze and timer at once."""
        self.rfid._commit_in_progress[self.lane] = True
        timer_handle = MagicMock()
        self.rfid._scan_timers[self.lane] = timer_handle

        self.rfid._prepare_lane_for_explicit_write(self.lane, reason="test")

        self.assertFalse(self.rfid._commit_in_progress.get(self.lane, False))
        self.assertNotIn(self.lane, self.rfid._scan_timers)
        self.reactor.unregister_timer.assert_called_once_with(timer_handle)


# ---------------------------------------------------------------------------
# Tests: cmd_RFID_WRITE clears commit-freeze
# ---------------------------------------------------------------------------

class TestRfidWriteClearsCommitFreeze(unittest.TestCase):
    """cmd_RFID_WRITE must clear _commit_in_progress and call _scan_once."""

    def setUp(self):
        self.reactor = _make_reactor()
        self.rfid = _make_rfid(lanes="lane2", reactor=self.reactor)
        self.port = "lane2"

        # Make _find_reader_for_port return this rfid instance for lane2
        self.rfid._find_reader_for_port = MagicMock(
            return_value=(self.rfid, self.port)
        )

        # Mock _scan_once to return a minimal non-None result without SPI access
        self.rfid._scan_once = MagicMock(return_value={
            "lane": self.port,
            "uid_hex": "AABBCCDD",
            "tag_text": None,
            "raw_len": 0,
            "raw_bytes": b"",
            "spoolman_id": None,
        })

        # Mock _fetch_spoolman_spool to return None so the command exits early
        # (after the scan step we care about) without needing a live Spoolman.
        self.rfid._fetch_spoolman_spool = MagicMock(return_value=None)

    def test_commit_freeze_cleared_before_scan(self):
        """cmd_RFID_WRITE must clear _commit_in_progress before calling _scan_once."""
        self.rfid._commit_in_progress[self.port] = True

        gcmd = _make_gcmd(lane="2", spool_id=42)
        self.rfid.cmd_RFID_WRITE(gcmd)

        self.assertFalse(
            self.rfid._commit_in_progress.get(self.port, False),
            "_commit_in_progress must be False after RFID_WRITE",
        )

    def test_scan_once_called_despite_prior_freeze(self):
        """cmd_RFID_WRITE must call _scan_once even if _commit_in_progress was True."""
        self.rfid._commit_in_progress[self.port] = True

        gcmd = _make_gcmd(lane="2", spool_id=42)
        self.rfid.cmd_RFID_WRITE(gcmd)

        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_stale_pending_scan_cleared_by_explicit_write(self):
        """cmd_RFID_WRITE must discard any stale pending assignment from a prior scan."""
        self.rfid._pending[self.port] = {
            "lane": self.port,
            "spoolman_id": 7,
            "uid_hex": "DEADBEEF",
        }

        gcmd = _make_gcmd(lane="2", spool_id=42)
        self.rfid.cmd_RFID_WRITE(gcmd)

        self.assertNotIn(
            self.port,
            self.rfid._pending,
            "RFID_WRITE must clear stale _pending state so an old scan cannot commit later",
        )
        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_scan_once_called_without_prior_freeze(self):
        """cmd_RFID_WRITE must also call _scan_once when no freeze was active."""
        # Ensure no freeze is set
        self.rfid._commit_in_progress.pop(self.port, None)

        gcmd = _make_gcmd(lane="2", spool_id=42)
        self.rfid.cmd_RFID_WRITE(gcmd)

        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_active_scan_timer_stopped_before_scan(self):
        """cmd_RFID_WRITE must stop any active scan timer before calling _scan_once."""
        timer_handle = MagicMock()
        self.rfid._scan_timers[self.port] = timer_handle

        gcmd = _make_gcmd(lane="2", spool_id=42)
        self.rfid.cmd_RFID_WRITE(gcmd)

        self.reactor.unregister_timer.assert_called_with(timer_handle)
        self.assertNotIn(self.port, self.rfid._scan_timers)
        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)


# ---------------------------------------------------------------------------
# Tests: cmd_RFID_BAMBU_WRITE clears commit-freeze
# ---------------------------------------------------------------------------

class TestRfidBambuWriteClearsCommitFreeze(unittest.TestCase):
    """cmd_RFID_BAMBU_WRITE must clear _commit_in_progress and call _scan_once."""

    def setUp(self):
        self.reactor = _make_reactor()
        self.rfid = _make_rfid(lanes="lane2", reactor=self.reactor)
        self.port = "lane2"

        # Make _find_reader_for_port return this rfid instance for lane2
        self.rfid._find_reader_for_port = MagicMock(
            return_value=(self.rfid, self.port)
        )

        # Mock _scan_once to return no tag (so command exits early without a
        # real Bambu write operation, but after the scan step we care about).
        self.rfid._scan_once = MagicMock(return_value={
            "lane": self.port,
            "uid_hex": None,
            "tag_text": None,
            "raw_len": 0,
            "raw_bytes": b"",
            "spoolman_id": None,
        })

    def test_commit_freeze_cleared_before_scan(self):
        """cmd_RFID_BAMBU_WRITE must clear _commit_in_progress before calling _scan_once."""
        self.rfid._commit_in_progress[self.port] = True

        gcmd = _make_gcmd(lane="2")
        self.rfid.cmd_RFID_BAMBU_WRITE(gcmd)

        self.assertFalse(
            self.rfid._commit_in_progress.get(self.port, False),
            "_commit_in_progress must be False after RFID_BAMBU_WRITE",
        )

    def test_scan_once_called_despite_prior_freeze(self):
        """cmd_RFID_BAMBU_WRITE must call _scan_once even if _commit_in_progress was True."""
        self.rfid._commit_in_progress[self.port] = True

        gcmd = _make_gcmd(lane="2")
        self.rfid.cmd_RFID_BAMBU_WRITE(gcmd)

        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_scan_once_called_without_prior_freeze(self):
        """cmd_RFID_BAMBU_WRITE must also call _scan_once when no freeze was active."""
        self.rfid._commit_in_progress.pop(self.port, None)

        gcmd = _make_gcmd(lane="2")
        self.rfid.cmd_RFID_BAMBU_WRITE(gcmd)

        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_stale_pending_cleared_by_explicit_bambu_write(self):
        """cmd_RFID_BAMBU_WRITE must discard any stale pending assignment from a prior scan."""
        self.rfid._pending[self.port] = {
            "lane": self.port,
            "spoolman_id": 7,
            "uid_hex": "DEADBEEF",
        }

        gcmd = _make_gcmd(lane="2")
        self.rfid.cmd_RFID_BAMBU_WRITE(gcmd)

        self.assertNotIn(
            self.port,
            self.rfid._pending,
            "RFID_BAMBU_WRITE must clear stale _pending state so an old scan cannot commit later",
        )
        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)

    def test_active_scan_timer_stopped_before_scan(self):
        """cmd_RFID_BAMBU_WRITE must stop any active scan timer before calling _scan_once."""
        timer_handle = MagicMock()
        self.rfid._scan_timers[self.port] = timer_handle

        gcmd = _make_gcmd(lane="2")
        self.rfid.cmd_RFID_BAMBU_WRITE(gcmd)

        self.reactor.unregister_timer.assert_called_with(timer_handle)
        self.assertNotIn(self.port, self.rfid._scan_timers)
        self.rfid._scan_once.assert_called_once_with(self.port, max_pages=4)


# ---------------------------------------------------------------------------
# Tests: regression — normal scan/commit cycle is unaffected
# ---------------------------------------------------------------------------

class TestNormalScanCommitUnaffected(unittest.TestCase):
    """Ensure that the normal scan/commit flow is not broken by the fix."""

    def setUp(self):
        self.reactor = _make_reactor()
        self.rfid = _make_rfid(lanes="lane2", reactor=self.reactor)
        self.lane = "lane2"

    def test_start_scan_timer_still_clears_commit_freeze(self):
        """_start_scan_timer must still clear _commit_in_progress (existing behaviour)."""
        self.rfid._commit_in_progress[self.lane] = True
        self.rfid._start_scan_timer(self.lane)
        self.assertFalse(
            self.rfid._commit_in_progress.get(self.lane, False),
            "_start_scan_timer must clear _commit_in_progress",
        )

    def test_end_scan_session_clears_scan_state(self):
        """_end_scan_session must still clear scan state (existing behaviour)."""
        timer_handle = MagicMock()
        self.rfid._scan_timers[self.lane] = timer_handle
        self.rfid._scan_seen_uids[self.lane] = {"AABBCCDD"}

        self.rfid._end_scan_session(self.lane, reason="test")

        self.assertNotIn(self.lane, self.rfid._scan_timers)
        self.assertNotIn(self.lane, self.rfid._scan_seen_uids)

    def test_scan_once_blocked_when_commit_freeze_set_outside_write(self):
        """_scan_once must still be blocked by _commit_in_progress when NOT called from write."""
        self.rfid._commit_in_progress[self.lane] = True
        result = self.rfid._scan_once(self.lane, max_pages=4)
        self.assertIsNone(
            result.get("uid_hex"),
            "_scan_once must return no UID when commit_in_progress is set",
        )


if __name__ == "__main__":
    unittest.main()
