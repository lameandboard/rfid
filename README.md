### Acknowledgements

This project is designed to integrate with and operate alongside existing community tools and hardware, including:
AFC Klipper Plugin with the BTT Vivid

We appreciate the broader open-source community and the ecosystems that make advanced integrations like this possible.

If you believe any part of this project requires additional attribution or clarification, please open an issue and we will review it promptly.

# RFID

![Python](https://img.shields.io/badge/Python-78.6%25-blue?logo=python) ![Shell](https://img.shields.io/badge/Shell-21.4%25-green?logo=gnu-bash) [![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

Adds RFID filament identification to the AFC (Automated Filament Changer) Klipper add-on.
It is currently structured around three Klipper extras:

| File | Role |
|---|---|
| `extras/rfid.py` | AFC integration, lane mapping, scan / pending / commit flow, auto driver detection |
| `extras/mfrc522.py` | MFRC522 reader driver |
| `extras/pn532.py` | PN532 reader driver |

This repo is designed to work with AFC lanes and shared readers, so one physical RFID reader can serve multiple AFC lanes with:

```ini
lanes: 1,2
```

---

## What It Does

- Maps one or more AFC lanes to each RFID reader
- Supports **two reader modules**: MFRC522 and PN532 (NXP)
- **Auto-detects** the connected reader chip at startup (`driver: auto`) — falls back to PN532 if MFRC522 version check fails
- Supports direct scan and two-stage scan flows:
  - `RFID_SCAN`
  - `RFID_SCAN_BEGIN` (alias: `RFID_BEGIN_LOAD`)
  - `RFID_SCAN_COMMIT` (alias: `RFID_COMMIT_LOAD`)
- **Event-driven** scan begin/commit via AFC Klipper events (`afc:lane_prep_start` / `afc:lane_loaded`) — fully reactor-safe, no blocking
- Assigns the scanned spool to the AFC lane with `SET_SPOOL_ID`
- Prints scan results to the console when `messages: True`
- Prints step-by-step scan progress to the console when `debug: True`
- Keeps a managed local copy of `AFC_lane.py` inside this repo and symlinks Klipper to that copy
- Writes a **rotating log** to `~/printer_data/logs/rfid.log` (1 MB, 5 backups)
- **Auto-injects** `[include rfid/*.cfg]` into `printer.cfg`
- **Symlinks** `config/` into `~/printer_data/config/rfid/` — drop new `.cfg` files there to be picked up automatically
- Installs **AFC git hooks** (`post-merge` / `post-checkout`) so `AFC_lane.py` is re-patched whenever AFC updates (only when AFC is installed)

---

## Requirements

- Klipper
- Moonraker
- An MFRC522-compatible **or** PN532-compatible RFID reader wired to a supported MCU SPI bus

**Optional:**
- AFC-Klipper-Add-On — required only for event-driven automatic scanning (`afc:lane_prep_start` / `afc:lane_loaded`). If not installed, all AFC steps are skipped and RFID still works via manual GCode commands (`RFID_SCAN`, `RFID_SCAN_BEGIN`, `RFID_SCAN_COMMIT`).
- Happy Hare (MMU) — if detected at `~/Happy-Hare`, `~/Happy_Hare`, or `~/klipper_mmu`, the installer copies `config/vivid_hh.cfg` instead of `config/vivid.cfg` as the ready-to-use config template.

| Component | Default Path |
|---|---|
| Klipper | `~/klipper` |
| AFC-Klipper-Add-On | `~/AFC-Klipper-Add-On` |
| Happy Hare | `~/Happy-Hare` (or `~/Happy_Hare` / `~/klipper_mmu`) |
| Klipper config | `~/printer_data/config` |
| Moonraker config | `~/printer_data/config/moonraker.conf` |

Moonraker's git repo updater expects a `git_repo` section with `type`, `path`, `origin`, and `primary_branch`, and `managed_services` can be used to restart Klipper after updates. The installer writes that block automatically and non-interactively.

---

## Installation

```bash
cd ~
if [ -d "RFID/.git" ]; then
  git -C RFID pull
else
  git clone https://github.com/ikwidtech/RFID.git RFID
fi
cd RFID
./install.sh
```

### Optional Arguments

| Flag | Description |
|---|---|
| `--klipper-dir /path` | Path to Klipper directory |
| `--afc-dir /path` | Path to AFC-Klipper-Add-On directory |
| `--config-dir /path` | Path to printer_data/config |
| `--moonraker-conf /path` | Path to moonraker.conf |
| `--moonraker-url http://...` | Moonraker URL |
| `--hh-dir /path` | Path to Happy Hare repo (default: `~/Happy-Hare`, falling back to `~/Happy_Hare`) |
| `--update` | Run as an update (non-destructive) |
| `--force` | Force reinstall |
| `-b, --branch BRANCH` | RFID repo branch to install from |
| `--no-restart` | Skip service restart after install |
| `--no-hooks` | Skip installing AFC git hooks |
| `--no-afc` | Skip all AFC integration steps |

---

## What the Installer Does

1. Symlinks `extras/rfid.py`, `extras/mfrc522.py`, and `extras/pn532.py` into `~/klipper/klippy/extras/`.
2. *(Skipped if AFC not installed)* Copies `~/AFC-Klipper-Add-On/extras/AFC_lane.py` into this repo as `extras/AFC_lane.py` and adds a GPL provenance header.
3. *(Skipped if AFC not installed)* Patches that local repo copy to insert two Klipper event hooks:
   - `afc:lane_prep_start` — fires before `prep_load(self)` to trigger async RFID scan
   - `afc:lane_loaded` — fires after spool assignment to commit the pending scan result
4. *(Skipped if AFC not installed)* Symlinks `~/klipper/klippy/extras/AFC_lane.py` to the local managed copy inside this repo.
5. Symlinks `config/` into `~/printer_data/config/rfid/`. If Happy Hare is detected (`~/Happy-Hare`, `~/Happy_Hare`, or `~/klipper_mmu`), `config/vivid_hh.cfg` is used as the ready-to-use example config; otherwise `config/vivid.cfg` is used.
6. Injects `[include rfid/*.cfg]` into `printer.cfg` (after the last existing `[include ...]` line, or at the top if none found).
7. Creates or replaces a Moonraker `[update_manager RFID]` block that points at this repo using the `git_repo` updater format.
8. *(Skipped if AFC not installed)* Installs `post-merge` and `post-checkout` git hooks in the AFC repo so that `AFC_lane.py` is automatically re-patched whenever AFC updates.

That means AFC itself stays untouched in its own repo, and your patched RFID-aware `AFC_lane.py` is owned by this repo.

If AFC updates its own `AFC_lane.py` later, the git hooks handle it automatically — or rerun `install.sh` manually. The installer will rebuild the local managed copy from the current AFC source and patch it again.

---

## Klipper Configuration

Add one `[rfid ...]` section per physical RFID reader. All `.cfg` files placed in `config/` are automatically included via `[include rfid/*.cfg]`.

The `config/vivid.cfg` file shipped in this repo is a ready-to-use example for the BIGTREETECH VIVID board with AFC. For Happy Hare (MMU) setups, use `config/vivid_hh.cfg` — the installer selects the right one automatically based on whether Happy Hare is detected.

### Hardware SPI Example (MFRC522 / PN532 Auto-Detect)

```ini
[rfid mfrc522_0]
spi_bus: spi2_PB2_PB11_PB10
cs_pin: Vivid_1:RFID0_CS
spi_speed: 100000
lanes: 1,2
messages: True
debug: False
scan_delay: 0.05
scan_window: 10.0
driver: auto
max_pages: 135

[rfid mfrc522_1]
spi_bus: spi2_PB2_PB11_PB10
cs_pin: Vivid_1:RFID1_CS
spi_speed: 100000
lanes: 3,4
messages: True
debug: False
scan_delay: 0.05
scan_window: 10.0
driver: auto
max_pages: 135
```

### Software SPI Example

```ini
[rfid mfrc522_0]
sck_pin: PB10
mosi_pin: PB11
miso_pin: PB2
cs_pin: Vivid_1:RFID0_CS
spi_speed: 100000
lanes: 1,2
messages: True
debug: True
driver: auto
max_pages: 135
```

### Options

| Option | Default | Description |
|---|---|---|
| `lanes:` | `1, 2` | Comma-separated AFC lane names handled by that reader |
| `driver:` | `auto` | Reader chip: `auto`, `mfrc522`, or `pn532` |
| `messages:` | `True` | Print user-facing scan/commit messages to the Klipper console |
| `debug:` | `False` | Print detailed step-by-step scan messages to the Klipper console |
| `spi_speed:` | `100000` | SPI speed passed to the reader driver |
| `scan_delay:` | `0.05` | Polling interval in seconds between tag read attempts during the scan window |
| `scan_window:` | `10.0` | Seconds the timer-based scan engine keeps trying before giving up (used for both event-driven and GCode-initiated scans) |
| `max_pages:` | `135` | Number of NTAG/Ultralight pages to read from page 4 onward (range(4, 4+max_pages)); reads stop early once a spool_id is found |
| `event_timeout:` | `60.0` | Seconds before a pending scan result expires |
| `auto_create_spool:` | `False` | Automatically create a Spoolman spool when a tag has filament metadata but no spool ID |
| `auto_write:` | `False` | Write the resolved `spool_id` back to the RFID tag after a successful Spoolman UID lookup or auto-create. Best-effort only — silently skipped if the tag has moved. |
| `spoolman_url:` | `""` | Base URL of the Spoolman API (e.g. `http://localhost:7912`). If not set, auto-detected from Moonraker's `[spoolman]` config. Required for `auto_create_spool` and `RFID_CHECK_TAG CREATE=1`. |

`lanes:` is the preferred option. Single-lane setups may still use a one-item list such as:

```ini
lanes: 1
```

---

## Supported Reader Modules

### MFRC522 (`extras/mfrc522.py`)

- Register-based SPI driver
- Supports MIFARE Classic, NTAG, Ultralight
- NDEF text and URL record parsing
- Reads UID and full tag memory in a single pass via `read_tag_info()`

### PN532 (`extras/pn532.py`)

- Framed SPI command protocol (not register-compatible with MFRC522)
- ISO14443A passive targets — NTAG / Mifare Ultralight-style page reads
- Full NDEF TLV parser with URI prefix table (all standard NFC URI prefixes)
- Reads UID and full tag memory in a single pass via `read_tag_info()`
- Reactor-safe: no `reactor.pause()` in timer/event contexts

### Auto-Detection (`driver: auto`)

When `driver: auto` is set (the default), the RFID module probes the MFRC522 first by reading its version register. If the version is not recognized, it falls back to the PN532 by reading its firmware version. If both fail, it defaults to MFRC522. The selected driver is logged at startup.

---

## Tag Data Expectations

The current code path supports a wide range of tag formats.
In practice, the working path reads tag content, extracts a text payload, and then tries to find a spool identifier from content such as:

- OpenSpool / JSON text containing `spool_id`, `spoolman_id`, `spoolId`, or `id`
- Key/value style text
- URL query parameters
- Plain digits on the tag

The most important part is that the tag resolves to a numeric Spoolman spool ID that can be passed to:

```ini
SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>
```

If your tags currently contain OpenSpool JSON, that is fine.

---

## Multi-Format Tag Parser (`extras/rfid_tag_parser.py`)

The `rfid_tag_parser` module automatically identifies and parses filament metadata from a wide range of spool RFID tag formats. When a tag is scanned and no Spoolman spool ID is embedded, the parser extracts filament information (material, color, temperatures, weight, brand) that can be used to automatically create a spool in Spoolman.

### Supported Formats

| Format | Tag Type | Detection |
|---|---|---|
| **ELEGOO EPC-256** | NTAG213, binary | Header `0x36` + manufacturer `0xEEEEEEEE` |
| **Bambu Lab** | MIFARE Classic 1K (encrypted) | HKDF-derived sector keys from UID; requires `pycryptodome` |
| **OpenSpool** | NTAG215/216, NDEF JSON | `"protocol":"openspool"` field |
| **OpenTag3D** | NTAG213/215/216, NDEF JSON | `application/vnd.opentag3d` or `material`/`color`/`brand` JSON |
| **OpenPrintTag** | NTAG, NDEF CBOR | `application/vnd.openprinttag`; requires `cbor2` (optional) |
| **SimplyPrint URL** | NTAG, NDEF URI | URL containing `simplyprint.io` with query params |
| **Anycubic ACE** | NTAG213/215, binary | Magic bytes `0x7B 0x00` at start of user memory |
| **Creality CFS/K1/K2** | MIFARE Classic 1K | Binary block with date+color+material pattern |
| **QIDI Box** | MIFARE Classic 1K | 3-byte lookup: material (1–50), color (1–24) |
| **Generic NDEF JSON** | Any | NDEF text record with JSON material/color/brand fields |

### Bambu Lab Tag Support

Bambu Lab spools use **MIFARE Classic 1K** tags with **per-UID derived encryption keys** (KDF from Bambu-Research-Group/RFID-Tag-Guide). Your MFRC522 or PN532 reader can read these tags if:

1. **`pycryptodome` is installed** — required for key derivation:
   ```bash
   pip3 install pycryptodome
   ```
2. The tag is within RF range when `RFID_CHECK_TAG` is issued.

**Write-back is not possible** for Bambu tags — they are RSA-signed and read-only. Instead, the `uid → spoolman_id` mapping is stored in the persistent UID cache (`rfid_uid_cache.json`) so subsequent scans resolve the spool ID instantly without re-reading the tag blocks.

If `pycryptodome` is not installed, Bambu tag content decryption is skipped and a log message is emitted indicating that `pycryptodome` is missing. The UID is still readable and can be associated with a Spoolman spool via the UID cache.

### Auto-Create Spool from Tag Data

When `auto_create_spool: True` is configured (or `CREATE=1` is passed to `RFID_CHECK_TAG`), the module will:

1. Parse the tag for filament metadata.
2. Search Spoolman for a matching filament (by material + color + vendor).
3. Create a new Spoolman filament if none found.
4. Create a new Spoolman spool from the filament.
5. Cache `uid → new_spoolman_id` persistently.
6. Attempt to write the spool ID back to the tag (for writable tags).

**Example config:**
```ini
[rfid my_reader]
# ... SPI config ...
lanes: 1, 2
auto_create_spool: True
spoolman_url: http://localhost:7912
```

### Optional Dependencies

| Library | Purpose | Install |
|---|---|---|
| `pycryptodome` | Bambu Lab tag decryption (HKDF key derivation) | `pip3 install pycryptodome` |
| `cbor2` | OpenPrintTag CBOR payload decoding | `pip3 install cbor2` |

Both are optional — the parser falls back gracefully with a log warning if they are not installed.

---

## G-Code Commands

All scan commands accept both `LANE=` (1-based, AFC) and `SLOT=` (0-based, Happy Hare) to identify
the target lane or gate.

| Command | Description |
|---|---|
| `RFID_LANES` | Show which AFC lanes / Happy Hare gates map to which readers |
| `RFID_SLOTS` | Alias of `RFID_LANES` |
| `RFID_PENDING` | Show lanes/gates with a pending scan waiting to be committed |
| `RFID_SCAN [LANE=<lane> \| SLOT=<n>]` | Scan and immediately assign spool to AFC lane or Happy Hare gate |
| `RFID_SLOT_SCAN SLOT=<n>` | Alias of `RFID_SCAN` |
| `RFID_SCAN_BEGIN [LANE=<lane> \| SLOT=<n>] [TIMEOUT=<s>] [MAX_PAGES=<n>]` | Scan and store result as pending; commit with `RFID_SCAN_COMMIT` |
| `RFID_SLOT_SCAN_BEGIN SLOT=<n> [TIMEOUT=<s>] [MAX_PAGES=<n>]` | Alias of `RFID_SCAN_BEGIN` |
| `RFID_BEGIN_LOAD LANE=<lane>` | Alias of `RFID_SCAN_BEGIN` |
| `RFID_SCAN_COMMIT [LANE=<lane> \| SLOT=<n>]` | Commit pending scan to AFC lane or Happy Hare gate |
| `RFID_SLOT_SCAN_COMMIT SLOT=<n>` | Alias of `RFID_SCAN_COMMIT` |
| `RFID_COMMIT_LOAD LANE=<lane>` | Alias of `RFID_SCAN_COMMIT` |
| `RFID_CACHE_CLEAR` | Clear the UID → spoolman_id scan cache |
| `RFID_CACHE_LIST` | List all entries in the UID → spoolman_id scan cache |
| `RFID_CHECK_TAG [LANE=<n>\|SLOT=<n>] [CREATE=0\|1] [WRITE=0\|1]` | Scan tag, look up or create a Spoolman spool, optionally write ID back to tag |
| `RFID_WRITE LANE=<n>\|SLOT=<n> SPOOLID=<id>` | Fetch spool from Spoolman and write it to the tag in OpenSpool format |

`MAX_PAGES` defaults to the `max_pages` config value for the reader (135 if not set).
`TIMEOUT` overrides `scan_window` for that one scan; it defaults to the reader's configured `scan_window`.

On AFC commit, calls `SET_SPOOL_ID LANE=<lane> SPOOL_ID=<id>`.
On Happy Hare commit, issues:

```
MMU_GATE_MAP GATE=<n> SPOOLID=<id>
MMU_SPOOLMAN SPOOLID=<id> GATE=<n> UPDATE=1
```

`MMU_GATE_MAP` assigns the spool to the gate in Happy Hare's local gate map.
`MMU_SPOOLMAN` then pushes the assignment into the Spoolman database.

### Per-Reader Command

```text
RFID_TAG NAME=<reader_name>
```

Performs a single-attempt scan on the named reader and reports UID, raw length, spoolman_id, and tag text.

### RFID_CHECK_TAG

```text
RFID_CHECK_TAG [LANE=<n>|SLOT=<n>] [CREATE=0|1] [WRITE=0|1]
```

Scans the tag on the reader that serves the specified lane, parses its filament metadata, checks for an existing Spoolman spool, and optionally creates one. The lane alone is sufficient to identify the correct reader — no `NAME=` is needed.

| Parameter | Default | Description |
|---|---|---|
| `LANE=` / `SLOT=` | — | Lane (1-based) or slot (0-based) to scan; identifies the reader automatically |
| `CREATE=1` | `auto_create_spool` config | Create a Spoolman spool from tag data if no spool ID is found |
| `WRITE=0\|1` | 0 | Write the new or found spool ID back to the tag (for writable tags); pass `WRITE=1` to enable |

**Behavior:**
1. Scans the tag and reports the UID to the console.
2. Checks the UID cache (`_UID_SPOOL_CACHE`) first — reports `spoolman_id (from cache)` if found.
3. Parses the tag for filament metadata (material, color, brand, temperatures, weight) using all supported formats.
4. Attempts a Bambu Lab MIFARE Classic authenticated read if no text-format data is found.
5. Reports all parsed filament fields.
6. If no spool ID found and `CREATE=1` (or `auto_create_spool: True`):
   - Creates a Spoolman filament + spool via the REST API.
   - Caches `uid → new_spoolman_id` persistently.
   - Writes the spool ID back to the tag if `WRITE=1` and the tag is writable.
   - For Bambu tags (RSA-signed, read-only), skips write and reports `spoolman_id cached`.

---

### RFID_WRITE

```text
RFID_WRITE LANE=<n>|SLOT=<n> SPOOLID=<id>
```

Looks up a spool in Spoolman by its numeric ID, builds an [OpenSpool](https://github.com/spuder/OpenSpool) JSON payload from the filament data, and writes it to the tag currently on the lane's reader.

| Parameter | Description |
|---|---|
| `LANE=` / `SLOT=` | Lane (1-based) or slot (0-based) whose reader should write the tag |
| `SPOOLID=` | Numeric Spoolman spool ID to fetch and encode |

**Supported tag types:**

| Tag type | Write format |
|---|---|
| NTAG / Mifare Ultralight (SAK 0x00) | NDEF Well-Known Text TLV, pages 4+ |
| MIFARE Classic 1K/4K (SAK 0x08/0x18) | UTF-8 JSON bytes in data blocks, sector 1+, default key A |

Tag type is auto-detected from the SAK byte during the write attempt. Bambu Lab tags (MIFARE Classic with RSA-signed sectors) are read-only and cannot be written.

**Behavior:**

1. Scans the reader on the specified lane (waits up to `scan_window` seconds for a tag).
2. **UID guard** — if the detected tag's UID is cached as the *active* spool on a **different lane**, the write is refused to prevent overwriting another lane's tag. New UIDs (never seen before) and stale cache entries are always allowed through.
3. Fetches `GET /api/v1/spool/<id>` from the Spoolman server.
4. Encodes the filament metadata as OpenSpool JSON (protocol, type, color_hex, brand, min/max temps, weight, spoolman_id).
5. Writes the payload to the tag using the appropriate format for the detected tag type.
6. **Caches `uid → spoolman_id`** on success so that subsequent `RFID_SCAN` / `RFID_CHECK_TAG` commands can identify the tag immediately from the UID alone.

**Example:**

```gcode
RFID_WRITE LANE=1 SPOOLID=12
```

This writes spool #12's filament info (e.g. `{"protocol":"openspool","version":"1.0","type":"PLA","color_hex":"FF6600","brand":"Generic","min_temp":200,"max_temp":230,"spoolman_id":12}`) to the tag in lane 1.

---

## Happy Hare (MMU) Support

When `lanes=` contains only numeric values or `lane{n}`-style names, the module automatically maps
them to Happy Hare gates — no extra config is required.
Lanes are 1-based and gates are 0-based, so `lane1` maps to gate `0`, `lane2` to gate `1`, etc.

```ini
[rfid my_reader]
# ... SPI config ...
lanes: 1, 2, 3, 4   # auto-mapped to HH gates 0-3 (and AFC lanes 1-4)
```

If your `lanes=` values are **non-numeric** (e.g. `lanes: extruder, bypass`), the auto-mapping is
disabled and Happy Hare commands will be inactive.  In that case, add an explicit `slots=` line to
enable HH support and use `SLOT=` in G-code:

```ini
[rfid my_reader]
# ... SPI config ...
lanes: extruder, bypass   # AFC lane names
slots: 0, 1               # explicit HH gate mapping (0-based)
```

If you need to map a reader to a different set of gates than its AFC lanes, you can also use an
explicit `slots=` override in the config section.

All G-code commands, including the `RFID_SLOT_*` aliases, are listed in the
[G-Code Commands](#g-code-commands) section above.

---

## Event-Driven Scan Flow

The installer patches `AFC_lane.py` to fire two Klipper events:

1. **`afc:lane_prep_start`** — fired just before AFC begins loading filament. The RFID module responds by starting a reactor timer that keeps scanning until a tag is found or `scan_window` seconds have elapsed (reactor-safe, no `reactor.pause()`).
2. **`afc:lane_loaded`** — fired after the filament is loaded and the spool value is set. The RFID module commits the pending scan result for that lane via `SET_SPOOL_ID`.

This means RFID scanning and spool assignment are fully automatic during normal AFC load operations, with no manual G-code commands required.

### GCode-Initiated Scans Use the Same Engine

`RFID_SCAN`, `RFID_SCAN_BEGIN`, and `RFID_SCAN_COMMIT` all use the same timer-based scan engine as the event-driven path. This means they benefit from the same fast-mode / safe-mode two-read confirmation, candidate TTL aging, and `scan_window` deadline. The deprecated `TRIES` and `DELAY` parameters have been removed — use `TIMEOUT` (overrides `scan_window` for one call) and the config-level `scan_delay` instead.

---

## Moonraker Update Manager Block

The installer writes this style of block into `moonraker.conf`:

```ini
[update_manager RFID]
type: git_repo
channel: dev
path: /absolute/path/to/RFID
origin: https://github.com/ikwidtech/RFID.git
primary_branch: main
managed_services: klipper
info_tags: desc=RFID for AFC / Klipper
```

Those options match Moonraker's documented `git_repo` updater fields for external repos. `primary_branch: main` is important because Moonraker defaults to `master` if it is omitted.

---

## AFC Git Hooks

The installer places `post-merge` and `post-checkout` hooks in `~/AFC-Klipper-Add-On/.git/hooks/`. These hooks automatically re-run `install.sh --no-restart --no-hooks` whenever AFC is updated via `git pull` or a branch checkout, ensuring `AFC_lane.py` stays patched. Activity is logged to `~/printer_data/logs/rfid_hook.log`.

Use `--no-hooks` to skip installing these hooks.

---

## Notes

- `messages: True` is meant for user-facing console output.
- `debug: True` is meant for detailed live trace output while scanning.
- Because `AFC_lane.py` is now managed locally in this repo, you do not need to directly patch the AFC repo by hand.
- If Klipper or Moonraker does not restart automatically after install, restart them manually.
- All RFID activity is also written to `~/printer_data/logs/rfid.log` (rotating, 1 MB max, 5 backups).

---

## Repo Structure

```
extras/
  rfid.py        – main Klipper extra (AFC integration, auto driver selection)
  mfrc522.py     – MFRC522 SPI reader driver
  pn532.py       – PN532 SPI reader driver
config/
  vivid.cfg      – example config for BIGTREETECH VIVID board (AFC setup)
  vivid_hh.cfg   – example config for BIGTREETECH VIVID board (Happy Hare / MMU setup)
install.sh       – installer script
README.md
LICENSE
```

---

## Acknowledgements

This project is designed to integrate with and operate alongside existing community tools and hardware, including:

- [ArmoredTurtle AFC Klipper Add-On](https://github.com/ArmoredTurtle/AFC-Klipper-Add-On) (Automated Filament Changer)
- [BIGTREETECH VIVID](https://github.com/bigtreetech/BIGTREETECH-VIVID)

We appreciate the broader open-source community and the ecosystems that make advanced integrations like this possible.

If you believe any part of this project requires additional attribution or clarification, please open an issue and we will review it promptly.

---

## License

This project is licensed under the **GNU General Public License v3.0**. See [`LICENSE`](LICENSE) for the full text.
