"""Buttons for Tion breezers."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import TionInstance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the Tion buttons."""
    tion_instance: TionInstance = hass.data[DOMAIN][config.unique_id]
    async_add_entities([TionResetFilterButton(tion_instance), TionBoostButton(tion_instance)])
    return True


class TionBoostButton(ButtonEntity, CoordinatorEntity):
    """Start Turbo for the selected duration (5/10/15 min). Runs the breezer's native turbo
    with the on-device schedule paused for the duration (the schedule would otherwise cancel
    turbo), then restores the previous speed and the schedule."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "boost"
    _attr_icon = "mdi:fan-plus"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-boost"
        self._attr_device_info = instance.device_info

    async def async_press(self) -> None:
        await self.coordinator.async_start_boost()

    @property
    def available(self) -> bool:
        return True


class TionResetFilterButton(ButtonEntity, CoordinatorEntity):
    """Reset the filter resource counter.

    IRREVERSIBLE: zeroes the breezer's filter-life counter (use only after
    physically replacing the filter).
    """

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "reset_filter"
    _attr_icon = "mdi:air-filter"
    _attr_entity_category = EntityCategory.CONFIG
    _confirm_window_s = 10
    _notify_id = "tion_reset_filter_confirm"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-reset_filter"
        self._attr_device_info = instance.device_info
        self._armed_until = None

    async def async_press(self) -> None:
        # Accidental-press protection: the first press only arms; a second press within
        # the confirm window actually resets the (irreversible) filter counter.
        now = dt_util.utcnow()
        if self._armed_until is not None and now <= self._armed_until:
            self._armed_until = None
            persistent_notification.async_dismiss(self.hass, self._notify_id)
            _LOGGER.info("Resetting Tion filter resource counter (confirmed)")
            await self.coordinator.set(reset_filter=True)
        else:
            self._armed_until = now + timedelta(seconds=self._confirm_window_s)
            _LOGGER.warning("Filter reset armed: press again within %ss to confirm", self._confirm_window_s)
            persistent_notification.async_create(
                self.hass,
                f"Нажмите «Сброс ресурса фильтра» ещё раз в течение {self._confirm_window_s} секунд, "
                f"чтобы подтвердить необратимое обнуление счётчика ресурса фильтра.",
                title="Tion: подтвердите сброс фильтра",
                notification_id=self._notify_id,
            )

    @property
    def available(self) -> bool:
        return True
