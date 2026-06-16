"""
Sensors for Tion breezers
"""
import logging
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorEntityDescription, SensorDeviceClass, SensorStateClass, SensorEntity, RestoreSensor,
)
from homeassistant.const import (
    UnitOfTemperature, UnitOfTime, UnitOfPower, UnitOfEnergy, UnitOfVolume, UnitOfVolumeFlowRate,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import TionInstance
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)

# Manufacturer nominal airflow per fan speed (m³/h); index = fan speed, 0 = off.
# "в зависимости от условий эксплуатации" — these are nominal, so derived volumes are estimates.
AIRFLOW_M3H = {0: 0, 1: 30, 2: 45, 3: 60, 4: 75, 5: 90, 6: 140}

# Tion 4S fan power draw per speed (W) from dentra CONFIGURATION.md; 0 = standby.
FAN_POWER_W = {0: 0.73, 1: 15.1, 2: 16.2, 3: 23.3, 4: 23.8, 5: 25.2, 6: 30.7}


def _current_airflow_m3h(coordinator: TionInstance) -> float:
    """Nominal current productivity (m³/h) from the active fan speed, 0 when off."""
    if not coordinator.data.get("is_on"):
        return 0.0
    speed = int(coordinator.data.get("fan_speed") or 0)
    return float(AIRFLOW_M3H.get(speed, 0))


def _fan_power_w(coordinator: TionInstance) -> float:
    """Fan power draw (W): table by active speed, standby when off."""
    speed = int(coordinator.data.get("fan_speed") or 0) if coordinator.data.get("is_on") else 0
    return FAN_POWER_W.get(speed, FAN_POWER_W[0])


def _total_power_w(coordinator: TionInstance) -> float:
    """Total power draw (W) = heater + fan."""
    return (coordinator.data.get("heater_power") or 0) + _fan_power_w(coordinator)


_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]  # bit0=Mon .. bit6=Sun


def _days_str(mask: int) -> str:
    if mask == 0x7F:
        return "Ежедневно"
    days = [_WEEKDAYS[i] for i in range(7) if mask & (1 << i)]
    return ",".join(days) if days else "—"

SENSOR_TYPES: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="in_temp",
        translation_key="in_temp",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        icon="mdi:import",
    ),
    SensorEntityDescription(
        key="out_temp",
        translation_key="out_temp",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        icon="mdi:export",
    ),
    SensorEntityDescription(
        key="filter_remain",
        translation_key="filter_remain",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:air-filter",
    ),

    SensorEntityDescription(
        key="fan_speed",
        translation_key="current_fan_speed",
        entity_registry_enabled_default=True,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:fan",
    ),
    SensorEntityDescription(
        key="rssi",
        translation_key="rssi",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        entity_registry_enabled_default=False,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:access-point",
    ),
    # --- Tier 2: extra telemetry decoded from the same 4S state frame ---
    SensorEntityDescription(
        key="heater_power",
        translation_key="heater_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        icon="mdi:radiator",
    ),
    SensorEntityDescription(
        key="work_time_d",
        translation_key="work_time",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:timer-outline",
    ),
    SensorEntityDescription(
        key="fan_time_d",
        translation_key="fan_time",
        native_unit_of_measurement=UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:fan-clock",
    ),
    SensorEntityDescription(
        key="pcb_ctl_c",
        translation_key="pcb_ctl_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:thermometer",
    ),
    SensorEntityDescription(
        key="pcb_pwr_c",
        translation_key="pcb_pwr_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:thermometer",
    ),
    SensorEntityDescription(
        key="fw_version",
        translation_key="firmware",
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:chip",
    ),
    SensorEntityDescription(
        key="airflow_m3",
        translation_key="air_passed",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.VOLUME,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_registry_enabled_default=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:weather-windy",
    ),
)


async def async_setup_platform(_hass: HomeAssistant, _config, _async_add_entities, _discovery_info=None):
    _LOGGER.critical("Sensors configuration via configuration.yaml is not supported!")
    return False


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities):
    """Set up the sensor entry"""
    tion_instance = hass.data[DOMAIN][config.unique_id]
    entities: list[SensorEntity] = [
        TionSensor(description, tion_instance) for description in SENSOR_TYPES]
    entities.append(TionEnergySensor(tion_instance))
    entities.append(TionProductivitySensor(tion_instance))
    entities.append(TionFanPowerSensor(tion_instance))
    entities.append(TionTotalPowerSensor(tion_instance))
    entities.append(TionScheduleSensor(tion_instance))
    async_add_entities(entities)

    return True


class TionScheduleSensor(SensorEntity, CoordinatorEntity):
    """Device schedule (12 timers). State = number of active timers; the full active
    schedule is exposed in the `timers` attribute (read-only)."""

    _attr_has_entity_name = True
    _attr_translation_key = "schedule"
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-schedule"
        self._attr_device_info = instance.device_info

    @staticmethod
    def _is_active(t: dict) -> bool:
        """A timer only fires if it is enabled AND has at least one weekday selected.
        The Tion app "deletes" a schedule by zeroing its days (days=0 -> never fires) while
        leaving the slot enabled; such inert slots must not be counted as active."""
        return bool(t.get("enabled")) and int(t.get("days") or 0) != 0

    @property
    def native_value(self) -> int:
        # Number of active schedules (a schedule = a start/stop timer pair).
        active = sum(1 for t in (self.coordinator.data.get("schedule") or []) if self._is_active(t))
        return active // 2

    @property
    def extra_state_attributes(self) -> dict:
        sched = self.coordinator.data.get("schedule") or []
        timers = [
            {
                "id": t["id"],
                "active": self._is_active(t),
                "enabled": t["enabled"],
                "days": _days_str(t["days"]),
                "time": t["time"],
                "fan_speed": t["fan_speed"],
                "target_temp": t["target_temp"],
                "heater": t["heater"],
                "power": t.get("power"),
                "air": "recirculation" if t.get("device_mode") else "outside",
                "raw": t.get("raw"),
                "settings": t.get("settings"),
            }
            for t in sched
        ]
        active_timers = sum(1 for t in sched if self._is_active(t))
        return {"slots_total": len(sched), "active_timers": active_timers, "timers": timers}

    @property
    def available(self) -> bool:
        return True


class TionFanPowerSensor(SensorEntity, CoordinatorEntity):
    """Fan power draw (W), from the active fan speed (manufacturer table)."""

    _attr_has_entity_name = True
    _attr_translation_key = "fan_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:fan"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-fan_power"
        self._attr_device_info = instance.device_info

    @property
    def native_value(self) -> float:
        return round(_fan_power_w(self.coordinator), 1)

    @property
    def available(self) -> bool:
        return True


class TionTotalPowerSensor(SensorEntity, CoordinatorEntity):
    """Total power draw (W) = heater + fan."""

    _attr_has_entity_name = True
    _attr_translation_key = "total_power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:flash"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-total_power"
        self._attr_device_info = instance.device_info

    @property
    def native_value(self) -> float:
        return round(_total_power_w(self.coordinator), 1)

    @property
    def available(self) -> bool:
        return True


class TionProductivitySensor(SensorEntity, CoordinatorEntity):
    """Current breezer productivity (m³/h), from the active fan speed (manufacturer table)."""

    _attr_has_entity_name = True
    _attr_translation_key = "productivity"
    _attr_device_class = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
    _attr_icon = "mdi:weather-windy"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-productivity"
        self._attr_device_info = instance.device_info

    @property
    def native_value(self) -> float:
        return _current_airflow_m3h(self.coordinator)

    @property
    def available(self) -> bool:
        return True


class TionEnergySensor(RestoreSensor, CoordinatorEntity):
    """Cumulative total energy (kWh), integrated from total power (heater + fan).

    The breezer has no energy register, so we Riemann-integrate the total power
    draw across polls and persist the total across restarts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(self, instance: TionInstance):
        CoordinatorEntity.__init__(self=self, coordinator=instance)
        self._attr_unique_id = f"{instance.unique_id}-energy"
        self._attr_device_info = instance.device_info
        self._energy: float = 0.0
        self._last_ts = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            try:
                self._energy = float(last.native_value)
            except (ValueError, TypeError):
                self._energy = 0.0
        self._last_ts = dt_util.utcnow()
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        now = dt_util.utcnow()
        if self._last_ts is not None:
            dt_h = (now - self._last_ts).total_seconds() / 3600.0
            power = _total_power_w(self.coordinator)
            if dt_h > 0 and power:
                self._energy += power * dt_h / 1000.0
        self._last_ts = now
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return round(self._energy, 3)

    @property
    def available(self) -> bool:
        return True


class TionSensor(SensorEntity, CoordinatorEntity):
    """Representation of a sensor."""

    _attr_has_entity_name = True

    def __init__(self, description: SensorEntityDescription, instance: TionInstance):
        """Initialize the sensor."""

        CoordinatorEntity.__init__(
            self=self,
            coordinator=instance,
        )
        self.entity_description = description
        self._attr_device_info = instance.device_info
        self._attr_unique_id = f"{instance.unique_id}-{description.key}"

        _LOGGER.debug(f"Init of sensor {description.key} ({instance.unique_id})")

    @property
    def native_value(self):
        """Return the state of the sensor."""
        value = self.coordinator.data.get(self.entity_description.key)

        if self.entity_description.key == "fan_speed":
            if not self.coordinator.data.get("is_on"):
                # return zero fan speed if breezer turned off
                value = 0

        return value

    def _handle_coordinator_update(self) -> None:
        self._attr_assumed_state = False if self.coordinator.last_update_success else True
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return True
