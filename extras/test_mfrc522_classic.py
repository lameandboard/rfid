"""Tests for MIFARE Classic (Bambu tag) fallback read path.

Validates that:
  1. read_all_tags() detects MIFARE Classic tags (SAK & 0x08) via SAK, skips
     the Type-2 page reads, and returns a uid-only entry with sak field set.
  2. Normal NTAG/Ultralight (SAK 0x00) tags still go through the page-read path
     (no regression).
  3. _scan_once() in rfid.py calls _try_bambu_read() for Classic tags that have
     raw_len=0 and sak & 0x08 set.
  4. When _try_bambu_read succeeds, the result is passed to _apply_tag_parser and
     the tag entry is updated (raw_bytes, raw_len, spoolman_id).
  5. When _try_bambu_read fails (e.g. pycryptodome missing), the scan still
     returns the uid without crashing and without raw data.

Run with:
    python3 extras/test_mfrc522_classic.py
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Path setup — mirror the other test files in this directory
# ---------------------------------------------------------------------------
_EXTRAS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_EXTRAS_DIR)
for _p in (_REPO_ROOT, _EXTRAS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Stub Klipper bus module (unavailable outside Klipper)
_BUS_MOD = MagicMock()
_BUS_MOD.MCU_SPI_from_config = MagicMock(return_value=MagicMock())
sys.modules["extras.bus"] = _BUS_MOD

import extras.rfid as _rfid_module  # noqa: E402
from extras.mfrc522 import MFRC522Device  # noqa: E402

# ---------------------------------------------------------------------------
# MFRC522Device test helpers
# ---------------------------------------------------------------------------

def _make_spi():
    """Minimal SPI mock: send() returns a zero-padded response list."""
    spi = MagicMock()
    spi.spi_transfer = MagicMock(return_value=[0x00] * 32)
    return spi


def _make_device():
    """Return an MFRC522Device wired to a mock SPI."""
    spi = _make_spi()
    dev = MFRC522Device(spi, reactor=None)
    dev._initialized = True  # skip hardware init
    return dev


# ---------------------------------------------------------------------------
# Tests: MFRC522Device.read_all_tags — MIFARE Classic detection
# ---------------------------------------------------------------------------

class TestReadAllTagsClassicDetection(unittest.TestCase):
    """read_all_tags() should detect MIFARE Classic via SAK and skip page reads."""

    def _make_classic_device(self, uid_bytes=None, sak=0x08):
        """Return a device whose anticoll/select reports a Classic tag."""
        if uid_bytes is None:
            uid_bytes = [0xC2, 0xC3, 0x04, 0xEB]
        dev = _make_device()

        # Patch request to succeed (simulates tag present)
        dev.request = MagicMock(return_value=(dev.MI_OK, [0x04, 0x00]))
        # Patch anticoll+select to return the given UID and SAK
        dev._anticoll_and_select_with_sak = MagicMock(return_value=(uid_bytes, sak))
        # read_4pages should never be called for Classic
        dev.read_4pages = MagicMock(return_value=b"\x00" * 16)
        dev.halt_tag = MagicMock()
        dev._rf_reset = MagicMock()
        dev._clear_mask = MagicMock()
        dev._write_reg = MagicMock()
        return dev

    def test_classic_tag_returns_uid_only_entry(self):
        """For SAK 0x08, read_all_tags must return a uid-only entry (no page reads)."""
        uid = [0xC2, 0xC3, 0x04, 0xEB]
        dev = self._make_classic_device(uid_bytes=uid, sak=0x08)

        tags = dev.read_all_tags(max_pages=5)

        self.assertEqual(len(tags), 1, "Expected exactly one tag entry")
        tag = tags[0]
        self.assertEqual(tag["uid_hex"], "C2C304EB")
        self.assertEqual(tag["raw_len"], 0)
        self.assertIsNone(tag["raw_bytes"])
        self.assertEqual(tag["sak"], 0x08)
        # read_4pages must NOT have been called
        dev.read_4pages.assert_not_called()

    def test_classic_4k_tag_also_skips_page_reads(self):
        """SAK 0x18 (MIFARE Classic 4K) must also be detected and skip page reads."""
        uid = [0xAA, 0xBB, 0xCC, 0xDD]
        dev = self._make_classic_device(uid_bytes=uid, sak=0x18)

        tags = dev.read_all_tags(max_pages=5)

        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0]["sak"], 0x18)
        self.assertEqual(tags[0]["raw_len"], 0)
        dev.read_4pages.assert_not_called()

    def test_ntag_tag_still_reads_pages(self):
        """SAK 0x00 (NTAG/Ultralight) must still use the page-read path (no regression)."""
        uid = [0x01, 0x02, 0x03, 0x04]
        dev = self._make_classic_device(uid_bytes=uid, sak=0x00)
        # Return 16 bytes of page data so the read loop terminates cleanly
        dev.read_4pages = MagicMock(return_value=b"\x00" * 16)
        # Make read loop stop after one successful read by having max_pages=4
        # and then returning None on the second call (simulates end of tag).
        dev.read_4pages = MagicMock(side_effect=[b"\x00" * 16, None])

        tags = dev.read_all_tags(max_pages=8)

        self.assertEqual(len(tags), 1)
        # For NTAG, read_4pages must be called
        dev.read_4pages.assert_called()
        self.assertEqual(tags[0]["sak"], 0x00)

    def test_classic_tag_is_halted_after_detection(self):
        """Classic tag must be halted (not left selected) after being skipped."""
        dev = self._make_classic_device(sak=0x08)
        dev.read_all_tags(max_pages=5)
        dev.halt_tag.assert_called()

    def test_uid_only_entry_has_expected_schema(self):
        """uid-only entry must contain the full expected schema keys."""
        dev = self._make_classic_device(sak=0x08)
        tags = dev.read_all_tags(max_pages=5)
        tag = tags[0]
        for key in ("uid", "uid_hex", "raw_bytes", "raw_len", "spoolman_id", "tag_text", "sak"):
            self.assertIn(key, tag, f"Expected key '{key}' in tag dict")


class TestReadMifareClassicWupaFallback(unittest.TestCase):
    """read_mifare_classic_tag() should fall back to WUPA when REQA misses (halted tag)."""

    def test_reqa_miss_tries_wupa(self):
        """When REQA finds nothing, WUPA must be attempted to wake halted tags."""
        dev = _make_device()

        reqa_called = []
        wupa_called = []

        def _mock_request(mode):
            if mode == dev.PICC_REQA:
                reqa_called.append(True)
                return (dev.MI_NOTAGERR, None)
            if mode == dev.PICC_WUPA:
                wupa_called.append(True)
                return (dev.MI_NOTAGERR, None)  # still no tag
            return (dev.MI_NOTAGERR, None)

        dev.request = _mock_request
        dev._anticoll_and_select = MagicMock(return_value=None)

        result = dev.read_mifare_classic_tag(key_list=[bytes(6)] * 16)

        self.assertIsNone(result, "Should return None when both REQA and WUPA fail")
        self.assertTrue(reqa_called, "REQA must have been attempted")
        self.assertTrue(wupa_called, "WUPA must have been attempted after REQA miss")

    def test_reqa_success_skips_wupa(self):
        """When REQA succeeds, WUPA must NOT be sent."""
        dev = _make_device()
        reqa_calls = []
        wupa_calls = []

        def _mock_request(mode):
            if mode == dev.PICC_REQA:
                reqa_calls.append(True)
                return (dev.MI_OK, [0x04, 0x00])
            if mode == dev.PICC_WUPA:
                wupa_calls.append(True)
                return (dev.MI_NOTAGERR, None)
            return (dev.MI_NOTAGERR, None)

        dev.request = _mock_request
        dev._anticoll_and_select = MagicMock(return_value=None)

        dev.read_mifare_classic_tag(key_list=[bytes(6)] * 16)

        self.assertTrue(reqa_calls)
        self.assertFalse(wupa_calls, "WUPA must NOT be attempted when REQA succeeds")


# ---------------------------------------------------------------------------
# Rfid._scan_once tests — Bambu fallback triggered for Classic tags
# ---------------------------------------------------------------------------

def _make_rfid(lanes="lane3", spoolman_url=""):
    """Instantiate a minimal Rfid instance (mirrors test_deferred_uid.py helper)."""
    reactor = MagicMock()
    reactor.monotonic.return_value = 1000.0
    reactor.NEVER = float("inf")
    reactor.register_timer.return_value = MagicMock()
    reactor.register_async_callback = MagicMock()

    printer = MagicMock()
    printer.get_reactor.return_value = reactor
    printer.lookup_object.return_value = MagicMock()
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
    rfid_obj.gcode = MagicMock()
    return rfid_obj


def _classic_tag_entry(uid_hex="C2C304EB", sak=0x08):
    """Return a uid-only tag dict as produced by read_all_tags() for a Classic tag."""
    uid = [int(uid_hex[i:i+2], 16) for i in range(0, len(uid_hex), 2)]
    return {
        "uid": uid,
        "uid_hex": uid_hex,
        "raw_bytes": None,
        "raw_len": 0,
        "spoolman_id": None,
        "tag_text": "",
        "sak": sak,
    }


# MIFARE Classic 1K data blocks: 16 sectors × 3 data blocks per sector
# (sector trailer is not included in authenticated block reads).
_MIFARE_CLASSIC_1K_DATA_BLOCKS = 48  # 16 sectors × 3 data blocks

_FAKE_BAMBU_BLOCKS = {
    "uid_bytes": bytes.fromhex("C2C304EB"),
    "uid_hex": "C2C304EB",
    "blocks": {i: bytes(16) for i in range(_MIFARE_CLASSIC_1K_DATA_BLOCKS)},
}


class TestScanOnceClassicFallback(unittest.TestCase):
    """_scan_once() must attempt Bambu read for MIFARE Classic tags."""

    def setUp(self):
        self.rfid = _make_rfid(lanes="lane3")
        self.lane = "lane3"
        # Clear the module-level UID spool cache so tests don't interfere
        # with each other via cache hits that bypass the Bambu fallback path.
        _rfid_module._UID_SPOOL_CACHE.clear()

    def _wire_reader(self, tags):
        """Make reader.read_all_tags return the given tag list."""
        self.rfid.reader = MagicMock()
        self.rfid.reader.read_all_tags = MagicMock(return_value=tags)
        # Ensure uid_fast_scan is disabled to force read_all_tags path
        self.rfid.uid_fast_scan = False

    def test_classic_tag_triggers_bambu_read(self):
        """_scan_once must call _try_bambu_read for a MIFARE Classic uid-only entry."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read.assert_called_once_with("C2C304EB")

    def test_classic_tag_bambu_read_succeeds_returns_uid_and_raw(self):
        """When Bambu read succeeds, result must include uid_hex and non-zero raw_len."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read = MagicMock(return_value=_FAKE_BAMBU_BLOCKS)
        self.rfid._apply_tag_parser = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.assertEqual(result.get("uid_hex"), "C2C304EB")
        self.assertGreater(result.get("raw_len", 0), 0,
                           "raw_len must be non-zero after successful Bambu read")
        self.assertEqual(result.get("raw_bytes"), _FAKE_BAMBU_BLOCKS)

    def test_classic_tag_bambu_read_parses_spoolman_id(self):
        """Parsed spoolman_id from Bambu blocks must appear in the scan result."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read = MagicMock(return_value=_FAKE_BAMBU_BLOCKS)
        self.rfid._apply_tag_parser = MagicMock(return_value={"spoolman_id": 42, "tag_format": "bambu"})

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.assertEqual(result.get("spoolman_id"), 42)

    def test_classic_tag_bambu_read_fails_returns_uid_no_crash(self):
        """When Bambu read fails, _scan_once must still return the uid without crashing."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        # uid must be present (we still detected the tag even if Bambu read failed)
        self.assertEqual(result.get("uid_hex"), "C2C304EB")
        self.assertIsNone(result.get("spoolman_id"))

    def test_ntag_tag_does_not_trigger_bambu_read(self):
        """For NTAG/Ultralight (SAK 0x00), _try_bambu_read must NOT be called."""
        ntag_entry = {
            "uid": [0x01, 0x02, 0x03, 0x04],
            "uid_hex": "01020304",
            "raw_bytes": b"\x00" * 32,
            "raw_len": 32,
            "spoolman_id": None,
            "tag_text": "",
            "sak": 0x00,
        }
        self._wire_reader([ntag_entry])
        self.rfid._try_bambu_read = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read.assert_not_called()

    def test_classic_tag_with_raw_bytes_already_set_skips_bambu_read(self):
        """If a Classic tag already has raw_bytes (e.g. from a cache hit), skip Bambu read."""
        entry = _classic_tag_entry()
        entry["raw_bytes"] = b"\x01" * 16
        entry["raw_len"] = 16
        self._wire_reader([entry])
        self.rfid._try_bambu_read = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read.assert_not_called()

    def test_no_tags_no_bambu_read(self):
        """When read_all_tags returns [], _try_bambu_read must not be called."""
        self._wire_reader([])
        self.rfid._try_bambu_read = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read.assert_not_called()
        self.assertIsNone(result.get("uid_hex"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
