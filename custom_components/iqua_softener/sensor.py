from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, Optional

from homeassistant import config_entries, core
from homeassistant.core import callback
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfVolume, UnitOfMass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_UUID,
    VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
)

from .coordinator import IquaSoftenerCoordinator

from homeassistant.helpers.entity import DeviceInfo

_LOGGER = logging.getLogger(__name__)


def _kv_float(kv: Dict[str, Any], key: str) -> Optional[float]:
    v = kv.get(key)
    if v is None:
        return None
    s = str(v).strip().replace("%", "").replace("Days", "").strip()
    try:
        return float(s)
    except Exception:
        return None


class IquaBaseSensor(SensorEntity, CoordinatorEntity, ABC):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{device_uuid}_{entity_description.key}".lower()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_from_data(self.coordinator.data)
        self.async_write_ha_state()

    @abstractmethod
    def update_from_data(self, data: Dict[str, Any]) -> None:
        ...


class IquaKVSensor(IquaBaseSensor):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
        kv_key: str,
    ) -> None:
        super().__init__(coordinator, device_uuid, entity_description)
        self._kv_key = kv_key

    def update_from_data(self, data: Dict[str, Any]) -> None:
        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        val = kv.get(self._kv_key)
        if val is None:
            self._attr_native_value = None
            return

        f = _kv_float(kv, self._kv_key)
        self._attr_native_value = f if f is not None else str(val)


class IquaUsagePatternSensor(IquaBaseSensor):
    """
    Represents a table row (Sun..Sat) as state + attributes.

    State: weekly average (float)
    Attributes: Sun..Sat values (floats)
    """

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
        table_key: str,
        row_label: str,
    ) -> None:
        super().__init__(coordinator, device_uuid, entity_description)
        self._table_key = table_key
        self._row_label = row_label

    def update_from_data(self, data: Dict[str, Any]) -> None:
        tables = data.get("tables", {})
        if not isinstance(tables, dict):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        table = tables.get(self._table_key)
        if not isinstance(table, dict):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        col_titles = table.get("column_titles", [])
        rows = table.get("rows", [])
        if not isinstance(col_titles, list) or not isinstance(rows, list):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        row = next((r for r in rows if isinstance(r, dict) and r.get("label") == self._row_label), None)
        if not row:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        values = row.get("values", [])
        if not isinstance(values, list):
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return

        attrs: Dict[str, Any] = {}
        nums: list[float] = []

        for i, day in enumerate(col_titles):
            if i >= len(values):
                break
            try:
                v = float(str(values[i]).strip())
                attrs[str(day)] = v
                nums.append(v)
            except Exception:
                continue

        self._attr_extra_state_attributes = attrs
        self._attr_native_value = (sum(nums) / len(nums)) if nums else None


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    config = hass.data[DOMAIN][config_entry.entry_id]
    device_uuid = config[CONF_DEVICE_UUID]
    coordinator: IquaSoftenerCoordinator = config["coordinator"]

    sensors: list[IquaBaseSensor] = [
        # ---------- Capacity ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_capacity_remaining_percent",
                name="iqua_capacity_remaining_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="capacity_remaining_percent",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_capacity_remaining_at_regen_percent",
                name="iqua_average_capacity_remaining_at_regen_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_capacity_remaining_at_regen",
        ),

        # ---------- Water usage ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_treated_water_liters",
                name="iqua_treated_water_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="treated_water",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_untreated_water_liters",
                name="iqua_untreated_water_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="untreated_water",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_water_today_liters",
                name="iqua_water_today_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="water_today",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_daily_use_liters",
                name="iqua_average_daily_use_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_daily_use",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_water_totalizer_liters",
                name="iqua_water_totalizer_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="water_totalizer",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_treated_water_available_liters",
                name="iqua_treated_water_available_liters",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="treated_water_left",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_current_flow_lpm",
                name="iqua_current_flow_lpm",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water-pump",
            ),
            kv_key="current_flow_rate",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_peak_flow_lpm",
                name="iqua_peak_flow_lpm",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-line",
            ),
            kv_key="peak_flow",
        ),

        # ---------- Water usage history (table) ----------
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_avg_pattern",
                name="iqua_daily_water_usage_avg_pattern",
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Average Usage (Liters)",
        ),
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_reserved_pattern",
                name="iqua_daily_water_usage_reserved_pattern",
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
        ),

        # ---------- Salt usage ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_total_kg",
                name="iqua_salt_total_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="salt_total",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_salt_efficiency_ppm_per_kg",
                name="iqua_total_salt_efficiency_ppm_per_kg",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-bell-curve",
            ),
            kv_key="total_salt_efficiency",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_monitor_level_percent",
                name="iqua_salt_monitor_level_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="salt_monitor_level",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_out_of_salt_days",
                name="iqua_out_of_salt_days",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
            ),
            kv_key="out_of_salt_days",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_salt_dose_per_recharge_kg",
                name="iqua_average_salt_dose_per_recharge_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_salt_dose_per_recharge",
        ),

        # ---------- Rock removed ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_rock_removed_kg",
                name="iqua_total_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="total_rock_removed",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_average_rock_removed_kg",
                name="iqua_daily_average_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="daily_average_rock_removed",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_since_regen_rock_removed_kg",
                name="iqua_since_regen_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="since_regen_rock_removed",
        ),
    ]

    async_add_entities(sensors)