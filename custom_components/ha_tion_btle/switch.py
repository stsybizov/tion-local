"""Maintenance switch for Tion breezers.

Turning it on makes Home Assistant release the single BLE connection and stop
polling, so the Tion phone app can connect to the breezer (e.g. to run a
firmware update). Turning it off returns control to HA (reconnect + resume).
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the Tion switches."""
    tion_instance: TionInstance = hass.data[DOMAIN][config.unique_id]
    async_add_entities([
        TionMaintenanceSwitch(tion_instance),
        TionSoundSwitch(tion_instance),
        TionLedSwitch(tion_instance),
    ])
    return True


def _is_on(value) -> bool | None:
    """Interpret a breezer flag that may arrive as an "on"/"off" string (from a poll)
    or as a bool (from our optimistic update in TionInstance.set)."""
    if value is None:
        return None
    return value is True or value == "on"


class _TionFlagSwitch(SwitchEntity, CoordinatorEntity):
    """Base for simple on/off breezer flags backed by coordinator.set(<key>=bool)."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _data_key: str = ""
    _set_key: str = ""

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-{self._data_key}"
        self._attr_device_info = instance.device_info

    @property
    def is_on(self) -> bool | None:
        return _is_on(self.coordinator.data.get(self._data_key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.set(**{self._set_key: True})
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.set(**{self._set_key: False})
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True


class TionSoundSwitch(_TionFlagSwitch):
    """Breezer buzzer/sound notifications."""

    _attr_translation_key = "sound"
    _attr_icon = "mdi:volume-high"
    _data_key = "sound"
    _set_key = "sound"


class TionLedSwitch(_TionFlagSwitch):
    """Breezer LED backlight / light alerts."""

    _attr_translation_key = "led"
    _attr_icon = "mdi:led-on"
    _data_key = "light"
    _set_key = "light"


class TionMaintenanceSwitch(SwitchEntity, CoordinatorEntity):
    """Release the BLE link for the Tion app while on."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "app_access"
    _attr_icon = "mdi:bluetooth-transfer"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-app_access"
        self._attr_device_info = instance.device_info

    @property
    def is_on(self) -> bool:
        return self.coordinator.maintenance

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_maintenance(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_maintenance(False)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # Stay available even while the link is released, so it can be switched back.
        return True
