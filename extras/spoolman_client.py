# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <https://www.gnu.org/licenses/>.
#
# Copyright (c) 2026 lameandboard

import json
import logging
import time
from typing import Optional
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SpoolmanDB fetch caches — populated at most once per process.
# _SPOOLMANDB_MATERIALS_CACHE (dict-based):
#   None = not yet attempted
#   {}   = attempted but failed (so we don't retry on every scan)
#   dict = successfully populated
# _SPOOLMANDB_BAMBU_CACHE (list-based):
#   None = not yet attempted
#   []   = attempted but failed (so we don't retry on every scan)
#   list = successfully populated
# ---------------------------------------------------------------------------
_SPOOLMANDB_MATERIALS_CACHE: Optional[dict] = None   # material_lower -> density (float)
_SPOOLMANDB_BAMBU_CACHE: Optional[list] = None        # list of filament dicts from bambulab.json

# Hardcoded density fallback table (g/cm³) — used when SpoolmanDB is unreachable.
_DENSITY_FALLBACK: dict = {
    "pla":          1.24,
    "pla+":         1.24,
    "abs":          1.04,
    "petg":         1.27,
    "nylon":        1.52,
    "pa":           1.52,
    "tpu":          1.21,
    "flexible":     1.21,
    "asa":          1.05,
    "pc":           1.30,
    "hips":         1.03,
    "pva":          1.23,
    "tpe":          1.21,
    "peek":         1.32,
    "pei":          1.27,
    "pom":          1.41,
}
_DENSITY_DEFAULT: float = 1.24  # PLA — safe fallback for unknown materials

# Maps tag_format → brand name used when the parser does not supply one.
_TAG_FORMAT_BRANDS: dict = {
    "elegoo": "ELEGOO",
    "anycubic_ace": "Anycubic",
    "creality_cfs": "Creality",
    "qidi": "QIDI",
    "opentag3d": "Generic",
    "openspool": "Generic",
    "openprinttag": "Generic",
    "simplyprint_url": "Generic",
    "generic_ndef_json": "Generic",
}

# Default spool weight (grams) used when the tag does not supply one.
_DEFAULT_SPOOL_WEIGHT_G: int = 1000


def _fetch_spoolmandb_materials() -> dict:
    """Fetch and cache the SpoolmanDB materials.json density table.

    Returns a dict mapping lowercase material name -> density (float).
    On network failure returns {} so callers fall back to _DENSITY_FALLBACK.
    Result is cached in _SPOOLMANDB_MATERIALS_CACHE for the process lifetime.
    """
    global _SPOOLMANDB_MATERIALS_CACHE
    if _SPOOLMANDB_MATERIALS_CACHE is not None:
        return _SPOOLMANDB_MATERIALS_CACHE
    try:
        req = request.Request(
            "https://donkie.github.io/SpoolmanDB/materials.json",
            headers={"Accept": "application/json"},
        )
        with request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        result: dict = {}
        if isinstance(raw, list):
            for entry in raw:
                name = str(entry.get("name") or "").strip()
                density = entry.get("density")
                if name and density is not None:
                    try:
                        result[name.lower()] = float(density)
                    except (TypeError, ValueError):
                        pass
        elif isinstance(raw, dict):
            for name, entry in raw.items():
                density = entry.get("density") if isinstance(entry, dict) else entry
                if density is not None:
                    try:
                        result[name.lower()] = float(density)
                    except (TypeError, ValueError):
                        pass
        _SPOOLMANDB_MATERIALS_CACHE = result
        LOG.debug("spoolman: SpoolmanDB materials loaded (%d entries)", len(result))
        return result
    except Exception as exc:
        LOG.debug(
            "spoolman: SpoolmanDB materials fetch failed: %s — using fallback densities", exc
        )
        _SPOOLMANDB_MATERIALS_CACHE = {}
        return {}


def _fetch_spoolmandb_bambu() -> list:
    """Fetch and cache the SpoolmanDB Bambu Lab filament database.

    Returns a list of filament dicts from filaments/bambulab.json.
    On network failure returns [] so callers fall back to materials lookup.
    Result is cached in _SPOOLMANDB_BAMBU_CACHE for the process lifetime.
    """
    global _SPOOLMANDB_BAMBU_CACHE
    if _SPOOLMANDB_BAMBU_CACHE is not None:
        return _SPOOLMANDB_BAMBU_CACHE
    try:
        req = request.Request(
            "https://donkie.github.io/SpoolmanDB/filaments/bambulab.json",
            headers={"Accept": "application/json"},
        )
        with request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read())
        filaments: list = []
        if isinstance(raw, dict):
            filaments = raw.get("filaments") or []
        elif isinstance(raw, list):
            filaments = raw
        _SPOOLMANDB_BAMBU_CACHE = filaments if isinstance(filaments, list) else []
        LOG.debug(
            "spoolman: SpoolmanDB Bambu filaments loaded (%d entries)",
            len(_SPOOLMANDB_BAMBU_CACHE),
        )
        return _SPOOLMANDB_BAMBU_CACHE
    except Exception as exc:
        LOG.debug(
            "spoolman: SpoolmanDB Bambu fetch failed: %s — falling back to materials lookup",
            exc,
        )
        _SPOOLMANDB_BAMBU_CACHE = []
        return []


class SpoolmanClient:
    def __init__(self, base_url, api_key=None, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def _req(self, method, path, body=None):
        url = f"{self.base_url}{path}"
        body_str = json.dumps(body) if body is not None else ""
        data = body_str.encode("utf-8") if body_str else None

        # Build a curl command equivalent for easy copy-paste diagnosis.
        # Authorization header value is redacted so tokens never appear in logs.
        curl_parts = [f"curl -s -X {method} '{url}'"]
        for k, v in self.headers.items():
            if k.lower() == "authorization":
                curl_parts.append(f"-H '{k}: ***'")
            else:
                curl_parts.append(f"-H '{k}: {v}'")
        if body_str:
            # Escape single-quotes in the body so the shell command is valid.
            safe_body = body_str.replace("'", "'\\''")
            curl_parts.append(f"-d '{safe_body}'")
        curl_cmd = " ".join(curl_parts)

        LOG.debug("spoolman: → %s %s%s", method, url,
                  f"  body={body_str[:400]}{'...' if len(body_str) > 400 else ''}"
                  if body_str else "")
        LOG.debug("spoolman:   %s", curl_cmd)

        req = request.Request(url, data=data, headers=self.headers, method=method)
        t0 = time.monotonic()
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                elapsed_ms = (time.monotonic() - t0) * 1000
                resp_str = raw.decode("utf-8", errors="replace") if raw else ""
                LOG.debug(
                    "spoolman: ← HTTP %s %s (%.0fms)%s",
                    resp.status,
                    resp.reason,
                    elapsed_ms,
                    f"  resp={resp_str[:400]}{'...' if len(resp_str) > 400 else ''}"
                    if resp_str else "",
                )
                if not raw:
                    return None
                return json.loads(resp_str)
        except url_error.HTTPError as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            LOG.info(
                "spoolman: ← HTTP %s %s (%.0fms)%s",
                exc.code,
                exc.reason,
                elapsed_ms,
                f"  error={err_body[:400]}{'...' if len(err_body) > 400 else ''}"
                if err_body else "",
            )
            exc._body_text = err_body  # type: ignore[attr-defined]
            raise

    # --- UID extra-field helpers (rfid_uid_1 … rfid_uid_N numbered model) ---

    @staticmethod
    def uid_field_name(n):
        """Return the Spoolman extra-field key for UID slot *n* (1-based)."""
        return f"rfid_uid_{n}"

    def fields_exist(self, max_uids):
        """Return ``True`` only if all ``rfid_uid_1`` … ``rfid_uid_{max_uids}`` extra fields
        exist in Spoolman.

        Issues a single ``GET /api/v1/field/spool`` to list all spool extra fields and
        checks that every numbered slot key is present in the response.  Returns
        ``False`` if any are missing or if the request fails.  Logs at INFO so a
        missing-field situation is always visible.
        """
        try:
            fields = self._req("GET", "/api/v1/field/spool") or []
            if not isinstance(fields, list):
                fields = []
            existing_keys = {f.get("key") for f in fields if isinstance(f, dict)}
        except Exception as exc:
            LOG.info("spoolman: rfid_uid_N fields check failed: %s", exc)
            return False
        missing = [
            self.uid_field_name(n)
            for n in range(1, max_uids + 1)
            if self.uid_field_name(n) not in existing_keys
        ]
        if missing:
            LOG.info(
                "spoolman: rfid_uid_N fields check: missing %s", ", ".join(missing),
            )
            return False
        LOG.info("spoolman: rfid_uid_N fields check: all %d present", max_uids)
        return True

    def ensure_rfid_uid_fields(self, max_uids):
        """Ensure ``rfid_uid_1`` … ``rfid_uid_{max_uids}`` extra fields exist on Spoolman spools.

        Issues a single ``GET /api/v1/field/spool`` to find which fields already exist,
        then POSTs to create only the missing ones.  When all fields are already present,
        logs at INFO and returns immediately without any POST calls.
        A 409 on POST (concurrent create) is treated as success.

        Returns a ``(ok, permanent)`` tuple:
          ``(True,  True)``  — all fields are ready to use
          ``(False, True)``  — permanent skip (405/422 — won't be fixed by retrying)
          ``(False, False)`` — transient failure (network/timeout/5xx); retry later
        """
        try:
            fields = self._req("GET", "/api/v1/field/spool") or []
            if not isinstance(fields, list):
                fields = []
            existing_keys = {f.get("key") for f in fields if isinstance(f, dict)}
        except url_error.HTTPError as exc:
            if exc.code == 429:
                LOG.warning("ensure_rfid_uid_fields: rate limited listing fields")
                return False, False
            LOG.warning(
                "ensure_rfid_uid_fields: GET /api/v1/field/spool failed (HTTP %s): %s",
                exc.code, exc,
            )
            return False, 400 <= exc.code < 500
        except Exception as exc:
            LOG.warning(
                "ensure_rfid_uid_fields: GET /api/v1/field/spool failed: %s", exc
            )
            return False, False

        missing = [
            (n, self.uid_field_name(n))
            for n in range(1, max_uids + 1)
            if self.uid_field_name(n) not in existing_keys
        ]
        if not missing:
            LOG.info("spoolman: rfid_uid_N fields already exist, skipping creation")
            return True, True

        # Create only the missing fields using the keyed POST endpoint.
        for n, field_key in missing:
            try:
                self._req(
                    "POST",
                    f"/api/v1/field/spool/{field_key}",
                    {
                        "name": f"RFID UID {n}",
                        "field_type": "text",
                        "default_value": "\"\"",
                    },
                )
                LOG.info("Created Spoolman extra field: %s", field_key)
            except url_error.HTTPError as exc:
                if exc.code == 409:
                    # Concurrent create — treat as success.
                    continue
                if exc.code == 405:
                    LOG.warning(
                        "Spoolman rejected keyed field create for %s with HTTP 405",
                        field_key,
                    )
                    return False, True
                if exc.code == 422:
                    LOG.warning(
                        "Spoolman rejected field create for %s with HTTP 422",
                        field_key,
                    )
                    return False, True
                if exc.code == 429:
                    return False, False
                LOG.warning(
                    "Failed to create Spoolman extra field %s (HTTP %s): %s",
                    field_key, exc.code, exc,
                )
                return False, 400 <= exc.code < 500
            except Exception as exc:
                LOG.warning(
                    "ensure_rfid_uid_fields: POST /api/v1/field/spool/%s failed: %s",
                    field_key, exc,
                )
                return False, False

        return True, True

    def get_spool(self, spool_id):
        """Fetch a spool by ID.  Returns the spool dict."""
        return self._req("GET", f"/api/v1/spool/{spool_id}")

    def get_uid_slots(self, spool_id, max_uids):
        """Return occupied UID slots from a Spoolman spool.

        Returns a dict mapping slot index (1-based ``int``) to UID hex string for all
        ``rfid_uid_N`` fields that contain a non-empty value on ``spool_id``.
        Returns an empty dict when no UIDs are registered.
        Raises on HTTP/network errors so callers can abort to avoid data loss.
        """
        spool = self.get_spool(spool_id)
        extra = (spool or {}).get("extra") or {}
        slots = {}
        for n in range(1, max_uids + 1):
            val = (extra.get(self.uid_field_name(n)) or "").strip()
            if val:
                # Values are stored as JSON-encoded strings; decode with fallback
                # to the raw string for any legacy values written before this fix.
                # Only accept the decoded result when it is a str to avoid edge
                # cases where a digits-only UID (e.g. "1234") decodes to int.
                try:
                    decoded = json.loads(val)
                    if isinstance(decoded, str):
                        val = decoded
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                if val:
                    slots[n] = val
        return slots

    def find_spool_by_uid(self, uid_hex, max_uids):
        """Search Spoolman for a spool whose ``rfid_uid_N`` extra field equals ``uid_hex``.

        Pre-check: if the ``rfid_uid_N`` extra fields do not yet exist in Spoolman,
        no spool can possibly hold this UID — create the fields and return ``None``
        immediately to avoid false-positive matches.

        Primary path: queries each numbered slot with the JSON-encoded value (since
        Spoolman stores extra field values as JSON-encoded strings).  Each hit is
        **verified** by fetching the candidate spool's UID slots and confirming
        ``uid_hex`` is actually present before returning, preventing false positives.

        Fallback path: if all per-slot queries return empty, fetches all spools and
        scans their ``extra`` dicts in Python, decoding JSON-encoded values with a
        fallback to the raw string for backward compatibility.

        Returns ``None`` if not found.
        """
        # Short-circuit: if rfid_uid_N fields don't exist yet, no spool can have
        # this UID — create them now and skip the search entirely.
        if not self.fields_exist(max_uids):
            LOG.info(
                "find_spool_by_uid: rfid_uid_N fields not present"
                " — creating them and skipping search (uid=%s)", uid_hex,
            )
            self.ensure_rfid_uid_fields(max_uids)
            return None
        primary_had_error = False
        for n in range(1, max_uids + 1):
            field_key = self.uid_field_name(n)
            try:
                spools = self._req(
                    "GET",
                    f"/api/v1/spool?extra[{field_key}]={url_parse.quote(json.dumps(uid_hex), safe='')}",
                )
                if isinstance(spools, list) and spools:
                    spool_id_val = spools[0].get("id")
                    if spool_id_val is not None:
                        candidate_id = int(spool_id_val)
                        # Verify the candidate actually contains uid_hex to prevent
                        # false-positive matches caused by Spoolman partial matching.
                        try:
                            slots = self.get_uid_slots(candidate_id, max_uids)
                        except Exception as verify_exc:
                            LOG.debug(
                                "find_spool_by_uid: verification fetch for spool %d"
                                " failed: %s", candidate_id, verify_exc,
                            )
                            continue
                        if slots is not None and uid_hex in slots.values():
                            return candidate_id
                        LOG.info(
                            "find_spool_by_uid: query returned spool %d for uid=%s"
                            " but verification failed (uid not in extra fields)"
                            " — treating as no match", candidate_id, uid_hex,
                        )
            except Exception as exc:
                LOG.debug(
                    "find_spool_by_uid uid=%s field=%s: %s", uid_hex, field_key, exc
                )
                primary_had_error = True
        if primary_had_error:
            return None
        # Fallback: fetch all spools and scan rfid_uid_N values in Python.
        try:
            all_spools = self._req("GET", "/api/v1/spool") or []
            if isinstance(all_spools, list):
                for spool in all_spools:
                    extra = (spool.get("extra") or {})
                    for n in range(1, max_uids + 1):
                        raw_v = extra.get(self.uid_field_name(n))
                        if raw_v is None:
                            continue
                        decoded_v = str(raw_v)
                        try:
                            candidate = json.loads(str(raw_v))
                            if isinstance(candidate, str):
                                decoded_v = candidate
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
                        if decoded_v == uid_hex:
                            spool_id_val = spool.get("id")
                            if spool_id_val is not None:
                                return int(spool_id_val)
        except Exception as exc:
            LOG.debug("find_spool_by_uid fallback scan failed: %s", exc)
        return None

    def _patch_uid_field(self, spool_id, field_key, value):
        """PATCH a single ``rfid_uid_N`` field on *spool_id*.

        Values are JSON-encoded before sending (Spoolman stores extra field values
        as JSON-encoded strings).
        Returns ``True`` on success.  On HTTP errors re-raises
        ``url_error.HTTPError`` (with ``_body_text`` attached by ``_req``) so
        callers can inspect the status code and act (e.g. auto-create fields on
        400 "Unknown extra field").  Returns ``False`` on non-HTTP errors.
        """
        payload = {"extra": {field_key: json.dumps(value)}}
        try:
            self._req("PATCH", f"/api/v1/spool/{spool_id}", payload)
            LOG.debug(
                "_patch_uid_field spool=%s field=%s: succeeded",
                spool_id, field_key,
            )
            return True
        except url_error.HTTPError:
            raise  # Let callers inspect status code and body
        except Exception as exc:
            LOG.debug(
                "_patch_uid_field spool=%s field=%s failed: %s",
                spool_id, field_key, exc,
            )
            return False

    def add_uid_to_spool(self, spool_id, uid_hex, max_uids):
        """Write ``uid_hex`` into the first empty ``rfid_uid_N`` slot on ``spool_id``.

        Safe read-modify-write: slots are fetched first; if the fetch fails an
        exception is raised so callers can abort rather than silently overwrite data.
        Returns ``True`` when ``uid_hex`` is now registered (was already there, or
        written to a free slot).  Returns ``False`` when all slots are occupied,
        when field creation fails (permanent or transient), or when the retry PATCH
        fails with a non-schema error.  Raises ``HTTPError`` for non-400 HTTP errors
        on the first PATCH attempt so callers can log them at an appropriate level.

        When the PATCH returns HTTP 400 "Unknown extra field", attempts to
        auto-create all ``rfid_uid_N`` fields via ``ensure_rfid_uid_fields`` and
        retries the PATCH once.
        """
        slots = self.get_uid_slots(spool_id, max_uids)
        for n, v in slots.items():
            if v == uid_hex:
                LOG.debug(
                    "add_uid_to_spool: uid=%s already in slot %d on spool %s",
                    uid_hex, n, spool_id,
                )
                return True
        # Ensure all rfid_uid_N extra fields exist before attempting any PATCH.
        # This prevents HTTP 400 "Unknown extra field" in normal operation.
        if not self.fields_exist(max_uids):
            LOG.info(
                "add_uid_to_spool: rfid_uid_N fields not all present"
                " — creating before PATCH"
            )
            self.ensure_rfid_uid_fields(max_uids)
        for n in range(1, max_uids + 1):
            if n not in slots:
                field_key = self.uid_field_name(n)
                try:
                    return self._patch_uid_field(spool_id, field_key, uid_hex)
                except url_error.HTTPError as exc:
                    body_text = getattr(exc, "_body_text", "")
                    if exc.code == 400 and "unknown extra field" in body_text.lower():
                        # Fields don't exist — try to create them and retry once.
                        LOG.info(
                            "add_uid_to_spool: HTTP 400 Unknown extra field for spool=%s"
                            " field=%s — attempting to auto-create rfid_uid_N fields",
                            spool_id, field_key,
                        )
                        ok, _permanent = self.ensure_rfid_uid_fields(max_uids)
                        if ok:
                            try:
                                retry_ok = self._patch_uid_field(spool_id, field_key, uid_hex)
                                if retry_ok:
                                    LOG.info(
                                        "add_uid_to_spool: retry PATCH succeeded for"
                                        " spool=%s field=%s uid=%s",
                                        spool_id, field_key, uid_hex,
                                    )
                                return retry_ok
                            except url_error.HTTPError as retry_exc:
                                retry_body_text = getattr(retry_exc, "_body_text", "")
                                if (
                                    retry_exc.code == 400
                                    and "unknown extra field" in retry_body_text.lower()
                                ):
                                    # Retry hit the same expected schema problem — give up gracefully.
                                    return False
                                raise
                            except Exception:
                                return False
                        # Field creation failed (permanent or transient) — give up.
                        if _permanent:
                            LOG.warning(
                                "add_uid_to_spool: field auto-create failed for spool=%s"
                                " field=%s (permanent=%s) — uid=%s will not be persisted",
                                spool_id, field_key, _permanent, uid_hex,
                            )
                        else:
                            LOG.warning(
                                "add_uid_to_spool: field auto-create failed for spool=%s"
                                " field=%s (permanent=%s) — uid=%s was not persisted"
                                " this attempt",
                                spool_id, field_key, _permanent, uid_hex,
                            )
                        return False
                    raise  # Propagate non-400 errors to callers
        LOG.warning(
            "add_uid_to_spool: all %d rfid_uid_N slots occupied on spool %s;"
            " cannot register uid=%s",
            max_uids, spool_id, uid_hex,
        )
        return False

    def remove_uid_from_spool(self, spool_id, uid_hex, max_uids):
        """Clear the ``rfid_uid_N`` slot that contains ``uid_hex`` on ``spool_id``.

        Safe read-modify-write: slots are fetched first; raises on fetch failure.
        Returns ``True`` when ``uid_hex`` is no longer on the spool (was absent, or
        successfully cleared).  Returns ``False`` on PATCH failure (HTTP or otherwise).
        """
        slots = self.get_uid_slots(spool_id, max_uids)
        for n, v in slots.items():
            if v == uid_hex:
                try:
                    return self._patch_uid_field(spool_id, self.uid_field_name(n), "")
                except Exception as exc:
                    LOG.debug(
                        "remove_uid_from_spool spool=%s uid=%s failed: %s",
                        spool_id, uid_hex, exc,
                    )
                    return False
        return True  # uid_hex not present — nothing to do

    # --- Vendor helpers ---

    def find_vendor(self, name):
        """Return the first vendor dict whose name matches (case-insensitive), or None.

        Raises on HTTP/network errors so callers can abort instead of creating
        duplicate records on lookup failure.
        """
        results = self._req("GET", f"/api/v1/vendor?name={url_parse.quote(name, safe='')}")
        items = results if isinstance(results, list) else (results or {}).get("items", [])
        for v in items:
            if str(v.get("name", "")).lower() == name.lower():
                return v
        return None

    def create_vendor(self, name):
        """Create a new vendor and return the vendor dict."""
        return self._req("POST", "/api/v1/vendor", {"name": name})

    def find_or_create_vendor(self, name):
        """Return the vendor_id for *name*, creating the vendor if it does not exist.

        Raises ``ValueError`` when both the lookup and the create call return an
        unexpected response (e.g. Spoolman returns an empty body).
        """
        vendor = self.find_vendor(name)
        if vendor is None:
            vendor = self.create_vendor(name)
        if not isinstance(vendor, dict) or vendor.get("id") is None:
            raise ValueError(
                f"Spoolman vendor find/create for {name!r} returned unexpected response: {vendor!r}"
            )
        return int(vendor["id"])

    # --- Filament helpers ---

    def find_filament(self, name, vendor_id):
        """Return the first filament dict matching name + vendor_id, or None.

        Raises on HTTP/network errors so callers can abort instead of creating
        duplicate records on lookup failure.
        """
        results = self._req(
            "GET",
            f"/api/v1/filament?name={url_parse.quote(name, safe='')}&vendor_id={vendor_id}",
        )
        items = results if isinstance(results, list) else (results or {}).get("items", [])
        for f in items:
            vendor_info = f.get("vendor") or {}
            if (str(f.get("name", "")).lower() == name.lower()
                    and int(vendor_info.get("id", -1)) == int(vendor_id)):
                return f
        return None

    def create_filament(self, name, vendor_id, material, density=None, color_hex=None, diameter=1.75):
        """Create a new filament and return the filament dict."""
        body = {"name": name, "vendor_id": int(vendor_id), "material": material}
        # density and diameter are required by the Spoolman API; always send them.
        try:
            body["density"] = float(density) if density is not None else _DENSITY_DEFAULT
        except (TypeError, ValueError):
            body["density"] = _DENSITY_DEFAULT
        try:
            body["diameter"] = float(diameter) if diameter is not None else 1.75
        except (TypeError, ValueError):
            body["diameter"] = 1.75
        if color_hex:
            body["color_hex"] = str(color_hex).lstrip("#").upper()
        return self._req("POST", "/api/v1/filament", body)

    def find_or_create_filament(self, name, vendor_id, material,
                                density=None, color_hex=None, diameter=1.75):
        """Return the filament_id for *name*+*vendor_id*, creating it if necessary.

        Raises ``ValueError`` when both the lookup and the create call return an
        unexpected response (e.g. Spoolman returns an empty body).
        """
        fil = self.find_filament(name, vendor_id)
        if fil is None:
            fil = self.create_filament(name, vendor_id, material, density, color_hex, diameter)
        if not isinstance(fil, dict) or fil.get("id") is None:
            raise ValueError(
                f"Spoolman filament find/create for {name!r} returned unexpected response: {fil!r}"
            )
        return int(fil["id"])

    # --- Spool helpers ---

    def create_spool(self, filament_id, initial_weight=None, remaining_weight=None,
                     spool_weight=None, lot_nr=None, extra=None):
        """Create a new spool and return the spool dict.

        Args:
            filament_id:      required — the filament to associate with the spool.
            initial_weight:   weight of the filament on a brand-new full spool (grams).
            remaining_weight: current remaining filament weight (grams).
            spool_weight:     weight of the empty spool holder (grams).
            lot_nr:           lot / tray UID string for identifying this spool.
            extra:            dict of extra field key→value pairs to store on the spool.
        """
        body = {"filament_id": int(filament_id)}
        if initial_weight is not None:
            try:
                body["initial_weight"] = float(initial_weight)
            except (ValueError, TypeError):
                pass
        if remaining_weight is not None:
            try:
                body["remaining_weight"] = float(remaining_weight)
            except (ValueError, TypeError):
                pass
        if spool_weight is not None:
            try:
                body["spool_weight"] = float(spool_weight)
            except (ValueError, TypeError):
                pass
        if lot_nr:
            body["lot_nr"] = str(lot_nr)
        if extra and isinstance(extra, dict):
            body["extra"] = {str(k): str(v) for k, v in extra.items() if v is not None}
        return self._req("POST", "/api/v1/spool", body)

    def auto_create_spool(self, filament_info: dict, uid_hex: Optional[str] = None) -> Optional[int]:
        """Find or create a Spoolman vendor/filament/spool from tag filament data.

        Returns the new Spoolman spool ID on success, or None on failure.

        Required before creating anything:
          * material  — filament type (e.g. "PLA", "PETG")

        Optional but used when present:
          * color_hex   — 6-digit hex color string; omitted from filament if absent
          * weight_g    — spool weight in grams; defaults to 1000 g if not supplied
          * brand       — inferred from tag_format, falls back to "Generic"
          * diameter_mm — defaults to 1.75 mm

        The optional uid_hex argument (hardware RFID UID) is stored in the
        spool's extra field (rfid_uid_1) at creation time when provided, after
        ensuring the extra-field schema exists in Spoolman.

        Steps:
        1. Determine density via SpoolmanDB (Bambu DB or materials.json) or fallback table.
        2. Resolve vendor_id: find or create, with Generic fallback if brand fails.
        3. Search for existing filament by external_id, then material + vendor (prefer color_hex match).
        4. If no match: POST /api/v1/filament — create the filament.
        5. POST /api/v1/spool — create the spool referencing the filament,
           including lot_nr (tray_uid) and uid_hex in extra fields.
        """
        material = str(filament_info.get("material") or "").strip()
        if not material:
            LOG.debug("auto_create_spool: skipped — no material in tag data")
            return None

        color_hex = str(filament_info.get("color_hex") or "").strip().lstrip("#").upper()
        if not color_hex:
            LOG.debug(
                "auto_create_spool: no color_hex in tag data — proceeding without color"
            )

        weight_g = filament_info.get("weight_g")
        if weight_g is None:
            weight_g = _DEFAULT_SPOOL_WEIGHT_G
            LOG.debug(
                "auto_create_spool: weight not in tag data, defaulting to %d g",
                _DEFAULT_SPOOL_WEIGHT_G,
            )
        else:
            try:
                weight_g = float(weight_g)
            except (TypeError, ValueError):
                weight_g = _DEFAULT_SPOOL_WEIGHT_G
                LOG.debug(
                    "auto_create_spool: invalid weight_g in tag data: %r, defaulting to %d g",
                    filament_info.get("weight_g"), _DEFAULT_SPOOL_WEIGHT_G,
                )

        brand = str(filament_info.get("brand") or "").strip()
        if not brand:
            tag_fmt = str(filament_info.get("tag_format") or "")
            brand = _TAG_FORMAT_BRANDS.get(tag_fmt, "Generic")
            LOG.debug(
                "auto_create_spool: brand not in tag data, deduced %r from tag_format=%r",
                brand, tag_fmt,
            )

        diameter_mm = filament_info.get("diameter_mm") or 1.75
        is_bambu = bool(filament_info.get("is_bambu")) or "bambu" in brand.lower()

        # material_id (e.g. "GFA50", "GFG02") is the Bambu external filament DB id.
        # Used as Spoolman external_id to look up an existing matching filament entry.
        material_id = str(filament_info.get("material_id") or "").strip() or None
        tray_uid = str(filament_info.get("tray_uid") or "").strip() or None

        # ------------------------------------------------------------------
        # 1. Determine density (required by Spoolman POST /api/v1/filament)
        # ------------------------------------------------------------------
        density: float = _DENSITY_DEFAULT
        material_lower = material.lower().strip()

        if is_bambu:
            bambu_filaments = _fetch_spoolmandb_bambu()
            bambu_match = None
            for entry in bambu_filaments:
                if str(entry.get("material") or "").lower().strip() == material_lower:
                    bambu_match = entry
                    if color_hex:
                        colors = entry.get("colors") or []
                        for c in colors:
                            if str(c.get("hex") or "").upper().lstrip("#") == color_hex:
                                bambu_match = entry
                                color_hex = str(c.get("hex") or color_hex).upper().lstrip("#")
                                break
                    break
            if bambu_match is not None:
                try:
                    density = float(bambu_match["density"])
                    LOG.debug(
                        "auto_create_spool: density=%s from SpoolmanDB Bambu material=%s",
                        density, material,
                    )
                except (KeyError, TypeError, ValueError):
                    is_bambu = False
            else:
                is_bambu = False

        if not is_bambu:
            mat_db = _fetch_spoolmandb_materials()
            if mat_db:
                db_density = mat_db.get(material_lower)
                if db_density is not None:
                    density = db_density
                else:
                    density = _DENSITY_FALLBACK.get(material_lower, _DENSITY_DEFAULT)
                LOG.debug(
                    "auto_create_spool: density=%s from SpoolmanDB materials material=%s",
                    density, material,
                )
            else:
                density = _DENSITY_FALLBACK.get(material_lower, _DENSITY_DEFAULT)
                LOG.debug(
                    "auto_create_spool: density=%s from fallback table"
                    " (SpoolmanDB materials unavailable) material=%s",
                    density, material,
                )

        # ------------------------------------------------------------------
        # 2. Resolve or create vendor — try brand first, then Generic fallback.
        #    Raises are caught so a failed brand lookup doesn't abort the whole
        #    operation; we fall back to "Generic" instead.
        # ------------------------------------------------------------------
        vendor_id: Optional[int] = None
        resolved_vendor_name: Optional[str] = None
        _vendor_candidates: list = []
        if brand and brand.lower() != "generic":
            _vendor_candidates.append(brand)
        _vendor_candidates.append("Generic")

        for _vname in _vendor_candidates:
            try:
                vendor_id = self.find_or_create_vendor(_vname)
                resolved_vendor_name = _vname
                LOG.debug(
                    "auto_create_spool: resolved vendor id=%s name=%r", vendor_id, _vname
                )
                break
            except Exception as exc:
                LOG.debug(
                    "auto_create_spool: vendor find/create failed for %r: %s", _vname, exc
                )
                if _vname != "Generic":
                    LOG.debug(
                        "auto_create_spool: vendor resolution failed for %r,"
                        " falling back to 'Generic'", brand,
                    )

        if vendor_id is None:
            LOG.debug(
                "auto_create_spool: vendor resolution failed"
                " — proceeding without vendor_id"
            )

        # ------------------------------------------------------------------
        # 3. Search for an existing matching filament.
        #    First try by external_id (Bambu material_id like "GFA50"), then
        #    fall back to material + vendor search with color_hex preference.
        # ------------------------------------------------------------------
        filament_id: Optional[int] = None
        search_ok = True
        try:
            # 3a. Search by external_id when a Bambu material_id is available.
            if material_id:
                ext_results = self._req(
                    "GET",
                    "/api/v1/filament?" + url_parse.urlencode({"external_id": material_id}),
                )
                if isinstance(ext_results, list) and ext_results:
                    filament_id = int(ext_results[0]["id"])
                    LOG.debug(
                        "auto_create_spool: found filament id=%s by external_id=%s",
                        filament_id, material_id,
                    )

            # 3b. Fall back to material + vendor search.
            if filament_id is None:
                params: dict = {"material": material}
                if vendor_id is not None:
                    params["vendor_name"] = resolved_vendor_name
                filaments = self._req(
                    "GET", "/api/v1/filament?" + url_parse.urlencode(params)
                )
                if isinstance(filaments, list) and filaments:
                    if color_hex:
                        for f in filaments:
                            if str(f.get("color_hex") or "").upper() == color_hex:
                                filament_id = int(f["id"])
                                break
                    if filament_id is None:
                        filament_id = int(filaments[0]["id"])
                    LOG.debug(
                        "auto_create_spool: found filament id=%s material=%s",
                        filament_id, material,
                    )
        except url_error.URLError as exc:
            LOG.debug("auto_create_spool: filament search failed: %s", exc)
            search_ok = False
        except Exception:
            LOG.exception("auto_create_spool: filament search error")
            search_ok = False

        if not search_ok:
            return None

        # ------------------------------------------------------------------
        # 4. Create filament if no match found
        # ------------------------------------------------------------------
        if filament_id is None:
            filament_name = f"{brand} {material}" if brand else material
            filament_body: dict = {
                "name": filament_name,
                "material": material,
                "density": density,
                "diameter": float(diameter_mm),
                "weight": float(weight_g),
            }
            if color_hex:
                filament_body["color_hex"] = color_hex
            if vendor_id is not None:
                filament_body["vendor_id"] = vendor_id
            if material_id:
                filament_body["external_id"] = material_id
            min_temp = filament_info.get("min_temp")
            max_temp = filament_info.get("max_temp")
            bed_temp = filament_info.get("bed_temp")
            # settings_extruder_temp: use min_temp (lower hotend bound) as the
            # recommended extruder temperature; max_temp is not a separate Spoolman field.
            if min_temp is not None:
                filament_body["settings_extruder_temp"] = int(min_temp)
            elif max_temp is not None:
                filament_body["settings_extruder_temp"] = int(max_temp)
            if bed_temp is not None:
                filament_body["settings_bed_temp"] = int(bed_temp)
            try:
                created = self._req("POST", "/api/v1/filament", filament_body)
                if not isinstance(created, dict) or created.get("id") is None:
                    LOG.warning(
                        "auto_create_spool: filament create returned unexpected response (id missing): %r",
                        created,
                    )
                    return None
                filament_id = int(created["id"])
                LOG.debug(
                    "auto_create_spool: created filament id=%s name=%r density=%s",
                    filament_id, filament_name, density,
                )
            except url_error.URLError as exc:
                LOG.debug("auto_create_spool: filament create failed: %s", exc)
                return None
            except Exception:
                LOG.exception("auto_create_spool: filament create error")
                return None

        # ------------------------------------------------------------------
        # 5. Create spool, including lot_nr (tray_uid) and uid_hex extra field.
        #    The rfid_uid_1 extra field must be registered in Spoolman before
        #    it can be set on the spool.  Attempt to ensure it exists first;
        #    if that fails, create the spool without the extra UID field (the
        #    caller's add_uid_to_spool call will register it on the next scan).
        # ------------------------------------------------------------------
        spool_weight_g = filament_info.get("spool_weight_g")
        spool_extra: Optional[dict] = None
        if uid_hex:
            try:
                ok, _ = self.ensure_rfid_uid_fields(1)
                if ok:
                    spool_extra = {self.uid_field_name(1): uid_hex}
            except Exception as exc:
                LOG.debug(
                    "auto_create_spool: ensure_rfid_uid_fields failed: %s"
                    " — creating spool without extra UID field", exc
                )
        try:
            created_spool = self.create_spool(
                filament_id,
                remaining_weight=float(weight_g),
                spool_weight=spool_weight_g,
                lot_nr=tray_uid,
                extra=spool_extra,
            )
            if not isinstance(created_spool, dict) or created_spool.get("id") is None:
                LOG.warning(
                    "auto_create_spool: spool create returned unexpected response (id missing): %r",
                    created_spool,
                )
                return None
            new_spool_id = int(created_spool["id"])
            LOG.debug("auto_create_spool: created spool id=%s", new_spool_id)
            return new_spool_id
        except url_error.URLError as exc:
            LOG.debug("auto_create_spool: spool create failed: %s", exc)
            return None
        except Exception:
            LOG.exception("auto_create_spool: spool create error")
            return None

    @staticmethod
    def build_openspool_payload(spool_data: dict) -> Optional[str]:
        """Convert a Spoolman spool dict to an OpenSpool JSON string.

        Returns the JSON-encoded payload, or None if material is missing.
        The ``spoolman_id`` field is included so a subsequent read can
        immediately identify the spool without a Spoolman lookup.
        """
        filament = spool_data.get("filament") or {}
        material = str(filament.get("material") or "").strip()
        if not material:
            return None

        payload: dict = {
            "protocol": "openspool",
            "version": "1.0",
            "type": material,
        }

        color_hex = str(filament.get("color_hex") or "").strip().lstrip("#").upper()
        if color_hex:
            payload["color_hex"] = color_hex

        vendor = filament.get("vendor") or {}
        brand = str(vendor.get("name") or "").strip()
        if brand:
            payload["brand"] = brand

        min_temp = filament.get("settings_extruder_temp")
        if min_temp is not None:
            try:
                payload["min_temp"] = int(min_temp)
            except (TypeError, ValueError):
                pass

        max_temp = filament.get("settings_extruder_temp_max")
        if max_temp is not None:
            try:
                payload["max_temp"] = int(max_temp)
            except (TypeError, ValueError):
                pass

        remaining = spool_data.get("remaining_weight")
        if remaining is not None:
            try:
                payload["weight"] = float(remaining)
            except (TypeError, ValueError):
                pass

        spool_id_val = spool_data.get("id")
        if spool_id_val is not None:
            payload["spoolman_id"] = int(spool_id_val)

        return json.dumps(payload)
