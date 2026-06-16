"""Numbers for Tion breezers."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode, NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the Tion numbers."""
    tion_instance: TionInstance = hass.data[DOMAIN][config.unique_id]
    async_add_entities([TionHeaterTempNumber(tion_instance)])
    return True


class TionHeaterTempNumber(NumberEntity, CoordinatorEntity):
    """Heater target temperature (0–25 °C). Convenience control mirroring the climate setpoint."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "heater_temp"
    _attr_icon = "mdi:thermometer"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 25
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-heater_temp"
        self._attr_device_info = instance.device_info

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("heater_temp")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.set(heater_temp=int(value))
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True
