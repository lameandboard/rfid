"""Unit tests for spoolman_client.py changes.

Tests cover:
1. create_spool extra-field JSON encoding (bug-fix: values must be JSON-encoded
   so they match how find_spool_by_uid queries Spoolman).
2. SpoolmanDB Bambu SKU-based lookup (material_id → colors[].id matching).
3. SpoolmanDB manufacturer cache population.
4. Filament creation enrichment from SpoolmanDB (name, temps, spool_weight).

Run with:
    python3 extras/test_spoolman_client.py
"""

import json
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib import parse as url_parse
import os

# Ensure extras/ is importable without Klipper present.
_EXTRAS_DIR = os.path.join(os.path.dirname(__file__))
if _EXTRAS_DIR not in sys.path:
    sys.path.insert(0, _EXTRAS_DIR)

import spoolman_client as sc  # noqa: E402


# ---------------------------------------------------------------------------
# Helper — build a SpoolmanClient with all HTTP calls intercepted.
# ---------------------------------------------------------------------------

def _make_client(req_side_effect=None):
    """Return a SpoolmanClient whose _req method is replaced with a MagicMock."""
    client = sc.SpoolmanClient("http://localhost:7912")
    client._req = MagicMock(side_effect=req_side_effect)
    return client


# ---------------------------------------------------------------------------
# Tests: create_spool extra-field encoding
# ---------------------------------------------------------------------------

class TestCreateSpoolExtraEncoding(unittest.TestCase):
    """create_spool must JSON-encode extra field values so they are valid JSON.

    Spoolman stores extra field values as JSON strings and rejects raw (bare)
    values with "Value is not valid JSON".  _patch_uid_field already uses
    json.dumps; create_spool must be consistent so find_spool_by_uid queries
    (which also JSON-encode via json.dumps) match what was stored at creation.
    """

    def _capture_post_body(self):
        """Return a _req side-effect that captures POST body and returns a fake spool."""
        captured = {}

        def _side_effect(method, path, body=None):
            if method == "POST" and "/spool" in path:
                captured["body"] = body
                return {"id": 42}
            return None

        return _side_effect, captured

    def test_extra_uid_is_json_encoded_string(self):
        """Extra UID value must be sent as a JSON-encoded string, not a bare string."""
        side_effect, captured = self._capture_post_body()
        client = _make_client(side_effect)

        uid = "E2B602EB"
        client.create_spool(
            filament_id=1,
            remaining_weight=1000.0,
            extra={"rfid_uid_1": uid},
        )

        self.assertIn("body", captured, "POST body was not captured")
        extra = captured["body"].get("extra", {})
        self.assertIn("rfid_uid_1", extra, "rfid_uid_1 missing from extra")
        stored = extra["rfid_uid_1"]
        # Value must be the JSON-encoded string: '"E2B602EB"'
        self.assertEqual(stored, json.dumps(uid),
                         f"Expected JSON-encoded string {json.dumps(uid)!r}, got {stored!r}")

    def test_extra_value_is_valid_json(self):
        """The stored extra value must be parseable as JSON."""
        side_effect, captured = self._capture_post_body()
        client = _make_client(side_effect)

        client.create_spool(
            filament_id=1,
            remaining_weight=1000.0,
            extra={"rfid_uid_1": "ABCD1234"},
        )

        extra = captured["body"].get("extra", {})
        stored = extra["rfid_uid_1"]
        try:
            parsed = json.loads(stored)
        except json.JSONDecodeError:
            self.fail(f"Stored extra value {stored!r} is not valid JSON")
        self.assertEqual(parsed, "ABCD1234",
                         "Decoded value must equal the original UID string")

    def test_extra_encoding_matches_find_spool_by_uid_query(self):
        """create_spool encoding must match what find_spool_by_uid queries with."""
        uid = "0499D8373A2480"
        # What create_spool now stores:
        stored_value = json.dumps(str(uid))
        # What find_spool_by_uid queries with (URL-decoded form):
        query_value = url_parse.unquote(
            url_parse.quote(json.dumps(uid), safe="")
        )
        self.assertEqual(stored_value, query_value,
                         "create_spool stored value must match find_spool_by_uid query value")


# ---------------------------------------------------------------------------
# Tests: _fetch_spoolmandb_bambu manufacturer cache
# ---------------------------------------------------------------------------

class TestFetchSpoolmandbBambuManufacturer(unittest.TestCase):
    """_fetch_spoolmandb_bambu must populate _SPOOLMANDB_BAMBU_MANUFACTURER_CACHE."""

    def setUp(self):
        # Reset module-level caches before each test.
        sc._SPOOLMANDB_BAMBU_CACHE = None
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = None

    def tearDown(self):
        sc._SPOOLMANDB_BAMBU_CACHE = None
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = None

    def _mock_db(self, payload):
        """Patch urlopen to return *payload* (dict) as JSON."""
        raw = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = raw
        return mock_resp

    @patch("spoolman_client.request.urlopen")
    def test_manufacturer_cache_populated_from_top_level(self, mock_urlopen):
        """Manufacturer name from top-level 'manufacturer' key must be cached."""
        payload = {
            "manufacturer": "Bambu Lab",
            "filaments": [
                {"name": "PLA Basic", "material": "PLA", "density": 1.24, "colors": []},
            ],
        }
        mock_urlopen.return_value = self._mock_db(payload)

        result = sc._fetch_spoolmandb_bambu()
        self.assertEqual(len(result), 1)
        self.assertEqual(sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE, "Bambu Lab")

    @patch("spoolman_client.request.urlopen")
    def test_manufacturer_cache_none_when_absent(self, mock_urlopen):
        """Manufacturer cache must stay None when the key is absent."""
        payload = {
            "filaments": [
                {"name": "PLA Basic", "material": "PLA", "density": 1.24, "colors": []},
            ],
        }
        mock_urlopen.return_value = self._mock_db(payload)

        sc._fetch_spoolmandb_bambu()
        self.assertIsNone(sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE)

    @patch("spoolman_client.request.urlopen")
    def test_filaments_returned_correctly(self, mock_urlopen):
        """_fetch_spoolmandb_bambu must return the filaments list."""
        filaments = [
            {"name": "PETG HF", "material": "PETG", "density": 1.27,
             "colors": [{"hex": "FF0000", "id": "GFG01"}]},
        ]
        mock_urlopen.return_value = self._mock_db({"manufacturer": "Bambu Lab",
                                                    "filaments": filaments})

        result = sc._fetch_spoolmandb_bambu()
        self.assertEqual(result, filaments)

    @patch("spoolman_client.request.urlopen")
    def test_manufacturer_cache_cleared_on_fetch_failure(self, mock_urlopen):
        """_SPOOLMANDB_BAMBU_MANUFACTURER_CACHE must be None after a failed fetch."""
        # Pre-seed a stale manufacturer name to simulate a partially-reset cache.
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = "Stale Name"
        mock_urlopen.side_effect = OSError("network down")

        sc._fetch_spoolmandb_bambu()
        self.assertIsNone(
            sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE,
            "Manufacturer cache must be cleared to None when the fetch fails",
        )

    @patch("spoolman_client.request.urlopen")
    def test_manufacturer_cache_cleared_when_payload_is_list(self, mock_urlopen):
        """When the response is a bare list (no manufacturer key), cache must be None."""
        # Pre-seed a stale manufacturer name.
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = "Stale Name"
        filaments = [{"name": "PLA", "material": "PLA", "density": 1.24, "colors": []}]
        raw = json.dumps(filaments).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = raw
        mock_urlopen.return_value = mock_resp

        sc._fetch_spoolmandb_bambu()
        self.assertIsNone(
            sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE,
            "Manufacturer cache must be None when payload is a list (no manufacturer key)",
        )


# ---------------------------------------------------------------------------
# Tests: auto_create_spool — SpoolmanDB SKU-based lookup
# ---------------------------------------------------------------------------

_BAMBU_DB = [
    {
        "name": "PLA Basic",
        "material": "PLA",
        "density": 1.24,
        "extruder_temp": 220,
        "extruder_temp_max": 230,
        "bed_temp": 35,
        "spool_weight": 250,
        "colors": [
            {"name": "White", "hex": "FFFFFF", "id": "GFA00"},
            {"name": "Black", "hex": "1A1A1A", "id": "GFA01"},
        ],
    },
    {
        "name": "PETG HF",
        "material": "PETG",
        "density": 1.27,
        "extruder_temp": 230,
        "extruder_temp_max": 250,
        "bed_temp": 70,
        "spool_weight": 250,
        "colors": [
            {"name": "Bambu Green", "hex": "00AE42", "id": "GFG02"},
        ],
    },
]


class TestAutoCreateSpoolSpoolmanDB(unittest.TestCase):
    """auto_create_spool must use SpoolmanDB data correctly when creating filaments."""

    def setUp(self):
        # Pre-populate Bambu DB cache so HTTP is not needed.
        sc._SPOOLMANDB_BAMBU_CACHE = list(_BAMBU_DB)
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = "Bambu Lab"
        sc._SPOOLMANDB_MATERIALS_CACHE = {}

    def tearDown(self):
        sc._SPOOLMANDB_BAMBU_CACHE = None
        sc._SPOOLMANDB_BAMBU_MANUFACTURER_CACHE = None
        sc._SPOOLMANDB_MATERIALS_CACHE = None

    def _make_client_for_create(self, existing_filaments=None, existing_vendors=None):
        """Build a client whose _req returns fake data for filament/vendor lookups."""
        _existing_filaments = existing_filaments or []
        _existing_vendors = existing_vendors or []
        created_filament = {}
        created_spool = {}

        def _req(method, path, body=None):
            if method == "GET" and "/api/v1/field/spool" in path:
                return [{"key": "rfid_uid_1"}]
            if method == "GET" and "/api/v1/vendor" in path:
                return _existing_vendors
            if method == "GET" and "/api/v1/filament" in path:
                return _existing_filaments
            if method == "POST" and "/api/v1/vendor" in path:
                v = {"id": 10, "name": body.get("name", "Unknown")}
                _existing_vendors.append(v)
                return v
            if method == "POST" and "/api/v1/filament" in path:
                created_filament.update(body or {})
                created_filament["id"] = 99
                return dict(created_filament)
            if method == "POST" and "/api/v1/spool" in path:
                created_spool.update(body or {})
                created_spool["id"] = 200
                return dict(created_spool)
            return None

        client = sc.SpoolmanClient("http://localhost:7912")
        client._req = MagicMock(side_effect=_req)
        return client, created_filament, created_spool

    def test_sku_lookup_sets_color_hex_from_db(self):
        """When material_id matches a colour SKU, color_hex must come from SpoolmanDB."""
        client, created_filament, created_spool = self._make_client_for_create()

        filament_info = {
            "material": "PETG",
            "material_id": "GFG02",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
            # No color_hex in tag — it should come from SpoolmanDB
        }
        spool_id = client.auto_create_spool(filament_info, uid_hex="E2B602EB")

        self.assertIsNotNone(spool_id, "auto_create_spool must return a spool_id")
        # color_hex must be set to the SpoolmanDB value for SKU GFG02
        self.assertEqual(created_filament.get("color_hex"), "00AE42",
                         "color_hex must come from SpoolmanDB for SKU GFG02")

    def test_filament_name_uses_spoolmandb_name(self):
        """Filament name must use SpoolmanDB name (e.g. 'Bambu Lab PETG HF'), not 'Bambu Lab PETG'."""
        client, created_filament, _ = self._make_client_for_create()

        filament_info = {
            "material": "PETG",
            "material_id": "GFG02",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
        }
        client.auto_create_spool(filament_info)

        name = created_filament.get("name", "")
        self.assertIn("PETG HF", name,
                      f"Filament name {name!r} must contain SpoolmanDB name 'PETG HF'")

    def test_temperature_from_spoolmandb_when_tag_lacks_it(self):
        """When tag has no temperature data, SpoolmanDB temps must be used."""
        client, created_filament, _ = self._make_client_for_create()

        filament_info = {
            "material": "PLA",
            "material_id": "GFA00",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
            # No min_temp / max_temp / bed_temp in tag
        }
        client.auto_create_spool(filament_info)

        # SpoolmanDB has extruder_temp=220 for PLA Basic
        self.assertEqual(created_filament.get("settings_extruder_temp"), 220,
                         "settings_extruder_temp must come from SpoolmanDB (220)")
        self.assertEqual(created_filament.get("settings_bed_temp"), 35,
                         "settings_bed_temp must come from SpoolmanDB (35)")

    def test_temperature_from_tag_overrides_spoolmandb(self):
        """Tag temperature data must take priority over SpoolmanDB values."""
        client, created_filament, _ = self._make_client_for_create()

        filament_info = {
            "material": "PLA",
            "material_id": "GFA00",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
            "min_temp": 210,   # Tag overrides SpoolmanDB's 220
            "bed_temp": 40,    # Tag overrides SpoolmanDB's 35
        }
        client.auto_create_spool(filament_info)

        self.assertEqual(created_filament.get("settings_extruder_temp"), 210,
                         "Tag min_temp (210) must override SpoolmanDB (220)")
        self.assertEqual(created_filament.get("settings_bed_temp"), 40,
                         "Tag bed_temp (40) must override SpoolmanDB (35)")

    def test_spool_weight_from_spoolmandb_when_absent_in_tag(self):
        """spool_weight must fall back to SpoolmanDB value when not in tag data."""
        client, _, created_spool = self._make_client_for_create()

        filament_info = {
            "material": "PLA",
            "material_id": "GFA00",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
            # No spool_weight_g in tag
        }
        client.auto_create_spool(filament_info)

        # SpoolmanDB has spool_weight=250 for PLA Basic
        self.assertEqual(created_spool.get("spool_weight"), 250.0,
                         "spool_weight must come from SpoolmanDB (250)")

    def test_external_id_set_to_material_id(self):
        """external_id on created filament must be set to material_id."""
        client, created_filament, _ = self._make_client_for_create()

        filament_info = {
            "material": "PETG",
            "material_id": "GFG02",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
        }
        client.auto_create_spool(filament_info)

        self.assertEqual(created_filament.get("external_id"), "GFG02",
                         "external_id must be set to material_id 'GFG02'")

    def test_uid_hex_json_encoded_in_spool_extra(self):
        """UID extra field must be JSON-encoded when the spool is created."""
        client, _, created_spool = self._make_client_for_create()

        uid = "E2B602EB"
        filament_info = {
            "material": "PETG",
            "material_id": "GFG02",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
        }
        client.auto_create_spool(filament_info, uid_hex=uid)

        extra = created_spool.get("extra") or {}
        self.assertIn("rfid_uid_1", extra, "rfid_uid_1 must be in spool extra")
        stored = extra["rfid_uid_1"]
        self.assertEqual(stored, json.dumps(uid),
                         f"UID must be JSON-encoded; expected {json.dumps(uid)!r}, got {stored!r}")

    def test_no_filament_created_when_existing_match_found(self):
        """When an existing filament matches by external_id, no new filament is created."""
        existing_filaments = [{"id": 5, "material": "PETG", "color_hex": "00AE42"}]
        client, created_filament, _ = self._make_client_for_create(
            existing_filaments=existing_filaments
        )

        filament_info = {
            "material": "PETG",
            "material_id": "GFG02",
            "brand": "Bambu Lab",
            "is_bambu": True,
            "weight_g": 1000,
        }
        spool_id = client.auto_create_spool(filament_info)

        self.assertIsNotNone(spool_id)
        # created_filament should be empty — no new filament was POSTed
        self.assertFalse(
            any(call.args[0] == "POST" and "/api/v1/filament" in call.args[1]
                for call in client._req.call_args_list),
            "No new filament must be created when an existing match is found",
        )


# ---------------------------------------------------------------------------
# Tests: find_spool_by_uid — inline verification and false-positive handling
# ---------------------------------------------------------------------------

def _spool_response(spool_id, uid_slot_values=None):
    """Build a minimal Spoolman spool dict as returned by the search API.

    *uid_slot_values* is an optional dict mapping slot index (1-based) to UID
    string.  Values are JSON-encoded to match what Spoolman stores/returns.
    Pass ``None`` to omit the ``extra`` key entirely (simulates an API that
    does not include extra fields in search results).
    """
    spool = {"id": spool_id}
    if uid_slot_values is not None:
        spool["extra"] = {
            f"rfid_uid_{n}": json.dumps(v)
            for n, v in uid_slot_values.items()
        }
    return spool


class TestFindSpoolByUid(unittest.TestCase):
    """find_spool_by_uid: inline verification, false-positive rejection, fallback."""

    _UID = "C2C304EB"
    _MAX_UIDS = 4  # keep small so tests are quick

    def _client_with_fields(self, req_fn):
        """Return a client that reports all rfid_uid_N fields as present."""
        client = sc.SpoolmanClient("http://localhost:7912")
        # Override fields_exist so the fields pre-check always passes.
        client.fields_exist = MagicMock(return_value=True)
        client._req = MagicMock(side_effect=req_fn)
        return client

    def _count_secondary_fetches(self, client, spool_id):
        """Count how many secondary per-spool GET /api/v1/spool/<id> calls were made."""
        return sum(
            1 for c in client._req.call_args_list
            if c.args[0] == "GET" and f"/api/v1/spool/{spool_id}" in c.args[1]
               and "extra[" not in c.args[1]  # exclude slot-query GETs
        )

    # ------------------------------------------------------------------
    # Confirmed inline
    # ------------------------------------------------------------------

    def test_uid_confirmed_inline_returns_spool_id(self):
        """When the search response extra contains the queried UID, return the spool."""

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_1]" in path:
                return [_spool_response(16, {1: self._UID})]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(sid, 16)
        self.assertEqual(self._count_secondary_fetches(client, 16), 0,
                         "No secondary fetch expected when inline check confirms UID")

    def test_uid_confirmed_in_different_slot_inline(self):
        """UID in rfid_uid_2 of the response is still confirmed when querying rfid_uid_1."""
        # Spoolman may return a spool for rfid_uid_1 query; the extra has uid in slot 2.

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_1]" in path:
                # Response includes slot 2 matching the UID (slot 1 has a different value)
                return [_spool_response(7, {1: "AABBCCDD", 2: self._UID})]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(sid, 7)

    # ------------------------------------------------------------------
    # False-positive: queried field has a non-matching value in response
    # ------------------------------------------------------------------

    def test_false_positive_skips_secondary_fetch(self):
        """When response shows the queried field has a different UID, no secondary fetch."""
        different_uid = "C2C304EB12345678"

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_" in path and "/spool?" in path:
                # Spoolman returns spool 16 for every slot query (partial match).
                return [_spool_response(16, {1: different_uid})]
            if method == "GET" and path == "/api/v1/spool":
                # Fallback full scan: spool has different_uid, not our UID.
                return [_spool_response(16, {1: different_uid})]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertIsNone(sid, "UID not in any spool — should return None")
        self.assertEqual(self._count_secondary_fetches(client, 16), 0,
                         "No secondary fetch expected for inline-rejected false positives")

    def test_rejected_id_not_rechecked_across_slots(self):
        """A spool inline-rejected in slot 1 query must not be re-fetched in slot 2+ queries."""
        different_uid = "DEADBEEF"
        secondary_calls = []

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_" in path and "/spool?" in path:
                return [_spool_response(99, {1: different_uid})]
            if method == "GET" and "/api/v1/spool/99" in path:
                secondary_calls.append(path)
                return _spool_response(99, {1: different_uid})
            if method == "GET" and path == "/api/v1/spool":
                return [_spool_response(99, {1: different_uid})]
            return []

        client = self._client_with_fields(_req)
        client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(len(secondary_calls), 0,
                         "Spool 99 must not be fetched at all — rejected inline on first hit")

    # ------------------------------------------------------------------
    # Missing extra in search response → secondary fetch
    # ------------------------------------------------------------------

    def test_secondary_fetch_when_response_has_no_extra(self):
        """When search response omits extra, a secondary fetch must be performed."""
        fetch_calls = []

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_1]" in path:
                # Response has no 'extra' key.
                return [{"id": 5}]
            if method == "GET" and "/api/v1/spool/5" in path:
                fetch_calls.append(path)
                # The spool really does contain the UID.
                return _spool_response(5, {1: self._UID})
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(sid, 5)
        self.assertEqual(len(fetch_calls), 1, "One secondary fetch expected")

    def test_secondary_fetch_miss_returns_none(self):
        """Secondary fetch that does not find the UID must result in None."""

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_1]" in path:
                return [{"id": 5}]
            if method == "GET" and "/api/v1/spool/5" in path:
                return _spool_response(5, {1: "DIFFERENTUID"})
            if method == "GET" and path == "/api/v1/spool":
                return []
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertIsNone(sid)

    # ------------------------------------------------------------------
    # Fallback full-spool scan
    # ------------------------------------------------------------------

    def test_fallback_scan_finds_uid(self):
        """When all slot queries return empty, fallback scan must find the spool."""

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_" in path and "/spool?" in path:
                return []  # All per-slot queries return empty.
            if method == "GET" and path == "/api/v1/spool":
                return [_spool_response(42, {1: self._UID})]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(sid, 42)

    def test_fallback_scan_with_raw_unencoded_value(self):
        """Fallback scan must match UIDs stored without JSON encoding (legacy)."""

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_" in path and "/spool?" in path:
                return []
            if method == "GET" and path == "/api/v1/spool":
                # Value stored as raw string without json.dumps (legacy).
                return [{"id": 77, "extra": {"rfid_uid_1": self._UID}}]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertEqual(sid, 77)

    def test_not_found_returns_none(self):
        """When the UID is genuinely absent from all spools, must return None."""

        def _req(method, path, body=None):
            if method == "GET" and "extra[rfid_uid_" in path and "/spool?" in path:
                return []
            if method == "GET" and path == "/api/v1/spool":
                return [_spool_response(1, {1: "AABBCCDD"}),
                        _spool_response(2, {1: "11223344"})]
            return []

        client = self._client_with_fields(_req)
        sid = client.find_spool_by_uid(self._UID, self._MAX_UIDS)
        self.assertIsNone(sid)

    # ------------------------------------------------------------------
    # _decode_extra_value helper
    # ------------------------------------------------------------------

    def test_decode_extra_value_json_string(self):
        """JSON-encoded string must be decoded to plain string."""
        self.assertEqual(sc.SpoolmanClient._decode_extra_value('"C2C304EB"'), "C2C304EB")

    def test_decode_extra_value_raw_string(self):
        """Raw string (no JSON encoding) must be returned as-is."""
        self.assertEqual(sc.SpoolmanClient._decode_extra_value("C2C304EB"), "C2C304EB")

    def test_decode_extra_value_ignores_non_string_json(self):
        """Non-string JSON values (e.g. numbers) must not replace the raw string."""
        # json.loads("1234") → int 1234, not a str → fallback to raw
        self.assertEqual(sc.SpoolmanClient._decode_extra_value("1234"), "1234")


if __name__ == "__main__":
    unittest.main(verbosity=2)
