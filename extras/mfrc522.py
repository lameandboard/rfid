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
Type 2 tag reader for Klipper-based RFID helpers.

This implementation is intentionally small and focused:
- MFRC522 SPI register access
- ISO14443A REQA / anticollision / select
- Type 2 / NTAG READ support
- simple NDEF text / URI extraction

It is written to work with an MCU_SPI object created by Klipper's bus helper.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from typing import Iterable, Optional


class MFRC522Device:
    # Register map
    CommandReg = 0x01
    ComIEnReg = 0x02
    DivIEnReg = 0x03
    ComIrqReg = 0x04
    DivIrqReg = 0x05
    ErrorReg = 0x06
    Status1Reg = 0x07
    Status2Reg = 0x08
    FIFODataReg = 0x09
    FIFOLevelReg = 0x0A
    ControlReg = 0x0C
    BitFramingReg = 0x0D
    CollReg = 0x0E
    ModeReg = 0x11
    TxControlReg = 0x14
    TxASKReg = 0x15
    RFCfgReg = 0x26
    CRCResultRegH = 0x21
    CRCResultRegL = 0x22
    TModeReg = 0x2A
    TPrescalerReg = 0x2B
    TReloadRegH = 0x2C
    TReloadRegL = 0x2D
    VersionReg = 0x37

    # PCD commands
    PCD_IDLE = 0x00
    PCD_CALCCRC = 0x03
    PCD_TRANSCEIVE = 0x0C
    PCD_AUTHENT = 0x0E
    PCD_SOFTRESET = 0x0F

    # PICC commands
    PICC_REQA = 0x26
    PICC_WUPA = 0x52
    PICC_READ = 0x30
    PICC_AUTHENT1A = 0x60
    PICC_AUTHENT1B = 0x61

    MI_OK = 0
    MI_NOTAGERR = 1
    MI_ERR = 2

    VALID_VERSION_VALUES = (0x88, 0x90, 0x91, 0x92, 0xA2, 0xB2)

    URI_PREFIXES = {
        0x00: "",
        0x01: "http://www.",
        0x02: "https://www.",
        0x03: "http://",
        0x04: "https://",
    }

    # Maps ISO14443A cascade selector byte to cascade level number (for debug logging)
    _SEL_TO_LEVEL = {0x93: 1, 0x95: 2, 0x97: 3}


    def __init__(self, spi, reactor=None):
        self.spi = spi
        self._reactor = reactor
        self._initialized = False
        self._command_timeout = 0.06
        self._debug_log = None

    # Minimum sleep between SPI status polls in standalone/test mode.
    # Yields the GIL and reduces bus hammering without adding meaningful latency.
    _SPI_POLL_INTERVAL = 0.0005  # 500 µs

    def set_reactor(self, reactor) -> None:
        self._reactor = reactor

    def set_debug_log(self, fn: "Optional[Callable[[str], None]]") -> None:
        """Inject a debug-log callable, or pass None to disable debug logging.

        Signature: fn(msg: str) -> None
        """
        self._debug_log = fn

    def _dbg(self, msg, *args, **kwargs) -> None:
        """Internal debug logger with lazy formatting support.

        Usage patterns (all equivalent when _debug_log is set):
        - _dbg("simple message")
        - _dbg("value: %s", some_value)
        - _dbg(lambda: "expensive: %s" % compute_expensive())
        """
        fn = self._debug_log
        if fn is None:
            # Avoid any formatting or callable invocation when debug is disabled.
            return

        # If msg is a callable and no extra args/kwargs are provided, call it lazily.
        if callable(msg) and not args and not kwargs:
            text = msg()
        elif args or kwargs:
            # Support %-style formatting first for format-string + args usage.
            try:
                if args and not kwargs:
                    text = msg % args
                elif kwargs and not args:
                    text = msg % kwargs
                else:
                    # Mixed args/kwargs: fall back to str.format
                    raise TypeError
            except TypeError:
                text = str(msg).format(*args, **kwargs)
        else:
            text = msg

        fn(text)

    def _now(self) -> float:
        if self._reactor is not None:
            return self._reactor.monotonic()
        return time.monotonic()

    def _wait_time(self, seconds: float) -> None:
        """Blocking sleep for standalone/test use only.

        Must NOT be called when running inside the Klipper reactor (i.e. when
        self._reactor is not None).  The polling loops in _crc_a() and
        transceive() already guard every call with ``if self._reactor is None``
        so this method is never reached in normal Klipper operation.  The guard
        here is a hard safety net: if a future code path forgets the guard it
        will fail loudly rather than silently blocking the reactor event loop.
        """
        if self._reactor is not None:
            raise RuntimeError(
                "MFRC522Device._wait_time() must not be called in Klipper reactor context; "
                "use non-blocking polling (busy-wait with self._now() deadline) instead."
            )
        time.sleep(seconds)

    # ---------- SPI ----------
    def _reg_addr_wr(self, reg: int) -> int:
        return ((reg << 1) & 0x7E)

    def _reg_addr_rd(self, reg: int) -> int:
        return (((reg << 1) & 0x7E) | 0x80)

    def _write_reg(self, reg: int, val: int) -> None:
        self.spi.spi_send([self._reg_addr_wr(reg), val & 0xFF])

    def _read_reg(self, reg: int) -> int:
        ret = self.spi.spi_transfer([self._reg_addr_rd(reg), 0x00])
        if isinstance(ret, dict) and "response" in ret:
            data = bytearray(ret["response"])
        else:
            data = bytearray(ret)
        if len(data) < 2:
            return 0xFF
        return data[1] & 0xFF

    def _set_mask(self, reg: int, mask: int) -> None:
        self._write_reg(reg, self._read_reg(reg) | (mask & 0xFF))

    def _clear_mask(self, reg: int, mask: int) -> None:
        self._write_reg(reg, self._read_reg(reg) & (~mask & 0xFF))

    # ---------- init ----------
    def reset(self) -> None:
        # Issue a software reset.  The MFRC522 datasheet recommends waiting
        # ~50 ms after reset for the oscillator to stabilise; however, calling
        # reactor.pause() here is unsafe (this runs inside a reactor timer
        # callback).  In practice the subsequent register writes provide enough
        # implicit settling time and the first scan attempt will succeed.
        self._write_reg(self.CommandReg, self.PCD_SOFTRESET)

    def initialize(self) -> None:
        if self._initialized:
            return
        self.reset()
        self._write_reg(self.TModeReg, 0x8D)
        self._write_reg(self.TPrescalerReg, 0x3E)
        self._write_reg(self.TReloadRegH, 0x00)
        self._write_reg(self.TReloadRegL, 0x1E)
        self._write_reg(self.TxASKReg, 0x40)
        self._write_reg(self.ModeReg, 0x3D)
        self._write_reg(self.RFCfgReg, 0x70)
        self.antenna_on()
        self._initialized = True

    def read_version(self) -> int:
        self.initialize()
        return self._read_reg(self.VersionReg)

    def antenna_on(self) -> None:
        self._set_mask(self.TxControlReg, 0x03)

    def antenna_off(self) -> None:
        self._clear_mask(self.TxControlReg, 0x03)

    def _rf_reset(self, field_off_s: float = 0.010, powerup_s: float = 0.005) -> None:
        """Cycle the RF field off/on to return all tags to IDLE state.

        Tags left in READY* state by a preceding read_all_tags() call do not
        respond to REQA or WUPA (ISO 14443-3A: only IDLE/HALT state tags respond
        to those commands).  Powering the field off for ~10 ms (0.010 s) causes
        tags to lose power and restart from IDLE, so the next REQA finds them
        reliably.

        Both *field_off_s* and *powerup_s* are in **seconds** and are capped at
        0.1 s each to avoid accidentally blocking the Klipper reactor for extended
        periods.  When running under the Klipper reactor, this method uses a
        busy-wait for timing (matching transceive(), which already busy-waits up to
        _command_timeout=60 ms).  Outside the reactor context (self._reactor is
        None), it delegates to _wait_time() so that time.sleep() may be used and
        CPU is not unnecessarily burned in standalone/test use.
        """
        field_off_s = min(field_off_s, 0.100)
        powerup_s = min(powerup_s, 0.100)
        self.antenna_off()
        if self._reactor is None:
            # Standalone/test mode: allow sleeping instead of spinning.
            self._wait_time(field_off_s)
        else:
            # Reactor context: use busy-wait to avoid time.sleep().
            deadline = self._now() + field_off_s
            while self._now() < deadline:
                pass
        self.antenna_on()
        if self._reactor is None:
            self._wait_time(powerup_s)
        else:
            deadline = self._now() + powerup_s
            while self._now() < deadline:
                pass

    @contextmanager
    def antenna_enabled(self):
        self.initialize()
        self.antenna_on()
        try:
            yield
        finally:
            # Antenna is intentionally left on: cycling RF power between reads
            # is unnecessary in a Klipper context and can cause re-init delays.
            pass

    # ---------- CRC ----------
    def _crc_a(self, payload: Iterable[int]) -> list[int]:
        self.initialize()
        self._write_reg(self.CommandReg, self.PCD_IDLE)
        self._write_reg(self.DivIrqReg, 0x04)
        self._write_reg(self.FIFOLevelReg, 0x80)
        for b in payload:
            self._write_reg(self.FIFODataReg, int(b) & 0xFF)
        self._write_reg(self.CommandReg, self.PCD_CALCCRC)
        deadline = self._now() + self._command_timeout
        while self._now() < deadline:
            if self._read_reg(self.DivIrqReg) & 0x04:
                return [
                    self._read_reg(self.CRCResultRegL) & 0xFF,
                    self._read_reg(self.CRCResultRegH) & 0xFF,
                ]
            if self._reactor is None:
                self._wait_time(self._SPI_POLL_INTERVAL)
        return [0x00, 0x00]

    # ---------- transceive ----------
    def transceive(self, command: int, send_data: list[int], tx_last_bits: int = 0, rx_align: int = 0):
        self.initialize()
        irq_en = 0x00
        wait_irq = 0x00
        if command == self.PCD_TRANSCEIVE:
            irq_en = 0x77
            wait_irq = 0x30

        self._write_reg(self.ComIEnReg, (irq_en | 0x80) & 0xFF)
        self._write_reg(self.ComIrqReg, 0x7F)
        self._write_reg(self.DivIrqReg, 0x7F)
        self._write_reg(self.FIFOLevelReg, 0x80)
        self._write_reg(self.CommandReg, self.PCD_IDLE)

        for b in send_data:
            self._write_reg(self.FIFODataReg, int(b) & 0xFF)

        self._write_reg(self.BitFramingReg, ((rx_align & 0x07) << 4) | (tx_last_bits & 0x07))
        self._write_reg(self.CommandReg, command)
        if command == self.PCD_TRANSCEIVE:
            self._set_mask(self.BitFramingReg, 0x80)

        deadline = self._now() + self._command_timeout
        irq = 0
        while self._now() < deadline:
            irq = self._read_reg(self.ComIrqReg)
            if irq & wait_irq:
                break
            if irq & 0x01:
                break
            if self._reactor is None:
                self._wait_time(self._SPI_POLL_INTERVAL)

        self._clear_mask(self.BitFramingReg, 0x80)

        err = self._read_reg(self.ErrorReg)
        if err & 0x13:
            return (self.MI_ERR, [], 0)

        status = self.MI_OK
        if irq & 0x01:
            status = self.MI_NOTAGERR

        back_data: list[int] = []
        back_bits = 0
        if command == self.PCD_TRANSCEIVE:
            fifo_level = self._read_reg(self.FIFOLevelReg)
            last_bits = self._read_reg(self.ControlReg) & 0x07
            if last_bits:
                back_bits = (fifo_level - 1) * 8 + last_bits
            else:
                back_bits = fifo_level * 8
            if fifo_level == 0:
                fifo_level = 1
            fifo_level = min(fifo_level, 64)
            for _ in range(fifo_level):
                back_data.append(self._read_reg(self.FIFODataReg))

        return (status, back_data, back_bits)

    # ---------- ISO14443A ----------
    def request(self, req_mode: int = PICC_REQA):
        self.initialize()
        self._dbg("mfrc522.request mode=0x%02X" % req_mode)
        self._write_reg(self.BitFramingReg, 0x07)
        status, data, bits = self.transceive(self.PCD_TRANSCEIVE, [req_mode], tx_last_bits=7)
        if status != self.MI_OK or bits != 0x10:
            _st_name = {self.MI_OK: "MI_OK", self.MI_NOTAGERR: "MI_NOTAGERR",
                        self.MI_ERR: "MI_ERR"}.get(status, str(status))
            self._dbg("mfrc522.request status=%s back_bits=%d atqa=%s" % (
                _st_name, bits, " ".join("%02X" % b for b in data)))
            return (self.MI_ERR, None)
        self._dbg("mfrc522.request status=MI_OK back_bits=%d atqa=%s" % (
            bits, " ".join("%02X" % b for b in data)))
        return (self.MI_OK, data)

    def _anticoll_level(self, sel: int):
        """ISO14443A anticollision with full bit-level collision resolution (ISO 14443-3 §6.4).

        Iteratively resolves UID bit collisions by reading the collision position
        from CollReg and forcing the colliding bit to 1 to narrow the search,
        until a single tag's full UID is isolated.

        Returns (MI_OK, uid_cln) where uid_cln is a 5-byte list [b0..b3, bcc],
        or (MI_ERR, None) on unrecoverable error.
        """
        known_bits = 0          # number of UID bits already known/forced
        known_bytes = bytearray(5)

        result_status = self.MI_ERR
        result_uid = None
        try:
            for _ in range(32):     # at most 32 UID bits to resolve
                byte_count = known_bits // 8
                bit_count  = known_bits & 0x07
                # NVB high nibble: total valid bytes in frame (SEL + NVB + UID bytes)
                nvb = ((byte_count + 2) << 4) | bit_count

                send = [sel, nvb] + list(known_bytes[:byte_count])
                if bit_count:
                    # Send the partial byte — mask to only the valid lower bits
                    send.append(known_bytes[byte_count] & ((1 << bit_count) - 1))

                self._clear_mask(self.CollReg, 0x80)   # ValuesAfterColl=0: preserve received bits after collision
                # Pass bit_count as both tx_last_bits and rx_align so transceive() sets BitFramingReg correctly
                status, back_data, _ = self.transceive(self.PCD_TRANSCEIVE, send,
                                                       tx_last_bits=bit_count, rx_align=bit_count)

                # Check for collision by reading CollReg: CollPosNotValid (bit5)==0 means valid collision pos
                coll_reg = self._read_reg(self.CollReg)
                if not (coll_reg & 0x20):
                    # Collision detected — read the collision position
                    coll_pos = coll_reg & 0x1F
                    if coll_pos == 0:
                        coll_pos = 32  # 0 means collision at bit 32 per MFRC522 datasheet
                    if coll_pos <= known_bits:
                        self._dbg("mfrc522.anticoll sel=0x%02X coll_pos=%d <= known_bits=%d" % (
                            sel, coll_pos, known_bits))
                        break
                    # Merge received bits into known_bytes
                    for i, b in enumerate(back_data):
                        idx = byte_count + i
                        if idx < 5:
                            known_bytes[idx] = b
                    # Force the colliding bit to 1 to select one branch
                    bit_idx = coll_pos - 1
                    if bit_idx // 8 >= 5:
                        self._dbg("mfrc522.anticoll sel=0x%02X coll_pos=%d out of range" % (
                            sel, coll_pos))
                        break
                    known_bytes[bit_idx // 8] |= (1 << (bit_idx % 8))
                    known_bits = coll_pos
                    continue

                # No collision — check for other errors
                if status != self.MI_OK or len(back_data) < 5:
                    self._dbg("mfrc522.anticoll sel=0x%02X status=%d (short response len=%d) coll_reg=0x%02X" % (
                        sel, status, len(back_data), coll_reg))
                    break

                # Full clean UID — validate BCC
                uid_cln = list(back_data[:5])
                bcc = 0
                for b in uid_cln[:4]:
                    bcc ^= b
                if bcc != uid_cln[4]:
                    self._dbg("mfrc522.anticoll sel=0x%02X bcc_fail computed=0x%02X received=0x%02X coll_reg=0x%02X" % (
                        sel, bcc, uid_cln[4], coll_reg))
                    break
                self._dbg("mfrc522.anticoll sel=0x%02X status=MI_OK uid_bytes=%s" % (
                    sel, " ".join("%02X" % b for b in uid_cln)))
                result_status = self.MI_OK
                result_uid = uid_cln
                break
            else:
                self._dbg("mfrc522.anticoll sel=0x%02X exceeded max iterations" % sel)
        finally:
            # Ensure BitFramingReg is reset before _select_level runs (it relies on 0x00)
            self._write_reg(self.BitFramingReg, 0x00)
        return (result_status, result_uid)

    def _select_level(self, sel: int, uid_cln: list[int]):
        frame = [sel, 0x70] + uid_cln[:5]
        frame += self._crc_a(frame)
        status, back_data, back_bits = self.transceive(self.PCD_TRANSCEIVE, frame)
        if status != self.MI_OK or back_bits != 0x18 or len(back_data) < 1:
            self._dbg("mfrc522.select sel=0x%02X status=MI_ERR" % sel)
            return (self.MI_ERR, None)
        sak = back_data[0] & 0xFF
        cascade = bool(sak & 0x04)
        self._dbg("mfrc522.select sel=0x%02X uid=%s status=MI_OK sak=0x%02X cascade=%s" % (
            sel, " ".join("%02X" % b for b in uid_cln[:5]), sak, cascade))
        return (self.MI_OK, sak)

    def _anticoll_and_select(self) -> Optional[list[int]]:
        """Run ISO14443A anticollision + select cascade and return UID bytes, or None on failure."""
        uid: list[int] = []
        for sel in (0x93, 0x95, 0x97):
            st, cln = self._anticoll_level(sel)
            if st != self.MI_OK:
                return None
            st, sak = self._select_level(sel, cln)
            if st != self.MI_OK:
                return None
            if cln[0] == 0x88:
                uid.extend(cln[1:4])
            else:
                uid.extend(cln[0:4])
            if not (sak & 0x04):
                return uid
        return uid or None

    def halt_tag(self) -> None:
        """Send ISO14443A HALT to silence the currently-selected tag.
        After HALT the tag ignores REQA until power-cycled or WUPA is sent.
        No response is expected; ignore transceive errors.
        """
        frame = [0x50, 0x00]
        frame += self._crc_a(frame)
        self._dbg("mfrc522.halt_tag sent")
        try:
            status, _, _ = self.transceive(self.PCD_TRANSCEIVE, frame)
            _st_name = {self.MI_OK: "MI_OK", self.MI_NOTAGERR: "MI_NOTAGERR",
                        self.MI_ERR: "MI_ERR"}.get(status, str(status))
            self._dbg("mfrc522.halt_tag status=%s" % _st_name)
        except Exception:
            pass

    def read_uid(self) -> Optional[list[int]]:
        self.initialize()
        self._dbg("mfrc522.read_uid enter")
        with self.antenna_enabled():
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                self._dbg("mfrc522.read_uid returning None (no tag)")
                return None
            uid: list[int] = []
            for sel in (0x93, 0x95, 0x97):
                level = self._SEL_TO_LEVEL[sel]
                self._dbg("mfrc522.read_uid level=%d sel=0x%02X uid_so_far=%s" % (
                    level, sel, uid))
                st, cln = self._anticoll_level(sel)
                if st != self.MI_OK:
                    self._dbg("mfrc522.read_uid failed at level=%d sel=0x%02X" % (level, sel))
                    return None
                st, sak = self._select_level(sel, cln)
                if st != self.MI_OK:
                    self._dbg("mfrc522.read_uid failed at level=%d sel=0x%02X" % (level, sel))
                    return None
                if cln[0] == 0x88:
                    uid.extend(cln[1:4])
                else:
                    uid.extend(cln[0:4])
                if not (sak & 0x04):
                    self._dbg("mfrc522.read_uid complete uid=%s" % (
                        "".join("%02X" % b for b in uid)))
                    self.halt_tag()
                    return uid
                self._dbg("mfrc522.read_uid cascade_continue sel=0x%02X sak=0x%02X" % (sel, sak))
            # All 3 cascade levels completed without clearing the cascade bit —
            # this indicates a malformed tag; return what we have or None.
            self._dbg("mfrc522.read_uid cascade exhausted; returning %s" % (
                "".join("%02X" % b for b in uid) if uid else "None"))
            if uid:
                self.halt_tag()
            return uid or None

    def read_uid_hex(self) -> Optional[str]:
        uid = self.read_uid()
        if uid is None:
            return None
        return "".join("%02X" % b for b in uid)

    def read_uid_fast(self) -> Optional[list[int]]:
        """Obtain UID as quickly as possible: REQA + anticollision only, no SELECT.

        Faster than :meth:`read_uid` because it skips the SELECT command at each
        cascade level — no CRC calculation, no additional SPI round-trip per level.
        The tag is **not** placed into the Selected state after this call, so memory
        reads are not possible in the same transaction.

        On a cache-hit fast path this lets the scan loop skip the full page-read
        entirely, shaving ~50–200 ms off each tick for already-known tags.

        If anticollision succeeds at cascade level 1 (with byte 0 == 0x88 indicating
        a multi-level tag) but then fails at a higher level, the UID bytes collected
        so far are returned (partial-UID best effort).  A partial UID will not match
        any full UID in the cache (since cache entries always use the complete UID
        hex string), so it will fall through to the full scan path harmlessly.

        Falls back to ``None`` if no tag is detected or anticollision fails at
        the very first level.
        """
        self.initialize()
        self._dbg("mfrc522.read_uid_fast enter")
        with self.antenna_enabled():
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                self._dbg("mfrc522.read_uid_fast returning None (no tag)")
                return None
            uid: list[int] = []
            for sel in (0x93, 0x95, 0x97):
                level = self._SEL_TO_LEVEL[sel]
                self._dbg("mfrc522.read_uid_fast level=%d sel=0x%02X uid_so_far=%s" % (
                    level, sel, "".join("%02X" % b for b in uid)))
                st, cln = self._anticoll_level(sel)
                if st != self.MI_OK:
                    self._dbg(
                        "mfrc522.read_uid_fast anticoll failed at level=%d returning %s" % (
                            level,
                            "".join("%02X" % b for b in uid) if uid else "None",
                        )
                    )
                    return uid if uid else None
                if cln[0] == 0x88:
                    # Cascade tag: collect UID bytes from this level and continue
                    uid.extend(cln[1:4])
                    self._dbg(
                        "mfrc522.read_uid_fast cascade_continue level=%d uid_so_far=%s" % (
                            level, "".join("%02X" % b for b in uid)))
                else:
                    uid.extend(cln[0:4])
                    self._dbg("mfrc522.read_uid_fast complete uid=%s" % (
                        "".join("%02X" % b for b in uid)))
                    return uid
            # All 3 cascade levels completed without clearing the cascade bit —
            # return whatever was collected (malformed / exotic tag).
            if uid:
                self._dbg("mfrc522.read_uid_fast cascade_exhausted uid=%s" % (
                    "".join("%02X" % b for b in uid)))
            return uid or None

    def read_uid_fast_hex(self) -> Optional[str]:
        """Return UID as a hex string via :meth:`read_uid_fast`, or ``None``."""
        uid = self.read_uid_fast()
        if uid is None:
            return None
        return "".join("%02X" % b for b in uid)

    # ---------- MIFARE Classic authenticated reads ----------

    def _auth_mifare_block(self, cmd: int, block_addr: int, key: bytes, uid: bytes) -> bool:
        """Authenticate a MIFARE Classic sector using the PCD_AUTHENT command.

        cmd     : PICC_AUTHENT1A (0x60) or PICC_AUTHENT1B (0x61)
        block_addr: any block within the target sector (typically the sector trailer)
        key     : 6-byte sector key
        uid     : tag UID (only first 4 bytes are used for crypto)

        Returns True if Status2Reg[MFCrypto1On] is set after the command
        (authentication succeeded), False otherwise.
        """
        # Write auth frame to FIFO: [cmd, block_addr, key[0..5], uid[0..3]]
        self._write_reg(self.CommandReg, self.PCD_IDLE)
        self._write_reg(self.FIFOLevelReg, 0x80)  # FlushBuffer
        frame = [cmd, block_addr & 0xFF] + list(key[:6]) + list(uid[:4])
        for b in frame:
            self._write_reg(self.FIFODataReg, b & 0xFF)
        self._write_reg(self.CommandReg, self.PCD_AUTHENT)
        # Poll for IdleIRq (bit 4) or timer interrupt (bit 0)
        deadline = self._now() + self._command_timeout
        while self._now() < deadline:
            irq = self._read_reg(self.ComIrqReg)
            if irq & 0x10:  # IdleIRq
                break
            if irq & 0x01:  # TimerIRq
                break
            if self._reactor is None:
                self._wait_time(self._SPI_POLL_INTERVAL)
        # Authentication succeeded when MFCrypto1On (Status2Reg bit 3) is set
        status2 = self._read_reg(self.Status2Reg)
        ok = bool(status2 & 0x08)
        self._dbg("mfrc522.auth_mifare block=%d key=%s ok=%s" % (
            block_addr, key.hex(), ok))
        return ok

    def _read_mifare_block(self, block_addr: int) -> Optional[bytes]:
        """Read a 16-byte MIFARE Classic block (must already be authenticated).

        Uses the same PICC_READ + CRC path as read_4pages() but expects exactly
        18 bytes back (16 data + 2 CRC bytes from the tag).
        """
        frame = [self.PICC_READ, block_addr & 0xFF]
        frame += self._crc_a(frame)
        status, back_data, back_bits = self.transceive(self.PCD_TRANSCEIVE, frame)
        if status != self.MI_OK or len(back_data) < 16:
            self._dbg("mfrc522.read_mifare_block block=%d failed status=%d" % (block_addr, status))
            return None
        self._dbg("mfrc522.read_mifare_block block=%d ok" % block_addr)
        return bytes(back_data[:16])

    def read_authenticated_blocks(
        self,
        uid: bytes,
        key_list: list,
        num_sectors: int = 16,
        use_key_b: bool = False,
    ) -> dict:
        """Authenticate and read data blocks for all sectors of a MIFARE Classic tag.

        The tag must have already been selected (anticollision + select completed)
        and the antenna must be enabled when this method is called.

        Parameters
        ----------
        uid       : Tag UID bytes (from anticollision).
        key_list  : List of 16 × 6-byte sector keys (one per sector).
        num_sectors: Number of sectors to read (1 K tag has 16 sectors).
        use_key_b : Use PICC_AUTHENT1B instead of PICC_AUTHENT1A.

        Returns
        -------
        dict mapping absolute block index → 16-byte data bytes.
        Sectors that fail authentication store None for their data blocks.
        The sector trailer block (block 3 of each sector) is not included.
        """
        auth_cmd = self.PICC_AUTHENT1B if use_key_b else self.PICC_AUTHENT1A
        result: dict = {}
        for sector in range(num_sectors):
            key = key_list[sector] if sector < len(key_list) else bytes(6)
            # Trailer block = sector * 4 + 3
            trailer_block = sector * 4 + 3
            if not self._auth_mifare_block(auth_cmd, trailer_block, key, uid):
                self._dbg("mfrc522.read_auth_blocks sector=%d auth_failed" % sector)
                # Clear crypto state before next sector
                self._clear_mask(self.Status2Reg, 0x08)
                # Pre-fill None for the three data blocks so callers get a consistent index map
                for blk_in_sector in range(3):
                    result[sector * 4 + blk_in_sector] = None
                continue
            for blk_in_sector in range(3):  # 3 data blocks per sector
                abs_block = sector * 4 + blk_in_sector
                data = self._read_mifare_block(abs_block)
                result[abs_block] = data
            # Clear MFCrypto1On before authenticating next sector
            self._clear_mask(self.Status2Reg, 0x08)
        return result

    def read_mifare_classic_tag(self, key_list: list, num_sectors: int = 16) -> Optional[dict]:
        """Full MIFARE Classic 1K read: REQA → anticoll → select → auth+read all sectors.

        Returns a dict suitable for rfid_tag_parser.parse_tag():
          {"uid_bytes": bytes, "uid_hex": str, "blocks": {abs_block: bytes}}
        Returns None if no tag is present.
        """
        self.initialize()
        with self.antenna_enabled():
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                return None
            uid = self._anticoll_and_select()
            if uid is None:
                return None
            uid_hex = "".join("%02X" % b for b in uid)
            self._dbg("mfrc522.read_mifare_classic uid=%s sectors=%d" % (uid_hex, num_sectors))
            blocks = self.read_authenticated_blocks(uid, key_list, num_sectors=num_sectors)
            self.halt_tag()
        return {
            "uid_bytes": bytes(uid),
            "uid_hex": uid_hex,
            "blocks": blocks,
        }

    # ---------- Type 2 / NTAG ----------
    def read_4pages(self, start_page: int) -> Optional[bytes]:
        frame = [self.PICC_READ, start_page & 0xFF]
        frame += self._crc_a(frame)
        status, back_data, back_bits = self.transceive(self.PCD_TRANSCEIVE, frame)
        if status != self.MI_OK or back_bits < (16 * 8) or len(back_data) < 16:
            _st_name = {self.MI_OK: "MI_OK", self.MI_NOTAGERR: "MI_NOTAGERR",
                        self.MI_ERR: "MI_ERR"}.get(status, str(status))
            self._dbg("mfrc522.read_4pages page=%d status=%s back_bits=%d len=%d" % (
                start_page, _st_name, back_bits, len(back_data)))
            return None
        self._dbg("mfrc522.read_4pages page=%d status=MI_OK bytes=16" % start_page)
        return bytes(back_data[:16])

    def read_type2_bytes(self, max_pages: int = 135) -> Optional[bytes]:
        self.initialize()
        data = bytearray()
        with self.antenna_enabled():
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                return None
            try:
                uid = self._anticoll_and_select()
                if uid is None:
                    return None
                self._dbg("mfrc522.read_type2 uid=%s max_pages=%d" % (
                    "".join("%02X" % b for b in uid), max_pages))
                for page in range(4, 4 + max_pages, 4):
                    block = self.read_4pages(page)
                    if block is None:
                        self._dbg("mfrc522.read_type2 page_read_failed at page=%d after %d bytes" % (
                            page, len(data)))
                        break
                    data.extend(block)
            finally:
                # Ensure the tag is halted even if anticollision/select or reads fail
                self.halt_tag()
        self._dbg("mfrc522.read_type2 total_bytes_read=%d" % len(data))
        return bytes(data)

    def read_type2_bytes_for_uid(self, uid, max_pages: int = 135) -> Optional[bytes]:
        if uid is None:
            return None
        self._dbg("mfrc522.read_type2 uid=%s max_pages=%d" % (
            "".join("%02X" % b for b in uid), max_pages))
        self.initialize()
        data = bytearray()
        with self.antenna_enabled():
            for page in range(4, 4 + max_pages, 4):
                block = self.read_4pages(page)
                if block is None:
                    self._dbg("mfrc522.read_type2 page_read_failed at page=%d after %d bytes" % (
                        page, len(data)))
                    break
                data.extend(block)
        self._dbg("mfrc522.read_type2 uid=%s max_pages=%d total_bytes_read=%d" % (
            "".join("%02X" % b for b in uid), max_pages, len(data)))
        return bytes(data)

    def _decode_ndef_message(self, payload: bytes) -> Optional[str]:
        self._dbg("mfrc522.ndef_decode payload_len=%d" % len(payload))
        if not payload:
            return None
        i = 0
        try:
            while i < len(payload):
                if payload[i] == 0x00:
                    i += 1
                    continue
                if payload[i] == 0xFE:
                    break
                if payload[i] != 0x03:
                    if i + 1 >= len(payload):
                        break
                    length = payload[i + 1]
                    i += 2 + length
                    continue
                if i + 1 >= len(payload):
                    self._dbg("mfrc522.ndef_decode no TLV type-3 found in %d bytes" % len(payload))
                    return None
                length = payload[i + 1]
                if length == 0xFF:
                    if i + 3 >= len(payload):
                        self._dbg("mfrc522.ndef_decode no TLV type-3 found in %d bytes" % len(payload))
                        return None
                    length = (payload[i + 2] << 8) | payload[i + 3]
                    start = i + 4
                else:
                    start = i + 2
                if start + length > len(payload):
                    # Buffer is still incomplete; more pages need to be read.
                    # Return None silently – this is expected during incremental reads.
                    return None
                msg = payload[start:start + length]
                result = self._decode_ndef_record(msg)
                if result is not None:
                    self._dbg("mfrc522.ndef_decode result=%s" % result)
                else:
                    self._dbg("mfrc522.ndef_decode TLV type-3 found but record type unsupported or could not be decoded in %d bytes" % len(payload))
                return result
        except Exception as exc:
            self._dbg("mfrc522.ndef_decode parse_error: %s" % exc)
            return None
        self._dbg("mfrc522.ndef_decode no TLV type-3 found in %d bytes" % len(payload))
        return None

    def _decode_ndef_record(self, msg: bytes) -> Optional[str]:
        if len(msg) < 3:
            return None
        header = msg[0]
        sr = bool(header & 0x10)
        il = bool(header & 0x08)
        tnf = header & 0x07
        # Accept TNF 0x01 (Well-Known: T / U), TNF 0x02 (MIME-type: application/json etc.),
        # and TNF 0x04 (Unknown – used by some Spool Painter / OpenSpool implementations
        # that do not announce a formal MIME type).
        if tnf not in (0x01, 0x02, 0x04):
            return None

        type_len = msg[1]
        idx = 2
        if sr:
            if idx >= len(msg):
                return None
            payload_len = msg[idx]
            idx += 1
        else:
            if idx + 3 >= len(msg):
                return None
            payload_len = int.from_bytes(msg[idx:idx + 4], "big")
            idx += 4
        id_len = 0
        if il:
            if idx >= len(msg):
                return None
            id_len = msg[idx]
            idx += 1

        if idx + type_len > len(msg):
            return None
        type_field = msg[idx:idx + type_len]
        idx += type_len
        idx += id_len
        if idx + payload_len > len(msg):
            return None
        payload = msg[idx:idx + payload_len]

        # TNF 0x02 – MIME-type record (e.g. application/json used by OpenSpool tags).
        # Only treat the payload as text for known text-like MIME types.
        if tnf == 0x02:
            # Require a non-empty TYPE field and payload for MIME records.
            if not payload or not type_field:
                return None
            mime_type = type_field.decode("ascii", errors="ignore").lower()
            # Strip any parameters (e.g. "; charset=utf-8") before evaluating.
            base_mime_type = mime_type.split(";", 1)[0].strip()
            if not base_mime_type:
                return None
            # Accept text/* and common JSON-based MIME types.
            is_text_like = (
                base_mime_type.startswith("text/")
                or base_mime_type == "application/json"
                or (base_mime_type.startswith("application/") and base_mime_type.endswith("+json"))
            )
            if not is_text_like:
                return None
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError:
                return payload.decode("latin1", errors="ignore")

        if type_field == b"T":
            if not payload:
                return None
            status = payload[0]
            lang_len = status & 0x3F
            text = payload[1 + lang_len:]
            try:
                if status & 0x80:
                    return text.decode("utf-16")
                return text.decode("utf-8")
            except Exception:
                return text.decode("latin1", errors="ignore")

        if type_field == b"U":
            if not payload:
                return None
            prefix = self.URI_PREFIXES.get(payload[0], "")
            try:
                return prefix + payload[1:].decode("utf-8")
            except Exception:
                return prefix + payload[1:].decode("latin1", errors="ignore")

        # TNF 0x04 – Unknown-type record (no TYPE field).  Some Spool Painter and
        # OpenSpool implementations write JSON data under this TNF when they do not
        # want to announce a MIME type.  Treat the payload as text when it looks
        # like JSON so the higher layers can extract spool metadata.
        if tnf == 0x04:
            if not payload:
                return None
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                text = payload.decode("latin1", errors="ignore")
            stripped = text.strip()
            if stripped.startswith(("{", "[")):
                return stripped
            return None

        return None

    def read_ndef_text(self, max_pages: int = 135) -> Optional[str]:
        raw = self.read_type2_bytes(max_pages=max_pages)
        if not raw:
            return None
        return self._decode_ndef_message(raw)

    def read_ntag_ndef_text(self, max_pages: int = 135) -> Optional[str]:
        return self.read_ndef_text(max_pages=max_pages)

    def read_ntag_text(self, max_pages: int = 135) -> Optional[str]:
        return self.read_ndef_text(max_pages=max_pages)

    def read_text(self, max_pages: int = 135) -> Optional[str]:
        return self.read_ndef_text(max_pages=max_pages)

    def extract_spoolman_id(self, text: Optional[str]) -> Optional[int]:
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

    def read_all_tags(self, max_pages: int = 135, max_tags: int = 4,
                      skip_deep_read_uids: Optional[set] = None,
                      rewake_after: bool = False) -> list[dict]:
        """Single-phase enumeration of all ISO14443A tags in the RF field.

        Pass 0 uses WUPA to wake any tags left in HALT from a previous scan
        window (halted tags ignore REQA but respond to WUPA).  Subsequent passes
        use REQA so that only tags still in IDLE state (i.e. new tags that have
        not yet been seen this call) respond — halted tags from earlier passes in
        this same call stay silent, preventing duplicates without needing a
        separate discovery phase.

        This single-phase design ensures that if a lane3 tag is already in front
        of the reader and halted, WUPA on pass 0 wakes it so it can be found and
        re-halted, while a new lane4 tag (in IDLE state) is found on pass 1 via
        REQA.  Both tags are returned in the same call.

        For each tag found: if its UID is in skip_deep_read_uids, a UID-only
        entry is added (no page reads).  Otherwise the tag is read immediately
        while still selected, then halted before the next pass.

        rewake_after — when True, a final WUPA is sent after all tags have been
        processed so they return to READY* state and will respond to the next
        REQA.  Set to True when fast_mode=False (safe/two-read mode) so the
        second confirmation tick can find the same tag again.

        Returns a list of tag-info dicts (same schema as read_tag_info()).
        Returns [] if no tags are found.
        """
        self.initialize()
        tags = []
        seen_uid_hexes: set[str] = set()
        self._dbg("mfrc522.read_all_tags enter max_tags=%d max_pages=%d" % (max_tags, max_pages))

        with self.antenna_enabled():
            for pass_num in range(max_tags):
                # Pass 0: WUPA wakes tags left in HALT from a previous scan window.
                # Pass 1+: REQA finds only IDLE tags (new arrivals or tags not yet
                # seen this call); tags halted in earlier passes stay silent.
                req_mode = self.PICC_WUPA if pass_num == 0 else self.PICC_REQA

                # --- retry request up to 3 times ---
                st = self.MI_ERR
                for attempt in range(3):
                    self._dbg(
                        "mfrc522.read_all_tags pass=%d attempt=%d request_start mode=%s",
                        pass_num, attempt,
                        "WUPA" if req_mode == self.PICC_WUPA else "REQA",
                    )
                    st, _ = self.request(req_mode)
                    self._dbg(
                        "mfrc522.read_all_tags pass=%d attempt=%d request_status=%s",
                        pass_num, attempt,
                        {self.MI_OK: "MI_OK", self.MI_NOTAGERR: "MI_NOTAGERR",
                         self.MI_ERR: "MI_ERR"}.get(st, str(st)),
                    )
                    if st == self.MI_OK:
                        break
                    self._clear_mask(self.Status2Reg, 0x08)
                    self._write_reg(self.BitFramingReg, 0x00)

                if st != self.MI_OK:
                    self._dbg(
                        "mfrc522.read_all_tags pass=%d request_failed found_tags=%d",
                        pass_num, len(tags),
                    )
                    break

                # --- retry anticollision/select up to 3 times ---
                uid = None
                for attempt in range(3):
                    uid = self._anticoll_and_select()
                    self._dbg(
                        lambda _p=pass_num, _a=attempt, _u=uid: (
                            "mfrc522.read_all_tags pass=%d attempt=%d uid=%s"
                            % (_p, _a, "".join("%02X" % b for b in _u) if _u else "None")
                        )
                    )
                    if uid is not None:
                        break
                    self._clear_mask(self.Status2Reg, 0x08)
                    self._write_reg(self.BitFramingReg, 0x00)

                if uid is None:
                    self._dbg(
                        "mfrc522.read_all_tags pass=%d anticoll_failed found_tags=%d",
                        pass_num, len(tags),
                    )
                    self._rf_reset()
                    self._clear_mask(self.Status2Reg, 0x08)
                    self._write_reg(self.BitFramingReg, 0x00)
                    break

                uid_hex = "".join("%02X" % b for b in uid)

                # --- dedup: tag already seen in an earlier pass this call ---
                if uid_hex in seen_uid_hexes:
                    self._dbg(
                        "mfrc522.read_all_tags pass=%d duplicate_uid=%s skipping",
                        pass_num, uid_hex,
                    )
                    self.halt_tag()
                    self._clear_mask(self.Status2Reg, 0x08)
                    self._write_reg(self.BitFramingReg, 0x00)
                    continue

                seen_uid_hexes.add(uid_hex)
                self._dbg(
                    "mfrc522.read_all_tags pass=%d discovered uid=%s",
                    pass_num, uid_hex,
                )

                # --- skip deep read if caller says so ---
                if skip_deep_read_uids and uid_hex in skip_deep_read_uids:
                    self._dbg("mfrc522.read_all_tags skip_deep_read uid=%s", uid_hex)
                    self.halt_tag()
                    self._clear_mask(self.Status2Reg, 0x08)
                    self._write_reg(self.BitFramingReg, 0x00)
                    tags.append({
                        "uid": uid,
                        "uid_hex": uid_hex,
                        "raw_bytes": None,
                        "raw_len": 0,
                        "spoolman_id": None,
                        "tag_text": "",
                    })
                    continue

                # --- tag is selected: read its pages now ---
                raw = bytearray()
                spoolman_id = None
                text = None
                for page in range(4, 4 + max_pages, 4):
                    block = self.read_4pages(page)
                    if block is None:
                        if not raw:
                            # First-block failure: reset reader state and defer to
                            # a later pass rather than returning a broken entry.
                            self._dbg(
                                "mfrc522.read_all_tags read_4pages failed on first block uid=%s; deferring to later pass",
                                uid_hex,
                            )
                            self._rf_reset()
                            return tags
                        # Non-first block: treat as end of tag memory.
                        break
                    raw.extend(block)
                    _partial_text = self._decode_ndef_message(bytes(raw))
                    if _partial_text is not None:
                        _sid = self.extract_spoolman_id(_partial_text)
                        if _sid is not None:
                            text = _partial_text
                            spoolman_id = _sid
                            self._dbg(
                                "mfrc522.read_all_tags early_exit page=%d after %d bytes (spool_id found)",
                                page, len(raw),
                            )
                            break
                        # NDEF decoded but no spoolman_id; further pages won't help.
                        text = _partial_text
                        self._dbg(
                            "mfrc522.read_all_tags no_sid_in_ndef page=%d uid=%s after %d bytes",
                            page, uid_hex, len(raw),
                        )
                        break

                raw = bytes(raw)
                if spoolman_id is None:
                    text = self._decode_ndef_message(raw or b"")
                    if text is None and raw:
                        text = raw.decode("utf-8", errors="ignore").strip("\x00")
                    spoolman_id = self.extract_spoolman_id(text)

                tags.append({
                    "uid": uid,
                    "uid_hex": uid_hex,
                    "raw_bytes": raw,
                    "raw_len": len(raw) if raw else 0,
                    "spoolman_id": spoolman_id,
                    "tag_text": text or "",
                })
                self._dbg(
                    "mfrc522.read_all_tags tag_appended uid=%s raw_len=%d spoolman_id=%s",
                    uid_hex, len(raw), spoolman_id,
                )

                # Halt this tag so it stays silent for subsequent passes (REQA won't
                # wake it); only a new WUPA or RF power cycle will wake it again.
                self.halt_tag()
                self._dbg("mfrc522.read_all_tags pass=%d halt_sent" % pass_num)
                self._clear_mask(self.Status2Reg, 0x08)
                self._write_reg(self.BitFramingReg, 0x00)

            if rewake_after and tags:
                # Safe-mode (fast_mode=False) requires the same tag to be seen on a
                # second tick for confirmation.  Send WUPA so halted tags return to
                # READY* and will respond to the next tick's REQA.
                self.request(self.PICC_WUPA)
                self._clear_mask(self.Status2Reg, 0x08)
                self._write_reg(self.BitFramingReg, 0x00)
                self._dbg("mfrc522.read_all_tags rewake_after wupa_sent")

            self._dbg("mfrc522.read_all_tags done tag_count=%d" % len(tags))

        self._dbg("mfrc522.read_all_tags return tag_count=%d" % len(tags))
        return tags

    def read_tag_info(self, max_pages: int = 135) -> Optional[dict]:
        self._dbg("mfrc522.read_tag_info enter max_pages=%d" % max_pages)
        self.initialize()
        with self.antenna_enabled():
            # --- retry REQA up to 3 times (mirrors read_all_tags) ---
            st = self.MI_ERR
            for attempt in range(3):
                self._dbg("mfrc522.read_tag_info attempt=%d reqa_start", attempt)
                st, _ = self.request(self.PICC_REQA)
                self._dbg(
                    lambda _a=attempt, _s=st: "mfrc522.read_tag_info attempt=%d reqa_status=%s" % (
                        _a,
                        {self.MI_OK: "MI_OK", self.MI_NOTAGERR: "MI_NOTAGERR",
                         self.MI_ERR: "MI_ERR"}.get(_s, str(_s))
                    )
                )
                if st == self.MI_OK:
                    break
                self._clear_mask(self.Status2Reg, 0x08)
                self._write_reg(self.BitFramingReg, 0x00)

            if st != self.MI_OK:
                self._dbg("mfrc522.read_tag_info returning None (no tag after reqa retries)")
                return None

            # --- retry anticollision/select up to 3 times (mirrors read_all_tags) ---
            uid = None
            for attempt in range(3):
                uid = self._anticoll_and_select()
                self._dbg(lambda: "mfrc522.read_tag_info attempt=%d uid=%s" % (
                    attempt,
                    ("".join("%02X" % b for b in uid) if uid else "None")
                ))
                if uid is not None:
                    break
                self._clear_mask(self.Status2Reg, 0x08)
                self._write_reg(self.BitFramingReg, 0x00)

            if uid is None:
                self._dbg("mfrc522.read_tag_info uid=None (no tag detected after anticoll retries)")
                return None
            uid_hex = "".join("%02X" % b for b in uid)
            # Read pages while tag is still selected.
            # Early-exit: stop as soon as we have enough data to parse a valid
            # spool_id so the tag doesn't have to stay in RF range for the
            # full max_pages read (important during AFC load).
            data = bytearray()
            spoolman_id = None
            text = None
            for page in range(4, 4 + max_pages, 4):
                block = self.read_4pages(page)
                if block is None:
                    break
                data.extend(block)
                _partial_text = self._decode_ndef_message(bytes(data))
                if _partial_text is not None:
                    _sid = self.extract_spoolman_id(_partial_text)
                    if _sid is not None:
                        text = _partial_text
                        spoolman_id = _sid
                        self._dbg(
                            "mfrc522.read_tag_info early_exit page=%d after %d bytes (spool_id found)",
                            page, len(data),
                        )
                        break
                    # NDEF decoded but no spoolman_id; additional pages won't help.
                    text = _partial_text
                    self._dbg(
                        "mfrc522.read_tag_info no_sid_in_ndef page=%d uid=%s after %d bytes",
                        page, uid_hex, len(data),
                    )
                    break
            raw = bytes(data)
            # Halt the tag so it stops responding to REQA and doesn't
            # interfere with the next scan attempt
            self.halt_tag()
        if spoolman_id is None:
            text = self._decode_ndef_message(raw or b"")
            if text is None and raw:
                # Best-effort fallback decode
                text = raw.decode("utf-8", errors="ignore").strip("\x00")
            spoolman_id = self.extract_spoolman_id(text)
        self._dbg("mfrc522.read_tag_info uid=%s raw_len=%d spoolman_id=%s" % (
            uid_hex, len(raw), spoolman_id))
        return {
            "uid": uid,
            "uid_hex": uid_hex,
            "raw_bytes": raw,
            "raw_len": len(raw),
            "spoolman_id": spoolman_id,
            "tag_text": text or "",
        }


    # ---------- NTAG / Type 2 write ----------

    def _anticoll_and_select_with_sak(self):
        """Run ISO14443A anticollision + select cascade, returning UID and final SAK.

        Returns ``(uid, sak)`` where *uid* is a list of UID bytes and *sak* is the
        final SAK byte (used to identify tag type).  Returns ``(None, 0)`` on failure.

        SAK quick reference:
          0x00 → NTAG / Mifare Ultralight (Type 2)
          0x08 → MIFARE Classic 1K
          0x18 → MIFARE Classic 4K
        """
        uid: list[int] = []
        final_sak = 0
        for sel in (0x93, 0x95, 0x97):
            st, cln = self._anticoll_level(sel)
            if st != self.MI_OK:
                return None, 0
            st, sak = self._select_level(sel, cln)
            if st != self.MI_OK:
                return None, 0
            final_sak = sak
            if cln[0] == 0x88:
                uid.extend(cln[1:4])
            else:
                uid.extend(cln[0:4])
            if not (sak & 0x04):
                return uid, final_sak
        return uid or None, final_sak

    def _write_page(self, page_addr: int, data: bytes) -> bool:
        """Write exactly 4 bytes to a Type 2 tag page via the NTAG WRITE command (0xA2).

        The tag must already be selected (anticoll + select done by the caller).
        Returns True on ACK (0x0A, 4 bits), False on NACK or communication error.
        """
        frame = [0xA2, page_addr & 0xFF] + list((data + b"\x00\x00\x00\x00")[:4])
        frame += self._crc_a(frame)
        status, back_data, back_bits = self.transceive(self.PCD_TRANSCEIVE, frame)
        ok = (
            status == self.MI_OK
            and back_bits == 4
            and len(back_data) >= 1
            and (back_data[0] & 0x0F) == 0x0A
        )
        self._dbg("mfrc522.write_page page=%d ok=%s" % (page_addr, ok))
        return ok

    def _write_type2_ndef(self, text: str) -> bool:
        """Write an NDEF text TLV to an already-selected Type 2 / NTAG tag.

        The caller is responsible for REQA, anticoll/select, and halting.
        """
        tlv = _encode_ndef_text_tlv(text)
        num_pages = (len(tlv) + 3) // 4
        for i in range(num_pages):
            chunk = tlv[i * 4:(i + 1) * 4]
            if not self._write_page(4 + i, chunk):
                self._dbg("mfrc522._write_type2_ndef failed at page=%d" % (4 + i))
                return False
        self._dbg("mfrc522._write_type2_ndef ok pages=%d" % num_pages)
        return True

    def _write_mifare_block(self, block_addr: int, data: bytes) -> bool:
        """Write 16 bytes to a MIFARE Classic block using the 2-phase WRITE (0xA0) protocol.

        The sector must already be authenticated.  Returns True on double ACK.
        """
        # Phase 1: MIFARE WRITE command + block address → expect 4-bit ACK (0x0A)
        frame1 = [0xA0, block_addr & 0xFF]
        frame1 += self._crc_a(frame1)
        st, back1, bits1 = self.transceive(self.PCD_TRANSCEIVE, frame1)
        if st != self.MI_OK or bits1 != 4 or not back1 or (back1[0] & 0x0F) != 0x0A:
            self._dbg("mfrc522._write_mifare_block phase1 NACK block=%d" % block_addr)
            return False
        # Phase 2: 16 data bytes → expect 4-bit ACK
        frame2 = list((data + b"\x00" * 16)[:16])
        frame2 += self._crc_a(frame2)
        st, back2, bits2 = self.transceive(self.PCD_TRANSCEIVE, frame2)
        ok = st == self.MI_OK and bits2 == 4 and back2 and (back2[0] & 0x0F) == 0x0A
        self._dbg("mfrc522._write_mifare_block block=%d ok=%s" % (block_addr, ok))
        return ok

    def _write_mifare_classic_json(self, uid: list, text: str) -> bool:
        """Write JSON *text* to MIFARE Classic data blocks using the default key A (0xFF×6).

        Writes starting at sector 1 (absolute block 4), using 3 data blocks per sector
        (48 bytes).  Sector trailer blocks are skipped.  Each sector is authenticated
        individually before its data blocks are written.

        Returns True on full success, False on any auth or write failure.
        """
        data = text.encode("utf-8")
        padded_len = ((len(data) + 15) // 16) * 16
        data_padded = data.ljust(padded_len, b"\x00")
        uid_bytes = bytes(uid[:4])
        default_key = b"\xFF\xFF\xFF\xFF\xFF\xFF"

        offset = 0
        sector = 1  # sector 0 holds manufacturer data; start at sector 1
        while offset < padded_len:
            trailer = sector * 4 + 3
            if not self._auth_mifare_block(0x60, trailer, default_key, uid_bytes):
                self._dbg("mfrc522._write_mifare_classic_json auth failed sector=%d" % sector)
                self._clear_mask(self.Status2Reg, 0x08)
                return False
            for blk in range(3):  # 3 data blocks per sector
                if offset >= padded_len:
                    break
                abs_block = sector * 4 + blk
                chunk = data_padded[offset:offset + 16]
                if not self._write_mifare_block(abs_block, chunk):
                    self._dbg(
                        "mfrc522._write_mifare_classic_json write failed block=%d" % abs_block
                    )
                    self._clear_mask(self.Status2Reg, 0x08)
                    return False
                offset += 16
            self._clear_mask(self.Status2Reg, 0x08)
            sector += 1

        return True

    def write_ndef_text(self, text: str) -> bool:
        """Encode *text* as an NDEF Well-Known Text record and write it to a Type 2 tag.

        Fully self-contained: cycles the RF field off/on first so any tag state left
        by a preceding read_all_tags() call (READY*, HALT, etc.) is cleared — all tags
        return to IDLE and respond normally to REQA.
        Then performs REQA → anticollision → select → write pages (starting at page 4) → halt.
        Returns True on full success, False on any failure (no tag, write error, etc.).
        """
        self.initialize()
        with self.antenna_enabled():
            self._rf_reset()
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                self._dbg("mfrc522.write_ndef_text no tag present")
                return False
            try:
                uid = self._anticoll_and_select()
                if uid is None:
                    self._dbg("mfrc522.write_ndef_text anticoll/select failed")
                    return False
                return self._write_type2_ndef(text)
            finally:
                self.halt_tag()

    def write_tag(self, text: str) -> bool:
        """Write *text* to the tag, auto-detecting the tag type via the SAK byte.

        - **NTAG / Ultralight (SAK 0x00)**: writes as NDEF Well-Known Text TLV
          starting at page 4.
        - **MIFARE Classic 1K/4K (SAK 0x08 / 0x18)**: authenticates each sector
          with the default key A (0xFF×6) and writes the UTF-8 JSON bytes to data
          blocks starting at sector 1 (absolute block 4).

        Fully self-contained: cycles the RF field off/on first so any tag state left
        by a preceding read_all_tags() call (READY*, HALT, etc.) is cleared — all tags
        return to IDLE and respond normally to REQA.
        Then performs REQA → anticollision → select → write → halt in one call.
        Returns True on full success, False on any failure.
        """
        self.initialize()
        with self.antenna_enabled():
            self._rf_reset()
            st, _ = self.request(self.PICC_REQA)
            if st != self.MI_OK:
                self._dbg("mfrc522.write_tag no tag present")
                return False
            try:
                uid, sak = self._anticoll_and_select_with_sak()
                if uid is None:
                    self._dbg("mfrc522.write_tag anticoll/select failed")
                    return False
                if sak & 0x08:
                    # MIFARE Classic 1K (0x08) or 4K (0x18)
                    self._dbg("mfrc522.write_tag type=mifare_classic sak=0x%02X" % sak)
                    return self._write_mifare_classic_json(uid, text)
                else:
                    # NTAG / Ultralight (SAK 0x00)
                    self._dbg("mfrc522.write_tag type=type2 sak=0x%02X" % sak)
                    return self._write_type2_ndef(text)
            finally:
                self.halt_tag()


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


# Backwards-compatible class name
MFRC522Handler = MFRC522Device
