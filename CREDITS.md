# Credits & Acknowledgements

This project builds on the work of many talented open-source authors, protocol designers,
and communities. We are grateful for the effort and generosity that goes into each of the
projects listed here.

---

## Core Integration: Klipper & AFC

- **Klipper** — 3D printer firmware at the heart of this integration  
  <https://github.com/Klipper3d/klipper>  
  Licensed under the GNU General Public License v3.0.

- **AFC Klipper Plugin** (Automated Filament Changer)  
  The primary framework this RFID extra integrates with, providing lane management,
  the `SET_SPOOL_ID` G-Code command, and AFC events (`afc:lane_prep_start` /
  `afc:lane_loaded`).  
  <https://github.com/ArmoredTurtle/AFC-Klipper-Add-On>  
  Licensed under the GNU General Public License v3.0.

- **Happy Hare MMU** — Alternative multi-material unit integration; this project
  supports Happy Hare slot/gate commands (`MMU_GATE_MAP`, `MMU_SPOOLMAN`).  
  <https://github.com/moggieuk/Happy-Hare>  
  Licensed under the GNU General Public License v3.0.

---

## Filament Database & Spool Management

- **Spoolman** — Filament spool management service whose HTTP API is used for spool
  lookup, auto-creation, and UID extra-field storage  
  <https://github.com/Donkie/Spoolman>  
  Licensed under the MIT License.

---

## RFID Tag Formats & Protocols

### OpenSpool
OpenSpool defines the open NDEF JSON filament-tagging format that this project reads and
writes.  
<https://github.com/spuder/OpenSpool>  
Licensed under the MIT License.

### Bambu Lab Tag Key Derivation
The HKDF-SHA256 sector-key derivation algorithm for Bambu Lab MIFARE Classic spool tags
was reverse-engineered and documented by the **MrBambuSpoolPal** project.  
Reference implementation (GPLv3):  
<https://github.com/MrBambuSpoolPal/MrBambuSpoolPal-BambuSpoolPal_AndroidApp/blob/c8aa59e6d4c132f9e78bde24d791bbb330a12b7d/source/app/src/main/java/app/mrb/bambuspoolpal/nfc/NfcTagProcessor.kt#L53-L139>

> **GPLv3 attribution notice:** No source code was copied from the above file.
> The HKDF algorithm and key-derivation parameters (IKM = 4-byte tag UID,
> Salt = Bambu master key, Info = `b"RFID-A\x00"` / `b"RFID-B\x00"`) were
> reimplemented independently in Python based solely on the published procedure.
> In accordance with the spirit of the GPL, the original work is explicitly
> credited here and in the relevant source-file docstring
> (`extras/rfid_tag_parser.py`).

### OpenTag3D
NDEF MIME `application/vnd.opentag3d` filament tag format.  
<https://github.com/OpenTag3D/opentag3d>

### OpenPrintTag
NDEF MIME `application/vnd.openprinttag` CBOR filament tag format.  
<https://github.com/OpenPrintTag/OpenPrintTag>

### SimplyPrint
URL-based NDEF tag format carrying filament metadata as query parameters on a
`simplyprint.io` URL.  
<https://simplyprint.io>

### ELEGOO, Bambu Lab, Anycubic ACE, Creality CFS, QIDI Box
Proprietary tag formats created by these manufacturers are parsed in a read-only,
interoperability capacity.  All trademarks are the property of their respective owners.
Parser code was written from publicly available documentation, community reverse-engineering,
and observed tag behaviour — no proprietary firmware or code was used.

---

## Hardware Driver References

- **MFRC522** — NXP MFRC522 RFID reader/writer chip  
  <https://www.nxp.com/products/rfid-nfc/nfc-hf/nfc-readers/standard-performance-mifare-and-ntag-frontend:MFRC522>  
  Driver (`extras/mfrc522.py`) is an original implementation for Klipper.

- **PN532** — NXP PN532 NFC controller  
  <https://www.nxp.com/products/rfid-nfc/nfc-hf/nfc-readers/nfc-integrated-solution:PN5321A3HN>  
  Driver (`extras/pn532.py`) is an original implementation for Klipper.

---

## Optional Dependencies

These packages are not bundled with this project and must be installed separately.
They are used only for specific optional features.

| Package | Purpose | License | Homepage |
|---|---|---|---|
| **pycryptodome** | HKDF-SHA256 key derivation for Bambu Lab tags | BSD 2-Clause | <https://www.pycryptodome.org/> |
| **pycryptodomex** | Alternate install of pycryptodome (Cryptodome namespace) | BSD 2-Clause | <https://www.pycryptodome.org/> |
| **cbor2** | CBOR payload decoding for OpenPrintTag | MIT | <https://github.com/agronholm/cbor2> |

---

## License & GPL Compliance

This project is distributed under the **GNU General Public License, version 3 or later**
(see [LICENSE](LICENSE) for the full text — <https://www.gnu.org/licenses/gpl-3.0.html>).

All source files contain the standard GPLv3 header. Where algorithms or concepts were
derived from other GPLv3 works, that attribution is noted explicitly — both in the
relevant source-file docstrings and in this file.

Integrated third-party libraries are used as ordinary dependencies and are not bundled;
their respective licenses apply independently.

---

## Missing Attributions

Open-source attribution is an ongoing responsibility. If you believe any project,
author, or contribution has not been properly credited, **please open an issue** and
we will review and correct it promptly:  
<https://github.com/lameandboard/rfid/issues>

Thank you to every open-source author, protocol designer, and community contributor
whose work makes this project possible. 🙏
