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

"""
RFID integration for Klipper/AFC.

Features:
- one or more [rfid <name>] sections
- each reader may serve one or more lanes
- event-driven scan begin / commit hooks
- manual scan commands
- rotating timestamped log
- all application-level output always written to rotating log file at ~/printer_data/logs/rfid.log
- hardware/driver debug output (e.g. MFRC522/PN532 low-level traces) is only logged when debug=True
- console output: user-facing messages when messages=True; debug trace when debug=True

Happy Hare (MMU) slot support:
- Configure lanes= in the [rfid <name>] section; the same values cover both AFC and Happy Hare.
- All scan commands (RFID_SCAN, RFID_SCAN_BEGIN, RFID_SCAN_COMMIT) accept both LANE= and SLOT=.
  RFID_SLOT_SCAN / RFID_SLOT_SCAN_BEGIN / RFID_SLOT_SCAN_COMMIT are convenience aliases.
- Slots are 0-based (slot0 = gate 0); lanes are 1-based (lane1 = gate 0, lane2 = gate 1, …).
- On commit (Happy Hare path), calls MMU_GATE_MAP GATE=<n> SPOOLID=<id> (local gate assignment)
  then MMU_SPOOLMAN UPDATE=1 SPOOLID=<id> GATE=<n> (Spoolman database sync).

Config parameters (per [rfid <name>] section):
- scan_window: total time (seconds) a scan timer runs before giving up (default 10.0)
- rfid_fast_mode: when True (default), commit on the very first valid read; when False, require
  two consecutive reads of the same UID (with a fallback that commits a single-read candidate
  the moment the tag is no longer detected — useful for fast-moving spools even in safe mode)
- uid_fast_scan: when True (default), attempt a quick anticollision-only UID read before the
  full page-read scan on each tick.  If the UID is already in the local cache the full scan is
  skipped entirely, saving ~50–200 ms per tick for previously-seen tags.  Falls back to the full
  scan automatically on a cache miss or if the fast read is unavailable for the current driver.
- candidate_ttl: (fast_mode=False only) how long (seconds) a scan candidate survives without
  re-sighting before it ages out (default 0.5, min 0.1)
- auto_commit_on_scan: when True, automatically run RFID_SCAN_COMMIT immediately after a valid
  spoolman_id is stored in _pending by the scan timer engine (default False).  Useful when scans
  happen outside an AFC load cycle and afc:lane_loaded may not fire.  Has no effect on
  synchronous GCode scan paths (_run_scan_window_sync / RFID_SCAN_BEGIN), which handle their
  own commit flow.
- max_uids: maximum number of RFID tags (UIDs) that can be associated with a single
  spool in Spoolman.  Each UID is stored in a separate extra field (rfid_uid_1 …
  rfid_uid_N).  Default 8, min 2.
- max_pages: number of NTAG/Ultralight pages to read starting at page 4 (default 135, min 4).
  The low-level drivers iterate pages in range(4, 4 + max_pages). Reads stop early as soon as
  a valid spool_id is parsed, so a higher ceiling has no speed cost in the normal case.
  NTAG page counts for reference (total / user pages, where user pages start at 4):
    NTAG213: 45 total pages (41 user pages) — use max_pages=41 to cover the full user area.
    NTAG215: 135 total pages (131 user pages) — the default max_pages=135 safely covers these.
    NTAG216: 231 total pages (227 user pages) — use max_pages>=227 (e.g. 231) to cover all user pages.
- scan_backoff_after / scan_backoff_delay: REMOVED.  These options are silently ignored if still
  present in a config file.  The scan loop always uses scan_delay regardless of no-tag streak.

Spoolman UID extra-field integration:
- If spoolman_url is configured (and extras/spoolman_client.py is importable), the RFID UID
  is stored in the spool's numbered extra fields (rfid_uid_1 … rfid_uid_N).  Each UID
  occupies one slot; max_uids controls the maximum number of slots per spool.
- rfid_uid_N extra fields must be created manually in Spoolman (Settings → Extra Fields)
  or via ``POST /api/v1/field/spool``.  A one-time warning is emitted when first
  PATCH attempt returns HTTP 400 "Unknown extra field".
- All HTTP calls run in background threads so they never block the Klipper reactor
  event loop (which would cause an MCU "timer too close" shutdown).
- If auto_create_spool=True and a scanned tag carries OpenSpool JSON (identified by
  protocol "openspool", with the "type" field describing the material) but no spoolman_id,
  a new vendor/filament/spool is created in Spoolman automatically.
Config parameters for Spoolman integration (per [rfid <name>] section):
- spoolman_url: base URL of your Spoolman instance (e.g. http://localhost:7912); required to
  enable any Spoolman HTTP integration
- spoolman_api_key: optional Bearer token for Spoolman authentication
- spoolman_timeout: HTTP request timeout in seconds (default 5.0)
- auto_create_spool: when True, auto-create a Spoolman spool from an OpenSpool NDEF tag that
  carries no spoolman_id (default False)
- auto_write: when True, write the resolved spool_id back to the RFID tag NDEF payload after
  a successful Spoolman UID lookup or auto-create; best-effort only — silently skipped if the
  tag has moved (default False)

UID → spoolman_id cache:
- _UID_SPOOL_CACHE is a module-level dict shared across all Rfid instances in the process.
- It is updated every time a uid_hex + spoolman_id pair is resolved (full read or cache hit);
  a _UID_CACHE_DIRTY flag is set so that actual disk writes are deferred and never happen
  inside a reactor timer callback.
- It is consulted in _scan_once when uid_hex is known but spoolman_id could not be parsed,
  making reads reliable even when the tag only passes the reader long enough for UID anticoll.
- Use RFID_CACHE_CLEAR to wipe the cache; RFID_CACHE_LIST to inspect it.
- The cache is persisted to _CACHE_PATH (~/RFID/cache/rfid_uid_cache.json) as JSON and
  survives Klipper restarts.  On the first Rfid instance init (after logger setup),
  _ensure_uid_cache_loaded() populates _UID_SPOOL_CACHE from that file;
  _flush_uid_cache_if_dirty() writes it atomically when a lane finishes loading, when a
  scan is committed via GCode, when Klipper disconnects or shuts down, or when the cache
  is explicitly cleared.

Klipper reactor-safety rules implemented here:
- Event handlers (register_event_handler) must never call reactor.pause() or
  perform any blocking operation.  They schedule work via register_timer().
- Timer callbacks perform one scan attempt per call and re-schedule themselves
  for the next retry by returning (event_time + delay).  No reactor.pause()
  is used inside them.  Timer callbacks only consult _UID_SPOOL_CACHE — they
  never make Spoolman HTTP calls.
- GCode command handlers run in a Klipper "completion" greenlet and MAY call
  reactor.pause().  _run_scan_window_sync() and _event_scan_begin() are therefore
  reserved exclusively for the GCode command path.  GCode-triggered scans now use
  the same timer-based scan engine as AFC events, including fast_mode / safe-mode
  two-read confirmation, candidate_ttl aging, _scan_blocked_uids window, and
  scan_window deadline enforcement.
- All Spoolman HTTP operations must run off the reactor thread so blocking network
  I/O never stalls the reactor.  All Spoolman network access is performed via
  SpoolmanClient methods (find_spool_by_uid, add_uid_to_spool, auto_create_spool,
  etc.), dispatched via _spoolman_run_async() or from GCode/completion contexts,
  never from timer/event callbacks on the reactor thread.
"""

from __future__ import annotations

import configparser
import concurrent.futures
import json
import logging
import logging.handlers
import os
import re
import time
from typing import Optional

from extras import bus
from extras.mfrc522 import MFRC522Handler, MFRC522Device
try:
    from extras.pn532 import PN532Handler
except ImportError:
    PN532Handler = None
    logging.getLogger(__name__).info(
        "PN532 support not available: extras/pn532.py could not be imported"
    )
try:
    from extras import rfid_tag_parser as _tag_parser
    _parse_tag = _tag_parser.parse_tag
except Exception:
    _tag_parser = None
    _parse_tag = None
    logging.getLogger(__name__).warning(
        "rfid_tag_parser not available: extras/rfid_tag_parser.py could not be imported"
        " — auto_create_spool will be disabled",
        exc_info=True,
    )
try:
    from extras.spoolman_client import SpoolmanClient
except ImportError:
    SpoolmanClient = None  # type: ignore[assignment,misc]
    logging.getLogger(__name__).info(
        "SpoolmanClient not available: extras/spoolman_client.py could not be imported"
    )

_LOGGERS = {}
_GLOBAL_CMDS_ATTR = "_rfid_global_registered"
_LOG_PATH = os.path.expanduser("~/printer_data/logs/rfid.log")
_CACHE_PATH = os.path.expanduser("~/RFID/cache/rfid_uid_cache.json")

# Minimum inter-attempt delay for async (timer-based) scans.
# Even when scan_delay=0.0 is configured, we must yield the reactor
# between timer callbacks to avoid starving the event loop.
_ASYNC_MIN_DELAY = 0.010  # 10 ms

# How long to wait before retrying a failed Spoolman fallback lookup in _handle_lane_loaded.
_SPOOLMAN_RETRY_DELAY_S = 3.0

# How long (seconds) a deferred UID saved at scan-window expiry remains valid for use
# by a subsequent lane_loaded event.  Prevents stale UIDs from being applied to a later
# load cycle that happens to use the same lane.
_DEFERRED_UID_TTL_S = 120.0

# Maximum authentication rounds per tag UID per scan window.  Set to 1 so
# each Bambu tag gets exactly one authenticated Key A read attempt before being
# marked exhausted for the remainder of the window, preventing repeated hammering.
_BAMBU_MAX_ROUNDS = 1

# Default factory MIFARE Classic Key A used on tags that have not had their
# sector keys personalised.  Used as a fallback when HKDF-derived keys fail.
_DEFAULT_MIFARE_KEY = b"\xff\xff\xff\xff\xff\xff"

# UID → spoolman_id cache: populated on every successful full read so that
# a subsequent pass that only yields a UID (no NDEF payload) can still
# resolve the spool ID.  Shared across all Rfid instances in this process.
# Thread-safety note: Klipper's reactor runs all timer callbacks sequentially
# in a single thread, so dict access here is safe without a lock.


def _load_uid_cache() -> dict:
    """Load the persisted UID→spoolman_id cache from disk. Returns {} on any error."""
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        clean = {}
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, int):
                clean[k] = v
            elif isinstance(v, dict) and isinstance(v.get("sid"), int):
                # Backward-compat: old fingerprinted entry {"sid": <int>, ...}
                # Extract just the int spool ID; fingerprinting is no longer used.
                clean[k] = v["sid"]
            else:
                try:
                    clean[k] = int(v)
                except Exception:
                    pass
        return clean
    except FileNotFoundError:
        return {}
    except Exception:
        logging.getLogger("rfid").warning(
            "rfid: failed to load UID cache from %s, starting empty",
            _CACHE_PATH,
            exc_info=True,
        )
        return {}


def _save_uid_cache(cache: dict) -> None:
    """Persist the UID→spoolman_id cache to disk. Best-effort; logs on failure."""
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        logging.getLogger("rfid").warning(
            "rfid: failed to save UID cache to %s", _CACHE_PATH, exc_info=True
        )


_UID_SPOOL_CACHE: dict = {}  # uid_hex -> int (spoolman_id); local write-through speed layer
# True whenever an in-memory entry was added since the last flush to disk.
_UID_CACHE_DIRTY: bool = False
# Populated lazily on first Rfid.__init__ so disk I/O occurs after logger setup.
_UID_CACHE_LOADED: bool = False

def _ensure_uid_cache_loaded() -> None:
    """Lazily populate _UID_SPOOL_CACHE from disk on the first Rfid instance init.

    Called once from Rfid.__init__() after _setup_logger so that any load
    warnings reach the rfid log file rather than the root logger.
    Subsequent calls are no-ops.
    """
    global _UID_CACHE_LOADED
    if _UID_CACHE_LOADED:
        return
    _UID_CACHE_LOADED = True
    _UID_SPOOL_CACHE.update(_load_uid_cache())


def _mark_uid_cache_dirty() -> None:
    """Mark the cache as needing a flush.  Called from scan timer callbacks."""
    global _UID_CACHE_DIRTY
    _UID_CACHE_DIRTY = True


def _flush_uid_cache_if_dirty() -> None:
    """Write the cache to disk if it has changed since the last flush.

    Safe to call from any context where blocking I/O is acceptable
    (e.g. GCode completion greenlets, lane-loaded event handlers).
    Clears the dirty flag on success.
    """
    global _UID_CACHE_DIRTY
    if _UID_CACHE_DIRTY:
        _save_uid_cache(_UID_SPOOL_CACHE)
        _UID_CACHE_DIRTY = False


def _cache_entry_sid(entry) -> Optional[int]:
    """Extract the spoolman_id from a cache entry (always a plain int now)."""
    if isinstance(entry, int):
        return entry
    return None


def _parse_lane_list(cfg_value) -> list[str]:
    if cfg_value is None:
        return []
    text = str(cfg_value).strip()
    if not text:
        return []
    parts = re.split(r"[,\s]+", text)
    lanes = []
    seen = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low.isdigit():
            low = f"lane{low}"
        if low not in seen:
            seen.add(low)
            lanes.append(low)
    return lanes


def _parse_slot_list(cfg_value) -> list[str]:
    if cfg_value is None:
        return []
    text = str(cfg_value).strip()
    if not text:
        return []
    parts = re.split(r"[,\s]+", text)
    slots = []
    seen = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        low = part.lower()
        if low.isdigit():
            low = f"slot{low}"
        if low not in seen:
            seen.add(low)
            slots.append(low)
    return slots


def _setup_logger(name: str) -> logging.Logger:
    logger_name = f"rfid.{name}"
    if logger_name in _LOGGERS:
        return _LOGGERS[logger_name]

    # Attach the RotatingFileHandler once to the shared parent 'rfid' logger so
    # that multiple [rfid <name>] sections all share one file handler, preventing
    # duplicate output and unsafe concurrent rotation of the same file.
    if "rfid" not in _LOGGERS:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        parent = logging.getLogger("rfid")
        parent.setLevel(logging.INFO)
        fh = logging.handlers.RotatingFileHandler(
            _LOG_PATH,
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(name)s] %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
        )
        parent.addHandler(fh)
        _LOGGERS["rfid"] = parent

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = True  # propagate to 'rfid' parent which owns the file handler

    _LOGGERS[logger_name] = logger
    return logger


class Rfid:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.gcode = self.printer.lookup_object("gcode")
        self._log = _setup_logger(self.name)
        _ensure_uid_cache_loaded()

        self.spi_speed = int(config.getint("spi_speed", 100000, minval=10000))
        self.messages = config.getboolean("messages", True)
        self.debug_en = config.getboolean("debug", False)
        self.event_timeout = float(config.getfloat("event_timeout", 60.0, minval=1.0))
        self.scan_delay = float(config.getfloat("scan_delay", 0.05, minval=0.0))
        self.max_pages = int(config.getint("max_pages", 135, minval=4))
        self.max_uids = int(config.getint("max_uids", 8, minval=2))
        self.scan_window = float(config.getfloat("scan_window", 10.0, minval=1.0))
        self.fast_mode = config.getboolean("rfid_fast_mode", True)
        self.candidate_ttl = float(config.getfloat("candidate_ttl", 0.5, minval=0.1))
        self.uid_fast_scan = config.getboolean("uid_fast_scan", True)
        # DEPRECATED: scan_backoff_after and scan_backoff_delay are removed.  They are
        # consumed silently so existing configs do not error out, but they have no effect.
        # The scan loop always uses scan_delay.  Remove these from your config file.
        _dep_bk_after = config.getint("scan_backoff_after", 0, minval=0)
        _dep_bk_delay = config.getfloat("scan_backoff_delay", 0.0, minval=0.0)
        if _dep_bk_after or _dep_bk_delay:
            logging.getLogger("rfid").warning(
                "rfid[%s]: config options 'scan_backoff_after' and 'scan_backoff_delay' are"
                " deprecated and have no effect — remove them from your config",
                config.get_name(),
            )
        self.auto_write = config.getboolean("auto_write", False)
        self.auto_commit_on_scan = config.getboolean("auto_commit_on_scan", False)
        self.auto_create_spool = config.getboolean("auto_create_spool", False)
        self.spoolman_url = config.get("spoolman_url", "").strip().rstrip("/")
        self._spoolman_api_key = config.get("spoolman_api_key", None)
        self._spoolman_timeout = config.getfloat("spoolman_timeout", 5.0, minval=1.0)
        self._spoolman_uid_index = config.getboolean("spoolman_uid_index", False)
        self._spoolman_uid_index_ttl = config.getfloat("spoolman_uid_index_ttl", 60.0, minval=1.0)

        lanes_cfg = config.get("lanes", None)
        if lanes_cfg is None:
            lanes_cfg = config.get("lane", None)
        self.lanes = _parse_lane_list(lanes_cfg)
        if not self.lanes:
            self.lanes = ["lane1", "lane2"]
        self.lane = self.lanes[0]

        # Happy Hare slot mapping: only mirror lanes into slots when all lane
        # values are numeric or "lane{n}"-shaped (i.e. valid gate indices).
        # Otherwise default to [] so HH commands are inactive unless slots= is
        # explicitly configured.
        slots_cfg = config.get("slots", None)
        if slots_cfg is None:
            slots_cfg = config.get("slot", None)
        if slots_cfg is not None:
            self.slots = _parse_slot_list(slots_cfg)
        else:
            lanes_are_gate_like = all(
                isinstance(l, str) and (l.isdigit() or re.fullmatch(r"lane\d+", l))
                for l in self.lanes
            )
            self.slots = list(self.lanes) if lanes_are_gate_like else []

        self.driver_cfg = config.get("driver", "auto").strip().lower()

        self.spi = bus.MCU_SPI_from_config(config, mode=0, default_speed=self.spi_speed)
        self._detected_driver: bool = False
        if self.driver_cfg == "auto":
            # Use MFRC522Handler as a safe placeholder until klippy:connect fires
            # and SPI commands are live.  The real probe in _handle_klippy_connect
            # will replace self.reader with the correct handler.
            self.reader = MFRC522Handler(self.spi)
        else:
            self.reader = self._init_driver(self.driver_cfg)
            self._wire_reader(self.reader)

        self._pending: dict[str, dict] = {}
        self._scan_timers: dict[str, object] = {}    # lane -> active timer handle
        self._scan_deadlines: dict[str, float] = {}  # lane -> monotonic deadline
        self._scan_candidates: dict[str, dict] = {}  # lane -> {uid_hex, spoolman_id, count, last_ts}
        self._scan_blocked_uids: dict[str, set] = {} # lane -> set of UIDs suppressed for current window
        self._scan_seen_uids: dict[str, set] = {}    # lane -> all uid_hex strings seen this window
        self._sync_scan_lanes: set = set()            # lanes currently polled by _run_scan_window_sync
        self._scan_gen: dict[str, int] = {}           # lane -> generation counter (invalidates stale callbacks)
        self._scan_no_tag_streak: dict[str, int] = {} # lane -> consecutive no-tag tick count
        self._scan_tick_count: dict[str, int] = {}    # lane -> total ticks this window
        self._scan_last_uid: dict[str, Optional[str]] = {} # lane -> most recently seen uid_hex this window
        self._commit_in_progress: dict[str, bool] = {} # lane -> True once a commit has been decided
        self._uid_lookup_in_flight: dict[str, bool] = {} # lane -> True while a deferred Spoolman fallback lookup is running
        self._lane_committed: dict[str, bool] = {}   # lane -> True after _event_scan_commit has succeeded once this session
        # Tracks whether afc:lane_loaded has been received for a lane in the
        # current scan session.  Set by _handle_lane_loaded; cleared by
        # _start_scan_timer so each new load cycle starts with a clean slate.
        # Used by _on_created (auto_create_spool) to decide whether to commit
        # immediately (lane already loaded) or leave the spool in _pending and
        # wait for lane_loaded to trigger the commit.
        self._lane_loaded_seen: dict[str, bool] = {}
        # Survives _clear_scan_state so lane_loaded can still find a deferred UID even
        # when the scan window timer expired before the lane_loaded event arrived.
        # Format: lane -> {"last_uid": str|None, "seen_uids": set, "ts": float}
        self._deferred_uid: dict[str, dict] = {}
        # Per-window failure cache for Classic authenticated reads.
        # Prevents repeated attempts for the same UID when auth/read already
        # failed earlier in the same scan window.
        # Format: lane -> {uid_hex: monotonic_timestamp_of_failure}
        self._auth_fail_uids: dict[str, dict[str, float]] = {}
        # Per-window set of UIDs whose Bambu blocks have been fully and successfully
        # read and parsed in the current scan window.  Once a UID is here, subsequent
        # _scan_once calls skip the expensive MIFARE auth re-read for that UID.
        # Format: lane -> set of uid_hex strings
        self._scan_complete_uids: dict[str, set[str]] = {}

        self._mmu_system = None  # "afc" or "hh"; detected at klippy:connect

        # Spoolman integration (async HTTP via bounded ThreadPoolExecutor)
        self._spoolman: Optional[SpoolmanClient] = None
        self._spoolman_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        if self.spoolman_url and SpoolmanClient is not None:
            self._spoolman = SpoolmanClient(
                self.spoolman_url,
                api_key=self._spoolman_api_key,
                timeout=self._spoolman_timeout,
                use_uid_index=self._spoolman_uid_index,
                uid_index_ttl=self._spoolman_uid_index_ttl,
            )
            self._spoolman_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="rfid_spoolman"
            )

        self.printer.register_event_handler("klippy:connect", self._handle_klippy_connect)
        self.printer.register_event_handler("afc:lane_prep_start", self._handle_lane_prep_start)
        self.printer.register_event_handler("afc:lane_loaded", self._handle_lane_loaded)
        self.printer.register_event_handler("klippy:disconnect", self._handle_klippy_flush)
        self.printer.register_event_handler("klippy:shutdown", self._handle_klippy_flush)

        self.gcode.register_mux_command(
            "RFID_TAG",
            "NAME",
            self.name,
            self.cmd_RFID_TAG,
            desc="Read tag on this reader and report UID + spoolman_id if found.",
        )

        if not hasattr(self.printer, _GLOBAL_CMDS_ATTR):
            setattr(self.printer, _GLOBAL_CMDS_ATTR, True)
            self.gcode.register_command(
                "RFID_LANES",
                self.cmd_RFID_LANES,
                desc="Show which lanes/slots are mapped to each reader.",
            )
            self.gcode.register_command(
                "RFID_PENDING",
                self.cmd_RFID_PENDING,
                desc="Show pending RFID scan assignments.",
            )
            self.gcode.register_command(
                "RFID_SCAN",
                self.cmd_RFID_SCAN,
                desc="Scan and immediately assign spool to AFC lane or Happy Hare gate (LANE= or SLOT=).",
            )
            self.gcode.register_command(
                "RFID_SCAN_BEGIN",
                self.cmd_RFID_SCAN_BEGIN,
                desc="Scan and store pending result for AFC lane or Happy Hare gate (LANE= or SLOT=).",
            )
            self.gcode.register_command(
                "RFID_SCAN_COMMIT",
                self.cmd_RFID_SCAN_COMMIT,
                desc="Commit pending scan to AFC lane or Happy Hare gate (LANE= or SLOT=).",
            )
            self.gcode.register_command(
                "RFID_BEGIN_LOAD",
                self.cmd_RFID_SCAN_BEGIN,
                desc="Alias of RFID_SCAN_BEGIN.",
            )
            self.gcode.register_command(
                "RFID_COMMIT_LOAD",
                self.cmd_RFID_SCAN_COMMIT,
                desc="Alias of RFID_SCAN_COMMIT.",
            )
            self.gcode.register_command(
                "RFID_CACHE_CLEAR",
                self.cmd_RFID_CACHE_CLEAR,
                desc="Clear the UID→spoolman_id scan cache.",
            )
            self.gcode.register_command(
                "RFID_CACHE_LIST",
                self.cmd_RFID_CACHE_LIST,
                desc="List all entries in the UID→spoolman_id scan cache.",
            )
            # Slot-named aliases kept for backward compatibility.
            self.gcode.register_command(
                "RFID_SLOTS",
                self.cmd_RFID_LANES,
                desc="Alias of RFID_LANES.",
            )
            self.gcode.register_command(
                "RFID_SLOT_SCAN",
                self.cmd_RFID_SCAN,
                desc="Alias of RFID_SCAN.",
            )
            self.gcode.register_command(
                "RFID_SLOT_SCAN_BEGIN",
                self.cmd_RFID_SCAN_BEGIN,
                desc="Alias of RFID_SCAN_BEGIN.",
            )
            self.gcode.register_command(
                "RFID_SLOT_SCAN_COMMIT",
                self.cmd_RFID_SCAN_COMMIT,
                desc="Alias of RFID_SCAN_COMMIT.",
            )
            self.gcode.register_command(
                "RFID_CHECK_TAG",
                self.cmd_RFID_CHECK_TAG,
                desc=(
                    "Scan a single tag, parse filament metadata, and optionally "
                    "create a Spoolman spool. "
                    "Params: LANE=|SLOT= CREATE=0|1 WRITE=0|1"
                ),
            )
            self.gcode.register_command(
                "RFID_WRITE",
                self.cmd_RFID_WRITE,
                desc=(
                    "Fetch spool info from Spoolman by ID and write it to the tag "
                    "in OpenSpool format. Params: LANE=<n>|SLOT=<n> SPOOLID=<id>"
                ),
            )
            self.gcode.register_command(
                "RFID_BAMBU_WRITE",
                self.cmd_RFID_BAMBU_WRITE,
                desc=(
                    "Write a Tray UID (SPOOLID) to block 9 of a Bambu-compatible "
                    "MIFARE Classic tag using Key B authentication. "
                    "Params: LANE=<n>|SLOT=<n> [TRAY_UID=<32-char hex>]"
                ),
            )
            self.gcode.register_command(
                "RFID_ERASE",
                self.cmd_RFID_ERASE,
                desc=(
                    "Erase the NDEF payload on the tag at LANE=<n> and remove it from the UID cache. "
                    "Params: LANE=<n>|SLOT=<n>"
                ),
            )

        self._log.info(
            "module loaded: reader=%s lanes=%s spi_speed=%s debug=%s messages=%s",
            self.name,
            self.lanes,
            self.spi_speed,
            self.debug_en,
            self.messages,
        )

    # ---------- logging ----------
    def _emit_console(self, msg: str) -> None:
        """Emit to Klipper console when messages=True."""
        if self.messages:
            self.gcode.respond_info(msg)

    def _respond(self, msg: str) -> None:
        """User-facing message: always logged; printed to console when messages=True."""
        self._log.info(msg)
        self._emit_console(msg)

    def _debug(self, msg: str) -> None:
        """Debug message: always written to log file; never printed to console.

        User-facing messages should go through _respond() which respects the
        ``messages`` gate.  Emitting debug lines to the Klipper console caused
        duplicate entries in klippy.log (rotating log + gcode response sink)
        and could trigger BlockingIOError on the gcode FD under load.
        """
        self._log.info(msg)

    def _debug_verbose(self, msg: str) -> None:
        """Verbose trace: always written to log file; NEVER printed to console.
        Use for high-frequency per-tick/per-tag hardware trace messages."""
        self._log.info(msg)

    # ---------- driver init ----------
    def _wire_reader(self, reader) -> None:
        """Attach reactor and debug-log callbacks to a freshly created reader handler."""
        if hasattr(reader, "set_reactor"):
            reader.set_reactor(self.reactor)
        if hasattr(reader, "set_debug_log"):
            reader.set_debug_log(self._debug if self.debug_en else None)
        if hasattr(reader, "dev") and hasattr(reader.dev, "set_debug_log"):
            reader.dev.set_debug_log(self._debug if self.debug_en else None)

    def _init_driver(self, driver_cfg: str):
        """Instantiate the correct reader backend.

        driver_cfg values:
          "mfrc522" – always use MFRC522Handler
          "pn532"   – always use PN532Handler
          "auto"    – probe MFRC522 first; fall back to PN532 if version check fails
        """
        if driver_cfg == "pn532":
            self._log.info("rfid[%s]: driver=pn532 (configured)", self.name)
            return PN532Handler(self.spi)

        if driver_cfg == "mfrc522":
            self._log.info("rfid[%s]: driver=mfrc522 (configured)", self.name)
            return MFRC522Handler(self.spi)

        # auto-detect
        try:
            candidate = MFRC522Handler(self.spi)
            if hasattr(candidate, "set_reactor"):
                candidate.set_reactor(self.reactor)
            ver = candidate.read_version()
            if ver in MFRC522Device.VALID_VERSION_VALUES:
                self._log.info(
                    "rfid[%s]: driver=mfrc522 (auto-detected, version=0x%02X)", self.name, ver
                )
                return candidate
            self._log.info(
                "rfid[%s]: MFRC522 version 0x%02X not recognized, trying PN532", self.name, ver
            )
        except Exception:
            self._log.info(
                "rfid[%s]: MFRC522 probe failed, trying PN532", self.name, exc_info=True
            )

        try:
            candidate = PN532Handler(self.spi)
            if hasattr(candidate, "set_reactor"):
                candidate.set_reactor(self.reactor)
            fw = candidate.dev.get_firmware_version()
            self._log.info(
                "rfid[%s]: driver=pn532 (auto-detected, fw=%d.%d)",
                self.name,
                fw["ver"],
                fw["rev"],
            )
            return candidate
        except Exception:
            self._log.info(
                "rfid[%s]: PN532 probe also failed; defaulting to mfrc522",
                self.name,
                exc_info=True,
            )

        return MFRC522Handler(self.spi)

    # ---------- helpers ----------
    def _normalize_lane(self, lane) -> str:
        lane = str(lane).strip().lower()
        if lane.startswith("lane"):
            return lane
        if lane.isdigit():
            return f"lane{lane}"
        return lane

    def _all_readers(self):
        readers = []
        pobj = getattr(self.printer, "objects", None)
        if isinstance(pobj, dict):
            for objname, obj in pobj.items():
                if objname.startswith("rfid "):
                    readers.append(obj)
        if not readers:
            for name in (
                "mfrc522_0", "mfrc522_1", "mfrc522_2", "mfrc522_3",
                "pn532_0", "pn532_1", "pn532_2", "pn532_3",
                "lane1", "lane2", "lane3", "lane4",
            ):
                try:
                    readers.append(self.printer.lookup_object(f"rfid {name}"))
                except Exception:
                    pass
        return readers

    def _is_spool_assigned_elsewhere(self, lane: str, spoolman_id: int) -> bool:
        """Return True if spoolman_id is already claimed by a lane other than *lane*.

        Checks two sources:
        1. In-flight _pending entries across all readers.
        2. Live AFC_lane objects via printer.lookup_object().

        All lookups are wrapped in broad except guards so that missing AFC
        objects simply cause the check to return False.
        """
        lane = self._normalize_lane(lane)
        # 1. Check in-flight pending scans across every reader.
        try:
            for reader in self._all_readers():
                pending = getattr(reader, "_pending", {})
                for pending_lane, entry in pending.items():
                    if self._normalize_lane(pending_lane) == lane:
                        continue
                    if entry.get("spoolman_id") == spoolman_id:
                        self._debug_verbose(
                            f"rfid[{self.name}]: DBG _is_spool_assigned_elsewhere:"
                            f" spoolman_id={spoolman_id} blocked by in-flight pending"
                            f" on lane={pending_lane} reader={getattr(reader, 'name', '?')}"
                        )
                        return True
        except Exception:
            pass
        # 2. Check live AFC lane objects.
        try:
            for reader in self._all_readers():
                for mapped_lane in getattr(reader, "lanes", []):
                    other = self._normalize_lane(mapped_lane)
                    if other == lane:
                        continue
                    try:
                        lane_obj = self.printer.lookup_object(f"AFC_lane {other}")
                        for attr in ("spool_id", "tool_spool_id"):
                            val = getattr(lane_obj, attr, None)
                            if val is not None:
                                try:
                                    if int(val) == spoolman_id:
                                        self._debug_verbose(
                                            f"rfid[{self.name}]: DBG _is_spool_assigned_elsewhere:"
                                            f" spoolman_id={spoolman_id} blocked by AFC_lane {other}"
                                            f" attr={attr} val={val}"
                                        )
                                        return True
                                except (ValueError, TypeError):
                                    pass
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def _find_reader_for_lane(self, lane: str):
        lane = self._normalize_lane(lane)
        for reader in self._all_readers():
            for mapped in getattr(reader, "lanes", []):
                if self._normalize_lane(mapped) == lane:
                    return reader, lane
        return None, lane

    def _lane_name_from_event(self, lane_obj) -> Optional[str]:
        lane_name = getattr(lane_obj, "name", lane_obj)
        if lane_name is None:
            return None
        return self._normalize_lane(lane_name)

    def _extract_spoolman_id(self, text: Optional[str]) -> Optional[int]:
        if not text:
            return None
        text = str(text).strip()
        if not text:
            return None

        try:
            payload = json.loads(text)
            for key in ("spoolman_id", "spool_id", "spoolId", "id"):
                value = payload.get(key)
                if value is not None and str(value).strip() != "":
                    return int(str(value).strip())
        except Exception:
            pass

        patterns = [
            r"(?:^|[?&;,\s])spoolman_id=(\d+)(?:$|[&;,\s])",
            r"(?:^|[?&;,\s])spool_id=(\d+)(?:$|[&;,\s])",
            r'"spoolman_id"\s*:\s*"?(\d+)"?',
            r'"spool_id"\s*:\s*"?(\d+)"?',
            r"\bspoolman[_ -]?id\b\s*[:=]\s*(\d+)",
            r"\bspool[_ -]?id\b\s*[:=]\s*(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        if re.fullmatch(r"\d+", text):
            return int(text)
        return None

    def _read_tag_text(self, max_pages: int) -> Optional[str]:
        for method_name in (
            "read_ntag_ndef_text",
            "read_ntag_text",
            "read_ndef_text",
            "read_text",
        ):
            method = getattr(self.reader, method_name, None)
            if method is None:
                continue
            try:
                return method(max_pages=max_pages)
            except TypeError:
                try:
                    return method(max_pages)
                except Exception:
                    self._log.exception("reader method %s failed", method_name)
            except Exception:
                self._log.exception("reader method %s failed", method_name)
        return None

    def _read_uid_hex(self) -> Optional[str]:
        for method_name in ("read_uid_hex", "get_uid_hex", "uid_hex", "read_uid"):
            method = getattr(self.reader, method_name, None)
            if method is None:
                continue
            try:
                uid = method()
                if uid is None:
                    return None
                if isinstance(uid, (list, tuple, bytes, bytearray)):
                    return "".join("%02X" % b for b in uid)
                return str(uid)
            except Exception:
                self._log.exception("reader method %s failed", method_name)
        return None

    @staticmethod
    def _bambu_blocks_ok(result: Optional[dict]) -> bool:
        """Return True only when required Bambu metadata blocks (2 and 5) are present.

        Block 2 contains the basic filament type string; block 5 contains color,
        spool weight, and diameter.  Without both we cannot parse meaningful
        filament info.  (Block numbering per BambuLabRfid.md.)
        """
        if result is None:
            return False
        blocks = result.get("blocks") or {}
        b2 = blocks.get(2)
        b5 = blocks.get(5)
        return (
            isinstance(b2, (bytes, bytearray)) and len(b2) == 16
            and isinstance(b5, (bytes, bytearray)) and len(b5) == 16
        )

    def _try_bambu_read(
        self,
        uid_hex: str,
        attempt_num: int = 1,
    ) -> Optional[dict]:
        """Attempt a Bambu Lab MIFARE Classic authenticated read using Key A only.

        Derives the 16 sector keys from the UID via HKDF (pycryptodome required),
        then authenticates and reads all sectors via the reader driver.

        Bambu Lab tags use HKDF-derived Key A keys exclusively (RFID-A\\0 context).
        Key B is not used — a single Key A attempt is made per call.

        Returns a dict ``{"uid_bytes": ..., "blocks": {...}}`` suitable for
        ``parse_tag()``, or None on failure.
        """
        if _tag_parser is None:
            return None
        try:
            uid_bytes = bytes.fromhex(uid_hex)
        except Exception:
            return None
        self._debug(
            f"rfid[{self.name}]: Bambu key derivation starting uid={uid_hex}"
            f" uid_len={len(uid_bytes)}B"
        )
        try:
            key_list = _tag_parser._bambu_derive_keys(uid_bytes)
        except ImportError as exc:
            self._log.warning(
                "rfid[%s]: pycryptodome not available — Bambu key derivation skipped"
                " uid=%s. Install with: pip3 install pycryptodome. (%s)",
                self.name, uid_hex, exc,
            )
            return None
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: Bambu key derivation failed uid=%s: %s",
                self.name, uid_hex, exc,
            )
            return None
        self._debug(
            f"rfid[{self.name}]: Bambu key derivation succeeded uid={uid_hex}"
            f" keys={len(key_list)} each={len(key_list[0]) if key_list else 0}B"
        )
        read_method = getattr(self.reader, "read_mifare_classic_tag", None)
        if read_method is None:
            self._log.warning(
                "rfid[%s]: reader has no read_mifare_classic_tag — Classic read not supported",
                self.name,
            )
            return None
        self._debug(
            f"rfid[{self.name}]: Bambu read attempt={attempt_num} uid={uid_hex}"
        )
        try:
            # Always use Key A (the default); Bambu tags are encrypted with
            # HKDF-derived Key A only.  Do not pass use_key_b so the call is
            # compatible with all reader drivers (mfrc522, pn532, etc.).
            return read_method(key_list)
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: Bambu MIFARE read failed uid=%s attempt=%d: %s",
                self.name, uid_hex, attempt_num, exc,
            )
            return None

    def _try_bambu_read_with_fallback(
        self,
        uid_hex: str,
        round_num: int = 1,
    ) -> Optional[dict]:
        """Attempt a Bambu MIFARE Classic authenticated read with default-key fallback.

        First tries HKDF-derived Key A keys (Bambu authentication).  If the
        required blocks are not obtained, logs a warning and retries with the
        default MIFARE key (FFFFFFFFFFFF) for backwards compatibility with tags
        that were programmed with factory-default sector keys.

        If both attempts fail, logs a clear error with the UID.

        round_num is a human-readable counter used only in debug log messages.
        """
        # First attempt: Bambu HKDF-derived keys.
        result = self._try_bambu_read(uid_hex, attempt_num=round_num)
        if self._bambu_blocks_ok(result):
            return result

        # HKDF attempt did not succeed — determine whether it returned None
        # (e.g. pycryptodome missing, reader doesn't support Classic, tag gone)
        # or returned partial/empty blocks (auth attempted but blocks missing).
        read_method = getattr(self.reader, "read_mifare_classic_tag", None)
        if read_method is None:
            self._debug(
                f"rfid[{self.name}]: default key fallback skipped uid={uid_hex}"
                " — reader has no read_mifare_classic_tag"
            )
            return result

        if result is None:
            self._debug(
                f"rfid[{self.name}]: HKDF read returned None uid={uid_hex}"
                " — retrying with default key FFFFFFFFFFFF"
            )
        else:
            self._log.warning(
                "rfid[%s]: Bambu HKDF auth did not yield required blocks uid=%s"
                " — retrying with default key FFFFFFFFFFFF",
                self.name, uid_hex,
            )

        try:
            fallback_result = read_method([_DEFAULT_MIFARE_KEY] * 16)
        except Exception as exc:
            self._log.error(
                "rfid[%s]: default key fallback failed uid=%s: %s",
                self.name, uid_hex, exc,
            )
            return result

        # Verify the fallback selected the same tag (re-select may pick a
        # different tag if multiple are in field or the tag changed between
        # attempts).
        if fallback_result is not None:
            fb_uid = fallback_result.get("uid_hex")
            if fb_uid is not None and fb_uid != uid_hex:
                self._log.warning(
                    "rfid[%s]: default key fallback selected different tag"
                    " uid=%s (expected %s) — discarding",
                    self.name, fb_uid, uid_hex,
                )
                return result

        if self._bambu_blocks_ok(fallback_result):
            self._debug(
                f"rfid[{self.name}]: default key fallback succeeded uid={uid_hex}"
            )
            return fallback_result

        self._log.error(
            "rfid[%s]: all MIFARE auth attempts failed uid=%s"
            " — both HKDF-derived and default key (FFFFFFFFFFFF) failed",
            self.name, uid_hex,
        )
        return fallback_result if fallback_result is not None else result

    def _apply_tag_parser(
        self,
        uid_hex: Optional[str],
        raw_bytes,
        tag_text: Optional[str] = None,  # noqa: ARG002 – reserved for future use
    ) -> Optional[dict]:
        """Run rfid_tag_parser.parse_tag() and return filament info dict or None."""
        if _tag_parser is None:
            return None
        try:
            return _tag_parser.parse_tag(raw_bytes, uid_hex)
        except Exception:
            self._log.debug(
                "rfid[%s]: rfid_tag_parser.parse_tag failed for uid=%s", self.name, uid_hex
            )
            return None

    # ------------------------------------------------------------------
    # Bambu Lab tag write support (Key B, RFID_BAMBU_WRITE command only)
    # ------------------------------------------------------------------
    # This code path is NEVER triggered by the scan / read flow.
    # It is invoked exclusively when the user issues RFID_BAMBU_WRITE.
    # All existing write helpers (_write_spoolman_id_to_tag, write_tag,
    # _write_mifare_classic_json, etc.) are completely unmodified.
    # ------------------------------------------------------------------

    def _try_bambu_write(
        self,
        uid_hex: str,
        block_data: dict,
    ) -> bool:
        """Write specific MIFARE Classic blocks to a Bambu-compatible tag using Key B.

        Derives Key B for every sector via HKDF-SHA256 with the ``RFID-B\\x00``
        context (same IKM/salt as Key A, different info string).  Falls back to
        the default MIFARE key (``FFFFFFFFFFFF``) on any per-sector auth failure so
        that blank tags whose Key B has not yet been personalised can still be written.

        This method is **only** called by ``cmd_RFID_BAMBU_WRITE``.  It is completely
        separate from the scan / read path and from the existing ``RFID_WRITE`` /
        ``write_tag`` flow.

        Parameters
        ----------
        uid_hex : str
            Hardware UID of the tag as a hex string (e.g. ``"62F0E76B"``).
            Must be in the byte order reported by the reader — do NOT reverse.
        block_data : dict
            Mapping of absolute block index → 16-byte ``bytes`` to write.

        Returns
        -------
        bool
            ``True`` if all requested blocks were written successfully.
        """
        if _tag_parser is None:
            self._log.warning(
                "rfid[%s]: Bambu write skipped uid=%s — rfid_tag_parser not loaded",
                self.name, uid_hex,
            )
            return False

        # Convert uid hex string to raw bytes.
        # The UID bytes must be passed to HKDF in the same order as the reader
        # returned them (e.g. for uid_hex="62F0E76B" → b'\x62\xf0\xe7\x6b').
        # Do NOT reverse these bytes — the Android reference uses the same order.
        try:
            uid_bytes = bytes.fromhex(uid_hex)
        except Exception:
            self._log.warning(
                "rfid[%s]: Bambu write: invalid uid_hex=%s", self.name, uid_hex
            )
            return False

        # Derive Key B using the RFID-B\x00 HKDF context.
        try:
            key_list_b = _tag_parser._bambu_derive_keys_b(uid_bytes)
        except ImportError as exc:
            self._log.warning(
                "rfid[%s]: pycryptodome not available — Bambu Key B derivation skipped"
                " uid=%s. Install with: pip3 install pycryptodome. (%s)",
                self.name, uid_hex, exc,
            )
            return False
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: Bambu Key B derivation failed uid=%s: %s",
                self.name, uid_hex, exc,
            )
            return False

        self._debug(
            f"rfid[{self.name}]: Bambu write uid={uid_hex}"
            f" blocks={sorted(block_data.keys())} key_b=derived"
        )

        write_method = getattr(self.reader, "write_authenticated_bambu_blocks", None)
        if write_method is None:
            self._log.warning(
                "rfid[%s]: reader has no write_authenticated_bambu_blocks"
                " — Bambu block write not supported",
                self.name,
            )
            return False

        # First attempt: HKDF-derived Key B (correct for tags programmed with
        # this toolchain or with RFID-B\x00 sector keys set).
        try:
            result = write_method(key_list_b, block_data, use_key_b=True)
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: Bambu Key B write failed uid=%s: %s",
                self.name, uid_hex, exc,
            )
            result = None

        if result is not None:
            actual_uid = result.get("uid_hex")
            if actual_uid is not None and actual_uid != uid_hex:
                self._log.warning(
                    "rfid[%s]: HKDF Key B write selected different tag"
                    " uid=%s (expected %s) — discarding",
                    self.name, actual_uid, uid_hex,
                )
            else:
                self._debug(
                    f"rfid[{self.name}]: Bambu write succeeded uid={uid_hex} (HKDF Key B)"
                )
                return True

        # Second attempt: fall back to the default MIFARE key (0xFF×6) as Key B.
        # Blank / factory-default MIFARE Classic tags that have not yet been
        # personalised will have Key B = FFFFFFFFFFFF.
        self._log.warning(
            "rfid[%s]: Bambu HKDF Key B write failed uid=%s"
            " — retrying with default key FFFFFFFFFFFF",
            self.name, uid_hex,
        )
        default_key = b"\xFF\xFF\xFF\xFF\xFF\xFF"
        fallback_keys = [default_key] * 16
        try:
            result = write_method(fallback_keys, block_data, use_key_b=True)
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: Bambu default Key B fallback failed uid=%s: %s",
                self.name, uid_hex, exc,
            )
            result = None

        if result is not None:
            actual_uid = result.get("uid_hex")
            if actual_uid is not None and actual_uid != uid_hex:
                self._log.warning(
                    "rfid[%s]: default Key B fallback selected different tag"
                    " uid=%s (expected %s) — discarding",
                    self.name, actual_uid, uid_hex,
                )
            else:
                self._debug(
                    f"rfid[{self.name}]: Bambu write succeeded uid={uid_hex}"
                    " (default Key B fallback)"
                )
                return True

        self._log.error(
            "rfid[%s]: all Bambu Key B write attempts failed uid=%s",
            self.name, uid_hex,
        )
        return False

    def _write_spoolman_id_to_tag(self, spoolman_id: int, uid_hex: str, is_bambu: bool = False) -> bool:
        """Merge ``{"spoolman_id": N}`` into the tag's NDEF JSON payload and write it back.

        Skipped for Bambu tags (RSA-signed, read-only).
        Returns True on success, False otherwise.
        """
        if is_bambu:
            self._log.debug(
                "rfid[%s]: skipping write-back for Bambu tag uid=%s (read-only)", self.name, uid_hex
            )
            return False
        read_method = getattr(self.reader, "read_ndef_text", None)
        write_method = getattr(self.reader, "write_ndef_text", None)
        if write_method is None:
            return False

        # Start from existing JSON payload if possible, otherwise an empty object.
        payload_obj = {}
        if read_method is not None:
            try:
                existing = read_method()
                if isinstance(existing, (bytes, bytearray)):
                    existing = existing.decode("utf-8", errors="ignore")
                if isinstance(existing, str) and existing.strip():
                    loaded = json.loads(existing)
                    if isinstance(loaded, dict):
                        payload_obj = loaded
            except Exception:
                # On any read/parse error, fall back to a fresh object.
                payload_obj = {}

        # Merge/overwrite the spoolman_id field.
        payload_obj["spoolman_id"] = spoolman_id

        try:
            write_method(json.dumps(payload_obj))
            self._log.info(
                "rfid[%s]: wrote spoolman_id=%s to tag uid=%s", self.name, spoolman_id, uid_hex
            )
            return True
        except Exception:
            self._log.debug(
                "rfid[%s]: write_ndef_text failed for uid=%s", self.name, uid_hex
            )
            return False

    def _scan_once(self, lane: str, max_pages: int) -> dict:
        self._debug_verbose(
            f"rfid[{self.name}]: DBG _scan_once enter lane={lane} max_pages={max_pages}"
        )

        # Commit-freeze guard: if a commit has already been decided for this lane,
        # do not touch the reader at all.  The scan timer will return NEVER on its
        # next tick; this guard prevents any racing _scan_once calls (e.g. from the
        # auto_write path) from issuing further SPI I/O on an already-frozen lane.
        if self._commit_in_progress.get(lane, False):
            self._debug_verbose(
                f"rfid[{self.name}]: DBG _scan_once skipped lane={lane} reason=commit_in_progress"
            )
            return {
                "lane": lane,
                "uid_hex": None,
                "tag_text": None,
                "raw_len": 0,
                "raw_bytes": b"",
                "spoolman_id": None,
                "ts": time.time(),
            }

        # --- Fast UID pre-scan + cache lookup ---
        # If uid_fast_scan is enabled and the reader supports read_uid_fast(),
        # obtain the UID without the full SELECT + page-read cycle.  A cache hit
        # skips the full scan entirely, giving near-instant identification for
        # previously-seen tags.  The returned dict on a cache hit has tag_text=""
        # and raw_bytes=b"" (NDEF payload is only populated by the full scan path).
        # On a cache miss or fast-read None we fall through to the full scan so
        # that read_all_tags() can issue WUPA for halted tags and NDEF payload data
        # can still be parsed for tags that have never been seen before.
        if self.uid_fast_scan and hasattr(self.reader, "read_uid_fast"):
            fast_uid: Optional[list[int]] = None
            try:
                fast_uid = self.reader.read_uid_fast()
            except Exception:
                self._log.exception(
                    "rfid[%s]: read_uid_fast failed, falling back to full scan", self.name
                )
            if fast_uid is not None:
                fast_uid_hex = "".join("%02X" % b for b in fast_uid)
                self._debug_verbose(
                    f"rfid[{self.name}]: fast_uid acquired uid={fast_uid_hex} lane={lane}"
                )
                cached = _UID_SPOOL_CACHE.get(fast_uid_hex)
                if cached is not None:
                    sid = _cache_entry_sid(cached)
                    if sid is not None and not self._is_spool_assigned_elsewhere(lane, sid):
                        self._log.info(
                            "rfid[%s]: UID cache hit uid=%s sid=%s lane=%s"
                            " -- skipping full scan",
                            self.name, fast_uid_hex, sid, lane,
                        )
                        # Normalize any old dict-format entry to the current plain-int format.
                        if not isinstance(cached, int):
                            _UID_SPOOL_CACHE[fast_uid_hex] = sid
                            _mark_uid_cache_dirty()
                        return {
                            "lane": lane,
                            "uid_hex": fast_uid_hex,
                            "tag_text": "",
                            "raw_len": 0,
                            "raw_bytes": b"",
                            "spoolman_id": sid,
                            "ts": time.time(),
                        }
                self._debug_verbose(
                    f"rfid[{self.name}]: fast_uid cache_miss uid={fast_uid_hex}"
                    f" lane={lane} -- proceeding to full scan"
                )
            else:
                # Fast read returned None: no tag in IDLE or HALT state was detected.
                # Fall through to the full scan path (read_all_tags pass 0 uses WUPA
                # which can also wake halted tags, so it is the authoritative no-tag check).
                self._debug_verbose(
                    f"rfid[{self.name}]: fast_uid no_tag lane={lane} -- proceeding to full scan"
                )

        # Try multi-tag enumeration path first so adjacent tags can be skipped.
        if hasattr(self.reader, "read_all_tags"):
            self._debug_verbose(f"rfid[{self.name}]: DBG _scan_once path=read_all_tags")
            try:
                tags = self.reader.read_all_tags(
                    max_pages=max_pages,
                    rewake_after=not self.fast_mode,
                )
            except Exception:
                self._log.exception("reader method read_all_tags failed")
                tags = []
            # Deduplicate by UID: the MFRC522 anti-collision loop can return the
            # same physical tag multiple times in a single inventory sweep.
            if tags:
                seen_uids: dict[str, dict] = {}
                for t in tags:
                    u = t.get("uid_hex")
                    if u is not None and u not in seen_uids:
                        seen_uids[u] = t
                    elif u is None:
                        seen_uids[f"_no_uid_{id(t)}"] = t
                before = len(tags)
                tags = list(seen_uids.values())
                if len(tags) != before:
                    self._debug_verbose(
                        f"rfid[{self.name}]: DBG _scan_once deduped {before} -> {len(tags)} tag(s)"
                    )
            self._debug_verbose(
                f"rfid[{self.name}]: DBG _scan_once read_all_tags returned {len(tags)} tag(s)"
            )
            if tags:
                for i, tag in enumerate(tags):
                    self._debug_verbose(
                        f"rfid[{self.name}]: DBG _scan_once tag[{i}]"
                        f" uid={tag.get('uid_hex')} sid={tag.get('spoolman_id')}"
                        f" raw_len={tag.get('raw_len', 0)}"
                        f" tag_text={tag.get('tag_text', '')!r}"
                    )
                first_tag = tags[0]
                for tag in tags:
                    uid_hex = tag.get("uid_hex")
                    sid = tag.get("spoolman_id")
                    # Cache lookup: plain UID → sid mapping.
                    if sid is None and uid_hex is not None:
                        cached = _UID_SPOOL_CACHE.get(uid_hex)
                        if cached is not None:
                            sid = _cache_entry_sid(cached)
                            self._debug_verbose(
                                f"rfid[{self.name}]: DBG _scan_once cache_hit uid={uid_hex} sid={sid}"
                            )
                    # --- MIFARE Classic / Bambu fallback ---
                    # When read_all_tags detects a MIFARE Classic tag (SAK & 0x08)
                    # it skips the Type-2 page reads and returns a uid-only entry.
                    # Attempt the Bambu HKDF-derived authenticated read here so the
                    # tag can be decoded without any extra caller-side changes.
                    tag_sak = tag.get("sak", 0)
                    if (
                        sid is None
                        and tag.get("raw_len", 0) == 0
                        and tag.get("raw_bytes") is None
                        and tag_sak & 0x08
                        and uid_hex is not None
                    ):
                        # Skip auth entirely if this UID was already fully read and
                        # parsed earlier in this scan window — we have all the data.
                        if uid_hex in self._scan_complete_uids.get(lane, set()):
                            self._debug(
                                f"rfid[{self.name}]: Bambu auth skipped uid={uid_hex}"
                                " (full read already complete this scan window)"
                            )
                        else:
                            # Per-scan-window attempt state for this UID.
                            # Format: {uid_hex: {"rounds": int, "exhausted": bool, "ts": float}}
                            _fail_cache = self._auth_fail_uids.setdefault(lane, {})
                            _uid_state = _fail_cache.get(uid_hex)
                            if _uid_state is not None and _uid_state.get("exhausted"):
                                self._debug(
                                    f"rfid[{self.name}]: Bambu auth skipped uid={uid_hex}"
                                    " (exhausted attempts this scan window)"
                                )
                            else:
                                _rounds_done = _uid_state.get("rounds", 0) if _uid_state else 0
                                _round_num = _rounds_done + 1
                                self._debug(
                                    f"rfid[{self.name}]: MIFARE Classic uid={uid_hex}"
                                    f" sak=0x{tag_sak:02X}"
                                    f" — Bambu auth round={_round_num}/{_BAMBU_MAX_ROUNDS}"
                                )
                                bambu_blocks = self._try_bambu_read_with_fallback(
                                    uid_hex, round_num=_round_num)
                                _MIFARE_BLOCK_SIZE = 16
                                if self._bambu_blocks_ok(bambu_blocks):
                                    self._debug(
                                        f"rfid[{self.name}]: Bambu authenticated read succeeded"
                                        f" uid={uid_hex} (required blocks present)"
                                    )
                                    filament_info = self._apply_tag_parser(uid_hex, bambu_blocks)
                                    tag["raw_bytes"] = bambu_blocks
                                    if isinstance(bambu_blocks, dict):
                                        _block_count = len(bambu_blocks.get("blocks") or {})
                                    else:
                                        _block_count = 0
                                    tag["raw_len"] = _block_count * _MIFARE_BLOCK_SIZE
                                    if filament_info is not None:
                                        # Mark this UID as fully read *and* successfully
                                        # parsed for this scan window so subsequent
                                        # _scan_once calls skip the expensive MIFARE
                                        # auth re-read — we already have all the data.
                                        self._scan_complete_uids.setdefault(lane, set()).add(uid_hex)
                                        self._debug(
                                            f"rfid[{self.name}]: Bambu full read complete"
                                            f" uid={uid_hex} — marked complete for this scan window"
                                        )
                                        sid = filament_info.get("spoolman_id")
                                        tag["spoolman_id"] = sid
                                        tag["filament_info"] = filament_info
                                        # Log a full labeled summary of all parsed spool
                                        # fields so users can see tray UID, weight,
                                        # production date, temperatures, etc. in one place
                                        # (matching what the Bambu Android app shows).
                                        if _tag_parser is not None and hasattr(
                                            _tag_parser, "format_bambu_info"
                                        ):
                                            summary = _tag_parser.format_bambu_info(
                                                filament_info, uid_hex=uid_hex
                                            )
                                            # Prefix every line with the reader name so
                                            # each line is attributable in syslog /
                                            # journald / file-tail output where log
                                            # records are not grouped.
                                            prefix = f"rfid[{self.name}]: "
                                            for line in summary.splitlines():
                                                self._log.info("%s%s", prefix, line)
                                        else:
                                            self._debug(
                                                f"rfid[{self.name}]: Bambu tag parsed"
                                                f" uid={uid_hex}"
                                                f" material={filament_info.get('material')}"
                                                f" color=#{filament_info.get('color_hex')}"
                                                f" brand={filament_info.get('brand')}"
                                            )
                                else:
                                    # Required blocks not obtained — track rounds; mark
                                    # exhausted once _BAMBU_MAX_ROUNDS rounds have been tried.
                                    _new_rounds = _rounds_done + 1
                                    _exhausted = _new_rounds >= _BAMBU_MAX_ROUNDS
                                    _fail_cache[uid_hex] = {
                                        "rounds": _new_rounds,
                                        "exhausted": _exhausted,
                                        "ts": self.reactor.monotonic(),
                                    }
                                    if _exhausted:
                                        self._debug(
                                            f"rfid[{self.name}]: Bambu auth exhausted uid={uid_hex}"
                                            f" after {_new_rounds} round(s)"
                                            " — will not retry this scan window"
                                        )
                                    else:
                                        self._debug(
                                            f"rfid[{self.name}]: Bambu auth round={_round_num}"
                                            f" incomplete uid={uid_hex}"
                                            f" — will retry (max {_BAMBU_MAX_ROUNDS} rounds)"
                                        )
                                    # Store any partial block data for diagnostics.
                                    if bambu_blocks is not None:
                                        tag["raw_bytes"] = bambu_blocks
                                        _bc = (len(bambu_blocks.get("blocks") or {})
                                               if isinstance(bambu_blocks, dict) else 0)
                                        tag["raw_len"] = _bc * _MIFARE_BLOCK_SIZE
                    # Cache update: store plain int sid.
                    if uid_hex and sid is not None:
                        existing = _UID_SPOOL_CACHE.get(uid_hex)
                        if existing != sid:
                            _UID_SPOOL_CACHE[uid_hex] = sid
                            _mark_uid_cache_dirty()
                    assigned = (sid is not None) and self._is_spool_assigned_elsewhere(lane, sid)
                    self._debug_verbose(
                        f"rfid[{self.name}]: DBG _scan_once evaluating"
                        f" uid={uid_hex} sid={sid} assigned_elsewhere={assigned}"
                    )
                    if sid is None or not assigned:
                        self._debug_verbose(
                            f"rfid[{self.name}]: DBG _scan_once selected"
                            f" uid={uid_hex} sid={sid}"
                        )
                        return {
                            "lane": lane,
                            "uid_hex": uid_hex,
                            "tag_text": tag.get("tag_text", ""),
                            "raw_len": tag.get("raw_len", 0),
                            "raw_bytes": tag.get("raw_bytes") or b"",
                            "spoolman_id": sid,
                            "filament_info": tag.get("filament_info"),
                            "ts": time.time(),
                        }
                # No unassigned tag found — return first tag so caller can log it,
                # but do not expose its spoolman_id as a valid hit.
                first_uid = first_tag.get("uid_hex")
                self._debug(
                    f"rfid[{self.name}]: DBG _scan_once all {len(tags)} tag(s) blocked,"
                    f" returning first with sid=None blocked_sid={first_tag.get('spoolman_id')}"
                )
                return {
                    "lane": lane,
                    "uid_hex": first_uid,
                    "tag_text": first_tag.get("tag_text", ""),
                    "raw_len": first_tag.get("raw_len", 0),
                    "raw_bytes": first_tag.get("raw_bytes") or b"",
                    "spoolman_id": None,
                    "filament_info": first_tag.get("filament_info"),
                    "blocked_spoolman_id": first_tag.get("spoolman_id"),
                    "ts": time.time(),
                }
            # No tags at all
            self._debug(f"rfid[{self.name}]: DBG _scan_once no tags detected")
            # Minimal RF recovery: clear MFCrypto1On so the next scan attempt
            # starts with a clean transceiver state.
            if hasattr(self.reader, "_clear_mask") and hasattr(self.reader, "Status2Reg"):
                try:
                    self.reader._clear_mask(self.reader.Status2Reg, 0x08)
                except Exception:
                    # RF recovery is best-effort; log and continue returning an empty result.
                    self._log.exception(
                        f"rfid[{self.name}]: _clear_mask RF recovery failed in _scan_once"
                    )
            return {
                "lane": lane,
                "uid_hex": None,
                "tag_text": None,
                "raw_len": 0,
                "raw_bytes": b"",
                "spoolman_id": None,
                "ts": time.time(),
            }

        # Single-tag fallback: prefer a single-pass tag read so UID, raw bytes,
        # and text come from the same physical scan attempt.
        info = None
        if hasattr(self.reader, "read_tag_info"):
            self._debug_verbose(f"rfid[{self.name}]: DBG _scan_once path=read_tag_info")
            try:
                info = self.reader.read_tag_info(max_pages=max_pages)
            except TypeError:
                info = self.reader.read_tag_info(max_pages)
            except Exception:
                self._log.exception("reader method read_tag_info failed")
                info = None
            self._debug_verbose(
                f"rfid[{self.name}]: DBG _scan_once read_tag_info returned"
                f" uid={info.get('uid_hex') if info else None}"
                f" sid={info.get('spoolman_id') if info else None}"
                f" raw_len={info.get('raw_len') if info else None}"
                f" tag_text={info.get('tag_text', '') if info else None!r}"
            )
        if info:
            uid_hex = info.get("uid_hex")
            tag_text = info.get("tag_text") or ""
            spoolman_id = info.get("spoolman_id")
            if spoolman_id is None and tag_text:
                spoolman_id = self._extract_spoolman_id(tag_text)
                self._debug_verbose(
                    f"rfid[{self.name}]: DBG _scan_once extracted spoolman_id={spoolman_id}"
                    f" from tag_text={tag_text!r}"
                )
            # Cache lookup: plain UID → sid mapping.
            if spoolman_id is None and uid_hex is not None:
                cached = _UID_SPOOL_CACHE.get(uid_hex)
                if cached is not None:
                    spoolman_id = _cache_entry_sid(cached)
                    self._debug_verbose(
                        f"rfid[{self.name}]: DBG _scan_once cache_hit uid={uid_hex} sid={spoolman_id}"
                    )
            raw_len = info.get("raw_len")
            if raw_len is None:
                raw_len = len(tag_text.encode("utf-8")) if tag_text else 0
            # Cache update: store plain int sid.
            if uid_hex and spoolman_id is not None:
                existing = _UID_SPOOL_CACHE.get(uid_hex)
                if existing != spoolman_id:
                    _UID_SPOOL_CACHE[uid_hex] = spoolman_id
                    _mark_uid_cache_dirty()
            if spoolman_id is not None and self._is_spool_assigned_elsewhere(lane, spoolman_id):
                self._debug_verbose(
                    f"rfid[{self.name}]: DBG _scan_once suppressing spoolman_id={spoolman_id}"
                    f" for lane={lane} (assigned elsewhere)"
                )
                spoolman_id = None
            self._debug_verbose(
                f"rfid[{self.name}]: DBG _scan_once result path=read_tag_info"
                f" uid={uid_hex} sid={spoolman_id} raw_len={raw_len}"
            )
            return {
                "lane": lane,
                "uid_hex": uid_hex,
                "tag_text": tag_text,
                "raw_len": raw_len,
                "raw_bytes": info.get("raw_bytes") or b"",
                "spoolman_id": spoolman_id,
                "ts": time.time(),
            }

        # Fallback for older readers.
        self._debug_verbose(f"rfid[{self.name}]: DBG _scan_once path=legacy (read_uid_hex + read_tag_text)")
        uid_hex = self._read_uid_hex()
        self._debug_verbose(f"rfid[{self.name}]: DBG _scan_once legacy uid_hex={uid_hex}")
        tag_text = self._read_tag_text(max_pages=max_pages) if uid_hex else None
        self._debug_verbose(f"rfid[{self.name}]: DBG _scan_once legacy tag_text={tag_text!r}")
        raw_len = len(tag_text.encode("utf-8")) if tag_text else 0
        spoolman_id = self._extract_spoolman_id(tag_text)
        self._debug_verbose(
            f"rfid[{self.name}]: DBG _scan_once legacy extracted spoolman_id={spoolman_id}"
        )
        # Cache lookup: plain UID → sid mapping.
        if spoolman_id is None and uid_hex is not None:
            cached = _UID_SPOOL_CACHE.get(uid_hex)
            if cached is not None:
                spoolman_id = _cache_entry_sid(cached)
                self._debug_verbose(
                    f"rfid[{self.name}]: DBG _scan_once cache_hit uid={uid_hex} sid={spoolman_id}"
                )
        # Cache update: store plain int sid.
        if uid_hex and spoolman_id is not None:
            existing = _UID_SPOOL_CACHE.get(uid_hex)
            if existing != spoolman_id:
                _UID_SPOOL_CACHE[uid_hex] = spoolman_id
                _mark_uid_cache_dirty()
        if spoolman_id is not None and self._is_spool_assigned_elsewhere(lane, spoolman_id):
            self._debug_verbose(
                f"rfid[{self.name}]: DBG _scan_once legacy suppressing spoolman_id={spoolman_id}"
                f" for lane={lane} (assigned elsewhere)"
            )
            spoolman_id = None
        self._debug_verbose(
            f"rfid[{self.name}]: DBG _scan_once result path=legacy"
            f" uid={uid_hex} sid={spoolman_id} raw_len={raw_len}"
        )
        return {
            "lane": lane,
            "uid_hex": uid_hex,
            "tag_text": tag_text,
            "raw_len": raw_len,
            "raw_bytes": tag_text.encode("utf-8") if tag_text else b"",
            "spoolman_id": spoolman_id,
            "ts": time.time(),
        }

    def _normalize_port(self, value) -> str:
        """Normalize lane=, slot=, or bare number to canonical lane{n} key.

        Indexing convention:
          - AFC lanes are 1-based  (lane1, lane2, …)
          - Happy Hare slots/gates are 0-based (slot0, slot1, …)
          - slot{n}  →  lane{n+1}  (e.g. slot0 = lane1 = gate0)
        """
        value = str(value).strip().lower()
        if value.startswith("slot"):
            n = value[4:]
            if n.isdigit():
                return f"lane{int(n) + 1}"
            return value
        if value.startswith("lane"):
            return value
        if value.isdigit():
            return f"lane{value}"
        return value

    def _find_reader_for_port(self, value: str):
        """Find the reader serving lane/slot *value*; returns (reader, port_key)."""
        port = self._normalize_port(value)
        for reader in self._all_readers():
            for mapped in getattr(reader, "lanes", []):
                if self._normalize_port(mapped) == port:
                    return reader, port
            for mapped in getattr(reader, "slots", []):
                if self._normalize_port(mapped) == port:
                    return reader, port
        return None, port

    def _assign_spool_to_gate(self, lane_n: int, spoolman_id: int) -> None:
        """Issue the right GCode for the detected MMU system.

        *lane_n* is the 1-based AFC lane number (lane1 → 1, lane2 → 2, …).
        Happy Hare gates are 0-based, so gate = lane_n - 1.
        """
        sid = int(spoolman_id)
        if self._mmu_system == "hh":
            gate = lane_n - 1
            self._debug(f"rfid[{self.name}]: HH assigning spoolman_id={sid} to gate {gate} (lane {lane_n})")
            for script in (
                f"MMU_GATE_MAP GATE={gate} SPOOLID={sid}",
                f"MMU_SPOOLMAN SPOOLID={sid} GATE={gate} UPDATE=1",
            ):
                self.reactor.register_async_callback(
                    lambda e, s=script: self.gcode.run_script_from_command(s)
                )
        else:
            self._debug(f"rfid[{self.name}]: AFC assigning spool_id={sid} to lane {lane_n}")
            script = f"SET_SPOOL_ID LANE=lane{lane_n} SPOOL_ID={sid}"
            self.reactor.register_async_callback(
                lambda e, s=script: self.gcode.run_script_from_command(s)
            )

    # ---------- Spoolman integration ----------
    def _spoolman_run_async(self, fn) -> None:
        """Submit fn() to the bounded Spoolman thread-pool executor.

        All Spoolman HTTP operations are dispatched this way so that blocking
        network I/O never stalls the Klipper reactor event loop (which would
        starve MCU timer callbacks and trigger a "timer too close" shutdown).
        A ThreadPoolExecutor with max_workers=2 bounds the number of concurrent
        Spoolman requests so that a burst of scans cannot spawn unlimited threads.
        """
        if self._spoolman is None:
            self._log.warning(
                "rfid[%s]: Spoolman async skipped: self._spoolman is None",
                self.name,
            )
            return
        if self._spoolman_executor is None:
            self._log.warning(
                "rfid[%s]: Spoolman async skipped: executor is None (client=%s)",
                self.name,
                self._spoolman is not None,
            )
            return
        try:
            self._log.debug("rfid[%s]: submitting Spoolman async task", self.name)
            self._spoolman_executor.submit(fn)
        except RuntimeError as exc:
            self._log.warning(
                "rfid[%s]: Spoolman async submit failed: %s",
                self.name, exc,
            )

    def _do_auto_create_spool(self, lane: str, uid_hex: str, tag_data: dict) -> None:
        """Background-thread worker: create a Spoolman spool from tag data.

        Runs entirely in a background thread.  Uses reactor.register_async_callback
        (which is thread-safe) to update reactor-side state (_pending, cache) and
        issue RFID_SCAN_COMMIT when creation succeeds.

        Before creating, always performs a Spoolman UID lookup to guard against
        duplicate spool creation when the early-commit path bypasses the normal
        lookup-before-create flow.  Only creates a new spool after all lookups
        definitively return no match.

        Delegates the full vendor/filament/spool creation pipeline (including
        SpoolmanDB density lookup) to SpoolmanClient.auto_create_spool, then
        registers the hardware UID in the spool's extra fields.
        """
        client = self._spoolman
        if client is None:
            return

        # Step 0: check whether this UID is already registered in Spoolman before
        # attempting to create anything.  The early-commit paths in
        # _scan_timer_callback dispatch here directly (without a prior lookup), so
        # this guard is the last line of defence against duplicate spool creation.
        if uid_hex:
            try:
                existing_sid = client.find_spool_by_uid(uid_hex, self.max_uids)
            except Exception as exc:
                self._log.warning(
                    "rfid: auto_create_spool: pre-create lookup failed (inconclusive)"
                    " uid=%s: %s — aborting creation to avoid duplicate spool",
                    uid_hex, exc,
                )
                # Treat a lookup error as inconclusive — do NOT create; freeze the lane
                # so no further ticks re-trigger creation until the lane is re-scanned.
                self._freeze_lane_async(lane, reason="auto_create_spool_lookup_error")
                return

            if existing_sid is not None:
                self._log.info(
                    "rfid: auto_create_spool: uid=%s already registered as spool %s"
                    " in Spoolman — skipping creation, using existing spool"
                    " (lookup succeeded before create was attempted)",
                    uid_hex, existing_sid,
                )
                _lane = lane
                _uid = uid_hex
                _sid = existing_sid
                _timeout = self.event_timeout

                def _on_found_existing(event_time):
                    self._commit_in_progress[_lane] = True
                    self._end_scan_session(_lane, reason="uid_found_before_create")
                    _UID_SPOOL_CACHE[_uid] = _sid
                    _mark_uid_cache_dirty()
                    self._pending[_lane] = {
                        "lane": _lane,
                        "spoolman_id": _sid,
                        "uid_hex": _uid,
                        "tag_text": None,
                        "ts": time.time(),
                        "timeout": _timeout,
                    }
                    self._respond(
                        f"rfid: auto_create_spool: uid={_uid} already exists as"
                        f" spool {_sid} on lane {_lane} — creation skipped"
                    )
                    if self._lane_loaded_seen.get(_lane) or _lane in self._sync_scan_lanes:
                        self._debug(
                            f"rfid[{self.name}]: auto_create_spool: lane_loaded already"
                            f" received (or sync scan) — committing existing spool"
                            f" {_sid} now"
                        )
                        self.gcode.run_script_from_command(f"RFID_SCAN_COMMIT LANE={_lane}")
                    else:
                        self._debug(
                            f"rfid[{self.name}]: auto_create_spool: existing spool {_sid}"
                            f" stored in pending for lane {_lane} — awaiting lane_loaded"
                        )

                self.reactor.register_async_callback(_on_found_existing)
                return

            self._log.info(
                "rfid: auto_create_spool: uid=%s not found in Spoolman"
                " — all lookups failed, proceeding with creation"
                " (auto_create_spool=True / CREATE=1)",
                uid_hex,
            )

        # Step 0b: For Bambu tags, check whether a spool already exists with the
        # same Tray UID stored as lot_nr.  A physical Bambu spool may carry two
        # RFID tags with different hardware UIDs but the same Tray UID; the second
        # tag would fail the uid lookup above but must resolve to the same spool.
        tray_uid = str(tag_data.get("tray_uid") or "").strip().upper()
        if tray_uid and str(tag_data.get("tag_format") or "") == "bambu":
            try:
                lot_nr_sid = self._spoolman_find_spool_by_tray_uid(tray_uid)
            except Exception as exc:
                self._log.warning(
                    "rfid: auto_create_spool: tray_uid lot_nr lookup failed"
                    " (inconclusive) tray_uid=%s uid=%s: %s"
                    " — aborting creation to avoid duplicate spool",
                    tray_uid, uid_hex, exc,
                )
                self._freeze_lane_async(lane, reason="auto_create_spool_lot_nr_lookup_error")
                return

            if lot_nr_sid is not None:
                self._log.info(
                    "rfid: auto_create_spool: tray_uid=%s already registered as spool %s"
                    " in Spoolman (lot_nr match) — skipping creation, associating uid=%s",
                    tray_uid, lot_nr_sid, uid_hex,
                )
                # Register the scanned UID on the existing spool so future scans
                # of this hardware tag resolve quickly via rfid_uid_N.
                if uid_hex:
                    try:
                        client.add_uid_to_spool(lot_nr_sid, uid_hex, self.max_uids)
                    except Exception as exc:
                        self._log.warning(
                            "rfid: auto_create_spool: failed to register uid=%s"
                            " on existing spool %s (lot_nr=%s): %s",
                            uid_hex, lot_nr_sid, tray_uid, exc,
                        )
                _lane = lane
                _uid = uid_hex
                _sid = lot_nr_sid
                _timeout = self.event_timeout

                def _on_found_by_lot_nr(event_time):
                    self._commit_in_progress[_lane] = True
                    self._end_scan_session(_lane, reason="tray_uid_lot_nr_found_before_create")
                    _UID_SPOOL_CACHE[_uid] = _sid
                    _mark_uid_cache_dirty()
                    self._pending[_lane] = {
                        "lane": _lane,
                        "spoolman_id": _sid,
                        "uid_hex": _uid,
                        "tag_text": None,
                        "ts": time.time(),
                        "timeout": _timeout,
                    }
                    self._respond(
                        f"rfid: auto_create_spool: Bambu tray_uid={tray_uid}"
                        f" found existing spool {_sid} on lane {_lane}"
                        f" — creation skipped, uid={_uid} associated"
                    )
                    if self._lane_loaded_seen.get(_lane) or _lane in self._sync_scan_lanes:
                        self._debug(
                            f"rfid[{self.name}]: auto_create_spool: lane_loaded already"
                            f" received (or sync scan) — committing existing spool"
                            f" {_sid} now"
                        )
                        self.gcode.run_script_from_command(f"RFID_SCAN_COMMIT LANE={_lane}")
                    else:
                        self._debug(
                            f"rfid[{self.name}]: auto_create_spool: existing spool {_sid}"
                            f" stored in pending for lane {_lane} — awaiting lane_loaded"
                        )

                self.reactor.register_async_callback(_on_found_by_lot_nr)
                return

        # for remaining weight.  Ensure standard keys are present before delegating.
        filament_info = dict(tag_data)
        if "material" not in filament_info or not filament_info.get("material"):
            filament_info["material"] = tag_data.get("type") or ""
        if "weight_g" not in filament_info or filament_info.get("weight_g") is None:
            filament_info["weight_g"] = tag_data.get("spool_weight") or tag_data.get("weight")

        try:
            spool_id = client.auto_create_spool(filament_info, uid_hex=uid_hex)
        except Exception as exc:
            self._log.warning(
                "rfid: auto_create_spool: failed lane=%s uid=%s: %s",
                lane, uid_hex, exc,
            )
            # Freeze the lane on the reactor thread so no further reads re-trigger creation.
            self._freeze_lane_async(lane, reason="auto_create_spool_exception")
            return

        if spool_id is None:
            self._log.warning(
                "rfid: auto_create_spool: spool creation returned None lane=%s uid=%s"
                " — check material/OpenSpool type data and Spoolman API/logs",
                lane, uid_hex,
            )
            # Freeze the lane on the reactor thread so no further reads re-trigger creation.
            self._freeze_lane_async(lane, reason="auto_create_spool_none")
            return

        self._log.info(
            "rfid: auto_create_spool: created spool id=%s lane=%s uid=%s",
            spool_id, lane, uid_hex,
        )

        # Register UID association via SpoolmanClient UID helpers.
        # The UID was already included in the spool extra at creation time; this
        # ensures it is also registered in the numbered rfid_uid_N slot system
        # so find_spool_by_uid() can locate the spool in future scans.
        # All global cache mutations are deferred to _on_created, which runs on the
        # reactor thread, to avoid data-race on _UID_SPOOL_CACHE/_UID_CACHE_DIRTY.
        try:
            old_sid = client.find_spool_by_uid(uid_hex, self.max_uids)
            if old_sid is not None and old_sid != spool_id:
                client.remove_uid_from_spool(old_sid, uid_hex, self.max_uids)
            client.add_uid_to_spool(spool_id, uid_hex, self.max_uids)
        except Exception as exc:
            self._log.warning(
                "rfid: auto_create_spool: uid association failed"
                " spool=%s uid=%s: %s",
                spool_id, uid_hex, exc,
            )

        # Hand results back to the reactor (register_async_callback is thread-safe).
        _lane = lane
        _uid = uid_hex
        _sid = spool_id
        _timeout = self.event_timeout

        def _on_created(event_time):
            # Freeze the lane immediately so no further reader I/O can overlap.
            self._commit_in_progress[_lane] = True
            # Stop the scan timer — the spool has been created and is ready to commit.
            self._end_scan_session(_lane, reason="auto_create_spool_done")
            _UID_SPOOL_CACHE[_uid] = _sid
            _mark_uid_cache_dirty()
            # Always store the new spool_id in _pending so that _handle_lane_loaded
            # can pick it up and call RFID_SCAN_COMMIT when the lane becomes ready.
            # We do NOT commit here unconditionally — assigning a spool to a lane
            # before lane_loaded fires causes the AFC system to receive the
            # spool assignment before the filament has actually finished loading.
            self._pending[_lane] = {
                "lane": _lane,
                "spoolman_id": _sid,
                "uid_hex": _uid,
                "tag_text": None,
                "ts": time.time(),
                "timeout": _timeout,
            }
            self._respond(
                f"rfid: auto_create_spool: spool {_sid} created for"
                f" uid={_uid} on lane {_lane}"
            )
            if self.auto_write:
                try:
                    current_uid = None
                    current_scan = self._scan_once(_lane, max_pages=self.max_pages)
                    if isinstance(current_scan, dict):
                        current_uid = current_scan.get("uid_hex") or current_scan.get("uid")
                    elif isinstance(current_scan, (tuple, list)) and current_scan:
                        current_uid = current_scan[0]
                    if current_uid == _uid:
                        self._write_spoolman_id_to_tag(_sid, _uid)
                except Exception:
                    pass
            # Two cases where we commit immediately rather than waiting:
            #   (a) lane_loaded already fired before spool creation finished —
            #       _handle_lane_loaded has already run but found no pending
            #       entry, so we must commit here now that we have one.
            #   (b) This is a synchronous GCode scan (RFID_SCAN_BEGIN / RFID_SCAN)
            #       which drives its own commit flow and does not use lane_loaded.
            # In all other AFC event-driven cases, leave the spool_id in _pending
            # and let _handle_lane_loaded trigger RFID_SCAN_COMMIT when it fires.
            if self._lane_loaded_seen.get(_lane) or _lane in self._sync_scan_lanes:
                self._debug(
                    f"rfid[{self.name}]: auto_create_spool: lane_loaded already"
                    f" received (or sync scan) — committing spool {_sid} now"
                )
                self.gcode.run_script_from_command(f"RFID_SCAN_COMMIT LANE={_lane}")
            else:
                self._debug(
                    f"rfid[{self.name}]: auto_create_spool: spool {_sid} stored in"
                    f" pending for lane {_lane} — awaiting lane_loaded to commit"
                )

        self.reactor.register_async_callback(_on_created)

    def _dispatch_uid_resolution(self, lane: str, uid_hex: str, tag_text: str) -> None:
        """Schedule a background Spoolman UID lookup for a tag with NDEF data but no spool_id.

        Dispatches to the thread-pool executor so blocking HTTP I/O never stalls the
        reactor.  On a successful UID lookup (Step 2), updates _pending and the UID cache
        via reactor.register_async_callback and issues RFID_SCAN_COMMIT.  If the UID is
        not found in Spoolman and auto_create_spool=True (Step 3), delegates to
        _do_auto_create_spool.  If neither applies, logs and stops cleanly.

        If auto_write=True, a best-effort write of spool_id back to the tag NDEF is
        attempted from the reactor thread after a successful lookup or auto-create.
        """
        if self._spoolman is None:
            return

        _lane = lane
        _uid = uid_hex
        _text = tag_text
        _timeout = self.event_timeout

        def _work():
            # Search Spoolman for a spool associated with this UID.
            found_sid: Optional[int] = None
            try:
                found_sid = self._spoolman_find_spool_by_uid(_uid)
            except Exception as exc:
                self._debug(
                    f"rfid: _dispatch_uid_resolution uid={_uid}: find_by_uid failed: {exc}"
                )

            if found_sid is not None:
                # UID found in Spoolman: stop the scan, cache and commit via reactor callback.
                _sid = found_sid

                def _on_found(event_time):
                    # Freeze the lane immediately so no further reader I/O can overlap.
                    self._commit_in_progress[_lane] = True
                    # Stop the scan timer — we have our answer.
                    self._end_scan_session(_lane, reason="uid_resolved_in_spoolman")
                    _UID_SPOOL_CACHE[_uid] = _sid
                    _mark_uid_cache_dirty()
                    self._pending[_lane] = {
                        "lane": _lane,
                        "spoolman_id": _sid,
                        "uid_hex": _uid,
                        "tag_text": _text,
                        "ts": time.time(),
                        "timeout": _timeout,
                    }
                    self._respond(
                        f"RFID: tag uid={_uid} matched spool {_sid} in Spoolman"
                        f" on lane {_lane}"
                    )
                    if self.auto_write:
                        try:
                            current_uid = None
                            current_scan = self._scan_once(_lane, max_pages=self.max_pages)
                            if isinstance(current_scan, dict):
                                current_uid = current_scan.get("uid_hex") or current_scan.get("uid")
                            elif isinstance(current_scan, (tuple, list)) and current_scan:
                                current_uid = current_scan[0]
                            if current_uid == _uid:
                                self._write_spoolman_id_to_tag(_sid, _uid)
                        except Exception:
                            pass
                    self.gcode.run_script_from_command(f"RFID_SCAN_COMMIT LANE={_lane}")

                self.reactor.register_async_callback(_on_found)
                return
            # 3. Not found in Spoolman — try auto-create if enabled and tag is OpenSpool.
            if self.auto_create_spool and _text:
                try:
                    tag_data = json.loads(_text)
                    if isinstance(tag_data, dict) and tag_data.get("protocol") == "openspool":
                        self._do_auto_create_spool(_lane, _uid, dict(tag_data))
                        return
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

            # 4. No match and no auto-create path succeeded — report the UID resolution result.
            _no_create = not self.auto_create_spool
            def _on_not_found(event_time):
                suffix = " and auto_create_spool=False" if _no_create else " (no matching OpenSpool data)"
                self._respond(
                    f"RFID: tag uid={_uid} not found in Spoolman{suffix} — UID resolution complete; scan will continue for other tags until the scan window ends"
                )

            self.reactor.register_async_callback(_on_not_found)

        if self._spoolman_executor is not None:
            self._spoolman_run_async(_work)
        else:
            self._log.warning(
                "rfid[%s]: not scheduling Spoolman UID resolution for uid=%s lane=%s"
                " (executor=%s)",
                self.name, uid_hex, lane,
                self._spoolman_executor is not None,
            )

    def _ensure_uid_in_spool_extra(self, uid_hex: str, spool_id: int) -> None:
        """Background: add uid_hex to spool's rfid_uid_N extra fields if not already present.

        Failures are logged at warning level so they are visible without debug=True.
        Call only from the thread-pool executor (never the reactor thread).
        """
        self._log.info(
            "rfid: ensuring uid=%s is registered on spool sid=%d", uid_hex, spool_id
        )
        try:
            client = self._spoolman
            if client is not None:
                ok = client.add_uid_to_spool(spool_id, uid_hex, self.max_uids)
            else:
                ok = self._spoolman_add_uid_to_spool(spool_id, uid_hex)
            if ok:
                self._log.info(
                    "rfid: uid=%s successfully registered on spool sid=%d",
                    uid_hex, spool_id,
                )
            else:
                self._log.warning(
                    "rfid: uid=%s could not be registered on spool sid=%d"
                    " — all rfid_uid_N slots occupied, fields missing/uncreatable,"
                    " or PATCH failed."
                    " Check Spoolman extra fields (Settings → Extra Fields)"
                    " or set max_uids higher.",
                    uid_hex, spool_id,
                )
        except Exception as exc:
            self._log.warning(
                "rfid: failed to register uid=%s on spool sid=%d: %s",
                uid_hex, spool_id, exc,
            )

    # ---------- event handlers ----------
    # ---------- scan session lifecycle helpers ----------

    def _clear_scan_state(self, lane: str, reason: str = "") -> None:
        """Clear all per-lane scan bookkeeping and bump the generation counter.

        Called both from ``_end_scan_session`` (outside a timer callback) and
        directly from inside ``_scan_timer_callback`` (where controlling the
        timer lifecycle via the return value is the correct pattern — not via
        ``unregister_timer``).

        Bumping the generation invalidates any timer callback that captured
        the previous generation, so late-firing callbacks become safe no-ops.
        """
        self._scan_deadlines.pop(lane, None)
        self._scan_candidates.pop(lane, None)
        self._scan_blocked_uids.pop(lane, None)
        self._scan_seen_uids.pop(lane, None)
        self._scan_last_uid.pop(lane, None)
        self._scan_no_tag_streak.pop(lane, None)
        self._scan_tick_count.pop(lane, None)
        self._auth_fail_uids.pop(lane, None)
        self._scan_complete_uids.pop(lane, None)
        self._scan_gen[lane] = self._scan_gen.get(lane, 0) + 1
        if reason:
            self._debug(
                f"rfid[{self.name}]: scan state cleared lane={lane} reason={reason}"
            )

    def _end_scan_session(self, lane: str, reason: str = "") -> None:
        """Cancel the active scan timer for *lane* and clear all per-lane scan state.

        Safe to call from event handlers and async reactor callbacks.  Do NOT
        call from inside ``_scan_timer_callback`` — the callback controls its
        own timer lifetime by returning ``reactor.NEVER`` after calling
        ``_clear_scan_state``.

        ``unregister_timer`` stops the timer even if it has already been
        scheduled to fire imminently; the generation bump ensures that any
        callback that slips through before the unregister takes effect will
        return ``reactor.NEVER`` immediately without doing any work.
        """
        existing = self._scan_timers.pop(lane, None)
        if existing is not None:
            self.reactor.unregister_timer(existing)
        self._clear_scan_state(lane, reason)

    def _freeze_lane_async(self, lane: str, reason: str = "") -> None:
        """Thread-safe: freeze a lane from a background thread via reactor callback.

        Sets ``_commit_in_progress[lane]`` and calls ``_end_scan_session`` on
        the reactor thread, preventing any further scan timer ticks or tag reads
        for the lane until a new scan window is started.  Safe to call from any
        background (non-reactor) thread.
        """
        _lane = lane
        _reason = reason
        def _do_freeze(event_time):
            self._commit_in_progress[_lane] = True
            self._end_scan_session(_lane, reason=_reason)
        self.reactor.register_async_callback(_do_freeze)

    # ---------- timer engine ----------

    def _start_scan_timer(self, lane: str) -> None:
        """Start a reactor timer that keeps scanning until a spoolman_id is found
        or scan_window seconds have elapsed.  Safe to call from an event handler
        because it only registers a timer — no reactor.pause() is used.
        """
        # End any in-progress session (cancels old timer, bumps gen, clears state).
        self._end_scan_session(lane, reason="new_window")
        # Clear any pending spool assignment and commit-freeze from a previous scan window.
        self._pending.pop(lane, None)
        self._commit_in_progress.pop(lane, None)
        self._lane_committed.pop(lane, None)
        self._uid_lookup_in_flight.pop(lane, None)
        # Explicitly mark lane_loaded as not-yet-seen for this new scan window.
        # Using False (not pop) means the guard in _handle_lane_prep_start can
        # distinguish "active AFC cycle, not yet loaded" (False) from "no cycle
        # tracking present" (key missing), avoiding blocking unrelated commits.
        self._lane_loaded_seen[lane] = False
        # Discard any deferred UID from a previous scan window so it is never
        # applied to the newly-starting window's lane_loaded event.
        self._deferred_uid.pop(lane, None)
        self._scan_deadlines[lane] = self.reactor.monotonic() + self.scan_window
        # Initialise fresh per-window state.
        self._scan_blocked_uids[lane] = set()
        self._scan_seen_uids[lane] = set()
        self._scan_last_uid[lane] = None
        self._scan_no_tag_streak[lane] = 0
        self._scan_tick_count[lane] = 0
        self._auth_fail_uids[lane] = {}
        self._scan_complete_uids[lane] = set()
        # Capture current generation so the timer callback can detect staleness.
        gen = self._scan_gen.get(lane, 0)

        handle = self.reactor.register_timer(
            lambda event_time, _lane=lane, _gen=gen: self._scan_timer_callback(_lane, event_time, _gen),
            self.reactor.monotonic() + _ASYNC_MIN_DELAY,
        )
        self._scan_timers[lane] = handle
        self._respond(
            f"rfid[{self.name}]: scan timer started for lane {lane}"
            f" window={self.scan_window:.1f}s"
        )

    def _maybe_schedule_auto_commit(self, lane: str, spoolman_id: int) -> None:
        """Schedule an async RFID_SCAN_COMMIT for *lane* when auto_commit_on_scan is enabled.

        Only schedules when the lane is NOT in _sync_scan_lanes (i.e. not a
        synchronous GCode scan via RFID_SCAN_BEGIN / _run_scan_window_sync,
        which handles its own commit flow).
        """
        if not self.auto_commit_on_scan or lane in self._sync_scan_lanes:
            return
        self._log.info(
            "rfid[%s]: auto_commit_on_scan: scheduling RFID_SCAN_COMMIT for lane=%s sid=%s",
            self.name, lane, spoolman_id,
        )
        self.reactor.register_async_callback(
            lambda e, ln=lane: self.gcode.run_script_from_command(
                f"RFID_SCAN_COMMIT LANE={ln}"
            )
        )

    def _run_scan_window_sync(self, lane: str) -> Optional[dict]:
        """Start the timer-based scan engine and block (via reactor.pause) until a
        result is stored in self._pending[lane] or the scan window expires.

        ONLY call from GCode command handlers (Klipper completion greenlet).
        Do NOT call from event handlers or reactor timer callbacks.
        """
        self._sync_scan_lanes.add(lane)
        self._start_scan_timer(lane)
        poll_interval = max(_ASYNC_MIN_DELAY, self.scan_delay)
        deadline = self._scan_deadlines.get(lane, self.reactor.monotonic() + self.scan_window)
        try:
            while True:
                self.reactor.pause(self.reactor.monotonic() + poll_interval)
                if lane in self._pending:
                    return self._pending[lane]
                # Timer finished naturally (window expired) — no timer handle left.
                if lane not in self._scan_timers:
                    return self._pending.get(lane)
                if self.reactor.monotonic() > deadline + 0.5:
                    # Safety margin exceeded — cancel the stale timer and give up.
                    self._end_scan_session(lane, reason="sync_scan_safety_timeout")
                    return self._pending.get(lane)
        finally:
            self._sync_scan_lanes.discard(lane)

    def _scan_timer_callback(self, lane: str, event_time: float, gen: int = 0) -> float:
        """Reactor timer callback: attempt one scan and reschedule or stop.

        Must NOT call reactor.pause().  Returns next wake time or
        self.reactor.NEVER to stop the timer.

        Two commit strategies, selected by self.fast_mode:

        fast_mode=True (default):
          Any tick that returns a valid spoolman_id for a UID that is not
          blocked for this window is immediately committed.  No second
          confirmation read is required.

        fast_mode=False (safe/two-read mode):
          - Tick 1 (observe): a valid tag is seen → stored as candidate (count=1).
          - Tick 2+ (confirm): the same uid is seen again → count incremented.
          - When count >= 2: commit to _pending and stop.
          - Fallback: if the current tick returns NO tag at all (uid_hex is None)
            and a count==1 candidate exists that has not yet expired, commit it
            immediately — the tag passed the reader once and is now gone.
          - No-tag / blocked ticks: leave current candidate alive; only evict it
            once it has not been re-seen for longer than candidate_ttl seconds.

        In both modes _scan_blocked_uids prevents the same UID from being
        re-committed within the same scan window.
        """
        try:
            # Stale-callback guard: if the generation has moved on (another
            # _start_scan_timer or _end_scan_session ran since this callback was
            # registered) bail out immediately without doing any SPI work.
            # _start_scan_timer always calls _end_scan_session first, which bumps
            # the generation to >= 1, so the captured `gen` is always >= 1.
            # The sentinel -1 ensures that a callback whose lane entry was
            # unexpectedly removed from _scan_gen also bails out safely.
            if gen != self._scan_gen.get(lane, -1):
                return self.reactor.NEVER

            now = self.reactor.monotonic()
            remaining = self._scan_deadlines.get(lane, 0.0) - now
            self._debug_verbose(
                f"rfid[{self.name}]: DBG timer_tick lane={lane} remaining={remaining:.2f}s"
                f" fast_mode={self.fast_mode}"
            )
            result = self._scan_once(lane, max_pages=self.max_pages)
            uid_hex = result.get("uid_hex")
            spoolman_id = result.get("spoolman_id")
            blocked_sid = result.get("blocked_spoolman_id")
            tag_text = result.get("tag_text") or ""
            raw_len = result.get("raw_len", 0)
            filament_info = result.get("filament_info")
            self._debug_verbose(
                f"rfid[{self.name}]: DBG timer_result lane={lane}"
                f" uid={uid_hex} raw_len={raw_len} spoolman_id={spoolman_id}"
                f" blocked_sid={blocked_sid}"
                + (f" tag_text={tag_text!r}" if tag_text else "")
            )

            # Track every uid_hex seen this scan window for cache-recovery at lane_loaded.
            if uid_hex is not None:
                self._scan_seen_uids.setdefault(lane, set()).add(uid_hex)
                self._scan_last_uid[lane] = uid_hex  # most recently seen UID for deferred lookup

            # Track no-tag streak and total tick count for diagnostics (used in expiry log).
            self._scan_tick_count[lane] = self._scan_tick_count.get(lane, 0) + 1
            if uid_hex is None:
                self._scan_no_tag_streak[lane] = self._scan_no_tag_streak.get(lane, 0) + 1
            else:
                self._scan_no_tag_streak[lane] = 0

            # UID known but spoolman_id not yet resolved.
            # This covers two cases:
            #   • Full NDEF read (raw_len > 0): tag text present, auto-create path may apply.
            #   • UID-only read (raw_len == 0): anticollision succeeded but NDEF memory was
            #     not returned (tag passed too quickly, partial read, etc.).  The auto-create
            #     OpenSpool path is skipped in this case because there is no NDEF payload.
            # For AFC event-driven scans, the Spoolman UID lookup is deferred to
            # _handle_lane_loaded to keep HTTP work out of the hot SPI polling loop.
            # For GCode sync scans (_sync_scan_lanes), the lookup is dispatched immediately
            # so the blocking caller gets a result before the scan window expires.
            # blocked_uids is initialised here (before the spoolman_id branch) so
            # we can mark the uid as seen and prevent re-processing it every tick.
            blocked_uids = self._scan_blocked_uids.setdefault(lane, set())

            if (
                uid_hex is not None
                and spoolman_id is None
                and not result.get("blocked_spoolman_id")
                and uid_hex not in blocked_uids
            ):
                if self._spoolman is not None:
                    # Spoolman configured: check for "good find" early-commit first.
                    # Only applies to AFC event-driven timer scans (not GCode sync scans
                    # whose lane is in _sync_scan_lanes — those must let _run_scan_window_sync
                    # drive the result via _pending so the GCode caller gets a return value).
                    if self.auto_create_spool and lane not in self._sync_scan_lanes:
                        try:
                            _td = json.loads(tag_text) if tag_text else None
                            if isinstance(_td, dict) and _td.get("protocol") == "openspool":
                                blocked_uids.add(uid_hex)
                                self._clear_scan_state(lane, reason="openspool_early_commit")
                                self._debug(
                                    f"rfid[{self.name}]: DBG good_find_early_commit"
                                    f" lane={lane} uid={uid_hex}"
                                    f" — OpenSpool tag, dispatching auto-create"
                                )
                                _lane, _uid, _data = lane, uid_hex, dict(_td)
                                if self._spoolman_executor is not None:
                                    self._spoolman_run_async(
                                        lambda l=_lane, u=_uid, d=_data:
                                            self._do_auto_create_spool(l, u, d)
                                    )
                                else:
                                    self._log.warning(
                                        "rfid[%s]: not scheduling Spoolman auto-create for uid=%s lane=%s"
                                        " (client=%s executor=%s)",
                                        self.name, uid_hex, lane,
                                        self._spoolman is not None,
                                        self._spoolman_executor is not None,
                                    )
                                return self.reactor.NEVER
                        except (json.JSONDecodeError, ValueError):
                            pass
                        # Bambu (or other parsed) tag with filament metadata: trigger
                        # auto-create directly without waiting for a Spoolman UID lookup.
                        if (
                            filament_info is not None
                            and filament_info.get("material")
                        ):
                            blocked_uids.add(uid_hex)
                            self._clear_scan_state(lane, reason="bambu_filament_early_commit")
                            self._debug(
                                f"rfid[{self.name}]: Bambu/parsed tag — dispatching auto-create"
                                f" lane={lane} uid={uid_hex}"
                                f" material={filament_info.get('material')}"
                                f" color=#{filament_info.get('color_hex')}"
                                f" brand={filament_info.get('brand')}"
                            )
                            _lane, _uid, _data = lane, uid_hex, dict(filament_info)
                            if self._spoolman_executor is not None:
                                self._spoolman_run_async(
                                    lambda l=_lane, u=_uid, d=_data:
                                        self._do_auto_create_spool(l, u, d)
                                )
                            else:
                                self._log.warning(
                                    "rfid[%s]: not scheduling Bambu auto-create for uid=%s lane=%s"
                                    " (executor unavailable)",
                                    self.name, uid_hex, lane,
                                )
                            return self.reactor.NEVER
                    # No early-commit: fire off background UID lookup for sync scans; for
                    # AFC event-driven scans defer the Spoolman lookup to _handle_lane_loaded
                    # so it never runs inside the hot scan-timer / SPI loop.
                    if lane in self._sync_scan_lanes:
                        self._log.info(
                            "rfid[%s]: UID acquired uid=%s lane=%s"
                            " — dispatching Spoolman lookup (sync scan)",
                            self.name, uid_hex, lane,
                        )
                        blocked_uids.add(uid_hex)  # prevent re-dispatch this window
                        self._dispatch_uid_resolution(lane, uid_hex, tag_text)
                        # Fall through — scan timer continues looking for other spool_id tags.
                    else:
                        # AFC event path: record UID as seen but do NOT query Spoolman here.
                        # The fallback Spoolman UID→SID lookup will run exactly once in
                        # _handle_lane_loaded, after the scan window ends, avoiding HTTP
                        # work during the tight SPI polling loop.
                        self._log.info(
                            "rfid[%s]: UID acquired uid=%s lane=%s"
                            " — Spoolman lookup deferred to lane_loaded",
                            self.name, uid_hex, lane,
                        )
                        blocked_uids.add(uid_hex)  # don't re-process this UID this window
                        # Full Bambu read: tag is confirmed — no point keeping the scan
                        # loop running.  Stop now and let lane_loaded trigger the lookup.
                        if (
                            filament_info is not None
                            and filament_info.get("tag_format") == "bambu"
                            and filament_info.get("material")
                        ):
                            self._debug(
                                f"rfid[{self.name}]: Bambu full read complete"
                                f" lane={lane} uid={uid_hex}"
                                f" — stopping scan, awaiting lane_loaded for Spoolman lookup"
                            )
                            return self.reactor.NEVER
                elif not self.auto_create_spool:
                    # No Spoolman and no auto-create: nothing more to do.
                    self._respond(
                        f"RFID: lane {lane} tag uid={uid_hex} read"
                        f" (no spoolman_id, auto_create_spool=False — scan complete)"
                    )
                    self._clear_scan_state(lane, reason="no_spoolman_no_autocreate")
                    return self.reactor.NEVER

            if spoolman_id is not None:
                # Valid tag seen.
                if uid_hex in blocked_uids:
                    self._debug_verbose(
                        f"rfid[{self.name}]: DBG candidate_skip lane={lane}"
                        f" uid={uid_hex} reason=blocked_this_window"
                    )
                    if not self.fast_mode:
                        # Apply candidate_ttl aging while a blocked tag is present.
                        candidate = self._scan_candidates.get(lane)
                        if candidate is not None:
                            age = now - candidate["last_ts"]
                            if age > self.candidate_ttl:
                                self._debug(
                                    f"rfid[{self.name}]: DBG candidate_ttl_expire lane={lane}"
                                    f" uid={candidate['uid_hex']} age={age:.2f}s"
                                )
                                self._scan_candidates.pop(lane, None)
                elif self.fast_mode or (
                    filament_info is not None
                    and filament_info.get("tag_format") == "bambu"
                    and filament_info.get("material")
                ):
                    # Fast mode: single valid read is enough — commit immediately.
                    # Also applies when the tag is a fully-decoded Bambu tag:
                    # MIFARE authentication + successful sector decryption is
                    # stronger confirmation than a second UID sighting, so there
                    # is no value in keeping the scan loop running.
                    _commit_reason = "fast_mode_commit" if self.fast_mode else "bambu_full_read_commit"
                    blocked_uids.add(uid_hex)
                    self._pending[lane] = {
                        "lane": lane,
                        "spoolman_id": int(spoolman_id),
                        "uid_hex": uid_hex,
                        "tag_text": result.get("tag_text"),
                        "ts": time.time(),
                        "timeout": self.event_timeout,
                    }
                    self._commit_in_progress[lane] = True
                    self._scan_timers.pop(lane, None)
                    self._clear_scan_state(lane, reason=_commit_reason)
                    self._debug(
                        f"rfid[{self.name}]: DBG single_read_commit lane={lane}"
                        f" uid={uid_hex} sid={spoolman_id} reason={_commit_reason}"
                    )
                    self._respond(
                        f"RFID: tag found on lane {lane}, spoolman_id={spoolman_id}"
                    )
                    self._maybe_schedule_auto_commit(lane, int(spoolman_id))
                    # Step 4: async ensure the UID is recorded in the spool's extra fields.
                    if uid_hex is not None and self._spoolman is not None and self._spoolman_executor is not None:
                        _eu, _es = uid_hex, int(spoolman_id)
                        self._spoolman_run_async(
                            lambda u=_eu, s=_es: self._ensure_uid_in_spool_extra(u, s)
                        )
                    elif uid_hex is not None and self._spoolman is not None:
                        self._log.warning(
                            "rfid[%s]: not scheduling Spoolman UID write for uid=%s sid=%s"
                            " because executor is unavailable",
                            self.name, uid_hex, spoolman_id,
                        )
                    else:
                        self._debug(
                            f"rfid[{self.name}]: DBG skipping Spoolman UID write"
                            f" uid={uid_hex} sid={spoolman_id}"
                            f" client={self._spoolman is not None}"
                            f" executor={self._spoolman_executor is not None}"
                        )
                    return self.reactor.NEVER
                else:
                    # Safe mode: two-read confirmation.
                    candidate = self._scan_candidates.get(lane)
                    if candidate is not None and candidate["uid_hex"] == uid_hex:
                        candidate["count"] += 1
                        candidate["last_ts"] = now
                        self._debug_verbose(
                            f"rfid[{self.name}]: DBG candidate_observe lane={lane}"
                            f" uid={uid_hex} sid={spoolman_id} count={candidate['count']}"
                        )
                    else:
                        self._scan_candidates[lane] = {
                            "uid_hex": uid_hex,
                            "spoolman_id": spoolman_id,
                            "count": 1,
                            "last_ts": now,
                        }
                        candidate = self._scan_candidates[lane]
                        self._debug(
                            f"rfid[{self.name}]: DBG candidate_new lane={lane}"
                            f" uid={uid_hex} sid={spoolman_id}"
                        )

                    if candidate["count"] >= 2:
                        # Confirmed — re-check assigned-elsewhere as a safety net.
                        blocked_at_confirm = self._is_spool_assigned_elsewhere(lane, spoolman_id)
                        if blocked_at_confirm:
                            self._debug(
                                f"rfid[{self.name}]: DBG candidate_blocked lane={lane}"
                                f" uid={uid_hex} sid={spoolman_id}"
                                f" reason=assigned_elsewhere_at_confirm"
                            )
                            blocked_uids.add(uid_hex)
                            self._scan_candidates.pop(lane, None)
                        else:
                            blocked_uids.add(uid_hex)
                            self._pending[lane] = {
                                "lane": lane,
                                "spoolman_id": int(spoolman_id),
                                "uid_hex": uid_hex,
                                "tag_text": result.get("tag_text"),
                                "ts": time.time(),
                                "timeout": self.event_timeout,
                            }
                            self._commit_in_progress[lane] = True
                            self._scan_timers.pop(lane, None)
                            self._clear_scan_state(lane, reason="safe_mode_confirm")
                            self._debug(
                                f"rfid[{self.name}]: DBG candidate_confirm lane={lane}"
                                f" uid={uid_hex} sid={spoolman_id}"
                            )
                            self._respond(
                                f"RFID: tag found on lane {lane}, spoolman_id={spoolman_id}"
                            )
                            self._maybe_schedule_auto_commit(lane, int(spoolman_id))
                            # Step 4: async ensure the UID is recorded in the spool's extra fields.
                            if uid_hex is not None and self._spoolman is not None and self._spoolman_executor is not None:
                                _eu, _es = uid_hex, int(spoolman_id)
                                self._spoolman_run_async(
                                    lambda u=_eu, s=_es: self._ensure_uid_in_spool_extra(u, s)
                                )
                            elif uid_hex is not None and self._spoolman is not None:
                                self._log.warning(
                                    "rfid[%s]: not scheduling Spoolman UID write for uid=%s sid=%s"
                                    " because executor is unavailable",
                                    self.name, uid_hex, spoolman_id,
                                )
                            else:
                                self._debug(
                                    f"rfid[{self.name}]: DBG skipping Spoolman UID write"
                                    f" uid={uid_hex} sid={spoolman_id}"
                                    f" client={self._spoolman is not None}"
                                    f" executor={self._spoolman_executor is not None}"
                                )
                            return self.reactor.NEVER
            else:
                # No valid tag this tick (spoolman_id is None).
                #
                # Auto-create path: if the tag carries OpenSpool JSON but has no
                # spoolman_id, and auto_create_spool is enabled, launch a background
                # thread to create the spool in Spoolman.  The background thread uses
                # reactor.register_async_callback (thread-safe) to hand the result
                # back and issue RFID_SCAN_COMMIT — no blocking I/O on the reactor.
                if (uid_hex is not None
                        and self.auto_create_spool
                        and self._spoolman is not None
                        and uid_hex not in blocked_uids):
                    try:
                        tag_data = json.loads(tag_text) if tag_text else None
                        if isinstance(tag_data, dict) and tag_data.get("protocol") == "openspool":
                            blocked_uids.add(uid_hex)
                            self._clear_scan_state(lane, reason="auto_create_defer")
                            self._debug(
                                f"rfid[{self.name}]: DBG auto_create_defer"
                                f" lane={lane} uid={uid_hex}"
                            )
                            _lane, _uid, _data = lane, uid_hex, dict(tag_data)
                            if self._spoolman_executor is not None:
                                self._spoolman_run_async(
                                    lambda l=_lane, u=_uid, d=_data:
                                        self._do_auto_create_spool(l, u, d)
                                )
                            else:
                                self._log.warning(
                                    "rfid[%s]: not scheduling Spoolman auto-create for uid=%s lane=%s"
                                    " (client=%s executor=%s)",
                                    self.name, uid_hex, lane,
                                    self._spoolman is not None,
                                    self._spoolman_executor is not None,
                                )
                            return self.reactor.NEVER
                    except (json.JSONDecodeError, ValueError):
                        pass

                if not self.fast_mode:
                    candidate = self._scan_candidates.get(lane)
                    if candidate is not None:
                        age = now - candidate["last_ts"]
                        # Fallback commit condition: no tag detected at all (uid_hex is None,
                        # not just a failed NDEF parse), only one sighting so far, and the
                        # candidate is still fresh (within candidate_ttl).  This handles the
                        # case where the spool passed through too quickly for a second read.
                        if uid_hex is None and candidate["count"] == 1 and age <= self.candidate_ttl:
                            # Fallback: tag was seen once and is now gone — commit it.
                            cand_uid = candidate["uid_hex"]
                            cand_sid = candidate["spoolman_id"]
                            if cand_uid not in blocked_uids:
                                blocked_uids.add(cand_uid)
                                self._pending[lane] = {
                                    "lane": lane,
                                    "spoolman_id": int(cand_sid),
                                    "uid_hex": cand_uid,
                                    "tag_text": None,
                                    "ts": time.time(),
                                    "timeout": self.event_timeout,
                                }
                                self._commit_in_progress[lane] = True
                                self._scan_timers.pop(lane, None)
                                self._clear_scan_state(lane, reason="single_sighting_fallback")
                                self._debug(
                                    f"rfid[{self.name}]: DBG single_sighting_fallback lane={lane}"
                                    f" uid={cand_uid} sid={cand_sid}"
                                )
                                self._respond(
                                    f"RFID: tag found on lane {lane}, spoolman_id={cand_sid}"
                                )
                                self._maybe_schedule_auto_commit(lane, int(cand_sid))
                                # Step 4: async ensure the UID is recorded in the spool's extra fields.
                                if cand_uid is not None and self._spoolman is not None and self._spoolman_executor is not None:
                                    _eu, _es = cand_uid, int(cand_sid)
                                    self._spoolman_run_async(
                                        lambda u=_eu, s=_es: self._ensure_uid_in_spool_extra(u, s)
                                    )
                                elif cand_uid is not None and self._spoolman is not None:
                                    self._log.warning(
                                        "rfid[%s]: not scheduling Spoolman UID write for uid=%s sid=%s"
                                        " because executor is unavailable",
                                        self.name, cand_uid, cand_sid,
                                    )
                                else:
                                    self._debug(
                                        f"rfid[{self.name}]: DBG skipping Spoolman UID write"
                                        f" uid={cand_uid} sid={cand_sid}"
                                        f" client={self._spoolman is not None}"
                                        f" executor={self._spoolman_executor is not None}"
                                    )
                                return self.reactor.NEVER
                        elif age > self.candidate_ttl:
                            self._debug(
                                f"rfid[{self.name}]: DBG candidate_ttl_expire lane={lane}"
                                f" uid={candidate['uid_hex']} age={age:.2f}s"
                            )
                            self._scan_candidates.pop(lane, None)

            if now >= self._scan_deadlines.get(lane, 0.0):
                ticks = self._scan_tick_count.get(lane, 0)
                streak = self._scan_no_tag_streak.get(lane, 0)
                # Before wiping scan state, persist any seen UIDs so that lane_loaded
                # can still trigger the deferred Spoolman lookup even when the scan
                # window has already expired (the common race condition).
                _pending_entry = self._pending.get(lane)
                if not (_pending_entry and _pending_entry.get("spoolman_id")):
                    _last = self._scan_last_uid.get(lane)
                    _seen = self._scan_seen_uids.get(lane, set())
                    if _last is not None or _seen:
                        self._deferred_uid[lane] = {
                            "last_uid": _last,
                            "seen_uids": _seen,
                            "ts": time.time(),
                        }
                        self._debug(
                            f"rfid[{self.name}]: deferred_uid_saved lane={lane}"
                            f" uid={_last} seen={sorted(_seen)!r}"
                            f" (scan window expired; awaiting lane_loaded)"
                        )
                self._scan_timers.pop(lane, None)
                self._clear_scan_state(lane)
                self._respond(
                    f"RFID: scan window expired for lane {lane}"
                    f" reader={self.name} ticks={ticks} no_tag_streak={streak} — no tag found"
                )
                return self.reactor.NEVER

            next_delay = max(_ASYNC_MIN_DELAY, self.scan_delay)
            return event_time + next_delay
        except Exception:
            self._log.exception(
                "rfid[%s]: scan timer callback failed for lane %s", self.name, lane
            )
            self._scan_timers.pop(lane, None)
            self._clear_scan_state(lane, reason="callback_exception")
            return self.reactor.NEVER

    def _handle_klippy_flush(self) -> None:
        """Flush the UID cache to disk on Klipper disconnect or shutdown."""
        _flush_uid_cache_if_dirty()
        if self._spoolman_executor is not None:
            self._spoolman_executor.shutdown(wait=False)
            self._spoolman_executor = None

    def _handle_klippy_connect(self) -> None:
        """Detect which MMU system is active so the right GCode is issued at commit.

        Also performs deferred driver auto-detection for ``driver=auto`` configs:
        the SPI version probe is intentionally skipped during ``__init__`` (before
        the MCU connects) and runs here, once SPI commands are live.

        Additionally, if SpoolmanClient was not yet initialised (because
        ``spoolman_url`` was not set in the rfid config), resolve the URL from
        moonraker.conf now and create the client/executor so that auto-create
        and UID-write paths work for users who only configure Spoolman in
        moonraker.conf.  Creating the SpoolmanClient object here is non-blocking;
        any HTTP calls are deferred to GCode command handlers.
        """
        if self.printer.lookup_object("mmu", None) is not None:
            self._mmu_system = "hh"
        else:
            self._mmu_system = "afc"
        self._debug(f"rfid[{self.name}]: detected MMU system: {self._mmu_system}")
        if self.driver_cfg == "auto" and not self._detected_driver:
            self.reader = self._init_driver("auto")
            self._wire_reader(self.reader)
            self._detected_driver = True
        # Lazy SpoolmanClient init: if the explicit spoolman_url was not provided
        # in the rfid config the client was not created in __init__.  Try to
        # resolve the URL from moonraker.conf now and, if found, create the
        # client and executor so auto-create and UID-write paths are available.
        # Also recreates the executor after a klippy:disconnect/reconnect cycle
        # (flush clears the executor so async tasks don't silently become no-ops).
        if SpoolmanClient is not None and (self._spoolman is None or self._spoolman_executor is None):
            url = self._get_spoolman_url()
            if url:
                if self._spoolman is None:
                    # Cache the resolved URL so _get_spoolman_url() doesn't re-read
                    # moonraker.conf from disk on every subsequent call.
                    self.spoolman_url = url
                    self._spoolman = SpoolmanClient(
                        url,
                        api_key=self._spoolman_api_key,
                        timeout=self._spoolman_timeout,
                        use_uid_index=self._spoolman_uid_index,
                        uid_index_ttl=self._spoolman_uid_index_ttl,
                    )
                    self._debug(
                        f"rfid[{self.name}]: SpoolmanClient initialised from moonraker.conf url={url}"
                    )
                if self._spoolman_executor is None:
                    self._spoolman_executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=2, thread_name_prefix="rfid_spoolman"
                    )

    def _handle_lane_prep_start(self, lane_obj) -> None:
        self._debug_verbose(
            "rfid[%s]: EVENT afc:lane_prep_start raw=%r name=%r"
            % (self.name, lane_obj, getattr(lane_obj, "name", None))
        )
        try:
            lane = self._lane_name_from_event(lane_obj)
            self._debug_verbose(
                "rfid[%s]: EVENT normalized prep_start lane=%s reader_lanes=%s"
                % (self.name, lane, self.lanes)
            )
            if not lane:
                return
            reader, lane = self._find_reader_for_lane(lane)
            self._debug(
                "rfid[%s]: EVENT prep_start matched reader=%s lane=%s"
                % (self.name, getattr(reader, "name", None), lane)
            )
            if reader is not self:
                self._debug_verbose(f"rfid[{self.name}]: EVENT prep_start not for this reader")
                return
            # No-double-scan guard: if this lane was already confirmed (tag read
            # and committed) within the current load cycle — i.e. _lane_committed
            # is True and lane_loaded has explicitly not yet fired for this cycle
            # (value is False, not just missing) — skip starting another scan
            # window.  Using an explicit `is False` check avoids treating a missing
            # _lane_loaded_seen entry (no active AFC cycle) as a mid-cycle block,
            # which would incorrectly prevent a legitimate new scan after an
            # unrelated commit (e.g. a previous RFID_SCAN on the same lane).
            if self._lane_committed.get(lane) and self._lane_loaded_seen.get(lane) is False:
                self._debug(
                    f"rfid[{self.name}]: prep_start: lane {lane} already committed"
                    f" this cycle — skipping redundant scan"
                )
                return
            # Start the continuous scan timer so the reader keeps scanning
            # throughout the spool-spinning window instead of only once.
            self._start_scan_timer(lane)
        except Exception:
            self._log.exception("rfid[%s]: EVENT prep_start handler failed", self.name)

    def _handle_lane_loaded(self, lane_obj) -> None:
        self._debug_verbose(
            "rfid[%s]: EVENT afc:lane_loaded raw=%r name=%r"
            % (self.name, lane_obj, getattr(lane_obj, "name", None))
        )
        try:
            lane = self._lane_name_from_event(lane_obj)
            self._debug_verbose(
                "rfid[%s]: EVENT normalized lane_loaded lane=%s reader_lanes=%s"
                % (self.name, lane, self.lanes)
            )
            if not lane:
                return
            reader, lane = self._find_reader_for_lane(lane)
            self._debug(
                "rfid[%s]: EVENT lane_loaded matched reader=%s lane=%s"
                % (self.name, getattr(reader, "name", None), lane)
            )
            if reader is not self:
                self._debug_verbose(f"rfid[{self.name}]: EVENT lane_loaded not for this reader")
                return
            # Record that lane_loaded has been received for this scan session.
            # _on_created (auto_create_spool) checks this flag: if it is True
            # when the background spool-creation finishes, it commits immediately;
            # otherwise it leaves the spool_id in _pending for this handler to
            # pick up when it eventually fires.
            self._lane_loaded_seen[lane] = True
            # Save seen-UIDs and last-seen UID before ending the session; both are
            # cleared by _end_scan_session → _clear_scan_state and are needed below.
            seen_uids_snapshot = self._scan_seen_uids.get(lane, set()).copy()
            last_uid_snapshot = self._scan_last_uid.get(lane)
            # Cancel the scan timer and clear all per-lane scan state.
            self._end_scan_session(lane, reason="lane_loaded")

            # --- Deferred-UID fallback: scan timer may have already expired and
            # called _clear_scan_state before lane_loaded arrived, leaving empty
            # snapshots.  _deferred_uid is saved at window-expiry and survives
            # _clear_scan_state so we can still recover the UID here.
            _deferred = self._deferred_uid.pop(lane, None)
            if not seen_uids_snapshot and last_uid_snapshot is None and _deferred is not None:
                _age = time.time() - _deferred["ts"]
                if _age <= _DEFERRED_UID_TTL_S:
                    seen_uids_snapshot = _deferred["seen_uids"]
                    last_uid_snapshot = _deferred["last_uid"]
                    self._debug(
                        f"rfid[{self.name}]: deferred_uid_recovered lane={lane}"
                        f" uid={last_uid_snapshot} seen={sorted(seen_uids_snapshot)!r}"
                        f" age={_age:.1f}s (scan window had expired before lane_loaded)"
                    )
                else:
                    self._log.info(
                        "rfid[%s]: deferred_uid for lane=%s expired"
                        " (age=%.1fs > ttl=%.1fs), ignoring",
                        self.name, lane, _age, _DEFERRED_UID_TTL_S,
                    )

            # --- Pending-entry cache-fill: uid_hex present but spoolman_id=None ---
            pending = self._pending.get(lane)
            if pending is not None and pending.get("spoolman_id") is None:
                uid_hex = pending.get("uid_hex")
                if uid_hex:
                    _entry = _UID_SPOOL_CACHE.get(uid_hex)
                    if _entry is not None:
                        sid = _cache_entry_sid(_entry)
                        if sid is not None and not self._is_spool_assigned_elsewhere(lane, sid):
                            self._debug(
                                f"rfid[{self.name}]: cache fill: lane={lane} uid={uid_hex}"
                                f" sid={sid} (pending had uid but no spoolman_id)"
                            )
                            pending["spoolman_id"] = sid

            # --- Cache recovery: UID seen during scan window but SID not resolved in time ---
            # If _pending has no entry (or has an entry with spoolman_id=None after the
            # cache-fill above), check seen_uids_snapshot against _UID_SPOOL_CACHE.
            if lane not in self._pending or self._pending[lane].get("spoolman_id") is None:
                # Iterate in a deterministic order to avoid non-deterministic spool assignment
                for uid_hex in sorted(seen_uids_snapshot):
                    _entry = _UID_SPOOL_CACHE.get(uid_hex)
                    if _entry is None:
                        continue
                    sid = _cache_entry_sid(_entry)
                    if sid is None:
                        continue
                    if self._is_spool_assigned_elsewhere(lane, sid):
                        self._debug(
                            f"rfid[{self.name}]: cache recovery blocked: lane={lane}"
                            f" uid={uid_hex} sid={sid} already assigned elsewhere"
                        )
                        continue
                    self._debug(
                        f"rfid[{self.name}]: cache recovery: lane={lane} uid={uid_hex}"
                        f" sid={sid} (from _UID_SPOOL_CACHE, seen during scan window)"
                    )
                    self._pending[lane] = {
                        "lane": lane,
                        "spoolman_id": sid,
                        "uid_hex": uid_hex,
                        "raw_bytes": None,
                        "tag_text": None,
                        "filament_info": None,
                        "ts": time.time(),
                        "timeout": self.event_timeout,
                    }
                    break

            if lane not in self._pending or self._pending[lane].get("spoolman_id") is None:
                # --- Spoolman fallback: UID known but no spoolman_id from tag text or cache ---
                # Run at most one Spoolman UID→SID lookup per lane per session, and only
                # after lane_loaded (never during the hot scan-timer / SPI polling loop).
                best_uid = last_uid_snapshot
                if best_uid is None and seen_uids_snapshot:
                    best_uid = min(seen_uids_snapshot)  # deterministic fallback
                if (
                    self._spoolman is not None
                    and self._spoolman_executor is not None
                    and best_uid is not None
                    and not self._uid_lookup_in_flight.get(lane)
                ):
                    _lane = lane
                    _uid = best_uid
                    _timeout = self.event_timeout
                    # Capture tray_uid from pending filament_info for Bambu lot_nr fallback.
                    _pending_fi = (self._pending.get(lane) or {}).get("filament_info") or {}
                    _tray_uid_fallback: Optional[str] = None
                    if str(_pending_fi.get("tag_format") or "") == "bambu":
                        _raw = str(_pending_fi.get("tray_uid") or "").strip().upper()
                        if _raw:
                            _tray_uid_fallback = _raw
                    self._respond(
                        f"RFID: lane {_lane} uid={_uid} -- dispatching Spoolman lookup"
                    )

                    def _fallback_work():
                        # Mark in-flight only when the worker actually starts running
                        # (i.e., the submit succeeded).  Cleared in the reactor callback.
                        self._uid_lookup_in_flight[_lane] = True
                        sid: Optional[int] = None
                        try:
                            sid = self._spoolman_find_spool_by_uid(_uid)
                        except Exception as exc:
                            self._log.warning(
                                "rfid[%s]: Spoolman fallback lookup (attempt 1) uid=%s failed: %s",
                                self.name, _uid, exc,
                            )
                        if sid is None:
                            # One retry after a short delay before giving up.
                            self._log.info(
                                "rfid[%s]: Spoolman fallback lookup uid=%s: attempt 1 found nothing,"
                                " retrying in %.0fs...",
                                self.name, _uid, _SPOOLMAN_RETRY_DELAY_S,
                            )
                            time.sleep(_SPOOLMAN_RETRY_DELAY_S)
                            try:
                                sid = self._spoolman_find_spool_by_uid(_uid)
                            except Exception as exc:
                                self._log.warning(
                                    "rfid[%s]: Spoolman fallback lookup (attempt 2) uid=%s failed: %s",
                                    self.name, _uid, exc,
                                )

                        # For Bambu tags: if uid lookup failed, try lot_nr (tray_uid) lookup.
                        # This handles the second RFID tag on a Bambu spool whose uid is not
                        # yet registered in Spoolman but whose tray_uid matches lot_nr.
                        if sid is None and _tray_uid_fallback:
                            self._log.info(
                                "rfid[%s]: Spoolman fallback: uid=%s not found;"
                                " trying Bambu tray_uid lot_nr=%s",
                                self.name, _uid, _tray_uid_fallback,
                            )
                            try:
                                sid = self._spoolman_find_spool_by_tray_uid(_tray_uid_fallback)
                                if sid is not None:
                                    # Associate the scanned uid with the existing spool.
                                    self._ensure_uid_in_spool_extra(_uid, sid)
                            except Exception as exc:
                                self._log.warning(
                                    "rfid[%s]: Spoolman lot_nr fallback tray_uid=%s failed: %s",
                                    self.name, _tray_uid_fallback, exc,
                                )

                        def _on_spoolman_result(event_time):
                            self._uid_lookup_in_flight.pop(_lane, None)
                            if self._commit_in_progress.get(_lane) or self._lane_committed.get(_lane):
                                self._debug(
                                    f"rfid[{self.name}]: fallback lookup: commit already"
                                    f" in progress/done for {_lane}, discarding result"
                                )
                                return
                            if sid is not None:
                                _UID_SPOOL_CACHE[_uid] = sid
                                _mark_uid_cache_dirty()
                                self._pending[_lane] = {
                                    "lane": _lane,
                                    "spoolman_id": sid,
                                    "uid_hex": _uid,
                                    "raw_bytes": None,
                                    "tag_text": None,
                                    "filament_info": None,
                                    "ts": time.time(),
                                    "timeout": _timeout,
                                }
                                self._log.info(
                                    "rfid[%s]: Spoolman fallback: found spool %s for uid=%s on lane %s",
                                    self.name, sid, _uid, _lane,
                                )
                                self._respond(
                                    f"RFID: tag uid={_uid} matched spool {sid} in Spoolman"
                                    f" (deferred) on lane {_lane}"
                                )
                                _flush_uid_cache_if_dirty()
                                self._commit_in_progress[_lane] = True
                                self.gcode.run_script_from_command(
                                    f"RFID_SCAN_COMMIT LANE={_lane}"
                                )
                            else:
                                self._log.warning(
                                    "rfid[%s]: Spoolman fallback lookup for uid=%s failed after"
                                    " retry — aborting commit for lane %s",
                                    self.name, _uid, _lane,
                                )
                                self._respond(
                                    f"RFID: Spoolman lookup failed for uid={_uid} on lane {_lane}"
                                    f" — commit aborted"
                                )

                        self.reactor.register_async_callback(_on_spoolman_result)

                    self._spoolman_run_async(_fallback_work)
                    return  # commit (or abort) will happen asynchronously from _on_spoolman_result
                self._debug(f"rfid[{self.name}]: lane_loaded but no pending scan for {lane}, skipping commit")
                return
            # Flush any new UID→spoolman_id mappings learned during this scan window.
            _flush_uid_cache_if_dirty()
            self.reactor.register_async_callback(
                lambda e, ln=lane: self.gcode.run_script_from_command("RFID_SCAN_COMMIT LANE=" + ln)
            )
        except Exception:
            self._log.exception("rfid[%s]: EVENT lane_loaded handler failed", self.name)

    # ---------- Spoolman UID extra-field helpers ----------

    def _spoolman_find_spool_by_uid(self, uid_hex: str) -> Optional[int]:
        """Query Spoolman for a spool whose ``rfid_uid_N`` extra field equals ``uid_hex``.

        Delegates to SpoolmanClient.find_spool_by_uid which handles field existence
        checks, exact-match queries, verification, and fallback scanning.

        Returns the spool ID (int) if found, None otherwise.
        Only call from GCode command handlers / thread-pool workers — never from
        reactor timer callbacks (blocking urllib calls).
        """
        if self._spoolman is None:
            return None
        try:
            sid = self._spoolman.find_spool_by_uid(uid_hex, self.max_uids)
            if sid is not None:
                self._debug(
                    f"rfid: _spoolman_find_spool_by_uid: found spool {sid} for uid={uid_hex}"
                )
            return sid
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: _spoolman_find_spool_by_uid uid=%s failed: %s",
                self.name, uid_hex, exc,
            )
            return None

    def _spoolman_find_spool_by_tray_uid(self, tray_uid: str) -> Optional[int]:
        """Query Spoolman for a spool whose ``lot_nr`` equals *tray_uid*.

        Delegates to SpoolmanClient.find_spool_by_lot_nr.  Used for Bambu tags
        that share the same Tray UID across two physical RFID tags — the second
        tag can resolve to the existing spool by lot_nr instead of creating a
        duplicate.

        Returns the spool ID (int) if found, None otherwise.
        Raises RuntimeError when the lookup is inconclusive (network error),
        so callers can abort creation rather than risk duplicates.
        Only call from GCode command handlers / thread-pool workers — never
        from reactor timer callbacks (blocking urllib calls).
        """
        if self._spoolman is None:
            return None
        sid = self._spoolman.find_spool_by_lot_nr(tray_uid)
        if sid is not None:
            self._debug(
                f"rfid: _spoolman_find_spool_by_tray_uid:"
                f" found spool {sid} for tray_uid={tray_uid}"
            )
        return sid

    def _spoolman_add_uid_to_spool(self, spool_id: int, uid_hex: str) -> bool:
        """Write ``uid_hex`` into the first empty ``rfid_uid_N`` slot on ``spool_id``.

        Delegates to SpoolmanClient.add_uid_to_spool which handles field existence,
        slot discovery, PATCH, and field auto-creation on HTTP 400.

        Returns True when ``uid_hex`` is now registered on the spool (was already
        there, or successfully written to a free slot).  Returns False on HTTP
        failure or when all slots are full.
        """
        if self._spoolman is None:
            return False
        try:
            return self._spoolman.add_uid_to_spool(spool_id, uid_hex, self.max_uids)
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: _spoolman_add_uid_to_spool spool_id=%s uid=%s failed: %s",
                self.name, spool_id, uid_hex, exc,
            )
            return False

    def _spoolman_remove_uid_from_spool(self, spool_id: int, uid_hex: str) -> bool:
        """Clear the ``rfid_uid_N`` slot that contains ``uid_hex`` on ``spool_id``.

        Delegates to SpoolmanClient.remove_uid_from_spool which handles the safe
        read-modify-write: fetches current slots first, then clears only the matching
        slot.

        Returns True when ``uid_hex`` is no longer registered (was already absent,
        or successfully removed).  Returns False on HTTP failure.
        """
        if self._spoolman is None:
            return False
        try:
            return self._spoolman.remove_uid_from_spool(spool_id, uid_hex, self.max_uids)
        except Exception as exc:
            self._log.warning(
                "rfid[%s]: _spoolman_remove_uid_from_spool spool_id=%s uid=%s failed: %s",
                self.name, spool_id, uid_hex, exc,
            )
            return False

    def _reassign_uid_to_spool(self, uid_hex: str, new_sid: int) -> None:
        """Atomically move ``uid_hex`` from whatever spool currently owns it to ``new_sid``.

        Steps:
        1. Find any existing spool that already owns this UID.
        2. If found and it is a different spool, remove the UID from that old spool.
        3. Write the UID to the first empty slot on ``new_sid``.  Up to ``max_uids``
           UIDs are supported; all existing UIDs on the spool are always preserved.
        4. Update the local ``_UID_SPOOL_CACHE`` only when all Spoolman updates succeed.

        Only call from GCode command handlers — never from reactor timer callbacks
        (blocking urllib calls).
        """
        self._debug(
            f"rfid: _reassign_uid_to_spool enter uid={uid_hex} new_sid={new_sid}"
        )

        old_sid = self._spoolman_find_spool_by_uid(uid_hex)
        success = True

        # UID is already on the correct spool — no Spoolman changes needed.
        if old_sid is not None and old_sid == new_sid:
            self._debug(
                f"rfid: _reassign_uid_to_spool uid={uid_hex} already on correct"
                f" spool sid={new_sid} — skipping remove/add"
            )
            _UID_SPOOL_CACHE[uid_hex] = new_sid
            _mark_uid_cache_dirty()
            return

        # Remove from old spool if it is different from the new one.
        if old_sid is not None and old_sid != new_sid:
            if not self._spoolman_remove_uid_from_spool(old_sid, uid_hex):
                success = False
                self._debug(
                    f"rfid: ERROR: failed to remove uid={uid_hex} from old spool sid={old_sid}"
                )

        # Add to new spool only if the removal step succeeded.
        if success:
            if not self._spoolman_add_uid_to_spool(new_sid, uid_hex):
                success = False
                self._debug(
                    f"rfid: ERROR: failed to add uid={uid_hex} to spool sid={new_sid}"
                )

        # Update local cache only when all Spoolman updates succeeded.
        if success:
            _UID_SPOOL_CACHE[uid_hex] = new_sid
            _mark_uid_cache_dirty()

    def _spoolman_remove_uid(self, uid_hex: str) -> None:
        """Remove ``uid_hex`` from whichever Spoolman spool currently owns it.

        Also evicts the UID from the local ``_UID_SPOOL_CACHE``.
        Used by ``RFID_ERASE`` and any reassignment cleanup path.

        Only call from GCode command handlers — never from reactor timer callbacks.
        """
        old_sid = self._spoolman_find_spool_by_uid(uid_hex)
        if old_sid is not None:
            self._spoolman_remove_uid_from_spool(old_sid, uid_hex)
        _UID_SPOOL_CACHE.pop(uid_hex, None)
        _mark_uid_cache_dirty()
    # ---------- Spoolman auto-create helpers ----------

    def _get_spoolman_url(self) -> Optional[str]:
        """Return the Spoolman server URL, or None if unavailable.

        Resolution order:
        1. ``spoolman_url`` config option (if non-empty).
        2. ``server`` key in the ``[spoolman]`` section of moonraker.conf.
        """
        if self.spoolman_url:
            return self.spoolman_url.rstrip("/")
        # Try reading from Moonraker config
        moonraker_conf = os.path.expanduser("~/printer_data/config/moonraker.conf")
        if os.path.isfile(moonraker_conf):
            cfg = configparser.ConfigParser()
            try:
                cfg.read(moonraker_conf)
                server = cfg.get("spoolman", "server", fallback=None)
                if server:
                    return server.strip().rstrip("/")
            except Exception as exc:
                self._debug(f"rfid: could not read spoolman URL from moonraker.conf: {exc}")
        return None

    def _auto_create_spool(self, filament_info: dict, uid_hex: Optional[str] = None) -> Optional[int]:
        """Delegate to SpoolmanClient.auto_create_spool."""
        if self._spoolman is None:
            return None
        return self._spoolman.auto_create_spool(filament_info, uid_hex=uid_hex)

    def _fetch_spoolman_spool(self, spool_id: int) -> Optional[dict]:
        """Delegate to SpoolmanClient.get_spool."""
        if self._spoolman is None:
            self._debug("rfid: _fetch_spoolman_spool: no Spoolman client configured")
            return None
        try:
            return self._spoolman.get_spool(int(spool_id))
        except Exception as exc:
            self._debug("rfid: _fetch_spoolman_spool id=%d failed: %s" % (spool_id, exc))
            return None

    def _build_openspool_payload(self, spool_data: dict) -> Optional[str]:
        """Delegate to SpoolmanClient.build_openspool_payload."""
        if SpoolmanClient is None:
            return None
        return SpoolmanClient.build_openspool_payload(spool_data)

    # ---------- internal scan / commit ----------
    def _event_scan_begin(
        self,
        port: str,
        timeout: Optional[float] = None,
        max_pages: Optional[int] = None,
    ) -> bool:
        """Scan using the timer-based engine and store the result as a pending entry.

        Uses the same fast_mode / safe-mode, candidate_ttl, _scan_blocked_uids, and
        scan_window logic as the AFC event-driven path.

        CAUTION: Uses reactor.pause() for polling.
        ONLY call from GCode command handlers (Klipper completion greenlet).
        For event handlers dispatch RFID_SCAN/RFID_SCAN_COMMIT GCode commands instead.
        """
        port = self._normalize_port(port)
        effective_window = self.scan_window if timeout is None else float(timeout)
        self._debug(f"rfid[{self.name}]: begin port={port} timeout={effective_window:.1f}")
        saved_window = self.scan_window
        saved_pages = self.max_pages
        if timeout is not None:
            self.scan_window = float(timeout)
        if max_pages is not None:
            self.max_pages = max(4, int(max_pages))
        try:
            result = self._run_scan_window_sync(port)
        finally:
            self.scan_window = saved_window
            self.max_pages = saved_pages
        if result is None:
            self._pending.pop(port, None)
            self._respond(f"RFID: no tag found for {port}")
            return False
        if result.get("spoolman_id") is None:
            uid = result.get("uid_hex", "unknown")
            # Cache miss path: before attempting auto_create, check whether this UID
            # is already registered in Spoolman's rfid_uid_N extra fields.  This handles
            # UIDs added manually in the Spoolman UI, cache evictions after restart,
            # and any case where the local cache doesn't yet know about the mapping.
            if uid != "unknown":
                self._debug(
                    f"rfid[{self.name}]: cache miss uid={uid} — querying Spoolman"
                    " for existing rfid_uid_N association"
                )
                found_sid = self._spoolman_find_spool_by_uid(uid)
                if found_sid is not None:
                    self._debug(
                        f"rfid[{self.name}]: spoolman_find_spool_by_uid: uid={uid}"
                        f" → spool_id={found_sid} (cache miss, Spoolman hit)"
                    )
                    # Sync local cache and set as the pending result.
                    _UID_SPOOL_CACHE[uid] = found_sid
                    _mark_uid_cache_dirty()
                    self._pending[port] = {
                        "lane": port,
                        "spoolman_id": found_sid,
                        "uid_hex": uid,
                        "tag_text": result.get("tag_text"),
                        "ts": time.time(),
                        "timeout": self.event_timeout,
                    }
                    self._debug(f"rfid[{self.name}]: pending stored for {port}")
                    return True
            if self.auto_create_spool and uid != "unknown" and _tag_parser is not None:
                raw_bytes = result.get("raw_bytes") or b""
                # Use filament_info already parsed in _scan_once if available (e.g. Bambu),
                # otherwise try to parse from raw bytes.
                filament_info = result.get("filament_info")
                if filament_info is None:
                    filament_info = self._apply_tag_parser(uid, raw_bytes, result.get("tag_text"))
                # If parse_tag detected a Bambu tag in raw bytes but could not decrypt it
                # (returns an error dict), attempt a fresh authenticated read as a fallback.
                if filament_info is None or _tag_parser.is_parse_error(filament_info):
                    bambu_blocks = self._try_bambu_read_with_fallback(uid)
                    if bambu_blocks is not None:
                        filament_info = self._apply_tag_parser(uid, bambu_blocks)
                if filament_info and filament_info.get("material"):
                    self._debug(
                        f"rfid[{self.name}]: auto_create_spool: parsed tag"
                        f" uid={uid} format={filament_info.get('tag_format')}"
                        f" material={filament_info.get('material')}"
                        f" color=#{filament_info.get('color_hex')}"
                        f" brand={filament_info.get('brand')}"
                    )
                    new_sid = self._auto_create_spool(filament_info, uid_hex=uid if uid != "unknown" else None)
                    if new_sid is not None:
                        self._debug(
                            f"rfid[{self.name}]: auto_create_spool lane={port} → spoolman_id={new_sid}"
                        )
                        self._respond(
                            f"RFID: auto-created spool id={new_sid}"
                            f" for uid={uid} ({filament_info.get('material', '?')})"
                        )
                        if uid != "unknown":
                            # Register UID in Spoolman extra field and update local cache.
                            self._reassign_uid_to_spool(uid, new_sid)
                        self._pending[port] = {
                            "lane": port,
                            "spoolman_id": new_sid,
                            "uid_hex": uid,
                            "tag_text": result.get("tag_text"),
                            "ts": time.time(),
                            "timeout": self.event_timeout,
                        }
                        self._debug(f"rfid[{self.name}]: pending stored for {port}")
                        return True
                    self._debug(
                        f"rfid[{self.name}]: auto_create_spool lane={port} → failed for uid={uid}"
                    )
                else:
                    self._debug(
                        f"rfid[{self.name}]: auto_create_spool: unrecognised tag format"
                        f" uid={uid} raw_len={len(raw_bytes)}"
                    )
            self._pending.pop(port, None)
            self._respond(
                f"RFID: tag found (uid={uid}) but no spoolman_id for {port}"
            )
            return False
        # spoolman_id resolved from tag NDEF payload — register UID in Spoolman.
        uid = result.get("uid_hex", "unknown")
        sid = result.get("spoolman_id")
        if uid != "unknown" and sid is not None:
            self._reassign_uid_to_spool(uid, int(sid))
        self._debug(f"rfid[{self.name}]: pending stored for {port}")
        return True

    def _event_scan_commit(self, port: str) -> bool:
        """Commit a pending scan; dispatches AFC or HH GCode based on detected system."""
        port = self._normalize_port(port)
        # Single-shot guard: prevent double-commit within the same scan session.
        if self._lane_committed.get(port):
            self._debug(f"rfid[{self.name}]: commit guard: {port} already committed this session")
            return False
        pending = self._pending.get(port)
        if not pending:
            self._debug(f"rfid[{self.name}]: no pending scan to commit for {port}")
            return False

        age = time.time() - float(pending.get("ts", 0.0))
        timeout = float(pending.get("timeout", self.event_timeout))
        if age > timeout:
            self._pending.pop(port, None)
            self._respond(f"RFID: pending scan expired for {port}")
            return False

        spoolman_id = pending.get("spoolman_id")
        if spoolman_id is None:
            self._pending.pop(port, None)
            self._respond(f"RFID: pending scan missing spoolman_id for {port}")
            return False

        self._lane_committed[port] = True
        self._respond(
            f"RFID: scan committed for {port}, spoolman_id={spoolman_id}"
        )
        gate_str = port[len("lane"):] if port.startswith("lane") else port
        if gate_str.isdigit():
            self._assign_spool_to_gate(int(gate_str), int(spoolman_id))
        elif self._mmu_system == "hh":
            # Happy Hare requires a numeric gate index; named ports are not
            # supported.  Emit a clear error rather than running a command
            # (SET_SPOOL_ID) that does not exist on HH-only setups.
            self._respond(
                f"RFID: cannot commit port '{port}' on Happy Hare — "
                "HH commits require a numeric lane/gate index (use LANE=<n> or SLOT=<n>)"
            )
            return False
        else:
            # Non-numeric port name on AFC: fall back to direct AFC command.
            script = f"SET_SPOOL_ID LANE={port} SPOOL_ID={int(spoolman_id)}"
            self.reactor.register_async_callback(
                lambda e, s=script: self.gcode.run_script_from_command(s)
            )
        self._pending.pop(port, None)
        _flush_uid_cache_if_dirty()
        self._debug(f"rfid[{self.name}]: commit complete for {port}")
        return True

    # ---------- gcode commands ----------
    def _resolve_port_param(self, gcmd, cmd_name: str) -> str:
        """Read LANE= or SLOT= from a GCode command.

        Bare numbers are prefixed before normalisation so _normalize_port can
        apply the correct offset:
          LANE=1  (or LANE=lane1)  →  "lane1"  (1-based)
          SLOT=0  (or SLOT=slot0)  →  "slot0"  (0-based, → lane1 after normalise)
        """
        lane_raw = gcmd.get("LANE", None)
        slot_raw = gcmd.get("SLOT", None)
        if lane_raw is not None:
            raw = str(lane_raw).strip()
            return f"lane{raw}" if raw.isdigit() else raw
        if slot_raw is not None:
            raw = str(slot_raw).strip()
            return f"slot{raw}" if raw.isdigit() else raw
        raise gcmd.error(f"{cmd_name} requires LANE= or SLOT=")

    def _slot_display(self, val: str) -> str:
        """Convert a stored lane/slot value to canonical slot display notation.

        lane{n}  →  slot{n-1}  (e.g. lane1 → slot0)
        slot{n}  →  slot{n}    (already slot-based, shown as-is)
        """
        v = str(val).strip().lower()
        m = re.fullmatch(r"lane(\d+)", v)
        if m:
            return f"slot{int(m.group(1)) - 1}"
        return v

    def cmd_RFID_TAG(self, gcmd):
        info = self._scan_once(self.lane, max_pages=self.max_pages)
        if not info or not info.get("uid_hex"):
            gcmd.respond_info(f"rfid[{self.name}]: no tag")
            return
        gcmd.respond_info(
            "rfid[%s]: uid=%s raw_len=%s spoolman_id=%s"
            % (
                self.name,
                info.get("uid_hex"),
                info.get("raw_len", 0),
                info.get("spoolman_id"),
            )
        )
        if info.get("tag_text"):
            gcmd.respond_info("rfid[%s]: tag_text=%s" % (self.name, info["tag_text"]))

    def cmd_RFID_LANES(self, gcmd):
        for reader in self._all_readers():
            slot_display = [self._slot_display(s) for s in reader.slots]
            self._respond(f"rfid[{reader.name}]: lanes={reader.lanes} slots={slot_display}")

    def cmd_RFID_PENDING(self, gcmd):
        shown = False
        for reader in self._all_readers():
            for port, pending in sorted(reader._pending.items()):
                shown = True
                age = time.time() - float(pending.get("ts", 0.0))
                self._respond(
                    "rfid: pending port=%s spoolman_id=%s age=%.1fs reader=%s"
                    % (port, pending.get("spoolman_id"), age, reader.name)
                )
        if not shown:
            self._respond("rfid: no pending scans")

    def cmd_RFID_SCAN(self, gcmd):
        raw = self._resolve_port_param(gcmd, "RFID_SCAN")
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"No rfid reader is mapped to {port}")
        if not reader._event_scan_begin(port):
            raise gcmd.error(f"No spoolman_id found for {port}")
        if not reader._event_scan_commit(port):
            raise gcmd.error(f"Unable to commit scan for {port}")

    def cmd_RFID_SCAN_BEGIN(self, gcmd):
        raw = self._resolve_port_param(gcmd, "RFID_SCAN_BEGIN")
        timeout = float(gcmd.get_float("TIMEOUT", self.event_timeout))
        max_pages = int(gcmd.get_int("MAX_PAGES", self.max_pages))
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"No rfid reader is mapped to {port}")
        if not reader._event_scan_begin(port, timeout=timeout, max_pages=max_pages):
            gcmd.respond_info(f"RFID: no spoolman_id found for {port}, skipping")
            return

    def cmd_RFID_SCAN_COMMIT(self, gcmd):
        raw = self._resolve_port_param(gcmd, "RFID_SCAN_COMMIT")
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"No rfid reader is mapped to {port}")
        if not reader._event_scan_commit(port):
            gcmd.respond_info(f"RFID: no pending or valid scan to commit for {port}, skipping")
            return

    def cmd_RFID_CACHE_CLEAR(self, gcmd):
        global _UID_CACHE_DIRTY
        count = len(_UID_SPOOL_CACHE)
        _UID_SPOOL_CACHE.clear()
        _save_uid_cache(_UID_SPOOL_CACHE)
        _UID_CACHE_DIRTY = False
        self._respond(f"rfid: cache cleared ({count} entries removed)")

    def cmd_RFID_CACHE_LIST(self, gcmd):
        if not _UID_SPOOL_CACHE:
            self._respond("rfid: cache is empty")
            return
        for uid_hex, entry in sorted(_UID_SPOOL_CACHE.items()):
            sid = _cache_entry_sid(entry)
            self._respond(f"rfid: cache uid={uid_hex} spoolman_id={sid}")

    def cmd_RFID_CHECK_TAG(self, gcmd):
        """RFID_CHECK_TAG [LANE=<n>|SLOT=<n>] [CREATE=0|1] [WRITE=0|1]

        Performs a single synchronous scan, parses filament metadata, and reports:
          - UID
          - Source: cache / tag / created
          - All parsed filament fields (material, color, brand, temps, weight, etc.)
          - Spoolman spool creation result (if CREATE=1)
          - Write-back result (if WRITE=1; skipped for Bambu tags)
        """
        raw = self._resolve_port_param(gcmd, "RFID_CHECK_TAG")
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"RFID_CHECK_TAG: no rfid reader is mapped to {port}")
        create = gcmd.get_int("CREATE", reader.auto_create_spool)
        write = gcmd.get_int("WRITE", 0)

        reader._debug(f"rfid[{reader.name}]: RFID_CHECK_TAG port={port} create={create} write={write}")

        scan_result = reader._run_scan_window_sync(port)

        if scan_result is None:
            gcmd.respond_info("RFID_CHECK_TAG: no tag detected")
            return

        uid_hex = scan_result.get("uid_hex") or "unknown"
        raw_bytes = scan_result.get("raw_bytes") or b""
        tag_text = scan_result.get("tag_text") or ""
        spoolman_id = scan_result.get("spoolman_id")
        source = "scan"

        gcmd.respond_info(f"RFID_CHECK_TAG: uid={uid_hex}")

        # Check local cache for a known UID → spool mapping.
        if spoolman_id is None and uid_hex != "unknown":
            _entry = _UID_SPOOL_CACHE.get(uid_hex)
            if _entry is not None:
                spoolman_id = _cache_entry_sid(_entry)
                source = "cache"

        if spoolman_id is not None:
            gcmd.respond_info(
                f"RFID_CHECK_TAG: uid={uid_hex} spoolman_id={spoolman_id} (from {source})"
            )
            return

        # Try to parse filament info from raw bytes, then try Bambu auth read.
        filament_info = None
        fmt = "?"
        if _tag_parser is not None:
            # Use filament_info from scan result if already parsed (e.g. Bambu via _scan_once).
            filament_info = scan_result.get("filament_info") if isinstance(scan_result, dict) else None
            if filament_info is None:
                filament_info = reader._apply_tag_parser(uid_hex, raw_bytes, tag_text)
            # If raw bytes parse detected a Bambu tag but could not decrypt it
            # (returns an error dict), still attempt authenticated read as a fallback.
            if (filament_info is None or _tag_parser.is_parse_error(filament_info)) and uid_hex != "unknown":
                bambu_blocks = reader._try_bambu_read_with_fallback(uid_hex)
                if bambu_blocks is not None:
                    filament_info = reader._apply_tag_parser(uid_hex, bambu_blocks)

        if filament_info and filament_info.get("material"):
            fmt = filament_info.get("tag_format", "?")
            mat = filament_info.get("material", "?")
            color = filament_info.get("color_hex", "?")
            brand = filament_info.get("brand", "?")

            # For Bambu tags: emit the full labeled summary (matching the Android app view)
            # so the user sees tray UID, all temperatures, weight, dates, etc. in one place.
            if fmt == "bambu" and hasattr(_tag_parser, "format_bambu_info"):
                summary = _tag_parser.format_bambu_info(filament_info, uid_hex=uid_hex)
                gcmd.respond_info(summary)
            else:
                gcmd.respond_info(
                    f"RFID_CHECK_TAG: format={fmt} material={mat}"
                    f" color=#{color} brand={brand}"
                )
                for key in ("min_temp", "max_temp", "bed_temp", "diameter_mm", "weight_g"):
                    val = filament_info.get(key)
                    if val is not None:
                        gcmd.respond_info(f"RFID_CHECK_TAG:   {key}={val}")

            spoolman_id_from_tag = filament_info.get("spoolman_id")
            if spoolman_id_from_tag is not None:
                spoolman_id = spoolman_id_from_tag
                source = "tag"
                if uid_hex != "unknown":
                    # Register UID in Spoolman extra field and update local cache.
                    reader._reassign_uid_to_spool(uid_hex, int(spoolman_id))
                gcmd.respond_info(
                    f"RFID_CHECK_TAG: uid={uid_hex} spoolman_id={spoolman_id} (from tag payload)"
                )
                return

            if create:
                spoolman_url = reader._get_spoolman_url()
                if not spoolman_url:
                    gcmd.respond_info(
                        "RFID_CHECK_TAG: auto-create requested but spoolman_url not configured"
                    )
                else:
                    # Before creating, check whether this UID is already in Spoolman.
                    # Only proceed with creation after a *definitive* not-found result.
                    # A lookup error is inconclusive — skip creation to avoid duplicates.
                    found_sid = None
                    lookup_error = False
                    if uid_hex != "unknown" and reader._spoolman is not None:
                        try:
                            found_sid = reader._spoolman.find_spool_by_uid(uid_hex, reader.max_uids)
                        except Exception as _lkp_exc:
                            lookup_error = True
                            gcmd.respond_info(
                                f"RFID_CHECK_TAG: Spoolman lookup failed for uid={uid_hex}"
                                f" ({_lkp_exc}) — skipping auto-create (inconclusive)"
                            )
                    if not lookup_error:
                        if found_sid is not None:
                            gcmd.respond_info(
                                f"RFID_CHECK_TAG: uid={uid_hex} already found as spool"
                                f" {found_sid} in Spoolman — skipping auto-create"
                            )
                            spoolman_id = found_sid
                            if write:
                                is_bambu = fmt == "bambu"
                                if is_bambu:
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: Bambu tag uid={uid_hex}: "
                                        "spoolman_id cached (cannot write to Bambu tag)"
                                    )
                                else:
                                    ok = reader._write_spoolman_id_to_tag(
                                        spoolman_id, uid_hex, is_bambu=is_bambu
                                    )
                                    if ok:
                                        gcmd.respond_info(
                                            f"RFID_CHECK_TAG: wrote spoolman_id={spoolman_id}"
                                            f" to tag uid={uid_hex}"
                                        )
                                    else:
                                        gcmd.respond_info(
                                            f"RFID_CHECK_TAG: write-back skipped or failed"
                                            f" for uid={uid_hex}"
                                        )
                        else:
                            # For Bambu tags: check whether a spool already exists
                            # with the same Tray UID stored as lot_nr before creating.
                            _tray_uid = str(filament_info.get("tray_uid") or "").strip().upper()
                            if _tray_uid and fmt == "bambu" and reader._spoolman is not None:
                                try:
                                    _lot_sid = reader._spoolman_find_spool_by_tray_uid(_tray_uid)
                                except Exception as _lot_exc:
                                    lookup_error = True
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: Spoolman lot_nr lookup failed"
                                        f" for tray_uid={_tray_uid}"
                                        f" ({_lot_exc}) — skipping auto-create (inconclusive)"
                                    )
                                    _lot_sid = None
                                if not lookup_error and _lot_sid is not None:
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: Bambu tray_uid={_tray_uid}"
                                        f" already found as spool {_lot_sid} in Spoolman"
                                        f" (lot_nr match) — skipping auto-create"
                                    )
                                    found_sid = _lot_sid
                                    spoolman_id = found_sid
                                    # Associate the scanned uid with the existing spool.
                                    if uid_hex != "unknown":
                                        reader._ensure_uid_in_spool_extra(uid_hex, _lot_sid)
                                    if write:
                                        is_bambu = fmt == "bambu"
                                        if is_bambu:
                                            gcmd.respond_info(
                                                f"RFID_CHECK_TAG: Bambu tag uid={uid_hex}: "
                                                "spoolman_id cached (cannot write to Bambu tag)"
                                            )
                                        else:
                                            ok = reader._write_spoolman_id_to_tag(
                                                spoolman_id, uid_hex, is_bambu=is_bambu
                                            )
                                            if ok:
                                                gcmd.respond_info(
                                                    f"RFID_CHECK_TAG: wrote"
                                                    f" spoolman_id={spoolman_id}"
                                                    f" to tag uid={uid_hex}"
                                                )
                                            else:
                                                gcmd.respond_info(
                                                    f"RFID_CHECK_TAG: write-back skipped"
                                                    f" or failed for uid={uid_hex}"
                                                )
                            if not lookup_error and found_sid is None:
                                # No existing spool found — proceed to create.
                                if uid_hex != "unknown":
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: uid={uid_hex} not found in Spoolman"
                                        f" — triggering auto-create (CREATE=1)"
                                    )
                                new_sid = reader._auto_create_spool(filament_info, uid_hex=uid_hex if uid_hex != "unknown" else None)
                                if new_sid is not None:
                                    spoolman_id = new_sid
                                    if uid_hex != "unknown":
                                        # Register UID in Spoolman extra field and update local cache.
                                        reader._reassign_uid_to_spool(uid_hex, int(spoolman_id))
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: created spoolman spool id={spoolman_id}"
                                        f" for uid={uid_hex}"
                                    )
                                    if write:
                                        is_bambu = fmt == "bambu"
                                        if is_bambu:
                                            gcmd.respond_info(
                                                f"RFID_CHECK_TAG: Bambu tag uid={uid_hex}: "
                                                "spoolman_id cached (cannot write to Bambu tag)"
                                            )
                                        else:
                                            ok = reader._write_spoolman_id_to_tag(
                                                spoolman_id, uid_hex, is_bambu=is_bambu
                                            )
                                            if ok:
                                                gcmd.respond_info(
                                                    f"RFID_CHECK_TAG: wrote spoolman_id={spoolman_id}"
                                                    f" to tag uid={uid_hex}"
                                                )
                                            else:
                                                gcmd.respond_info(
                                                    f"RFID_CHECK_TAG: write-back skipped or failed"
                                                    f" for uid={uid_hex}"
                                                )
                                else:
                                    gcmd.respond_info(
                                        f"RFID_CHECK_TAG: spool creation failed for uid={uid_hex}"
                                    )
        else:
            gcmd.respond_info(f"RFID_CHECK_TAG: uid={uid_hex} — no filament data parsed")

    def cmd_RFID_WRITE(self, gcmd):
        """RFID_WRITE LANE=<n>|SLOT=<n> SPOOLID=<id>

        Fetch spool info from Spoolman by ID, build an OpenSpool JSON payload,
        and write it to the tag on the specified lane's reader.

        UID guard: if the tag currently on the reader is the active spool on a
        *different* lane, the write is refused to prevent accidentally overwriting
        another lane's tag.
        """
        raw = self._resolve_port_param(gcmd, "RFID_WRITE")
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"RFID_WRITE: no rfid reader is mapped to {port}")
        spool_id = gcmd.get_int("SPOOLID", None)
        if spool_id is None:
            raise gcmd.error("RFID_WRITE requires SPOOLID=<id>")

        reader._debug(f"rfid[{reader.name}]: RFID_WRITE port={port} spool_id={spool_id}")

        # 1. Scan the lane once to detect the tag and get its UID.
        # Use _scan_once directly — we only need the UID, not a full spoolman_id
        # parse, and we want to avoid the timer-based scan window (and its latency)
        # as well as any _pending / auto_create_trigger side effects on blank/unknown tags.
        scan_result = reader._scan_once(port, max_pages=4)
        if scan_result is None or not scan_result.get("uid_hex"):
            gcmd.respond_info(f"RFID_WRITE: no tag detected on {port}")
            return

        uid_hex = scan_result.get("uid_hex") or "unknown"
        gcmd.respond_info(f"RFID_WRITE: detected tag uid={uid_hex} on {port}")

        # 2. UID conflict check: refuse only if this UID is the active spool on a
        #    *different* lane.  A new (uncached) UID, or a stale cache entry that is
        #    no longer assigned to any lane, are both allowed to proceed.
        if uid_hex != "unknown":
            _entry = _UID_SPOOL_CACHE.get(uid_hex)
            cached_sid = _cache_entry_sid(_entry) if _entry is not None else None
            if cached_sid is not None and int(cached_sid) != int(spool_id):
                if reader._is_spool_assigned_elsewhere(port, int(cached_sid)):
                    gcmd.respond_info(
                        f"RFID_WRITE: REFUSED — tag uid={uid_hex} is the active spool "
                        f"(spoolman_id={cached_sid}) on another lane. "
                        "Remove the wrong spool from the reader first."
                    )
                    return

        # 3. Fetch spool data from Spoolman.
        gcmd.respond_info(f"RFID_WRITE: fetching spool id={spool_id} from Spoolman ...")
        spool_data = reader._fetch_spoolman_spool(spool_id)
        if spool_data is None:
            gcmd.respond_info(
                f"RFID_WRITE: could not fetch spool id={spool_id} from Spoolman"
            )
            return

        # 4. Build the OpenSpool JSON payload.
        payload_text = reader._build_openspool_payload(spool_data)
        if payload_text is None:
            gcmd.respond_info(
                f"RFID_WRITE: spool id={spool_id} has no filament/material data"
            )
            return

        reader._debug(f"rfid[{reader.name}]: RFID_WRITE payload={payload_text!r}")

        # 5. Write to the tag — prefer write_tag (auto-detects NTAG vs MIFARE Classic),
        #    fall back to write_ndef_text for older driver versions.
        write_method = getattr(reader.reader, "write_tag", None) or getattr(
            reader.reader, "write_ndef_text", None
        )
        if write_method is None:
            gcmd.respond_info("RFID_WRITE: this reader does not support tag writing")
            return

        gcmd.respond_info("RFID_WRITE: writing OpenSpool payload to tag ...")
        try:
            ok = write_method(payload_text)
        except Exception as exc:
            gcmd.respond_info(f"RFID_WRITE: write error — {exc}")
            return

        if ok:
            # Register UID → spoolman_id in Spoolman extra field and update local cache.
            # This also cleans up any old spool that previously owned this UID.
            if uid_hex != "unknown":
                reader._reassign_uid_to_spool(uid_hex, int(spool_id))
            gcmd.respond_info(
                f"RFID_WRITE: success — wrote spool id={spool_id} "
                f"(OpenSpool) to tag uid={uid_hex}"
            )
        else:
            gcmd.respond_info(
                f"RFID_WRITE: write failed for tag uid={uid_hex} "
                "— check that the tag is writable and within range"
            )

    def cmd_RFID_BAMBU_WRITE(self, gcmd):
        """RFID_BAMBU_WRITE LANE=<n>|SLOT=<n> [TRAY_UID=<32-char hex>]

        Write a Tray UID (spool identifier) to block 9 of a Bambu-compatible
        MIFARE Classic 1K tag using HKDF-derived Key B authentication.

        This command is completely separate from RFID_WRITE — it does not
        touch any existing write path and does not affect non-Bambu tags.

        Block 9 (sector 2, block 1) stores a 32-character ASCII hex string that
        Bambu printers and Spoolman identify as the "Tray UID" / spool identifier.

        Parameters
        ----------
        LANE / SLOT:
            Which lane or slot reader to use.
        TRAY_UID (optional):
            32-character hex string to write as the Tray UID.
            If omitted, a random 16-byte value is generated and used.

        Workflow
        --------
        1. Detect the tag and obtain its hardware UID (4-byte MIFARE anti-coll UID).
        2. Derive Key B from the hardware UID using HKDF-SHA256 with the
           ``RFID-B\\x00`` context (same IKM/salt as Key A reads).
        3. Authenticate sector 2 with Key B and write TRAY_UID to block 9.
        4. Falls back to default Key B (0xFF×6) for blank / factory-default tags.
        5. Print the hardware UID and the written Tray UID for use in Spoolman.

        Example
        -------
        RFID_BAMBU_WRITE LANE=1
        RFID_BAMBU_WRITE LANE=1 TRAY_UID=5F390A603AAB4B8FB1524EA53B16FA77
        """
        raw = self._resolve_port_param(gcmd, "RFID_BAMBU_WRITE")
        reader, port = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"RFID_BAMBU_WRITE: no rfid reader is mapped to {port}")

        tray_uid_param = gcmd.get("TRAY_UID", None)

        # Validate or generate the Tray UID.
        if tray_uid_param is not None:
            tray_uid_param = tray_uid_param.strip().upper()
            if len(tray_uid_param) != 32 or not all(
                c in "0123456789ABCDEF" for c in tray_uid_param
            ):
                raise gcmd.error(
                    "RFID_BAMBU_WRITE: TRAY_UID must be a 32-character hex string "
                    "(e.g. 5F390A603AAB4B8FB1524EA53B16FA77)"
                )
            tray_uid = tray_uid_param
        else:
            # Generate a random 128-bit (16-byte) value — same length as a UUID.
            tray_uid = os.urandom(16).hex().upper()
            gcmd.respond_info(
                f"RFID_BAMBU_WRITE: no TRAY_UID provided — generated {tray_uid}"
            )

        reader._debug(
            f"rfid[{reader.name}]: RFID_BAMBU_WRITE port={port} tray_uid={tray_uid}"
        )

        # Scan to detect the tag and get its hardware UID.
        # Use _scan_once with minimal pages — we only need the UID, not NDEF data.
        scan_result = reader._scan_once(port, max_pages=4)
        if scan_result is None or not scan_result.get("uid_hex"):
            gcmd.respond_info(f"RFID_BAMBU_WRITE: no tag detected on {port}")
            return

        uid_hex = scan_result.get("uid_hex") or "unknown"
        gcmd.respond_info(
            f"RFID_BAMBU_WRITE: detected tag uid={uid_hex} on {port}"
        )

        if uid_hex == "unknown":
            gcmd.respond_info(
                "RFID_BAMBU_WRITE: could not determine tag UID — aborting"
            )
            return

        # Block 9 (sector 2, block 1) = Tray UID.
        # TRAY_UID is a 32-character hex string representing 16 raw bytes,
        # which fit exactly in one MIFARE Classic block.
        try:
            tray_uid_bytes = bytes.fromhex(tray_uid)
        except ValueError:
            gcmd.respond_info(
                "RFID_BAMBU_WRITE: invalid TRAY_UID — expected a 32-character hex string"
            )
            return
        if len(tray_uid_bytes) != 16:
            gcmd.respond_info(
                "RFID_BAMBU_WRITE: invalid TRAY_UID length — expected 16 bytes (32 hex characters)"
            )
            return
        block_data = {9: tray_uid_bytes}

        gcmd.respond_info(
            f"RFID_BAMBU_WRITE: writing Tray UID {tray_uid} to block 9 ..."
        )

        ok = reader._try_bambu_write(uid_hex, block_data)

        if ok:
            gcmd.respond_info(
                f"RFID_BAMBU_WRITE: success — Tray UID written to tag uid={uid_hex}\n"
                f"  Hardware UID (for key derivation) : {uid_hex}\n"
                f"  Tray UID (block 9, Spoolman ID)   : {tray_uid}\n"
                "  Use the Tray UID above as the spool identifier in Spoolman."
            )
        else:
            gcmd.respond_info(
                f"RFID_BAMBU_WRITE: write failed for tag uid={uid_hex}\n"
                "  Check that:\n"
                "    - The tag is a MIFARE Classic 1K within reader range\n"
                "    - pycryptodome is installed (pip3 install pycryptodome)\n"
                "    - The reader supports ISO 14443-A 3-pass authentication"
            )

    def cmd_RFID_ERASE(self, gcmd):
        """Erase the NDEF payload on the tag currently at LANE=<n> and evict its UID from cache."""
        raw = self._resolve_port_param(gcmd, "RFID_ERASE")
        reader, lane = self._find_reader_for_port(raw)
        if reader is None:
            raise gcmd.error(f"RFID_ERASE: no rfid reader is mapped to {lane}")

        # Busy-lane guard: refuse if lane is currently scanning.
        for r in self._all_readers():
            if lane in getattr(r, "_scan_timers", {}):
                raise gcmd.error(f"RFID_ERASE: lane {lane} is currently scanning; cannot erase")
            if lane in getattr(r, "_scan_candidates", {}):
                raise gcmd.error(f"RFID_ERASE: lane {lane} has an active scan candidate; cannot erase")

        reader._debug(f"rfid[{reader.name}]: RFID_ERASE lane={lane}")

        # Get the UID before erasing so we can evict it from the cache.
        uid_hex = None
        try:
            tags = reader.reader.read_all_tags(max_pages=4)
            if tags:
                uid_hex = tags[0].get("uid_hex")
        except Exception:
            pass
        if uid_hex is None:
            try:
                uid_hex = reader._read_uid_hex()
            except Exception:
                pass

        # Erase the tag by writing an empty NDEF text record.
        write_method = getattr(reader.reader, "write_ndef_text", None)
        if write_method is None:
            gcmd.respond_info("RFID_ERASE: driver does not support write_ndef_text")
            return

        try:
            ok = write_method("")
        except Exception as exc:
            gcmd.respond_info(f"RFID_ERASE: write error — {exc}")
            return

        if not ok:
            gcmd.respond_info(
                f"RFID_ERASE: erase failed for lane {lane}"
                " — check that the tag is writable and within range"
            )
            return

        # Remove UID from Spoolman extra field and evict from local cache.
        if uid_hex is not None:
            reader._spoolman_remove_uid(uid_hex)
            gcmd.respond_info(
                f"RFID_ERASE: tag at {lane} erased; UID {uid_hex} removed from Spoolman and cache"
            )
        else:
            gcmd.respond_info(
                f"RFID_ERASE: tag at {lane} erased (UID unknown, Spoolman/cache not changed)"
            )


def load_config_prefix(config):
    return Rfid(config)
