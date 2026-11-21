"""Climate entity on HA's native_* temperature API (Core >= 2026.11).

Plain pytest, no pytest-homeassistant-custom-component (it lags HA releases).
Run from the repo root:  python -m pytest tests
Needs only: homeassistant>=2026.11 (the BLE package __init__ is not imported).
"""
import asyncio
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.setup import async_setup_component

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ponytail: stub the package so climate.py/const.py load for real but __init__.py (bleak,
# bluetooth -> usb -> hassio import chain) doesn't; climate.py only needs TionInstance as a name.
_pkg = types.ModuleType("custom_components.ha_tion_btle")
_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "ha_tion_btle")]
_pkg.TionInstance = DataUpdateCoordinator
sys.modules[_pkg.__name__] = _pkg
from custom_components.ha_tion_btle.climate import TionClimateEntity  # noqa: E402


class FakeTion(DataUpdateCoordinator):
    """Stands in for TionInstance: no BLE, set() just updates data."""

    def __init__(self, hass):
        super().__init__(hass, logging.getLogger(__name__), name="fake", config_entry=None)
        self.data = {"heater_temp": 18, "out_temp": 17, "in_temp": 5, "fan_speed": 2,
                     "is_on": True, "heater": True, "air_mode": "outside", "state_raw": "on"}
        self.away_temp = 10
        self.device_info = None
        self.unique_id = "aa:bb:cc:dd:ee:ff"
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.sent = []

    async def set(self, **kwargs):
        self.sent.append(kwargs)
        if "heater_temp" in kwargs:
            self.data["heater_temp"] = kwargs["heater_temp"]


async def _run(config_dir: Path):
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await bootstrap.async_load_base_functionality(hass)
    try:
        assert await async_setup_component(hass, "climate", {})
        tion = FakeTion(hass)
        entity = TionClimateEntity(hass, tion)
        await hass.data["climate"].async_add_entities([entity])
        eid = entity.entity_id

        async def call(service, **data):
            await hass.services.async_call("climate", service, {"entity_id": eid, **data}, blocking=True)

        state = hass.states.get(eid)
        assert state.state == "heat"
        attrs = state.attributes
        assert (attrs["temperature"], attrs["current_temperature"]) == (18, 17)
        assert (attrs["min_temp"], attrs["max_temp"], attrs["target_temp_step"]) == (0, 25, 1)

        await call("set_temperature", temperature=21)
        assert tion.sent[-1] == {"heater_temp": 21}
        assert hass.states.get(eid).attributes["temperature"] == 21

        # away saves 21, drops to away_temp, and restores 21 on the way back
        await call("set_preset_mode", preset_mode="away")
        assert hass.states.get(eid).attributes["temperature"] == 10
        await call("set_preset_mode", preset_mode="none")
        assert hass.states.get(eid).attributes["temperature"] == 21
    finally:
        await hass.async_stop(force=True)


def test_climate_native_temperature(tmp_path, caplog):
    asyncio.run(_run(tmp_path))
    assert "deprecated" not in caplog.text, caplog.text
