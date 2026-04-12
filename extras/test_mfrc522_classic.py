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
import extras.rfid_tag_parser as _rtp  # noqa: E402
from extras.mfrc522 import MFRC522Device  # noqa: E402

_PYCRYPTODOME_OK = _rtp._PYCRYPTODOME_OK

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
        """_scan_once must call _try_bambu_read_with_fallback for a MIFARE Classic uid-only entry."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_called_once_with("C2C304EB", round_num=1)

    def test_classic_tag_bambu_read_succeeds_returns_uid_and_raw(self):
        """When Bambu read succeeds, result must include uid_hex and non-zero raw_len."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=_FAKE_BAMBU_BLOCKS)
        self.rfid._apply_tag_parser = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.assertEqual(result.get("uid_hex"), "C2C304EB")
        self.assertGreater(result.get("raw_len", 0), 0,
                           "raw_len must be non-zero after successful Bambu read")
        self.assertEqual(result.get("raw_bytes"), _FAKE_BAMBU_BLOCKS)

    def test_classic_tag_bambu_read_parses_spoolman_id(self):
        """Parsed spoolman_id from Bambu blocks must appear in the scan result."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=_FAKE_BAMBU_BLOCKS)
        self.rfid._apply_tag_parser = MagicMock(return_value={"spoolman_id": 42, "tag_format": "bambu"})

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.assertEqual(result.get("spoolman_id"), 42)

    def test_classic_tag_bambu_read_fails_returns_uid_no_crash(self):
        """When Bambu read fails, _scan_once must still return the uid without crashing."""
        self._wire_reader([_classic_tag_entry()])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        # uid must be present (we still detected the tag even if Bambu read failed)
        self.assertEqual(result.get("uid_hex"), "C2C304EB")
        self.assertIsNone(result.get("spoolman_id"))

    def test_ntag_tag_does_not_trigger_bambu_read(self):
        """For NTAG/Ultralight (SAK 0x00), _try_bambu_read_with_fallback must NOT be called."""
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
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_not_called()

    def test_classic_tag_with_raw_bytes_already_set_skips_bambu_read(self):
        """If a Classic tag already has raw_bytes (e.g. from a cache hit), skip Bambu read."""
        entry = _classic_tag_entry()
        entry["raw_bytes"] = b"\x01" * 16
        entry["raw_len"] = 16
        self._wire_reader([entry])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_not_called()

    def test_no_tags_no_bambu_read(self):
        """When read_all_tags returns [], _try_bambu_read_with_fallback must not be called."""
        self._wire_reader([])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)

        result = self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_not_called()
        self.assertIsNone(result.get("uid_hex"))


# ---------------------------------------------------------------------------
# Tests: Bambu key derivation output shape
# ---------------------------------------------------------------------------

class TestBambuKeyDerivation(unittest.TestCase):
    """_bambu_derive_keys() must return 16 × 6-byte keys for any UID."""

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_returns_16_keys(self):
        """Key list must have exactly 16 entries (one per MIFARE Classic sector)."""
        uid = bytes.fromhex("C2C304EB")
        keys = _rtp._bambu_derive_keys(uid)
        self.assertEqual(len(keys), 16, "Expected 16 sector keys")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_each_6_bytes(self):
        """Each derived key must be exactly 6 bytes (MIFARE key width)."""
        uid = bytes.fromhex("F29CDAEF")
        keys = _rtp._bambu_derive_keys(uid)
        for i, k in enumerate(keys):
            self.assertIsInstance(k, (bytes, bytearray),
                                  f"Key {i} is not bytes")
            self.assertEqual(len(k), 6,
                             f"Key {i} has wrong length {len(k)}, expected 6")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_are_bytes_not_list(self):
        """Keys must be bytes-like objects, not lists of ints."""
        uid = bytes.fromhex("AABBCCDD")
        keys = _rtp._bambu_derive_keys(uid)
        for i, k in enumerate(keys):
            self.assertIsInstance(k, (bytes, bytearray),
                                  f"Key {i} type {type(k)} is not bytes/bytearray")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_different_uids_give_different_keys(self):
        """Different UIDs must produce different key lists."""
        keys_a = _rtp._bambu_derive_keys(bytes.fromhex("C2C304EB"))
        keys_b = _rtp._bambu_derive_keys(bytes.fromhex("F29CDAEF"))
        self.assertNotEqual(keys_a, keys_b,
                            "Different UIDs must produce different keys")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_same_uid_deterministic(self):
        """Same UID must always yield the same keys (deterministic HKDF)."""
        uid = bytes.fromhex("C2C304EB")
        self.assertEqual(_rtp._bambu_derive_keys(uid), _rtp._bambu_derive_keys(uid))

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_uid_is_ikm_not_salt(self):
        """HKDF must use uid_bytes as IKM and _BAMBU_MASTER_KEY as salt (not swapped).

        The Android reference (MrBambuSpoolPal-BambuSpoolPal_AndroidApp,
        NfcTagProcessor.kt) uses BouncyCastle HKDFParameters(uid, masterKey, context)
        where the first argument is the IKM and the second is the salt:
          IKM  = uid bytes
          salt = _BAMBU_MASTER_KEY (the static Bambu device key)

        pycryptodome HKDF signature: HKDF(master, key_len, salt, ...)
        where 'master' is the IKM (first positional argument).
        Correct call: HKDF(master=uid_bytes, salt=_BAMBU_MASTER_KEY, ...)

        This test guards against the regression where _BAMBU_MASTER_KEY was
        accidentally passed as the IKM and uid_bytes as the salt, which produces
        completely wrong keys and causes silent authentication failure on every
        Bambu tag sector.
        """
        # Use the same _HKDF and _SHA256 that rfid_tag_parser.py resolved at
        # import time (either Cryptodome.* or Crypto.*) so this test works under
        # both pycryptodomex and pycryptodome installations.
        _HKDF_direct = _rtp._HKDF
        _SHA256_direct = _rtp._SHA256

        uid = bytes.fromhex("C2C304EB")
        master = _rtp._BAMBU_MASTER_KEY

        # Correctly derived keys (uid as IKM, _BAMBU_MASTER_KEY as salt)
        correct_raw = _HKDF_direct(uid, 6, master, _SHA256_direct, 16,
                                   context=b"RFID-A\x00")
        correct_keys = list(correct_raw)

        # Swapped (wrong) derivation (_BAMBU_MASTER_KEY as IKM, uid as salt)
        swapped_raw = _HKDF_direct(master, 6, uid, _SHA256_direct, 16,
                                   context=b"RFID-A\x00")
        swapped_keys = list(swapped_raw)

        # The function must match the correctly-ordered derivation
        actual_keys = _rtp._bambu_derive_keys(uid)
        self.assertEqual(actual_keys, correct_keys,
                         "Keys must match HKDF(master=uid_bytes, salt=_BAMBU_MASTER_KEY)")
        self.assertNotEqual(actual_keys, swapped_keys,
                            "Keys must NOT match the swapped (wrong) derivation")

    def test_derive_keys_import_error_when_no_pycryptodome(self):
        """_bambu_derive_keys raises ImportError when pycryptodome is unavailable."""
        orig = _rtp._PYCRYPTODOME_OK
        try:
            _rtp._PYCRYPTODOME_OK = False
            with self.assertRaises(ImportError):
                _rtp._bambu_derive_keys(bytes.fromhex("C2C304EB"))
        finally:
            _rtp._PYCRYPTODOME_OK = orig


# ---------------------------------------------------------------------------
# Tests: Failure cache behavior
# ---------------------------------------------------------------------------

class TestAuthFailCache(unittest.TestCase):
    """_auth_fail_uids must suppress repeated Bambu reads within a scan window."""

    def setUp(self):
        self.rfid = _make_rfid(lanes="lane3")
        self.lane = "lane3"
        _rfid_module._UID_SPOOL_CACHE.clear()

    def _wire_reader(self, tags):
        self.rfid.reader = MagicMock()
        self.rfid.reader.read_all_tags = MagicMock(return_value=tags)
        self.rfid.uid_fast_scan = False

    def test_first_failure_populates_cache(self):
        """After a failed round, uid must appear in _auth_fail_uids[lane]."""
        self._wire_reader([_classic_tag_entry(uid_hex="C2C304EB")])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)
        # Ensure the lane cache exists (normally set by _start_scan_timer)
        self.rfid._auth_fail_uids[self.lane] = {}

        self.rfid._scan_once(self.lane, max_pages=135)

        uid_state = self.rfid._auth_fail_uids.get(self.lane, {}).get("C2C304EB")
        self.assertIsNotNone(uid_state,
                             "UID should be in _auth_fail_uids after first failure")
        self.assertIsInstance(uid_state, dict,
                              "_auth_fail_uids entry must be a dict with round/exhausted state")
        self.assertEqual(uid_state.get("rounds"), 1)

    def test_second_attempt_skips_bambu_read(self):
        """If UID is marked exhausted in failure cache, _try_bambu_read_with_fallback must NOT be called."""
        self._wire_reader([_classic_tag_entry(uid_hex="C2C304EB")])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)
        # Pre-populate with exhausted state (as if max rounds have already been done)
        self.rfid._auth_fail_uids[self.lane] = {
            "C2C304EB": {"rounds": 2, "exhausted": True, "ts": 1000.0}
        }

        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_not_called()

    def test_cache_reset_on_clear_scan_state(self):
        """_clear_scan_state must remove lane from _auth_fail_uids."""
        self.rfid._auth_fail_uids[self.lane] = {
            "C2C304EB": {"rounds": 1, "exhausted": False, "ts": 1000.0}
        }

        self.rfid._clear_scan_state(self.lane, reason="test")

        self.assertNotIn(self.lane, self.rfid._auth_fail_uids,
                         "_auth_fail_uids lane entry must be removed by _clear_scan_state")

    def test_cache_initialised_on_start_scan_timer(self):
        """_start_scan_timer must initialise an empty _auth_fail_uids dict for the lane."""
        # Pre-populate stale state from a previous window
        self.rfid._auth_fail_uids[self.lane] = {
            "STALEUID": {"rounds": 2, "exhausted": True, "ts": 999.0}
        }

        self.rfid._start_scan_timer(self.lane)

        fail_cache = self.rfid._auth_fail_uids.get(self.lane)
        self.assertIsNotNone(fail_cache, "_auth_fail_uids[lane] must be set")
        self.assertNotIn("STALEUID", fail_cache,
                         "Stale UID from previous window must not appear in new window cache")

    def test_success_does_not_populate_failure_cache(self):
        """When Bambu read succeeds with required blocks, uid must NOT be added to failure cache."""
        self._wire_reader([_classic_tag_entry(uid_hex="C2C304EB")])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=_FAKE_BAMBU_BLOCKS)
        self.rfid._apply_tag_parser = MagicMock(return_value=None)
        self.rfid._auth_fail_uids[self.lane] = {}

        self.rfid._scan_once(self.lane, max_pages=135)

        self.assertNotIn("C2C304EB", self.rfid._auth_fail_uids.get(self.lane, {}),
                         "Successful read must not populate failure cache")

    def test_different_uids_cached_independently(self):
        """Failure cache must track UIDs independently — failing one UID must not block another."""
        # First scan: UID A fails — with _BAMBU_MAX_ROUNDS=1 it is immediately exhausted.
        self._wire_reader([_classic_tag_entry(uid_hex="C2C304EB")])
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)
        self.rfid._auth_fail_uids[self.lane] = {}
        self.rfid._scan_once(self.lane, max_pages=135)

        # Second scan: UID B (different) must still attempt Bambu read
        self._wire_reader([_classic_tag_entry(uid_hex="F29CDAEF")])
        self.rfid._try_bambu_read_with_fallback.reset_mock()
        self.rfid._scan_once(self.lane, max_pages=135)

        self.rfid._try_bambu_read_with_fallback.assert_called_once_with("F29CDAEF", round_num=1)


# ---------------------------------------------------------------------------
# Tests: Key B derivation
# ---------------------------------------------------------------------------

class TestBambuKeyDerivationB(unittest.TestCase):
    """_bambu_derive_keys_b() must return 16 × 6-byte Key B values distinct from Key A."""

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_b_returns_16_keys(self):
        """Key B list must have exactly 16 entries."""
        uid = bytes.fromhex("C2C304EB")
        keys = _rtp._bambu_derive_keys_b(uid)
        self.assertEqual(len(keys), 16)

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_b_each_6_bytes(self):
        """Each Key B must be exactly 6 bytes."""
        uid = bytes.fromhex("F29CDAEF")
        keys = _rtp._bambu_derive_keys_b(uid)
        for i, k in enumerate(keys):
            self.assertIsInstance(k, (bytes, bytearray), f"Key B {i} is not bytes")
            self.assertEqual(len(k), 6, f"Key B {i} has wrong length {len(k)}")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_b_differs_from_key_a(self):
        """Key B values must differ from Key A values for the same UID."""
        uid = bytes.fromhex("C2C304EB")
        keys_a = _rtp._bambu_derive_keys(uid)
        keys_b = _rtp._bambu_derive_keys_b(uid)
        self.assertNotEqual(keys_a, keys_b,
                            "Key B must differ from Key A (different HKDF context)")

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_b_deterministic(self):
        """Same UID must always yield the same Key B values."""
        uid = bytes.fromhex("C2C304EB")
        self.assertEqual(_rtp._bambu_derive_keys_b(uid), _rtp._bambu_derive_keys_b(uid))

    @unittest.skipUnless(_PYCRYPTODOME_OK, "pycryptodome not installed")
    def test_derive_keys_b_different_uids_differ(self):
        """Different UIDs must produce different Key B lists."""
        kb1 = _rtp._bambu_derive_keys_b(bytes.fromhex("C2C304EB"))
        kb2 = _rtp._bambu_derive_keys_b(bytes.fromhex("F29CDAEF"))
        self.assertNotEqual(kb1, kb2)

    def test_derive_keys_b_import_error_when_no_pycryptodome(self):
        """_bambu_derive_keys_b raises ImportError when pycryptodome is unavailable."""
        orig = _rtp._PYCRYPTODOME_OK
        try:
            _rtp._PYCRYPTODOME_OK = False
            with self.assertRaises(ImportError):
                _rtp._bambu_derive_keys_b(bytes.fromhex("C2C304EB"))
        finally:
            _rtp._PYCRYPTODOME_OK = orig


# ---------------------------------------------------------------------------
# Tests: RFID_CHECK_TAG NameError regression (result vs scan_result)
# ---------------------------------------------------------------------------

class TestCheckTagScanResultReference(unittest.TestCase):
    """cmd_RFID_CHECK_TAG must use scan_result (not 'result') — regression for NameError bug."""

    def setUp(self):
        self.rfid = _make_rfid(lanes="lane1")
        _rfid_module._UID_SPOOL_CACHE.clear()

    def test_check_tag_uses_scan_result_not_result(self):
        """cmd_RFID_CHECK_TAG must not raise NameError when tag has no cached spoolman_id.

        Previously line 3374 used the undefined name 'result' instead of 'scan_result',
        causing a NameError every time RFID_CHECK_TAG was called on an uncached tag.
        """
        # Wire a scan result with a valid uid but no spoolman_id, no filament_info
        fake_scan = {
            "uid_hex": "AABBCCDD",
            "raw_bytes": b"",
            "raw_len": 0,
            "tag_text": "",
            "spoolman_id": None,
            "filament_info": None,
            "ts": 0.0,
        }
        self.rfid._run_scan_window_sync = MagicMock(return_value=fake_scan)
        self.rfid._try_bambu_read_with_fallback = MagicMock(return_value=None)
        self.rfid._resolve_port_param = MagicMock(return_value="lane1")
        self.rfid._find_reader_for_port = MagicMock(return_value=(self.rfid, "lane1"))

        gcmd = MagicMock()
        gcmd.get_int = MagicMock(side_effect=lambda key, default: default)
        gcmd.get = MagicMock(return_value=None)
        gcmd.error = lambda msg: Exception(msg)

        # Must NOT raise NameError
        try:
            self.rfid.cmd_RFID_CHECK_TAG(gcmd)
        except NameError as exc:
            self.fail(f"cmd_RFID_CHECK_TAG raised NameError: {exc}")


# ---------------------------------------------------------------------------
# Tests: write_mifare_classic_authenticated_blocks (mfrc522.py)
# ---------------------------------------------------------------------------

class TestWriteMifareClassicAuthBlocks(unittest.TestCase):
    """write_mifare_classic_authenticated_blocks must skip block 0 and trailers,
    authenticate the correct sector, and call _write_mifare_block for each data block."""

    def _make_device(self):
        dev = _make_device()
        dev._auth_mifare_block = MagicMock(return_value=True)
        dev._write_mifare_block = MagicMock(return_value=True)
        dev._clear_mask = MagicMock()
        return dev

    def test_writes_block_9(self):
        """Block 9 (sector 2 data block 1) must be written after authenticating sector 2."""
        dev = self._make_device()
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        data = b"\xAA" * 16
        ok = dev.write_mifare_classic_authenticated_blocks(uid, keys, {9: data}, use_key_b=True)
        self.assertTrue(ok)
        # Sector 2 trailer = block 11
        dev._auth_mifare_block.assert_called_once_with(
            dev.PICC_AUTHENT1B, 11, keys[2], uid[:4]
        )
        dev._write_mifare_block.assert_called_once_with(9, data)

    def test_skips_block_0(self):
        """Block 0 (manufacturer block) must be silently skipped."""
        dev = self._make_device()
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {0: b"\x00" * 16}, use_key_b=True
        )
        self.assertTrue(ok)
        dev._auth_mifare_block.assert_not_called()
        dev._write_mifare_block.assert_not_called()

    def test_skips_sector_trailer(self):
        """Sector trailer blocks (3, 7, 11, …) must be silently skipped."""
        dev = self._make_device()
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        # Block 7 is the trailer of sector 1
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {7: b"\x00" * 16}, use_key_b=True
        )
        self.assertTrue(ok)
        dev._write_mifare_block.assert_not_called()

    def test_auth_failure_returns_false(self):
        """Returns False if sector authentication fails."""
        dev = self._make_device()
        dev._auth_mifare_block = MagicMock(return_value=False)
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {9: b"\xBB" * 16}, use_key_b=True
        )
        self.assertFalse(ok)
        dev._write_mifare_block.assert_not_called()

    def test_write_failure_returns_false(self):
        """Returns False if _write_mifare_block fails."""
        dev = self._make_device()
        dev._write_mifare_block = MagicMock(return_value=False)
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {9: b"\xCC" * 16}, use_key_b=True
        )
        self.assertFalse(ok)

    def test_uses_key_a_when_flag_false(self):
        """use_key_b=False must pass PICC_AUTHENT1A to _auth_mifare_block."""
        dev = self._make_device()
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {9: b"\xDD" * 16}, use_key_b=False
        )
        args = dev._auth_mifare_block.call_args[0]
        self.assertEqual(args[0], dev.PICC_AUTHENT1A)

    def test_halt_reselect_called_between_sectors(self):
        """HALT + re-SELECT must be issued between consecutive sector writes.

        When blocks from two different sectors are written, _halt_and_reselect()
        must be called exactly once (between the two sectors) to reset the
        MFRC522 Crypto1 state machine.
        """
        dev = self._make_device()
        dev._halt_and_reselect = MagicMock(return_value=True)
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        # Block 1 is in sector 0, block 9 is in sector 2 → two sectors
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {1: b"\xAA" * 16, 9: b"\xBB" * 16}, use_key_b=True
        )
        self.assertTrue(ok)
        dev._halt_and_reselect.assert_called_once()

    def test_halt_reselect_not_called_for_single_sector(self):
        """When all writes are within a single sector, no HALT + re-SELECT is needed."""
        dev = self._make_device()
        dev._halt_and_reselect = MagicMock(return_value=True)
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {9: b"\xAA" * 16, 10: b"\xBB" * 16}, use_key_b=True
        )
        self.assertTrue(ok)
        dev._halt_and_reselect.assert_not_called()

    def test_halt_reselect_failure_aborts_write(self):
        """If _halt_and_reselect fails mid-write, the method must return False."""
        dev = self._make_device()
        dev._halt_and_reselect = MagicMock(return_value=False)
        uid = b"\xC2\xC3\x04\xEB"
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16
        ok = dev.write_mifare_classic_authenticated_blocks(
            uid, keys, {1: b"\xAA" * 16, 9: b"\xBB" * 16}, use_key_b=True
        )
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# Tests: read_authenticated_blocks — HALT + re-SELECT between sectors
# ---------------------------------------------------------------------------

class TestReadAuthenticatedBlocksHaltReselect(unittest.TestCase):
    """read_authenticated_blocks must HALT + re-SELECT between consecutive sectors.

    The MFRC522 Crypto1 state machine requires a HALT + re-SELECT sequence
    between each sector authentication.  Without it, all sectors after sector 0
    fail authentication — the user-observed symptom.  Android's MifareClassic
    API handles this transparently; bare-metal drivers must replicate it.
    """

    def _make_device(self, auth_ok=True):
        """Return an MFRC522Device with key methods mocked for unit testing."""
        dev = _make_device()
        dev._auth_mifare_block = MagicMock(return_value=auth_ok)
        dev._read_mifare_block = MagicMock(return_value=bytes(16))
        dev._clear_mask = MagicMock()
        dev._halt_and_reselect = MagicMock(return_value=True)
        return dev

    def test_halt_reselect_called_between_every_sector(self):
        """_halt_and_reselect must be called exactly (num_sectors - 1) times."""
        num_sectors = 4
        dev = self._make_device()
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        dev.read_authenticated_blocks(uid, keys, num_sectors=num_sectors)

        self.assertEqual(
            dev._halt_and_reselect.call_count,
            num_sectors - 1,
            "Expected _halt_and_reselect called %d times for %d sectors" % (
                num_sectors - 1, num_sectors),
        )

    def test_halt_reselect_not_called_for_single_sector(self):
        """When reading only 1 sector, no HALT + re-SELECT is needed."""
        dev = self._make_device()
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        dev.read_authenticated_blocks(uid, keys, num_sectors=1)

        dev._halt_and_reselect.assert_not_called()

    def test_halt_reselect_failure_fills_remaining_sectors_with_none(self):
        """If _halt_and_reselect fails, remaining sectors must be filled with None."""
        dev = self._make_device()
        # Fail the re-select that happens before sector 2
        dev._halt_and_reselect = MagicMock(side_effect=[True, False])
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        result = dev.read_authenticated_blocks(uid, keys, num_sectors=4)

        # Sectors 0 and 1 succeed (halt_reselect[0] = True).
        # Data blocks per sector: sector 0 → 0,1,2; sector 1 → 4,5,6
        for blk in (0, 1, 2, 4, 5, 6):
            self.assertIsNotNone(result.get(blk),
                                 "Block %d should have data (sector 0 or 1)" % blk)
        # Sectors 2 and 3 must be None (halt_reselect[1] = False).
        # Data blocks: sector 2 → 8,9,10; sector 3 → 12,13,14
        for blk in (8, 9, 10, 12, 13, 14):
            self.assertIsNone(result.get(blk),
                              "Block %d should be None (sector 2+ after reselect failure)" % blk)

    def test_all_16_sectors_readable_with_successful_halt_reselect(self):
        """All 48 data blocks must be readable when halt_reselect always succeeds."""
        dev = self._make_device()
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        result = dev.read_authenticated_blocks(uid, keys, num_sectors=16)

        # 16 sectors × 3 data blocks = 48 entries.
        # Sector N has data blocks: N*4, N*4+1, N*4+2 (trailer N*4+3 is excluded).
        self.assertEqual(len(result), 48)
        for sector in range(16):
            for offset in range(3):
                blk = sector * 4 + offset
                self.assertIsNotNone(result.get(blk),
                                     "Block %d (sector %d) should have data" % (blk, sector))

    def test_auth_failure_still_continues_to_next_sector(self):
        """An auth failure on one sector must not stop reading subsequent sectors."""
        dev = self._make_device()
        # Fail auth on sector 1 only (block 7 is sector 1's trailer)
        def auth_side_effect(cmd, block, key, uid):
            return block != 7
        dev._auth_mifare_block = MagicMock(side_effect=auth_side_effect)
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        result = dev.read_authenticated_blocks(uid, keys, num_sectors=4)

        # Sector 0 (blocks 0-2) should have data
        for blk in range(3):
            self.assertIsNotNone(result.get(blk), "Sector 0 block %d should have data" % blk)
        # Sector 1 (blocks 4-6) should be None (auth failed)
        for blk in range(4, 7):
            self.assertIsNone(result.get(blk), "Sector 1 block %d should be None" % blk)
        # Sector 2 (blocks 8-10) should have data (read continues after sector 1 failure)
        for blk in range(8, 11):
            self.assertIsNotNone(result.get(blk), "Sector 2 block %d should have data" % blk)

    def test_halt_reselect_called_even_after_auth_failure(self):
        """_halt_and_reselect must be called before sector 2 even if sector 1 auth failed."""
        dev = self._make_device()
        # Fail auth on sector 1
        def auth_side_effect(cmd, block, key, uid):
            return block != 7
        dev._auth_mifare_block = MagicMock(side_effect=auth_side_effect)
        uid = bytes.fromhex("C2C304EB")
        keys = [b"\x01\x02\x03\x04\x05\x06"] * 16

        dev.read_authenticated_blocks(uid, keys, num_sectors=3)

        # _halt_and_reselect must be called twice: before sector 1 and before sector 2
        self.assertEqual(dev._halt_and_reselect.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
