"""The Tion breezer component."""
from __future__ import annotations

from bleak.backends.device import BLEDevice
import asyncio
import datetime
import logging
import math
from functools import cached_property

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothCallbackMatcher
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
# Vendored fork of tion_btle (see vendor/tion_btle) — we maintain the decoder ourselves
# to expose the full 4S state frame, so the integration no longer depends on the PyPI package.
from .vendor import tion_btle
from .vendor.tion_btle.tion import Tion, MaxTriesExceededError
from .const import DOMAIN, TION_SCHEMA, CONF_KEEP_ALIVE, CONF_AWAY_TEMP, CONF_MAC, PLATFORMS
from . import robust_connection
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, SupportsResponse

_LOGGER = logging.getLogger(__name__)

# Use HA's bleak-retry-connector for all BLE connections (see robust_connection).
robust_connection.apply()

# Weekday -> device day-bitmask bit (bit0=Mon .. bit5=Sat, bit6=Sun), verified on a 4S.
_WEEKDAY_NAMES = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}


def _parse_days(value) -> int:
    """Return the device day bitmask from an int mask, a list of weekday names
    (en 3-letter or ru 2-letter) and/or ISO numbers (1=Mon..7=Sun), or a comma/space
    separated string of those."""
    if isinstance(value, int):
        return value & 0x7F
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    mask = 0
    for v in (value or []):
        if isinstance(v, int):
            if 1 <= v <= 7:
                mask |= 1 << (v - 1)
            continue
        s = str(v).strip().lower()
        if not s:
            continue
        if s.isdigit():
            n = int(s)
            if 1 <= n <= 7:
                mask |= 1 << (n - 1)
            continue
        key = s[:3] if s[:3] in _WEEKDAY_NAMES else s[:2]
        if key in _WEEKDAY_NAMES:
            mask |= 1 << _WEEKDAY_NAMES[key]
    return mask & 0x7F


def _parse_hm(value) -> tuple[int, int]:
    """Parse 'HH:MM' (or 'HH:MM:SS') into (hours, minutes)."""
    if isinstance(value, (int, float)):
        return int(value) % 24, 0
    parts = str(value).strip().split(":")
    h = int(parts[0]) if parts and parts[0] else 0
    m = int(parts[1]) if len(parts) > 1 and parts[1] else 0
    return h % 24, m % 60


def _parse_air(value, default: int = 0) -> int:
    """Map an air-source string to device_mode (0=outside, 1=recirculation)."""
    s = str(value).strip().lower()
    if s.startswith("recir") or s.startswith("рец") or s in ("1", "company", "компан"):
        return 1
    if s.startswith("out") or s.startswith("улиц") or s == "0":
        return 0
    return default


def _as_bool(value, default: bool = False) -> bool:
    """Robust bool from a service field — handles real bools and templated strings
    ('true'/'false'/'on'/'off'/'1'/'0') from Lovelace card service calls."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("true", "on", "yes", "1")


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, config_entry: ConfigEntry):
    _LOGGER.info("Setting up %s ", config_entry.unique_id)

    hass.data.setdefault(DOMAIN, {})

    instance = TionInstance(hass, config_entry)
    hass.data[DOMAIN][config_entry.unique_id] = instance
    config_entry.async_on_unload(
        bluetooth.async_register_callback(
            hass=hass,
            callback=instance.update_btle_device,
            match_dict=BluetoothCallbackMatcher(address=instance.config[CONF_MAC], connectable=True),
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    # Forward platforms immediately, then refresh in the background. A slow/failed BLE
    # connect (the breezer can be briefly unreachable, especially on a weaker adapter)
    # must NOT block Home Assistant startup. Entities come up `unavailable` and become
    # available once the coordinator connects (it also keeps retrying on its interval).
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    async def _first_refresh_then_recover():
        await instance.async_refresh()
        # If HA restarted mid-boost, finalize it (restore speed + re-enable paused timers).
        await instance.async_recover_boost()

    config_entry.async_create_background_task(
        hass, _first_refresh_then_recover(), f"{DOMAIN}-first-refresh-{config_entry.unique_id}"
    )

    # Schedule editing service (writes one of the 12 on-device timers).
    if not hass.services.has_service(DOMAIN, "write_timer"):
        async def _svc_write_timer(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            d = call.data
            await inst.async_write_timer(
                int(d["timer_id"]),
                days=int(d.get("days", 0x7F)),
                hours=int(d.get("hours", 0)),
                minutes=int(d.get("minutes", 0)),
                fan_speed=int(d.get("fan_speed", 1)),
                target_temp=int(d.get("target_temp", 25)),
                heater=_as_bool(d.get("heater"), False),
                enabled=_as_bool(d.get("enabled"), True),
                device_mode=int(d.get("device_mode", 0)),
            )

        async def _svc_write_schedule(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            d = call.data
            air = d.get("air")
            device_mode = _parse_air(air, int(d.get("device_mode", 0))) if air is not None \
                else int(d.get("device_mode", 0))
            await inst.async_write_schedule(
                int(d["schedule"]),
                days=d.get("days", 0x7F),
                start=d.get("start", "00:00"),
                end=d.get("end", "00:00"),
                fan_speed=int(d.get("fan_speed", 1)),
                target_temp=int(d.get("target_temp", 25)),
                device_mode=device_mode,
                heater=_as_bool(d.get("heater"), False),
                enabled=_as_bool(d.get("enabled"), True),
            )

        async def _svc_refresh_schedule(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            await inst.async_refresh_schedule()

        async def _svc_set_turbo(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            await inst.async_set_turbo(int(call.data.get("seconds", 0)))

        async def _svc_set_timers_enabled(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            await inst.async_set_all_timers_enabled(bool(call.data.get("enabled", False)))

        async def _svc_read_turbo(call):
            inst = next(iter(hass.data[DOMAIN].values()))
            return await inst.async_read_turbo()

        hass.services.async_register(DOMAIN, "write_timer", _svc_write_timer)
        hass.services.async_register(DOMAIN, "write_schedule", _svc_write_schedule)
        hass.services.async_register(DOMAIN, "refresh_schedule", _svc_refresh_schedule)
        hass.services.async_register(DOMAIN, "set_turbo", _svc_set_turbo)
        hass.services.async_register(DOMAIN, "set_timers_enabled", _svc_set_timers_enabled)
        hass.services.async_register(DOMAIN, "read_turbo", _svc_read_turbo,
                                     supports_response=SupportsResponse.ONLY)
    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry — enables the 'Reload' action in the integration UI
    (no full Home Assistant restart needed to apply changes)."""
    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if unload_ok:
        instance = hass.data.get(DOMAIN, {}).pop(config_entry.unique_id, None)
        if instance is not None:
            # Cancel a pending boost timer and release the held BLE link.
            if getattr(instance, "_boost_unsub", None) is not None:
                instance._boost_unsub()
                instance._boost_unsub = None
            try:
                await instance._reset_link()  # force-drop the held BLE link
            except Exception as e:  # noqa: BLE001 - best effort
                _LOGGER.debug("unload: BLE release failed: %s", e)
        # Drop domain-level services when the last entry is gone.
        if not hass.data.get(DOMAIN):
            for svc in ("write_timer", "write_schedule", "refresh_schedule",
                        "set_turbo", "set_timers_enabled", "read_turbo"):
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)
    return unload_ok


class TionInstance(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):

        self._config_entry: ConfigEntry = config_entry

        assert self.config[CONF_MAC] is not None
        # https://developers.home-assistant.io/docs/network_discovery/#fetching-the-bleak-bledevice-from-the-address
        btle_device = bluetooth.async_ble_device_from_address(hass, self.config[CONF_MAC], connectable=True)
        if btle_device is None:
            raise ConfigEntryNotReady

        self.__keep_alive: int = 90
        try:
            self.__keep_alive = self.config[CONF_KEEP_ALIVE]
        except KeyError:
            pass

        # delay before next update if we got btle.BTLEDisconnectError
        # (kept modest so HA recovers quickly once the breezer is reachable again)
        self._delay: int = 120

        # Hard ceiling for a single poll. The held BLE link can silently die and a
        # reconnect (establish_connection waiting for an advertisement that never comes)
        # or a GATT read can hang forever; DataUpdateCoordinator has no per-update
        # timeout, so a single hung poll would freeze ALL future polling until a HA
        # restart. We bound each poll and, on timeout, drop the stale link so the next
        # poll reconnects from scratch. Must stay below the poll interval.
        self._poll_timeout: int = 60

        # Fetch firmware/hardware version once (separate DEV_INFO request, S4 only).
        self._fw_fetched: bool = False
        # Schedule (12 timers): fetched once, then injected into every response.
        self._schedule_fetched: bool = False
        self._schedule: list = []

        # Boost duration (minutes), settable via the boost_time select (5/10/15).
        self._boost_minutes: int = 10
        self._boost_unsub = None     # cancels the pending boost-revert timer
        self._boost_prev_speed: int = 0
        self._boost_prev_on: bool = False
        self._boost_active: bool = False
        self._boost_end_ts: float = 0   # epoch when the current boost should end
        self._boost_store = None     # homeassistant.helpers.storage.Store (lazy)
        self._post_set_unsub = None  # one-shot refresh scheduled after a set() command

        self.__tion: Tion = self.getTion(self.model, btle_device)
        self.__keep_alive = datetime.timedelta(seconds=self.__keep_alive)
        self._delay = datetime.timedelta(seconds=self._delay)
        self.rssi: int = 0
        # Maintenance mode: when on, HA releases the BLE link and stops polling so the
        # Tion phone app can connect (e.g. for a firmware update). See async_set_maintenance.
        self._maintenance: bool = False

        if self._config_entry.unique_id is None:
            _LOGGER.critical(f"Unique id is None for {self._config_entry.title}! "
                             f"Will fix it by using {self.unique_id}")
            hass.config_entries.async_update_entry(
                entry=self._config_entry,
                unique_id=self.unique_id,
            )
            _LOGGER.critical("Done! Please restart Home Assistant.")

        super().__init__(
            name=self.config['name'] if 'name' in self.config else TION_SCHEMA['name']['default'],
            hass=hass,
            logger=_LOGGER,
            update_interval=self.__keep_alive,
            update_method=self.async_update_state,
        )
        # Platforms are now set up before the first refresh (non-blocking startup), so
        # self.data must be a dict from the start (device_info / set() / entities read it).
        self.data = {}

    @property
    def config(self) -> dict:
        try:
            data = dict(self._config_entry.data or {})
        except AttributeError:
            data = {}

        try:
            options = self._config_entry.options or {}
            data.update(options)
        except AttributeError:
            pass
        return data

    @staticmethod
    def _decode_state(state: str) -> bool:
        return True if state == "on" else False

    async def _reset_link(self) -> None:
        """Force-drop the held BLE link so the next poll reconnects from scratch.

        Our persistent-link patch makes ``_disconnect`` a no-op; here we flip the
        force flag so it really closes. Best-effort: any error is swallowed because
        this runs on the failure path and must not mask the original problem.
        """
        try:
            self.__tion._force_disconnect = True
            await self.__tion._disconnect()
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            _LOGGER.debug("Link reset error (ignored): %s", e)
        finally:
            self.__tion._force_disconnect = False

    async def async_update_state(self):
        if self._maintenance:
            # Don't touch BLE while the app is meant to own the connection.
            self.logger.debug("Maintenance mode is on: skipping breezer poll")
            return self.data
        self.logger.info("Tion instance update started")
        response: dict[str, str | bool | int] = {}

        try:
            # Bound the poll so a dead held link can't hang the coordinator forever
            # (see _poll_timeout). On timeout we reset the link and retry next interval.
            async with asyncio.timeout(self._poll_timeout):
                response = await self.__tion.get()
            self.update_interval = self.__keep_alive

        except MaxTriesExceededError as e:
            _LOGGER.warning("Could not reach breezer (%s); resetting link and will retry", str(e))
            self.update_interval = self._delay
            await self._reset_link()
            raise UpdateFailed("MaxTriesExceededError") from e
        except (asyncio.TimeoutError, TimeoutError) as e:
            # The poll hung past _poll_timeout (dead link / no advertisement / stuck GATT).
            # Drop the stale link so the next poll reconnects cleanly instead of freezing.
            _LOGGER.warning("Breezer poll timed out after %ss; resetting link and will retry",
                            self._poll_timeout)
            self.update_interval = self._delay
            await self._reset_link()
            raise UpdateFailed("poll timeout") from e
        except Exception as e:
            # Any other fetch error (BLE/connection error, no data, ...) must be reported as
            # UpdateFailed so that:
            #  - at first refresh HA raises ConfigEntryNotReady and auto-retries setup
            #    (instead of a dead-end setup_error needing a manual reload), and
            #  - during normal operation the coordinator just retries on the next interval.
            _LOGGER.warning("Error fetching breezer state (%s: %s); resetting link and will retry",
                            type(e).__name__, str(e))
            self.update_interval = self._delay
            await self._reset_link()
            raise UpdateFailed(f"{type(e).__name__}: {e}") from e

        response["is_on"]: bool = self._decode_state(response["state"])
        response["heater"]: bool = self._decode_state(response["heater"])
        response["is_heating"] = self._decode_state(response["heating"])
        response["filter_remain"] = math.ceil(response["filter_remain"])
        response["fan_speed"] = int(response["fan_speed"])
        response["rssi"] = self.rssi

        # One-shot firmware/hardware version read (S4 exposes get_device_info()).
        if not self._fw_fetched and hasattr(self.__tion, "get_device_info"):
            try:
                async with asyncio.timeout(self._poll_timeout):
                    await self.__tion.get_device_info()
                self._fw_fetched = True
            except Exception as e:  # noqa: BLE001 - best effort, retry next poll
                _LOGGER.debug("device-info fetch failed (will retry): %s", e)
        response["fw_version"] = getattr(self.__tion, "fw_version", None)
        response["hw_version"] = getattr(self.__tion, "hw_version", None)
        response["state_raw"] = getattr(self.__tion, "last_state_raw", None)

        # Schedule: fetch all 12 timers once (slow: 12 BLE reads), then reuse cached copy.
        if not self._schedule_fetched and hasattr(self.__tion, "get_timers"):
            try:
                self._schedule = await self.__tion.get_timers()
                self._schedule_fetched = True
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("schedule fetch failed (will retry): %s", e)
        response["schedule"] = self._schedule

        self.logger.debug(f"Result is {response}")
        return response

    async def async_refresh_schedule(self) -> None:
        """Re-read the 12 schedule timers from the breezer (e.g. after editing in the app).

        The BLE read can drop slots on a weak link, so MERGE the fresh read into the cached
        copy by slot id: slots read this time are updated, slots missed keep their last-known
        value (never lose a slot from the display). Successive refreshes converge to 12/12."""
        if not hasattr(self.__tion, "get_timers"):
            return
        fresh = await self.__tion.get_timers()
        by_id = {int(t["id"]): t for t in (self._schedule or [])}
        for t in fresh:
            by_id[int(t["id"])] = t
        self._schedule = [by_id[i] for i in sorted(by_id)]
        self.data["schedule"] = self._schedule
        self.async_update_listeners()

    async def async_write_timer(self, timer_id: int, *, days: int = 0x7F, hours: int = 0,
                                minutes: int = 0, fan_speed: int = 1, target_temp: int = 25,
                                heater: bool = False, enabled: bool = True,
                                device_mode: int = 0, refresh: bool = True) -> None:
        """Write one of the 12 on-device schedule timers, then refresh the cached schedule
        (pass refresh=False to skip the re-read, e.g. when writing both slots of a pair)."""
        if not hasattr(self.__tion, "set_timer"):
            _LOGGER.warning("Schedule timers not supported for this model")
            return
        # settings byte — byte-for-byte as the Tion app writes it (verified on a live 12-slot
        # dump incl. a heater-on sample): bits 0-2 constant; bit4 = enabled; bit3 = heater
        # but INVERTED (0 = heater on, 1 = heater off, same inverted logic as the main state
        # frame). So enabled+heater-off = 0x1f, enabled+heater-on = 0x17, disabled = 0x0f.
        settings = 0b0000_0111       # power|sound|led (always set)
        if enabled:
            settings |= (1 << 4)     # timer_state (enabled)
        if not heater:
            settings |= (1 << 3)     # heater_mode bit: SET means heater OFF
        timer7 = [days & 0x7F, int(hours) & 0xFF, int(minutes) & 0xFF, settings,
                  int(target_temp) & 0xFF, int(fan_speed) & 0xFF, int(device_mode) & 0xFF]
        async with asyncio.timeout(self._poll_timeout):
            await self.__tion.set_timer(int(timer_id), timer7)
        if refresh:
            await self.async_refresh_schedule()

    async def async_write_schedule(self, index: int, *, days, start, end,
                                   fan_speed: int = 1, target_temp: int = 25,
                                   device_mode: int = 0, heater: bool = False,
                                   enabled: bool = True) -> None:
        """Write a full schedule (one of 6) the way the device stores it: two consecutive
        timer slots. Slot 2*index = start (carries the real fan/temp/air); slot 2*index+1
        = stop, where only the end time matters (the app writes fan5/temp30/outside
        defaults there, so we do the same)."""
        idx = int(index)
        if not 0 <= idx <= 5:
            raise ValueError("schedule index must be 0..5")
        start_slot = idx * 2
        daymask = _parse_days(days)
        sh, sm = _parse_hm(start)
        eh, em = _parse_hm(end)
        await self.async_write_timer(
            start_slot, days=daymask, hours=sh, minutes=sm, fan_speed=fan_speed,
            target_temp=target_temp, heater=heater, enabled=enabled,
            device_mode=device_mode, refresh=False)
        await self.async_write_timer(
            start_slot + 1, days=daymask, hours=eh, minutes=em, fan_speed=5,
            target_temp=25, heater=False, enabled=enabled, device_mode=0, refresh=False)
        await self.async_refresh_schedule()

    @property
    def away_temp(self) -> int:
        """Temperature for away mode"""
        return self.config[CONF_AWAY_TEMP] if CONF_AWAY_TEMP in self.config else TION_SCHEMA[CONF_AWAY_TEMP]['default']

    async def set(self, **kwargs):
        if "fan_speed" in kwargs:
            kwargs["fan_speed"] = int(kwargs["fan_speed"])

        original_args = kwargs.copy()
        if "is_on" in kwargs:
            kwargs["state"] = "on" if kwargs["is_on"] else "off"
            del kwargs["is_on"]
        # The library encodes these as "on"/"off" strings; map booleans coming from
        # switch/button entities before handing them over.
        for flag in ("heater", "sound", "light", "reset_filter", "reset_errors"):
            if flag in kwargs and isinstance(kwargs[flag], bool):
                kwargs[flag] = "on" if kwargs[flag] else "off"

        # Heater↔damper dependency: the 4S can't heat recirculated air, so enabling the
        # heater forces the intake to outside (unless the caller already sets a mode).
        if kwargs.get("heater") == "on" and "mode" not in kwargs \
                and self.data.get("mode") == "recirculation":
            _LOGGER.info("Heater requires outside intake; switching air mode to outside")
            kwargs["mode"] = "outside"
            original_args["mode"] = "outside"

        args = ', '.join('%s=%r' % x for x in kwargs.items())
        _LOGGER.info("Need to set: " + args)
        await self.__tion.set(kwargs)
        self.data.update(original_args)
        self.async_update_listeners()
        # Pull fresh device state shortly after a command so derived sensors (heater_power,
        # heating state, temps) reflect the change without waiting for the next full poll.
        if self._post_set_unsub is not None:
            self._post_set_unsub()
        self._post_set_unsub = async_call_later(self.hass, 25, self._post_set_refresh)

    @callback
    def _post_set_refresh(self, _now) -> None:
        self._post_set_unsub = None
        self.hass.async_create_task(self.async_request_refresh())

    @property
    def boost_minutes(self) -> int:
        return self._boost_minutes

    def set_boost_minutes(self, minutes: int) -> None:
        self._boost_minutes = max(1, int(minutes))

    def _get_boost_store(self) -> Store:
        if self._boost_store is None:
            self._boost_store = Store(self.hass, 1, f"{DOMAIN}_boost_{self._config_entry.entry_id}")
        return self._boost_store

    async def _persist_boost_state(self) -> None:
        await self._get_boost_store().async_save({
            "active": self._boost_active,
            "prev_speed": self._boost_prev_speed,
            "prev_on": self._boost_prev_on,
            "end_ts": self._boost_end_ts,
        })

    async def async_start_boost(self) -> None:
        """Official Turbo: run the breezer's NATIVE turbo at max for boost_minutes, then return
        to the previous speed. Verified directly (vs the Tion app): native turbo holds at max
        even with an active schedule and does NOT touch the schedule — so we DON'T pause or
        rewrite the schedule (that was unsafe and unnecessary). An HA timer enforces the exact
        duration (the device's own turbo time is imprecise) by setting the previous speed back.
        """
        if self._boost_unsub is not None:
            self._boost_unsub()
            self._boost_unsub = None
        # Make sure we have a real device state before snapshotting (don't act on empty data
        # right after startup — that would treat the breezer as off and send a spurious manual
        # command that yanks turbo down, then turn it off at the end).
        if "is_on" not in self.data or self.data.get("fan_speed") is None:
            await self.async_request_refresh()
        # Remember what to restore to (current speed if running, else off).
        self._boost_prev_on = bool(self.data.get("is_on"))
        self._boost_prev_speed = int(self.data.get("fan_speed", 0)) if self._boost_prev_on else 0
        secs = self._boost_minutes * 60
        self._boost_end_ts = datetime.datetime.now().timestamp() + secs
        _LOGGER.info("Boost: native turbo %s min (restore to on=%s speed=%s)",
                     self._boost_minutes, self._boost_prev_on, self._boost_prev_speed)
        self._boost_active = True
        await self._persist_boost_state()
        # Native turbo only ramps a RUNNING breezer; if it's off, turn it on AT MAX so the
        # manual turn-on speed equals the turbo speed (a lower manual speed would make turbo
        # revert to it). If it's already running under the schedule, leave it — turbo holds.
        if not self.data.get("is_on"):
            await self.set(fan_speed=6, is_on=True)
        if hasattr(self.__tion, "set_turbo"):
            async with asyncio.timeout(self._poll_timeout):
                await self.__tion.set_turbo(secs)
        else:
            await self.set(fan_speed=6, is_on=True)
        self._boost_unsub = async_call_later(self.hass, secs, self._boost_timer_fired)
        await self.async_request_refresh()

    @callback
    def _boost_timer_fired(self, _now) -> None:
        self._boost_unsub = None
        self.hass.async_create_task(self._async_boost_finished())

    async def _async_boost_finished(self) -> None:
        # Stop turbo by setting an explicit speed (set_turbo 0 does NOT cancel on this firmware).
        # Returns the breezer to its pre-boost speed; the schedule continues from there.
        if self._boost_prev_on and self._boost_prev_speed > 0:
            _LOGGER.info("Boost finished: restoring speed=%s", self._boost_prev_speed)
            await self.set(fan_speed=self._boost_prev_speed, is_on=True)
        else:
            _LOGGER.info("Boost finished: turning breezer off")
            await self.set(is_on=False)
        self._boost_active = False
        await self._get_boost_store().async_remove()

    async def async_recover_boost(self) -> None:
        """After an HA restart mid-boost, end it cleanly: restore the previous speed (stops a
        still-running native turbo). No schedule writes involved."""
        try:
            data = await self._get_boost_store().async_load()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("boost recovery load failed: %s", e)
            return
        if not data or not data.get("active"):
            return
        _LOGGER.warning("Recovering interrupted boost: restoring previous speed")
        self._boost_prev_speed = int(data.get("prev_speed", 0))
        self._boost_prev_on = bool(data.get("prev_on", False))
        self._boost_active = True
        await self._async_boost_finished()

    async def _write_timer_raw(self, raw_hex: str, enabled=None) -> None:
        """Write a timer back from its raw 8-byte snapshot (id + 7 data bytes), optionally
        overriding the enabled bit. Writing from raw avoids any decode/encode field loss."""
        b = bytes.fromhex(raw_hex)
        if len(b) < 8:
            return
        settings = b[4]
        if enabled is True:
            settings |= 0x10
        elif enabled is False:
            settings &= ~0x10
        timer7 = [b[1], b[2], b[3], settings & 0xFF, b[5], b[6], b[7]]
        async with asyncio.timeout(self._poll_timeout):
            await self.__tion.set_timer(b[0], timer7)

    async def _capture_verified_schedule(self) -> list | None:
        """Read the 12 timers TWICE and return the decoded list only if both reads agree
        byte-for-byte (12 slots each). Guards against the flaky BLE read crossing data — we
        must never write back a corrupted snapshot (that scrambled the schedule once)."""
        if not hasattr(self.__tion, "get_timers"):
            return None
        try:
            a = await self.__tion.get_timers()
            b = await self.__tion.get_timers()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("verified schedule read failed: %s", e)
            return None
        ra = {t["id"]: t.get("raw") for t in a}
        rb = {t["id"]: t.get("raw") for t in b}
        if len(ra) != 12 or ra != rb or any(v is None for v in ra.values()):
            _LOGGER.warning("schedule reads inconsistent (%d/%d slots); skipping timer rewrite",
                            len(ra), len(rb))
            return None
        return a

    async def async_set_all_timers_enabled(self, enabled: bool, only_ids=None,
                                           snapshot=None) -> bool:
        """Enable/disable on-device schedule timers, writing from a VERIFIED raw snapshot
        (two agreeing reads) to avoid corrupting the schedule. Returns True on success,
        False if the read could not be verified (no writes performed).

        only_ids: when set, the enabled bit is set ONLY for these slot ids and cleared for
        the rest (restore pre-boost state exactly). snapshot: reuse a previously captured
        verified snapshot instead of re-reading."""
        if not hasattr(self.__tion, "set_timer"):
            _LOGGER.warning("Schedule timers not supported for this model")
            return False
        snap = snapshot if snapshot is not None else await self._capture_verified_schedule()
        if not snap:
            return False
        only = set(only_ids) if only_ids is not None else None
        for t in snap:
            tid = int(t["id"])
            want = (tid in only) if only is not None else enabled
            await self._write_timer_raw(t["raw"], enabled=want)
        await self.async_refresh_schedule()
        return True

    async def async_set_turbo(self, seconds: int) -> None:
        """Invoke the breezer's NATIVE turbo (device-side timer) for `seconds` (0 cancels).
        Test/entry point for the dentra-exact turbo frame; on success this can replace the
        emulated boost."""
        if not hasattr(self.__tion, "set_turbo"):
            _LOGGER.warning("Native turbo not supported for this model")
            return
        async with asyncio.timeout(self._poll_timeout):
            await self.__tion.set_turbo(int(seconds))
        await self.async_request_refresh()

    async def async_read_turbo(self) -> dict:
        """Diagnostics (service response): native turbo state (is_active + remaining seconds)
        + current speed/mode + the last raw state frame. Used to observe what the Tion app's
        Turbo does on the device."""
        turbo = None
        if hasattr(self.__tion, "get_turbo"):
            try:
                async with asyncio.timeout(self._poll_timeout):
                    turbo = await self.__tion.get_turbo()
            except Exception as e:  # noqa: BLE001
                _LOGGER.debug("read_turbo failed: %s", e)
        return {
            "turbo": turbo,
            "fan_speed": self.data.get("fan_speed"),
            "is_on": self.data.get("is_on"),
            "mode": self.data.get("mode"),
            "state_raw": getattr(self.__tion, "last_state_raw", None),
        }

    @staticmethod
    def getTion(model: str, mac: str | BLEDevice) -> tion_btle.TionS3 | tion_btle.TionLite | tion_btle.TionS4:
        if model == 'S3':
            from .vendor.tion_btle.s3 import TionS3 as Breezer
        elif model == 'S4':
            from .vendor.tion_btle.s4 import TionS4 as Breezer
        elif model == 'Lite':
            from .vendor.tion_btle.lite import TionLite as Breezer
        else:
            raise NotImplementedError("Model '%s' is not supported!" % model)
        return Breezer(mac)

    async def connect(self):
        return await self.__tion.connect()

    async def disconnect(self):
        return await self.__tion.disconnect()

    @property
    def maintenance(self) -> bool:
        """True while the BLE link is released for the Tion app."""
        return self._maintenance

    async def async_set_maintenance(self, enabled: bool) -> None:
        """Enter/leave maintenance mode (release the BLE link for the phone app)."""
        self._maintenance = enabled
        if enabled:
            _LOGGER.info("Maintenance mode ON: releasing BLE link for the Tion app")
            try:
                # Force a real disconnect (our persistent _disconnect is normally a no-op).
                self.__tion._force_disconnect = True
                await self.__tion._disconnect()
            except Exception as e:  # noqa: BLE001 - best effort release
                _LOGGER.warning("Maintenance: error releasing link: %s", e)
            finally:
                self.__tion._force_disconnect = False
            self.async_update_listeners()
        else:
            _LOGGER.info("Maintenance mode OFF: reconnecting to breezer")
            await self.async_request_refresh()
            # The app may have edited the schedule while it held the link — re-read it
            # fresh from the device so the cached/displayed schedule tracks the breezer.
            try:
                await self.async_refresh_schedule()
            except Exception as e:  # noqa: BLE001 - best effort
                _LOGGER.debug("post-maintenance schedule refresh failed: %s", e)

    @property
    def device_info(self):
        data = self.data or {}
        pretty = {"S4": "4S", "S3": "3S", "Lite": "Lite"}.get(self.model, self.model)
        info = {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": f"Tion {pretty}",
            "manufacturer": "Tion",
            "model": f"Breezer {pretty}",
        }
        if data.get("fw_version") is not None:
            info['sw_version'] = data.get("fw_version")
        if data.get("hw_version") is not None:
            info['hw_version'] = data.get("hw_version")
        return info

    @cached_property
    def unique_id(self):
        return self.config[CONF_MAC]

    @cached_property
    def supported_air_sources(self) -> list[str]:
        if self.model == "S3":
            return ["outside", "mixed", "recirculation"]
        else:
            return ["outside", "recirculation"]

    @cached_property
    def model(self) -> str:
        try:
            model = self.config['model']
        except KeyError:
            _LOGGER.warning(f"Model was not found in config. "
                            f"Please update integration settings! Config is {self.config}")
            _LOGGER.warning("Assume that model is S3")
            model = 'S3'
        return model

    @callback
    def update_btle_device(
            self,
            service_info: bluetooth.BluetoothServiceInfoBleak,
            _change: bluetooth.BluetoothChange
    ) -> None:
        if service_info.device is not None:
            self.rssi = service_info.rssi
            self.__tion.update_btle_device(service_info.device)
