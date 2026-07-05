from __future__ import annotations

import asyncio
import logging

from bleak.backends.device import BLEDevice

if __package__ == "":
    from tion_btle.tion import TionException
    from tion_btle.light_family import TionLiteFamily
else:
    from .tion import TionException
    from .light_family import TionLiteFamily

_LOGGER = logging.getLogger(__name__)


class TionS4(TionLiteFamily):
    def __init__(self, mac: str | BLEDevice):
        super().__init__(mac)

        self.modes = ['outside', 'recirculation']

        # Extended 4S telemetry (decoded from the same state frame, see _decode_response).
        # Defaults so _generate_model_specific_json works before the first poll.
        self._pcb_ctl_temp: int = 0
        self._pcb_pwr_temp: int = 0
        self._work_time: int = 0   # seconds
        self._fan_time: int = 0    # seconds
        self._errors: int = 0
        self._max_fan_speed: int = 6
        self._heater_var: int = 0  # heater load, %
        self._airflow_counter: int = 0  # lifetime air counter (raw)
        # Device info (fetched separately via DEV_INFO request, see get_device_info).
        self._device_type: int = 0
        self._fw_version: int = 0
        self._hw_version: int = 0

        if mac == "dummy":
            _LOGGER.info("Dummy mode!")
            self._package_id: int = 0

    @property
    def REQUEST_DEVICE_INFO(self) -> list:
        return [50, 51]  # 0x32 0x33

    @property
    def REQUEST_TIMER(self) -> list:
        return [0x32, 0x34]  # FRAME_TYPE_TIMER_REQ = 0x3432

    @property
    def REQUEST_TIMERS_STATE(self) -> list:
        return [0x32, 0x35]  # FRAME_TYPE_TIMERS_STATE_REQ = 0x3532

    # --- dentra-style correlated transport (request_id matching) ----------------
    def _next_req_id(self) -> int:
        rid = (getattr(self, "_req_id_ctr", 0) + 1) & 0xFFFFFFFF
        self._req_id_ctr = rid
        return rid

    async def _collect_dentra_response(self, want_req_id: int, timeout: float = 4.0) -> bytes | None:
        """Assemble a dentra BLE frame from raw notification packets and return its
        app-payload IFF the echoed request_id matches (else keep waiting). Frame:
        [size(2 LE)][0x3a][random][type(2)][ble_req_id][app_payload][crc16]."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        buf = bytearray()
        started = False
        while loop.time() < deadline:
            if not self._delegation.haveNewData:
                await asyncio.sleep(0.03)
                continue
            pkt = bytes(self._delegation.data)
            if not pkt:
                continue
            marker = pkt[0]
            if marker in (0x80, 0x00):          # single / first
                buf = bytearray(pkt[1:])
                started = True
            elif marker in (0x40, 0xC0) and started:  # middle / last
                buf += pkt[1:]
            else:
                continue
            if started and len(buf) >= 2:
                size = int.from_bytes(buf[0:2], 'little')
                if 0 < size <= len(buf):
                    frame = bytes(buf[:size])
                    started = False
                    buf = bytearray()
                    if len(frame) >= 9 and frame[2] == 0x3A:
                        app = frame[7:-2]  # strip 7-byte header + 2-byte CRC
                        rid = int.from_bytes(app[0:4], 'little') if len(app) >= 4 else -1
                        if len(app) >= 4 and rid == want_req_id:
                            return app
                    # not our frame: keep waiting
        return None

    async def _xfer(self, frame_type: int, tail_payload: bytes, timeout: float = 4.0) -> bytes | None:
        """Send a correlated request (unique request_id) and return the matching response
        app-payload (request_id(4) + ...)."""
        # NB: do NOT drain here — responses lag ~1 request; _collect matches by req_id
        # and skips stale (lower) ids, so a late previous response won't be mismatched.
        rid = self._next_req_id()
        frame = self._build_ble_frame(
            frame_type, rid.to_bytes(4, 'little') + bytes(tail_payload), ble_req_id=rid & 0xFF)
        for pkt in self._fragment_ble(frame):
            await self._try_write(bytearray(pkt))
        return await self._collect_dentra_response(rid, timeout)

    def _command_getTimer_lib(self, timer_id: int) -> bytearray:
        """Lib-style timer read frame — this one DOES select the slot (the dentra
        read frame is ignored by the firmware and always returns slot 4)."""
        body = [TionLiteFamily.SINGLE_PACKET_ID, 0x00, 0x00, self.MAGIC_NUMBER, 0xa1] + \
            [0x32, 0x34] + self.random4 + self.random4 + [timer_id & 0xFF]
        body[1] = len(body) + len(self.CRC) - 1
        return bytearray(body + self.CRC)

    async def read_timer(self, timer_id: int) -> dict | None:
        """Read+decode one timer (link must be open). Uses the lib read frame (selects the
        slot) and correlates by the echoed timer_id in the response (response[0]==id),
        retrying on a crossed/mismatched response."""
        for _ in range(5):
            while self._delegation.haveNewData:
                _ = self._delegation.data
            try:
                await self._try_write(request=self._command_getTimer_lib(timer_id))
                resp = await self._get_data_from_breezer()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("read_timer(%d) io: %s", timer_id, e)
                continue
            if resp is not None and len(resp) >= 8 and resp[0] == (timer_id & 0xFF):
                return self.decode_timer(resp)
            await asyncio.sleep(0.05)
        return None

    @staticmethod
    def decode_timer(r: bytearray) -> dict | None:
        """Decode a timer response (verified on a live 4S):
        [0]=id, [1]=weekday bitmask (bit0=Mon..bit6=Sun), [2]=hours, [3]=minutes,
        [4]=settings (bit0 power,1 sound,2 led,3 heater_mode,4 timer_state/enabled),
        [5]=target temp, [6]=fan speed, [7]=device mode (air source)."""
        if r is None or len(r) < 8:
            return None
        settings = r[4]
        return {
            "id": r[0],
            "enabled": bool(settings & (1 << 4)),
            "days": r[1],
            "hours": r[2],
            "minutes": r[3],
            "time": f"{r[2]:02d}:{r[3]:02d}",
            "target_temp": r[5],
            "fan_speed": r[6],
            "heater": not bool(settings & (1 << 3)),  # bit3 INVERTED: 0=heater on, 1=off (verified vs app)
            "power": bool(settings & (1 << 0)),   # power on/off (start vs end of schedule)
            "device_mode": r[7],
            "settings": settings,
            "raw": bytes(r[:8]).hex(),  # full 8-byte timer for byte-level diagnostics
        }

    async def get_timers(self, count: int = 12, passes: int = 3) -> list[dict]:
        """Read+decode all schedule timers in ONE connection. The BLE link is weak and can
        drop slots, so we make several passes over only the still-missing slots (each slot is
        verified by its echoed id in read_timer), maximising the chance of a complete 12/12."""
        found: dict[int, dict] = {}
        try:
            await self.connect()
            for _pass in range(max(1, passes)):
                missing = [t for t in range(count) if t not in found]
                if not missing:
                    break
                for tid in missing:
                    decoded = None
                    for _ in range(2):
                        try:
                            decoded = await self.read_timer(tid)
                            if decoded is not None:
                                break
                        except Exception as e:  # noqa: BLE001
                            _LOGGER.debug("read_timer(%d) failed: %s", tid, e)
                        await asyncio.sleep(0.1)
                    if decoded is not None:
                        found[tid] = decoded
        finally:
            await self.disconnect()
        return [found[t] for t in sorted(found)]

    @staticmethod
    def _crc16_ccitt_false(data: bytes) -> int:
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
        return crc

    def _build_ble_frame(self, frame_type: int, payload: bytes, ble_req_id: int = 1) -> bytes:
        """Build a dentra-exact BLE frame: [size(2 LE)][0x3a][0xad][type(2 LE)][ble_req_id]
        [payload][crc16-ccitt-false, byte-swapped]. size = total incl size field + crc."""
        body = bytes([0x3A, 0xAD]) + int(frame_type).to_bytes(2, 'little') + \
            bytes([ble_req_id & 0xFF]) + bytes(payload)
        size_val = 2 + len(body) + 2  # size field = total frame length incl size + CRC
        head = size_val.to_bytes(2, 'little') + body
        crc = self._crc16_ccitt_false(head)
        return head + crc.to_bytes(2, 'big')  # bswap16 of LE-stored == big-endian on wire

    @staticmethod
    def _fragment_ble(frame: bytes) -> list:
        if len(frame) <= 19:
            return [bytes([0x80]) + frame]
        packets = [bytes([0x00]) + frame[:19]]
        rest = frame[19:]
        while len(rest) > 19:
            packets.append(bytes([0x40]) + rest[:19])
            rest = rest[19:]
        packets.append(bytes([0xC0]) + rest)
        return packets

    def _command_setTimer_libcrc(self, timer_id: int, timer7: list) -> bytes:
        """WRITE frame that mirrors the proven lib READ layout (timer_id at frame offset
        14, which the firmware uses to select the slot) BUT with a real CRC16 and proper
        fragmentation. dentra's _build_ble_frame header is 7 bytes, so 7 filler bytes
        before timer_id land it at offset 14 (= 6-byte lib header + 8 filler)."""
        rid = self._next_req_id()
        filler = rid.to_bytes(4, 'little') + bytes(3)  # 7 bytes (1st=bleid-equiv pos 6)
        payload = filler + bytes([timer_id & 0xFF]) + bytes(b & 0xFF for b in timer7)
        return self._build_ble_frame(0x3430, payload, ble_req_id=rid & 0xFF)

    async def set_timer(self, timer_id: int, timer7: list) -> bytes | None:
        """Write one schedule timer: lib-style slot selection (offset 14) + real CRC16 +
        fragmentation (the long write frame must be split, unlike the short read frame).
        Targeting is verified by reading the slot back."""
        resp = b""
        frame = self._command_setTimer_libcrc(timer_id, timer7)
        try:
            await self.connect()
            while self._delegation.haveNewData:
                _ = self._delegation.data
            for pkt in self._fragment_ble(frame):
                await self._try_write(bytearray(pkt))
            try:
                resp = await self._get_data_from_breezer()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("set_timer(%d) no response: %s", timer_id, e)
        finally:
            await self.disconnect()
        _LOGGER.info("set_timer(%d) frame=%s -> %s", timer_id, frame.hex(),
                     bytes(resp).hex() if resp else None)
        return resp

    @property
    def SET_PARAMS(self) -> list:
        return [48, 50]  # 0x30 0x32

    @property
    def REQUEST_PARAMS(self) -> list:
        return [50, 50]  # 0x32 0x32

    def _collect_message(self, package: bytearray) -> bool:
        """S4 frame assembler that de-duplicates retransmitted BLE fragments.

        The 4S (or the bluetooth_proxy in between) sometimes delivers the SAME middle
        notification several times. The base Lite assembler blindly appends every
        fragment, which inflates the state frame and shifts its tail — the fields at
        byte offsets 25-30 (errors / max_fan_speed / heater_var) end up read from a
        duplicated middle chunk instead of the real END packet. That surfaced as a
        phantom "Problem" (garbage errors) and a stuck heater_power (heater_var=109 ->
        1090 W) even with the heater off. We drop a fragment whose payload is identical
        to the one just appended, which reconstructs the real 31-byte state frame.
        """
        self._have_full_package = False
        _LOGGER.debug("Got %s from tion", bytes(package).hex())

        pid = package[0]
        if pid == self.FIRST_PACKET_ID or pid == self.SINGLE_PACKET_ID:
            self._data = package
            self._have_full_package = pid == self.SINGLE_PACKET_ID
            self._got_new_sequence = pid == self.FIRST_PACKET_ID
            self._last_fragment = None
        elif pid == self.MIDDLE_PACKET_ID or pid == self.END_PACKET_ID:
            if not self._got_new_sequence:
                _LOGGER.critical("Got %s packet but waiting for a first!",
                                 "middle" if pid == self.MIDDLE_PACKET_ID else "end")
            else:
                fragment = bytes(package[1:])
                if fragment == getattr(self, "_last_fragment", None):
                    _LOGGER.debug("Dropping duplicate BLE fragment %s", fragment.hex())
                else:
                    self._data += bytearray(fragment)
                    self._last_fragment = fragment
                if pid == self.END_PACKET_ID:
                    self._have_full_package = True
                    self._got_new_sequence = False
        else:
            _LOGGER.error("Unknown package id %s", hex(pid))

        if self._have_full_package:
            self._header = self._data[:15]
            self._data = self._data[15:-2]
            self._crc = self._data[-2:]

        return self._have_full_package

    def _decode_response(self, response: bytearray):
        _LOGGER.debug("Data is %s", bytes(response).hex())
        self._last_state_raw = bytes(response).hex()  # full state frame, for diagnostics
        try:
            self._mode = response[2]
            self._heater_temp = response[3]
            self._fan_speed = response[4]
            self._in_temp = self.decode_temperature(response[5])
            self._out_temp = self.decode_temperature(response[6])
            self._filter_remain = int.from_bytes(response[17:20], byteorder='little', signed=False) / 86400
            self._state = response[0] & 1
            self._sound = response[0] >> 1 & 1
            self._light = response[0] >> 2 & 1
            self._heater = True if response[0] >> 4 & 1 == 0 else False
            # Extended telemetry. Byte offsets verified against a live 4S frame and match
            # dentra/esphome-tion's tion4s_state_t: 7/8 PCB temps, 9-24 counters
            # (work/fan/filter seconds), 25-28 error flags, 29 max speed, 30 heater load %.
            if len(response) >= 31:
                self._pcb_ctl_temp = self.decode_temperature(response[7])
                self._pcb_pwr_temp = self.decode_temperature(response[8])
                self._work_time = int.from_bytes(response[9:13], byteorder='little', signed=False)
                self._fan_time = int.from_bytes(response[13:17], byteorder='little', signed=False)
                self._airflow_counter = int.from_bytes(response[21:25], byteorder='little', signed=False)
                self._errors = int.from_bytes(response[25:29], byteorder='little', signed=False)
                self._max_fan_speed = response[29]
                self._heater_var = response[30]
        except IndexError as e:
            raise TionException(
                "s4 _decode_response",
                f"Got bad response from Tion '{response}': {str(e)} while parsing"
            )

    def _generate_model_specific_json(self) -> dict:
        # Heater load (%) -> watts on the common 1000 W 4S element. heater_var is 0 when
        # the heater is off, but guard anyway: clamp to a valid 0-100 % and report 0 W
        # whenever the breezer is not actually heating (off, or fan-only mode) so a stray
        # reading can never surface as phantom heater power.
        heater_load = self._heater_var if 0 <= self._heater_var <= 100 else 0
        heater_power = round(heater_load * 10) if (self._state and self._heater) else 0
        return {
            "light": self.light,
            "heater_power": heater_power,
            "work_time_d": round(self._work_time / 86400, 1),
            "fan_time_d": round(self._fan_time / 86400, 1),
            "pcb_ctl_c": self._pcb_ctl_temp,
            "pcb_pwr_c": self._pcb_pwr_temp,
            "errors": self._errors,
            "max_fan_speed": self._max_fan_speed,
            # Lifetime air passed (m³): the counter accumulates the m³/h rate each second,
            # so volume = counter / 3600 (verified: counter/fan_time ≈ 30 m³/h average).
            "airflow_m3": round(self._airflow_counter / 3600),
        }

    def _encode_request(self, request: dict) -> bytearray:
        def encode_state() -> int:
            """Encode different device states to single status int"""
            #   power   sound   light   heater  true    resetSettings   resetErrorCounter   resetFilterResource
            #   0       1       2       3       4       5               6                   7
            return self._encode_state(request["state"]) | \
                (self._encode_state(request["sound"]) << 1) | \
                (self._encode_state(request["light"]) << 2) | \
                ((not self._encode_state(request["heater"])) << 3) | \
                (True << 4) | \
                (self._encode_state(request.get("reset_errors")) << 6) | \
                (self._encode_state(request.get("reset_filter")) << 7)
        try:
            sign = 181
        except KeyError:
            sign = 0

        return bytearray([0x00, 0x17, 0x00, self.MAGIC_NUMBER, self.random] +
                         self.SET_PARAMS + self.random4 + self.random4 +
                         [
                             encode_state(), 0x00, self._encode_mode(request["mode"]), int(request["heater_temp"]),
                             int(request["fan_speed"])
                         ] +
                         list(sign.to_bytes(2, byteorder='little')) + self.CRC
                         )

    @property
    def _packages(self) -> list:
        return [
            #                                       |           |                                               |
            bytearray([0x00, 0x2f, 0x00, 0x3a, 0x27, 0x31, 0x32, 0x72, 0x7b, 0x64, 0xd7, 0x31, 0xea, 0x58, 0x3a, 0x2f, 0x51, 0x00, 0x19, 0x04]),
            bytearray([0x40, 0x0e, 0x10, 0x1b, 0x26, 0x3b, 0x6e, 0x07, 0x00, 0xfa, 0x4e, 0x07, 0x00, 0x06, 0xff, 0xe5, 0x00, 0xa6, 0xe9, 0x22]),
            bytearray([0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x06, 0x00, 0x98, 0x5d])
        ]

    @property
    def command_getStatus(self) -> bytearray:
        return bytearray([TionLiteFamily.SINGLE_PACKET_ID, 0x10, 0x00, self.MAGIC_NUMBER, 0xa1] +
                         self.REQUEST_PARAMS +
                         self.random4 + self.random4 +
                         self.CRC
                         )

    @property
    def command_getDeviceInfo(self) -> bytearray:
        """Same envelope as command_getStatus but asks for DEV_INFO (0x3332)."""
        return bytearray([TionLiteFamily.SINGLE_PACKET_ID, 0x10, 0x00, self.MAGIC_NUMBER, 0xa1] +
                         self.REQUEST_DEVICE_INFO +
                         self.random4 + self.random4 +
                         self.CRC
                         )

    async def get_device_info(self) -> None:
        """Request the device-info frame and decode firmware/hardware versions."""
        try:
            await self.connect()
            await self._try_write(request=self.command_getDeviceInfo)
            response = await self._get_data_from_breezer()
        finally:
            await self.disconnect()
        self._decode_device_info(response)

    def _decode_device_info(self, response: bytearray) -> None:
        # Verified on a live 4S: the dev-info payload is [0]=?, [1:3] firmware u16,
        # [3:5] hardware u16 (e.g. fw bytes c7 04 -> 0x04C7, shown as "04C7" in the app).
        _LOGGER.debug("DevInfo is %s", bytes(response).hex())
        try:
            if len(response) >= 3:
                self._fw_version = int.from_bytes(response[1:3], byteorder='little', signed=False)
            if len(response) >= 5:
                self._hw_version = int.from_bytes(response[3:5], byteorder='little', signed=False)
        except IndexError:
            pass

    @property
    def fw_version(self) -> str | None:
        """Firmware version as a 4-hex-digit string (e.g. '003C'), or None if unknown."""
        return f"{self._fw_version:04X}" if self._fw_version else None

    @property
    def hw_version(self) -> str | None:
        return f"{self._hw_version:04X}" if self._hw_version else None

    async def set_turbo(self, seconds: int) -> bytes | None:
        """Enable (seconds>0) or cancel (0) the breezer's native turbo mode.

        Uses the dentra-exact BLE frame (FRAME_TYPE_TURBO_SET=0x4130) with a real CRC16 and
        fragmentation — the same construction that unblocked timer writes (the old lib-style
        frame with a dummy CRC was silently rejected). app payload (tion4s_raw_frame_t<
        tion4s_turbo_set_t>) = request_id(4 LE) + time(2 LE) + err(1=0)."""
        rid = self._next_req_id()
        secs = max(0, min(int(seconds), 0xFFFF))
        # The 4S frames carry an 8-byte request region after the frame type (ble_req_id + 4
        # request_id + 3 padding), and the actual data starts at frame offset 14 — exactly as
        # the WORKING timer write needed (timer_id at offset 14). Without the 3 padding bytes
        # the device read `time` from the wrong offset and ran a default ~35 s turbo. Add them
        # so `time` lands at offset 14 and the duration is honored.
        payload = rid.to_bytes(4, 'little') + bytes(3) + secs.to_bytes(2, 'little') + bytes([0x00])
        frame = self._build_ble_frame(0x4130, payload, ble_req_id=1)
        resp = b""
        try:
            await self.connect()
            while self._delegation.haveNewData:
                _ = self._delegation.data
            for pkt in self._fragment_ble(frame):
                await self._try_write(bytearray(pkt))
            try:
                resp = await self._get_data_from_breezer()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("set_turbo(%d) no response: %s", secs, e)
        finally:
            await self.disconnect()
        _LOGGER.info("set_turbo(%d) frame=%s -> %s", secs, frame.hex(),
                     bytes(resp).hex() if resp else None)
        return resp

    @property
    def last_state_raw(self) -> str | None:
        """Hex of the last decoded state frame (diagnostics)."""
        return getattr(self, "_last_state_raw", None)

    async def get_turbo(self) -> dict | None:
        """Read native turbo state via TURBO_REQ (0x4132). Response app payload =
        request_id(4) + tion4s_turbo_t{is_active(1), turbo_time(2 LE seconds), err(1)}."""
        app = None
        try:
            await self.connect()
            while self._delegation.haveNewData:
                _ = self._delegation.data
            app = await self._xfer(0x4132, b"")
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("get_turbo io: %s", e)
        finally:
            await self.disconnect()
        if not app or len(app) < 8:
            _LOGGER.info("get_turbo -> %s", bytes(app).hex() if app else None)
            return None
        out = {
            "is_active": bool(app[4]),
            "turbo_time": int.from_bytes(app[5:7], "little"),
            "err": app[7],
            "raw": bytes(app).hex(),
        }
        _LOGGER.info("get_turbo -> %s", out)
        return out

    @staticmethod
    def _sane_unix(ts: int) -> bool:
        return 946684800 < ts < 4102444800  # year 2000..2100

    async def get_time(self) -> int | None:
        """Read the device RTC via TIME_REQ (0x3632). Tries the dentra frame first
        (request_id-correlated, like get_turbo), then falls back to the lib-style read
        envelope — this firmware ignores the dentra read frame for TIMERs, so TIME may
        behave the same. Response payload carries int64 unix_time LE."""
        self._last_time_raw = None
        ts = None
        try:
            await self.connect()
            while self._delegation.haveNewData:
                _ = self._delegation.data
            app = await self._xfer(0x3632, b"")
            if app is not None and len(app) >= 12:
                self._last_time_raw = bytes(app).hex()
                cand = int.from_bytes(app[4:12], "little", signed=True)
                if self._sane_unix(cand):
                    ts = cand
            if ts is None:
                # lib-style read envelope, same construction as _command_getTimer_lib
                body = [TionLiteFamily.SINGLE_PACKET_ID, 0x00, 0x00, self.MAGIC_NUMBER, 0xa1,
                        0x32, 0x36] + self.random4 + self.random4
                body[1] = len(body) + len(self.CRC) - 1
                await self._try_write(request=bytearray(body + self.CRC))
                resp = await self._get_data_from_breezer()
                if resp:
                    self._last_time_raw = bytes(resp).hex()
                    if len(resp) >= 8:
                        cand = int.from_bytes(resp[0:8], "little", signed=True)
                        if self._sane_unix(cand):
                            ts = cand
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("get_time io: %s", e)
        finally:
            await self.disconnect()
        _LOGGER.info("get_time -> %s (raw=%s)", ts, self._last_time_raw)
        return ts

    async def set_time(self, unix_ts: int) -> bytes | None:
        """Set the device RTC via TIME_SET (0x3630). Same offset-14 quirk as
        set_timer/set_turbo: 3 pad bytes after request_id so unix_time lands at
        frame offset 14 (2-for-2 on this firmware for SET frames)."""
        rid = self._next_req_id()
        payload = rid.to_bytes(4, 'little') + bytes(3) + int(unix_ts).to_bytes(8, 'little', signed=True)
        frame = self._build_ble_frame(0x3630, payload, ble_req_id=1)
        resp = b""
        try:
            await self.connect()
            while self._delegation.haveNewData:
                _ = self._delegation.data
            for pkt in self._fragment_ble(frame):
                await self._try_write(bytearray(pkt))
            try:
                resp = await self._get_data_from_breezer()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("set_time(%d) no response: %s", unix_ts, e)
        finally:
            await self.disconnect()
        _LOGGER.info("set_time(%d) frame=%s -> %s", unix_ts, frame.hex(),
                     bytes(resp).hex() if resp else None)
        return resp
