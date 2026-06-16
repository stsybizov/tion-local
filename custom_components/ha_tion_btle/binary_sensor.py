"""Binary sensors for Tion breezers."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TionInstance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Tion 4S error/warning decode (from dentra/esphome-tion): the 32-bit `errors` field has
# errors EC01..EC11 in bits 0..10 and warnings WS01..WS06 in bits 24..29.
_ERRORS = [
    "При движении заслонки целевой концевой выключатель не меняет состояние в отличие от исходного",
    "При движении заслонки ни один концевой выключатель не меняет состояние",
    "Оба концевых выключателя замкнуты при отсутствии управляющего сигнала на силовой ключ заслонки",
    "Показания выходного датчика отличаются от целевой температуры на 3 °C, входной датчик ниже целевой",
    "Показания выходного датчика меньше целевой температуры на 6 °C, входной датчик ниже целевой",
    "Показания выходного датчика больше целевой температуры на 12 °C, входной датчик ниже целевой",
    "Температура на выходе выше допустимой или замыкание на двух выходных датчиках",
    "Температура на выходе ниже допустимой или обрыв на двух выходных датчиках",
    "Температура на выходе выше допустимой или замыкание на одном выходном датчике",
    "Температура на выходе ниже допустимой или обрыв на одном выходном датчике",
    "Обрыв электрической цепи питания нагревателя",
]
_WARNINGS = [
    "Температура поступающего воздуха выше допустимого значения",
    "Температура поступающего воздуха ниже допустимого значения",
    "Температура платы управления выше допустимого значения",
    "Температура силовой платы выше допустимого значения",
    "Температура платы управления ниже допустимого значения",
    "Температура силовой платы ниже допустимого значения",
]


def _decode_errors(errors: int | None) -> list[str]:
    """List active error/warning codes (e.g. 'EC11: …', 'WS03: …') from the bitmask."""
    if not errors:
        return []
    out = []
    for i, text in enumerate(_ERRORS):          # errors: bits 0..10
        if errors & (1 << i):
            out.append(f"EC{i + 1:02d}: {text}")
    for j, text in enumerate(_WARNINGS):         # warnings: bits 24..29
        if errors & (1 << (24 + j)):
            out.append(f"WS{j + 1:02d}: {text}")
    return out


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the Tion binary sensors."""
    tion_instance: TionInstance = hass.data[DOMAIN][config.unique_id]
    async_add_entities([TionProblemBinarySensor(tion_instance)])
    return True


class TionProblemBinarySensor(BinarySensorEntity, CoordinatorEntity):
    """On when the breezer reports any error/warning flag (errors != 0)."""

    coordinator: TionInstance
    _attr_has_entity_name = True
    _attr_translation_key = "problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-problem"
        self._attr_device_info = instance.device_info

    @property
    def is_on(self) -> bool | None:
        errors = self.coordinator.data.get("errors")
        if errors is None:
            return None
        return errors != 0

    @property
    def extra_state_attributes(self) -> dict:
        errors = self.coordinator.data.get("errors")
        return {
            "error_code": errors,
            "error_code_hex": None if errors is None else f"0x{errors:08X}",
            "problems": _decode_errors(errors),
        }

    @property
    def available(self) -> bool:
        return True
