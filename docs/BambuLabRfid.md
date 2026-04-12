# Bambu Lab RFID Tag Format

Reference document for the MIFARE Classic 1K RFID tags used on Bambu Lab
filament spools.  This is the block layout parsed by `rfid_tag_parser.py`
and logged by `rfid.py` after a successful authenticated read.

> **Source:** Reverse-engineered layout documented by the community at
> [Bambu-Research-Group/RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)
> and cross-referenced with
> [queengooborg/Bambu-Lab-RFID-Tag-Guide](https://github.com/queengooborg/Bambu-Lab-RFID-Tag-Guide).

---

## Overview

Bambu Lab spool tags are **MIFARE Classic 1K** chips (ISO/IEC 14443 Type A,
13.56 MHz) with a 4-byte UID and 16 sectors × 4 blocks × 16 bytes = 1 024
bytes of user memory.

Each sector's **Key A** is derived per-UID using HKDF-SHA256:

| HKDF parameter | Value |
|---|---|
| IKM (input key material) | 4-byte tag UID (unique per spool) |
| Salt | Static 16-byte Bambu master key |
| Info / context | `b"RFID-A\x00"` (7 bytes incl. null terminator) |
| Output length | 96 bytes → 16 × 6-byte sector keys |

**Key B is not used for reading.**  Per-sector Key A is sufficient to
authenticate and read all data blocks.  Key B is reserved for write access
(write support is outside the scope of this implementation).

---

## UID Types

| UID seen | Source | Notes |
|---|---|---|
| 4-byte hex (e.g. `62F0E76B`) | MFRC522 REQA/anticollision | Hardware UID — used as HKDF input |
| 32-char hex (e.g. `5F390A603AAB4B8FB1524EA53B16FA77`) | Block 9 of the tag | "Tray UID" — the identifier shown in the Bambu app |

The Tray UID is **not** the same as the MIFARE UID.  It is a 16-byte value
stored in plaintext inside the authenticated sector 2, block 1.

---

## Block Layout

All multi-byte integers are **little-endian** unless noted otherwise.
Sector trailers (blocks 3, 7, 11, 15, …) contain the sector keys and access
bits; they are never included in the parsed block dict.

### Sector 0 (blocks 0–2, trailer 3)

| Block | Byte offset | Field | Type | Notes |
|---|---|---|---|---|
| 0 | 0–15 | Manufacturer / UID block | — | Standard MIFARE block 0; not user data |
| 1 | 0–7 | Material Variant ID | ASCII | e.g. `GFL99` |
| 1 | 8–15 | Material ID | ASCII | e.g. `GFA50` |
| 2 | 0–15 | Basic filament type | ASCII | e.g. `PLA`, `ABS`, `PETG` |

### Sector 1 (blocks 4–6, trailer 7)

| Block | Byte offset | Field | Type | Notes |
|---|---|---|---|---|
| 4 | 0–15 | Detailed filament type | ASCII | e.g. `PLA Basic`, `ABS-GF` |
| 5 | 0–3 | Color RGBA | uint8 × 4 | R, G, B, A bytes |
| 5 | 4–5 | Spool weight | uint16 LE | Grams |
| 5 | 6–7 | *(reserved)* | — | — |
| 5 | 8–11 | Filament diameter | float32 LE | Millimetres (typically 1.75) |
| 6 | 0–1 | Drying temperature | uint16 LE | °C |
| 6 | 2–3 | Drying time | uint16 LE | Hours |
| 6 | 4–5 | Bed temp type | uint16 LE | Not publicly documented |
| 6 | 6–7 | Bed temperature | uint16 LE | °C |
| 6 | 8–9 | Max hotend temperature | uint16 LE | °C |
| 6 | 10–11 | Min hotend temperature | uint16 LE | °C |

### Sector 2 (blocks 8–10, trailer 11)

| Block | Byte offset | Field | Type | Notes |
|---|---|---|---|---|
| 9 | 0–15 | Tray UID | ASCII hex | 32-char hex string identifying the spool |

### Sector 3 (blocks 12–14, trailer 15)

| Block | Byte offset | Field | Type | Notes |
|---|---|---|---|---|
| 12 | 0–15 | Production date | ASCII | Format: `yyyy_MM_dd_HH_mm` |
| 14 | 4–5 | Filament length | uint16 LE | Metres |

### Sector 4 (blocks 16–18, trailer 19)

| Block | Byte offset | Field | Type | Notes |
|---|---|---|---|---|
| 16 | 0–1 | Format ID | uint16 LE | `0x0002` = extra colour data present |
| 16 | 2–3 | Color count | uint16 LE | Number of extra colors |
| 16 | 4–7 | Second color | uint8 × 4 (ABGR) | Alpha, Blue, Green, Red (reversed order) |

Sectors 5–15 are not currently parsed.

---

## Reading Workflow

1. **Detect the tag** — issue REQA; run ISO14443A anticollision to obtain the
   4-byte hardware UID (supports cascade levels 1–3 for 4/7/10-byte UIDs).

2. **Derive sector keys** — feed the UID bytes into HKDF-SHA256 with the
   static Bambu master key as the salt and `b"RFID-A\x00"` as the info/context
   to produce 16 × 6-byte Key A values (one per sector).
   Requires `pycryptodome` (`pip3 install pycryptodome`).

3. **Authenticate all sectors with Key A** — for each sector (0–15), issue
   the MIFARE `AUTHENT1A` command with the derived key for that sector.
   **Do not use Key B for reading.**

4. **Read data blocks** — after a successful sector authentication, read the
   three data blocks (not the trailer) in that sector.

5. **Parse and display** — pass the collected block dict to
   `rfid_tag_parser.parse_tag()`.  On success, call
   `rfid_tag_parser.format_bambu_info()` to produce a complete labeled summary
   of all spool fields.

---

## Hardware Requirements

- Reader must support ISO/IEC 14443 Type A **3-pass authentication**
  (MFRC522, PN532, ACR122U, RC663, Proxmark3 all qualify).
- Standard **pass-through USB HID card readers** do **not** support per-sector
  key authentication and cannot read Bambu tag sector data.
- `pycryptodome` must be installed for HKDF key derivation.

---

## Example Output

When a Bambu tag is successfully scanned, `rfid.py` logs at INFO level:

```
=== Bambu Lab RFID Tag ===
  Tag UID (hardware) : 62F0E76B
  Tray UID           : 5F390A603AAB4B8FB1524EA53B16FA77
  Filament Type      : PLA Basic
  Material           : PLA
  Material ID        : GFA50
  Color              : #FF3700
  Diameter           : 1.75 mm
  Weight             : 1000 g
  Filament Length    : 330 m
  Production Date    : 2024_03_15_10_30
  Drying             : 55 °C x 8 h
  Bed Temperature    : 60 °C
  Hotend Range       : 190-220 °C
```

---

## Key B

Key B is **not needed for reading** Bambu tags.  All sector data is protected
by Key A alone.  Key B would only be required for write operations, which are
not supported (Bambu tags are RSA-2048 signed and effectively read-only).
