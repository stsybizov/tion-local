from __future__ import annotations

from homeassistant.components.select import SelectEntityDescription, SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import DOMAIN

INPUT_SELECTS: tuple[SelectEntityDescription, ...] = (
    SelectEntityDescription(
            key="mode",
            translation_key="air_mode",
            icon="mdi:air-filter",
            entity_registry_enabled_default=True,
            entity_category=EntityCategory.CONFIG,
        ),
)


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the sensor entry"""
    tion_instance = hass.data[DOMAIN][config.unique_id]
    entities: list[SelectEntity] = [
        TionInputSelect(description, tion_instance, hass) for description in INPUT_SELECTS]
    entities.append(TionFanSpeedSelect(tion_instance))
    entities.append(TionBoostTimeSelect(tion_instance))
    async_add_entities(entities)

    return True


class TionInputSelect(SelectEntity, CoordinatorEntity):
    coordinator: TionInstance
    _attr_has_entity_name = True

    def select_option(self, option: str) -> None:
        pass

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.set(mode=option)
        self._handle_coordinator_update()

    def __init__(self, description: SelectEntityDescription, instance: TionInstance, hass: HomeAssistant):
        CoordinatorEntity.__init__(self=self, coordinator=instance, )
        self.hass = hass

        self.entity_description = description
        self._attr_device_info = instance.device_info
        self._attr_unique_id = f"{instance.unique_id}-{description.key}"
        self._attr_icon = self.entity_description.icon
        self._attr_entity_registry_enabled_default = self.entity_description.entity_registry_enabled_default
        self._attr_entity_category = self.entity_description.entity_category

        self._attr_options = self.coordinator.supported_air_sources
        self._attr_current_option = self.coordinator.data.get(self.entity_description.key)

    def _handle_coordinator_update(self) -> None:
        self._attr_current_option = self.coordinator.data.get(self.entity_description.key)
        self._attr_assumed_state = False if self.coordinator.last_update_success else True
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True


class TionFanSpeedSelect(SelectEntity, CoordinatorEntity):
    """Discrete fan-speed selector (1-6).

    Gives a clean "pick one of the available speeds" control (no percentage
    slider). Render it as a row of buttons with a Tile card + `select-options`
    feature. Selecting a speed also turns the breezer on.
    """
    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "speed"
    _attr_options = ["1", "2", "3", "4", "5", "6"]
    _attr_icon = "mdi:fan"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-fan_speed_select"
        self._attr_device_info = instance.device_info

    @property
    def current_option(self) -> str | None:
        speed = self.coordinator.data.get("fan_speed")
        # No highlighted speed while the breezer is off.
        if not self.coordinator.data.get("is_on") or not speed:
            return None
        return str(speed)

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.set(fan_speed=int(option), is_on=True)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True


class TionBoostTimeSelect(SelectEntity, CoordinatorEntity):
    """Turbo (boost) duration — 5/10/15 minutes, matching the Tion app's Turbo mode."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "boost_time"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["5", "10", "15"]

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-boost_time"
        self._attr_device_info = instance.device_info

    @property
    def current_option(self) -> str | None:
        return str(self.coordinator.boost_minutes)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.set_boost_minutes(int(option))
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
