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
# Copyright (c) 2026 ikwidtech

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Iterable, Optional


class PN532Error(Exception):
    pass


class PN532Timeout(PN532Error):
    pass


class PN532ProtocolError(PN532Error):
    pass


class PN532Device:
    # --- SPI framing bytes ---
    _SPI_STATREAD = 0x02
    _SPI_DATAWRITE = 0x01
    _SPI_DATAREAD = 0x03
    _SPI_READY = 0x01

    # --- PN532 frame constants ---
    PREAMBLE = 0x00
    STARTCODE1 = 0x00
    STARTCODE2 = 0xFF
    POSTAMBLE = 0x00
    HOST_TO_PN532 = 0xD4
    PN532_TO_HOST = 0xD5
    ACK_FRAME = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])

    # --- Core commands ---
    CMD_GET_FIRMWARE_VERSION = 0x02
    CMD_SAM_CONFIGURATION = 0x14
    CMD_INLIST_PASSIVE_TARGET = 0x4A
    CMD_INDATA_EXCHANGE = 0x40
    CMD_INRELEASE = 0x52

    # --- Tag-level commands (ISO14443A / Type 2) ---
    TAG_CMD_READ = 0x30   # Mifare Ultralight/NTAG: read 4 pages starting at page address
    TAG_CMD_WRITE = 0xA2  # NTAG/Ultralight: write 1 page (4 bytes) at page address
    TAG_CMD_HALT = 0x50   # ISO14443A HALT — silences the tag until a WUPA is received

    # --- MIFARE Classic auth commands (via InDataExchange) ---
    MIFARE_AUTH_KEY_A = 0x60
    MIFARE_AUTH_KEY_B = 0x61
    # Note: MIFARE Classic block reads use the same READ opcode (0x30) as NTAG/Ultralight.
    # Use TAG_CMD_READ for both tag types.

    URI_PREFIXES = {
        0x00: "",
        0x01: "http://www.",
        0x02: "https://www.",
        0x03: "http://",
        0x04: "https://",
        0x05: "tel:",
        0x06: "mailto:",
        0x07: "ftp://anonymous:anonymous@",
        0x08: "ftp://ftp.",
        0x09: "ftps://",
        0x0A: "sftp://",
        0x0B: "smb://",
        0x0C: "nfs://",
        0x0D: "ftp://",
        0x0E: "dav://",
        0x0F: "news:",
        0x10: "telnet://",
        0x11: "imap:",
        0x12: "rtsp://",
        0x13: "urn:",
        0x14: "pop:",
        0x15: "sip:",
        0x16: "sips:",
        0x17: "tftp:",
        0x18: "btspp://",
        0x19: "btl2cap://",
        0x1A: "btgoep://",
        0x1B: "tcpobex://",
        0x1C: "irdaobex://",
        0x1D: "file://",
        0x1E: "urn:epc:id:",
        0x1F: "urn:epc:tag:",
        0x20: "urn:epc:pat:",
        0x21: "urn:epc:raw:",
        0x22: "urn:epc:",
        0x23: "urn:nfc:",
    }

    # Minimum sleep between SPI status polls in _wait_ready (standalone mode).
    # Yields the GIL and reduces bus hammering without adding meaningful latency.
    _SPI_POLL_INTERVAL = 0.0005  # 500 µs

    def __init__(self, spi, reactor=None):
        self.spi = spi
        self._reactor = reactor
        self._initialized = False
        # Reduced timeouts keep worst-case reactor blocking well under the
        # 50 ms MCU watchdog threshold.  The PN532 SPI datasheet specifies
        # the device asserts ready within ~1 ms; 15 ms / 100 ms are generous.
        self._ready_timeout = 0.015
        self._command_timeout = 0.100

    def set_reactor(self, reactor) -> None:
        self._reactor = reactor

    def _now(self) -> float:
        if self._reactor is not None:
            return self._reactor.monotonic()
        return time.monotonic()

    def _wait_time(self, seconds: float) -> None:
        """Blocking sleep for standalone/test use only.

        Must NOT be called when running inside the Klipper reactor (i.e. when
        self._reactor is not None).  _wait_ready() already guards every call
        with ``if self._reactor is None`` so this is never reached in normal
        Klipper operation.  The guard here is a hard safety net.
        """
        if self._reactor is not None:
            raise RuntimeError(
                "PN532Device._wait_time() must not be called in Klipper reactor context; "
                "use non-blocking polling (busy-wait with self._now() deadline) instead."
            )
        time.sleep(seconds)

    # -------------------------------------------------------------------------
    # Low-level SPI helpers
    # -------------------------------------------------------------------------

    def _spi_write(self, payload: Iterable[int]) -> None:
        self.spi.spi_send([self._SPI_DATAWRITE] + [int(x) & 0xFF for x in payload])

    def _spi_read(self, count: int) -> bytes:
        ret = self.spi.spi_transfer([self._SPI_DATAREAD] + ([0x00] * count))
        if isinstance(ret, dict) and "response" in ret:
            data = bytearray(ret["response"])
        else:
            data = bytearray(ret)
        # first byte is the command echo slot
        return bytes(data[1:1 + count])

    def _spi_status(self) -> int:
        ret = self.spi.spi_transfer([self._SPI_STATREAD, 0x00])
        if isinstance(ret, dict) and "response" in ret:
            data = bytearray(ret["response"])
        else:
            data = bytearray(ret)
        if len(data) < 2:
            return 0x00
        return data[1] & 0xFF

    def _wait_ready(self, timeout: Optional[float] = None) -> None:
        """Poll SPI status until PN532 signals ready or the deadline is reached.

        A 500 µs sleep is inserted between polls in standalone/test mode
        (``_reactor is None``) to yield the GIL and reduce SPI bus hammering.
        When running inside the Klipper reactor the reduced default timeouts
        (15 ms ready / 100 ms command) keep worst-case blocking well under the
        50 ms MCU watchdog threshold.  ``reactor.pause()`` must never be called
        from a reactor timer callback.
        """
        deadline = self._now() + (self._ready_timeout if timeout is None else timeout)
        while self._now() < deadline:
            if self._spi_status() == self._SPI_READY:
                return
            if self._reactor is None:
                self._wait_time(self._SPI_POLL_INTERVAL)
        raise PN532Timeout("Timed out waiting for PN532 ready status")

    # -------------------------------------------------------------------------
    # Frame encode/decode
    # -------------------------------------------------------------------------

    def _build_frame(self, data: Iterable[int]) -> bytes:
        body = bytes(data)
        length = len(body) & 0xFF
        lcs = (-length) & 0xFF
        dcs = (-sum(body)) & 0xFF
        return bytes([
            self.PREAMBLE,
            self.STARTCODE1,
            self.STARTCODE2,
            length,
            lcs,
        ]) + body + bytes([dcs, self.POSTAMBLE])

    def _write_frame(self, body: Iterable[int]) -> None:
        frame = self._build_frame(body)
        self._spi_write(frame)

    def _read_exact_frame(self, timeout: Optional[float] = None) -> bytes:
        self._wait_ready(timeout=timeout)
        # read a generous amount; parse frame out of it
        raw = self._spi_read(64)

        # find start sequence 00 00 FF
        idx = raw.find(bytes([0x00, 0x00, 0xFF]))
        if idx < 0:
            raise PN532ProtocolError(f"PN532 response missing frame header: {raw.hex()}")

        if len(raw) < idx + 6:
            raise PN532ProtocolError("PN532 response too short for header")

        length = raw[idx + 3]
        lcs = raw[idx + 4]
        if ((length + lcs) & 0xFF) != 0:
            raise PN532ProtocolError("PN532 invalid LEN/LCS")

        frame_end = idx + 5 + length + 2  # data + dcs + postamble
        if len(raw) < frame_end:
            raise PN532ProtocolError("PN532 truncated frame")

        data = raw[idx + 5: idx + 5 + length]
        dcs = raw[idx + 5 + length]
        if ((sum(data) + dcs) & 0xFF) != 0:
            raise PN532ProtocolError("PN532 invalid DCS")

        return bytes(data)

    def _read_ack(self, timeout: Optional[float] = None) -> None:
        self._wait_ready(timeout=timeout)
        raw = self._spi_read(16)
        idx = raw.find(self.ACK_FRAME)
        if idx < 0:
            raise PN532ProtocolError(f"PN532 missing ACK frame: {raw.hex()}")

    def _command(self, cmd: int, params: Iterable[int] = (), timeout: Optional[float] = None) -> bytes:
        body = bytes([self.HOST_TO_PN532, cmd]) + bytes(int(x) & 0xFF for x in params)
        self._write_frame(body)
        self._read_ack(timeout=timeout)

        resp = self._read_exact_frame(timeout=timeout or self._command_timeout)
        if len(resp) < 2:
            raise PN532ProtocolError("PN532 response body too short")
        if resp[0] != self.PN532_TO_HOST:
            raise PN532ProtocolError(f"Unexpected response direction byte: 0x{resp[0]:02X}")
        if resp[1] != ((cmd + 1) & 0xFF):
            raise PN532ProtocolError(
                f"Unexpected response command byte: got 0x{resp[1]:02X}, expected 0x{(cmd + 1) & 0xFF:02X}"
            )
        return resp[2:]

    # -------------------------------------------------------------------------
    # Device init / state
    # -------------------------------------------------------------------------

    def wakeup(self) -> None:
        # PN532 over SPI often wakes on CS toggle + dummy bytes.
        # The datasheet suggests a short settling delay after wakeup, but we
        # must not call reactor.pause() here (unsafe in reactor timer context).
        # The subsequent SAM configuration command provides enough implicit time.
        self.spi.spi_send([0x00] * 16)

    def initialize(self) -> None:
        if self._initialized:
            return
        self.wakeup()
        self.sam_configuration()
        self._initialized = True

    def sam_configuration(self) -> None:
        # Normal mode, timeout 0x14 (~50 ms unit in many examples), use IRQ disabled
        self._command(self.CMD_SAM_CONFIGURATION, [0x01, 0x14, 0x00], timeout=1.0)

    def get_firmware_version(self) -> dict:
        self.initialize()
        resp = self._command(self.CMD_GET_FIRMWARE_VERSION, timeout=1.0)
        if len(resp) < 4:
            raise PN532ProtocolError("Firmware version response too short")
        return {
            "ic": resp[0],
            "ver": resp[1],
            "rev": resp[2],
            "support": resp[3],
        }

    # -------------------------------------------------------------------------
    # ISO14443A passive target
    # -------------------------------------------------------------------------

    def list_passive_target(self, baud: int = 0x00, wupa: bool = False) -> Optional[dict]:
        """
        InListPassiveTarget:
          MaxTg = 1
          BrTy = 0x00 -> 106 kbps type A

        When *wupa* is True, InitiatorData=0x52 is appended so the PN532 sends
        WUPA instead of REQA.  This wakes tags that are in READY* state (left
        there by a preceding read_all_tags() call) or HALT state.
        """
        self.initialize()
        cmd_data = [0x01, baud]
        if wupa:
            cmd_data.append(0x52)
        resp = self._command(self.CMD_INLIST_PASSIVE_TARGET, cmd_data, timeout=1.0)
        if not resp:
            return None

        nb_tg = resp[0]
        if nb_tg < 1:
            return None

        # Response format for one target (Type A):
        # [NbTg, Tg, SENS_RES(2), SEL_RES, NFCIDLen, NFCID...]
        if len(resp) < 7:
            raise PN532ProtocolError("Passive target response too short")

        tg = resp[1]
        sens_res = resp[2:4]
        sel_res = resp[4]
        uid_len = resp[5]
        if len(resp) < 6 + uid_len:
            raise PN532ProtocolError("Passive target UID truncated")
        uid = bytes(resp[6:6 + uid_len])

        return {
            "target": tg,
            "sens_res": bytes(sens_res),
            "sel_res": sel_res,
            "uid": uid,
        }

    def read_uid(self) -> Optional[bytes]:
        target = self.list_passive_target()
        if not target:
            return None
        return target["uid"]

    # -------------------------------------------------------------------------
    # Data exchange with selected target
    # -------------------------------------------------------------------------

    def in_data_exchange(self, target_no: int, data: Iterable[int], timeout: Optional[float] = None) -> bytes:
        resp = self._command(
            self.CMD_INDATA_EXCHANGE,
            [target_no] + [int(x) & 0xFF for x in data],
            timeout=timeout or self._command_timeout,
        )
        if not resp:
            raise PN532ProtocolError("InDataExchange response empty")

        status = resp[0]
        if status != 0x00:
            raise PN532Error(f"PN532 InDataExchange failed with status 0x{status:02X}")
        return bytes(resp[1:])

    # -------------------------------------------------------------------------
    # Tag reads (Type 2 / NTAG / Ultralight-oriented)
    # -------------------------------------------------------------------------

    def read_pages(self, start_page: int) -> bytes:
        """
        READ command returns 16 bytes = 4 pages starting at page address.
        """
        target = self.list_passive_target()
        if not target:
            raise PN532Error("No passive target found")
        return self.in_data_exchange(target["target"], [self.TAG_CMD_READ, start_page & 0xFF], timeout=1.0)

    def read_tag_memory(self, start_page: int = 44, max_pages: int = 135) -> bytes:
        """
        Reads tag pages in chunks of 4 pages using READ command.
        Stops on communication failure.
        """
        memory = bytearray()
        page = int(start_page)
        end_page = start_page + max_pages
        while page < end_page:
            try:
                chunk = self.read_pages(page)
            except Exception:
                break
            if not chunk:
                break
            memory.extend(chunk)
            page += 4
            # No sleep/pause between pages: the PN532 command round-trip
            # provides implicit pacing.  reactor.pause() must not be called
            # from reactor timer or event-handler contexts.
        return bytes(memory)

    # -------------------------------------------------------------------------
    # Simple NDEF extraction
    # -------------------------------------------------------------------------

    def _find_ndef_tlv(self, data: bytes) -> Optional[bytes]:
        """
        Looks for Type 2 Tag TLV 0x03 (NDEF message).
        """
        i = 0
        while i < len(data):
            t = data[i]
            if t == 0x00:  # NULL TLV
                i += 1
                continue
            if t == 0xFE:  # Terminator TLV
                return None
            if i + 1 >= len(data):
                return None
            l = data[i + 1]
            if l == 0xFF:
                if i + 3 >= len(data):
                    return None
                l = (data[i + 2] << 8) | data[i + 3]
                vstart = i + 4
            else:
                vstart = i + 2
            vend = vstart + l
            if vend > len(data):
                return None
            if t == 0x03:
                return data[vstart:vend]
            i = vend
        return None

    def _decode_ndef_message(self, ndef: bytes) -> Optional[str]:
        if not ndef:
            return None

        # Basic single-record parser
        # Header | TypeLen | PayloadLen | [IdLen] | Type | [ID] | Payload
        try:
            idx = 0
            header = ndef[idx]
            idx += 1

            sr = bool(header & 0x10)
            il = bool(header & 0x08)

            type_len = ndef[idx]
            idx += 1

            if sr:
                payload_len = ndef[idx]
                idx += 1
            else:
                payload_len = int.from_bytes(ndef[idx:idx + 4], "big")
                idx += 4

            id_len = 0
            if il:
                id_len = ndef[idx]
                idx += 1

            rec_type = ndef[idx:idx + type_len]
            idx += type_len

            if il:
                idx += id_len

            payload = ndef[idx:idx + payload_len]

            if rec_type == b"T" and len(payload) >= 1:
                status = payload[0]
                lang_len = status & 0x3F
                text_bytes = payload[1 + lang_len:]
                return text_bytes.decode("utf-8", errors="ignore").strip()

            if rec_type == b"U" and len(payload) >= 1:
                prefix = self.URI_PREFIXES.get(payload[0], "")
                uri = payload[1:].decode("utf-8", errors="ignore").strip()
                return prefix + uri

            # Fallback: try generic text-ish decode
            if payload:
                return payload.decode("utf-8", errors="ignore").strip()

        except Exception:
            return None

        return None

    def read_ndef_text(self, max_pages: int = 135) -> Optional[str]:
        info = self.read_tag_info(max_pages=max_pages)
        if info is None:
            return None
        return info.get("tag_text") or None

    def in_release(self, target_no: int) -> None:
        """Send InRelease to free a target slot in the PN532.

        This must be called after halting a tag so the PN532 forgets about it
        and the next InListPassiveTarget can discover a different tag.
        Errors are silently ignored.
        """
        try:
            self._command(self.CMD_INRELEASE, [target_no & 0xFF], timeout=self._command_timeout)
        except Exception:
            pass

    def halt_tag(self, target_no: int) -> None:
        """Send ISO14443A HALT to the selected tag, then release the PN532 target slot.

        A halted tag ignores future REQA broadcasts and only wakes on WUPA.
        Both the data-exchange and the release errors are silently ignored:
        a halted tag stops responding (causing an exchange error), which is expected.
        """
        try:
            self.in_data_exchange(target_no, [self.TAG_CMD_HALT, 0x00], timeout=self._command_timeout)
        except Exception:
            pass
        self.in_release(target_no)

    # ---------- MIFARE Classic authenticated reads ----------

    def _auth_mifare_block(
        self,
        target_no: int,
        block_addr: int,
        key: bytes,
        uid: bytes,
        use_key_b: bool = False,
    ) -> bool:
        """Authenticate a MIFARE Classic sector via PN532 InDataExchange.

        PN532 auth frame: [auth_cmd, block_addr, key[0..5], uid[0..3|0..6]]
        Returns True on success, False on any error or NACK status.
        """
        auth_cmd = self.MIFARE_AUTH_KEY_B if use_key_b else self.MIFARE_AUTH_KEY_A
        frame = [auth_cmd, block_addr & 0xFF] + list(key[:6]) + list(uid[:4])
        try:
            self.in_data_exchange(target_no, frame, timeout=0.5)
            return True
        except Exception:
            return False

    def _read_mifare_block(self, target_no: int, block_addr: int) -> Optional[bytes]:
        """Read a single 16-byte MIFARE Classic block (must already be authenticated)."""
        try:
            data = self.in_data_exchange(
                target_no, [self.TAG_CMD_READ, block_addr & 0xFF], timeout=0.5
            )
            if data and len(data) >= 16:
                return bytes(data[:16])
        except Exception:
            pass
        return None

    def read_authenticated_blocks(
        self,
        target_no: int,
        uid: bytes,
        key_list: list,
        num_sectors: int = 16,
        use_key_b: bool = False,
    ) -> dict:
        """Authenticate and read data blocks for all sectors of a MIFARE Classic tag.

        Parameters
        ----------
        target_no : PN532 target number returned by list_passive_target().
        uid       : Tag UID bytes.
        key_list  : List of up to 16 × 6-byte sector keys.
        num_sectors: Number of sectors to process (1 K tag = 16).
        use_key_b : Use MIFARE_AUTH_KEY_B instead of MIFARE_AUTH_KEY_A.

        Returns
        -------
        dict mapping absolute block index → 16-byte data bytes (or None on read error).
        Sector trailer blocks (every 4th block starting at block 3) are excluded.
        """
        result: dict = {}
        for sector in range(num_sectors):
            key = key_list[sector] if sector < len(key_list) else bytes(6)
            trailer_block = sector * 4 + 3
            if not self._auth_mifare_block(target_no, trailer_block, key, uid, use_key_b):
                # Pre-fill None for the three data blocks so callers get a consistent index map
                for blk_in_sector in range(3):
                    result[sector * 4 + blk_in_sector] = None
                continue
            for blk_in_sector in range(3):
                abs_block = sector * 4 + blk_in_sector
                result[abs_block] = self._read_mifare_block(target_no, abs_block)
        return result

    def read_mifare_classic_tag(self, key_list: list, num_sectors: int = 16) -> Optional[dict]:
        """Full MIFARE Classic 1K read: list passive target → auth+read all sectors.

        Returns a dict suitable for rfid_tag_parser.parse_tag():
          {"uid_bytes": bytes, "uid_hex": str, "blocks": {abs_block: bytes}}
        Returns None if no tag is present.
        """
        self.initialize()
        target = self.list_passive_target()
        if target is None:
            return None
        target_no = target["target"]
        uid = target["uid"]
        uid_hex = self.uid_hex(uid)
        blocks = self.read_authenticated_blocks(target_no, uid, key_list, num_sectors=num_sectors)
        self.halt_tag(target_no)
        return {
            "uid_bytes": bytes(uid),
            "uid_hex": uid_hex,
            "blocks": blocks,
        }

    def read_all_tags(self, max_pages: int = 135, max_tags: int = 4) -> list[dict]:
        """Enumerate ISO14443A tags in the RF field and return a list of tag-info dicts.

        Algorithm:
          1. Pass 0: list_passive_target(wupa=True) → wake any tags left in HALT
             from a previous scan window (halted tags ignore REQA but respond to WUPA).
          2. Passes 1+: list_passive_target() (REQA) → find remaining IDLE tags.
          3. Read its pages (UID + NDEF).
          4. HALT + InRelease → silence this tag so the next pass finds a different one.
          5. Repeat until no more tags respond or max_tags is reached.

        Returns a list of tag-info dicts (same schema as read_tag_info()).
        Returns [] if no tags are found.
        """
        self.initialize()
        tags = []
        target_no = None
        try:
            for pass_idx in range(max_tags):
                # Pass 0: WUPA wakes tags left in HALT from a previous scan window.
                # Passes 1+: REQA finds only IDLE tags (tags halted in earlier passes
                # stay silent, preventing duplicates without a separate discovery phase).
                target = self.list_passive_target(wupa=(pass_idx == 0))
                if target is None:
                    break

                target_no = target["target"]
                uid = target["uid"]
                uid_hex = self.uid_hex(uid)
                raw = self._read_tag_pages_for_target(
                    target_no=target_no,
                    start_page=4,
                    max_pages=max_pages,
                    timeout=1.0,
                )
                ndef = self._find_ndef_tlv(raw) if raw else None
                text = self._decode_ndef_message(ndef) if ndef else None
                if text is None and raw:
                    text = raw.decode("utf-8", errors="ignore").strip("\x00") or None
                tags.append({
                    "uid": uid,
                    "uid_hex": uid_hex,
                    "raw_bytes": raw,
                    "raw_len": len(raw) if raw else 0,
                    "spoolman_id": self._extract_spoolman_id(text),
                    "tag_text": text or "",
                })

                # HALT this tag so the next list_passive_target won't see it
                self.halt_tag(target_no)
                target_no = None
        finally:
            # Release any still-active target slot
            if target_no is not None:
                try:
                    self.in_release(target_no)
                except Exception:
                    pass

        return tags

    # -------------------------------------------------------------------------
    # Higher-level helpers
    # -------------------------------------------------------------------------

    def uid_hex(self, uid: Optional[bytes]) -> Optional[str]:
        if uid is None:
            return None
        return "".join(f"{b:02X}" for b in uid)

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
        for pat in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                try:
                    return int(m.group(1))
                except Exception:
                    pass
        if re.fullmatch(r"\d+", text):
            return int(text)
        return None

    def _read_tag_pages_for_target(
        self,
        target_no: int,
        start_page: int,
        max_pages: int,
        timeout: float = 1.0,
    ) -> bytes:
        """Read up to max_pages pages starting at start_page for an existing target.

        This helper encapsulates the common paging/stop logic so that both
        read_tag_info() and other callers can share the same behavior.

        Early-exit: reading stops as soon as enough data has been accumulated
        to successfully parse a spool_id, so the tag doesn't have to remain
        in RF range for the full max_pages read (important during AFC load).
        """
        memory = bytearray()
        end_page = start_page + max_pages
        page = start_page
        while page < end_page:
            try:
                chunk = self.in_data_exchange(
                    target_no, [self.TAG_CMD_READ, page & 0xFF], timeout=timeout
                )
            except Exception:
                break
            if not chunk:
                break
            memory.extend(chunk)
            # Early-exit: only break when we have a complete NDEF message with a
            # valid spool_id.  Avoid bytes(memory) copy — _find_ndef_tlv works on
            # bytearray directly.  Do NOT fall back to raw UTF-8: the read may
            # still be partial and a truncated numeric value could match a spool_id.
            _ndef = self._find_ndef_tlv(memory)
            _text = self._decode_ndef_message(_ndef) if _ndef else None
            if _text is not None and self._extract_spoolman_id(_text) is not None:
                break  # got what we need — stop reading
            page += 4
        return bytes(memory)

    def read_tag_info(self, max_pages: int = 135) -> Optional[dict]:
        self.initialize()
        target = self.list_passive_target()
        if target is None:
            return None
        uid = target["uid"]
        uid_hex = self.uid_hex(uid)
        # Read pages using the already-selected target number to avoid a
        # second InListPassiveTarget call (double-activation bug).
        # NTAG21x user memory starts at page 4 (pages 0-3 are reserved).
        raw = self._read_tag_pages_for_target(
            target_no=target["target"],
            start_page=4,
            max_pages=max_pages,
            timeout=1.0,
        )
        ndef = self._find_ndef_tlv(raw) if raw else None
        text = self._decode_ndef_message(ndef) if ndef else None
        if text is None and raw:
            text = raw.decode("utf-8", errors="ignore").strip("\x00") or None
        raw_len = len(raw)
        return {
            "uid": uid,
            "uid_hex": uid_hex,
            "raw_bytes": raw,
            "tag_text": text or "",
            "raw_len": raw_len,
            "spoolman_id": self._extract_spoolman_id(text),
        }

    # ---------- NTAG / Type 2 write ----------

    def _write_type2_ndef(self, target_no: int, text: str) -> bool:
        """Write an NDEF text TLV to an already-targeted Type 2 / NTAG tag.

        The caller is responsible for calling list_passive_target first.
        """
        tlv = _encode_ndef_text_tlv(text)
        num_pages = (len(tlv) + 3) // 4
        current_page = 4
        try:
            for i in range(num_pages):
                current_page = 4 + i
                chunk = tlv[i * 4:(i + 1) * 4]
                self.in_data_exchange(
                    target_no,
                    [self.TAG_CMD_WRITE, current_page & 0xFF] + list(chunk),
                    timeout=1.0,
                )
            return True
        except Exception as exc:
            self._dbg("pn532._write_type2_ndef error at page=%d: %s" % (current_page, exc))
            return False

    def _write_mifare_block(self, target_no: int, block_addr: int, data: bytes) -> bool:
        """Write 16 bytes to a MIFARE Classic block via the 2-phase WRITE (0xA0) protocol.

        MIFARE WRITE is a two-phase ISO14443-3 exchange:
          Phase 1 — send [0xA0, block_addr]; tag returns ACK.
          Phase 2 — send 16 data bytes; tag returns ACK.
        Each phase is one InDataExchange call; the PN532 handles the low-level ACK.
        The sector must already be authenticated.  Returns True on success.
        """
        try:
            self.in_data_exchange(target_no, [0xA0, block_addr & 0xFF], timeout=1.0)
            self.in_data_exchange(
                target_no, list((data + b"\x00" * 16)[:16]), timeout=1.0
            )
            return True
        except Exception as exc:
            self._dbg(
                "pn532._write_mifare_block block=%d error: %s" % (block_addr, exc)
            )
            return False

    def _write_mifare_classic_json(
        self, target_no: int, uid: bytes, text: str
    ) -> bool:
        """Write JSON *text* to MIFARE Classic data blocks using the default key A (0xFF×6).

        Writes starting at sector 1 (absolute block 4), using 3 data blocks per sector
        (48 bytes).  Each sector is authenticated individually before its blocks are written.
        Returns True on full success, False on any auth or write failure.
        """
        data = text.encode("utf-8")
        padded_len = ((len(data) + 15) // 16) * 16
        data_padded = data.ljust(padded_len, b"\x00")
        default_key = b"\xFF\xFF\xFF\xFF\xFF\xFF"

        offset = 0
        sector = 1  # sector 0 holds manufacturer data; start at sector 1
        while offset < padded_len:
            trailer = sector * 4 + 3
            if not self._auth_mifare_block(target_no, trailer, default_key, uid[:4]):
                self._dbg(
                    "pn532._write_mifare_classic_json auth failed sector=%d" % sector
                )
                return False
            for blk in range(3):  # 3 data blocks per sector
                if offset >= padded_len:
                    break
                abs_block = sector * 4 + blk
                chunk = data_padded[offset:offset + 16]
                if not self._write_mifare_block(target_no, abs_block, chunk):
                    self._dbg(
                        "pn532._write_mifare_classic_json write failed block=%d" % abs_block
                    )
                    return False
                offset += 16
            sector += 1

        return True

    def write_ndef_text(self, text: str) -> bool:
        """Encode *text* as an NDEF Well-Known Text record and write it to a Type 2 tag.

        Uses WUPA (instead of REQA) so the tag is found even if it was left in READY*
        state by a preceding read_all_tags() call.
        Calls list_passive_target once, then issues one WRITE (0xA2) command per page
        starting at page 4.  Returns True on full success, False on any error.
        """
        self.initialize()
        target = self.list_passive_target(wupa=True)
        if not target:
            return False
        return self._write_type2_ndef(target["target"], text)

    def write_tag(self, text: str) -> bool:
        """Write *text* to the tag, auto-detecting the tag type via SEL_RES (SAK).

        - **NTAG / Ultralight (SAK 0x00)**: writes as NDEF Well-Known Text TLV
          starting at page 4.
        - **MIFARE Classic 1K/4K (SAK bit 3 set — 0x08 / 0x18)**: authenticates each
          sector with the default key A (0xFF×6) and writes the UTF-8 JSON bytes to
          data blocks starting at sector 1 (absolute block 4).

        Uses WUPA (instead of REQA) so the tag is found even if it was left in READY*
        state by a preceding read_all_tags() call.
        Returns True on full success, False on any failure.
        """
        self.initialize()
        target = self.list_passive_target(wupa=True)
        if not target:
            return False
        sel_res = target.get("sel_res", 0)
        if sel_res & 0x08:
            # MIFARE Classic 1K (0x08) or 4K (0x18)
            self._dbg("pn532.write_tag type=mifare_classic sel_res=0x%02X" % sel_res)
            return self._write_mifare_classic_json(
                target["target"], target["uid"], text
            )
        else:
            # NTAG / Ultralight (SAK 0x00)
            self._dbg("pn532.write_tag type=type2 sel_res=0x%02X" % sel_res)
            return self._write_type2_ndef(target["target"], text)


def _encode_ndef_text_tlv(text: str) -> bytes:
    """Encode *text* as an NDEF Well-Known Type Text record wrapped in a Type 2 TLV.

    Layout written to tag memory (starting at page 4):
      0x03  <length>  <NDEF message>  0xFE  [0x00 padding to page boundary]

    NDEF Text record (TNF=0x01, Type='T', UTF-8, lang='en'):
      Header      0xD1  (MB=1 ME=1 CF=0 SR=1 IL=0 TNF=0x01)
      TypeLen     0x01
      PayloadLen  3 + len(text_bytes)
      Type        0x54  ('T')
      Payload     [len(lang), *lang_bytes, *text_bytes]  (UTF-8, lang='en' → 0x02 0x65 0x6E)

    The returned bytes are padded to a multiple of 4 (one NTAG/Ultralight page).
    """
    text_bytes = text.encode("utf-8")
    lang = b"en"
    ndef_payload = bytes([len(lang)]) + lang + text_bytes
    ndef_record = bytes([0xD1, 0x01, len(ndef_payload), 0x54]) + ndef_payload

    ndef_len = len(ndef_record)
    if ndef_len < 0xFF:
        tlv = bytes([0x03, ndef_len]) + ndef_record + b"\xFE"
    else:
        tlv = (
            bytes([0x03, 0xFF, (ndef_len >> 8) & 0xFF, ndef_len & 0xFF])
            + ndef_record
            + b"\xFE"
        )

    rem = len(tlv) % 4
    if rem:
        tlv += b"\x00" * (4 - rem)
    return tlv


class PN532Handler:
    """
    Thin wrapper to mirror the kind of API shape your rfid.py expects from
    a reader backend.
    """

    def __init__(self, spi, reactor=None):
        self.dev = PN532Device(spi, reactor=reactor)

    def set_reactor(self, reactor) -> None:
        self.dev.set_reactor(reactor)

    @contextmanager
    def antenna_enabled(self):
        # PN532 handles RF internally after SAM init; keep context for API parity
        self.dev.initialize()
        yield

    def get_version(self) -> str:
        fw = self.dev.get_firmware_version()
        return f"PN532 ic=0x{fw['ic']:02X} ver={fw['ver']}.{fw['rev']} support=0x{fw['support']:02X}"

    def read_uid(self) -> Optional[bytes]:
        return self.dev.read_uid()

    def read_tag_info(self, max_pages: int = 135) -> Optional[dict]:
        return self.dev.read_tag_info(max_pages=max_pages)

    def read_all_tags(self, max_pages: int = 135, max_tags: int = 4) -> list:
        return self.dev.read_all_tags(max_pages=max_pages, max_tags=max_tags)

    def read_ndef_text(self, max_pages: int = 135) -> Optional[str]:
        return self.dev.read_ndef_text(max_pages=max_pages)

    # rfid.py._read_tag_text() probes these names in order; all aliases
    # resolve to the same underlying read_ndef_text implementation.
    def read_ntag_ndef_text(self, max_pages: int = 135) -> Optional[str]:
        return self.dev.read_ndef_text(max_pages=max_pages)

    def read_ntag_text(self, max_pages: int = 135) -> Optional[str]:
        return self.dev.read_ndef_text(max_pages=max_pages)

    def read_text(self, max_pages: int = 135) -> Optional[str]:
        return self.dev.read_ndef_text(max_pages=max_pages)

    def write_ndef_text(self, text: str) -> bool:
        return self.dev.write_ndef_text(text)

    def write_tag(self, text: str) -> bool:
        return self.dev.write_tag(text)

    def read_mifare_classic_tag(self, key_list: list, num_sectors: int = 16) -> Optional[dict]:
        return self.dev.read_mifare_classic_tag(key_list, num_sectors=num_sectors)
