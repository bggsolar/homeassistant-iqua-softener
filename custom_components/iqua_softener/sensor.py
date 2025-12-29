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
from homeassistant.const import PERCENTAGE, UnitOfVolume, UnitOfMass, UnitOfTime
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_UUID,
    VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
)
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)


def _kv_float(kv: Dict[str, Any], key: str) -> Optional[float]:
    v = kv.get(key)
    if v is None:
        return None
    s = str(v).strip()
    s = s.replace("%", "").replace("Days", "").replace("Day", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _round(v: Optional[float], ndigits: int) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except Exception:
        return None


def _salt_monitor_to_percent(val: Any) -> Optional[float]:
    """Salt monitor seems to be 0..50 where 50 means 100%."""
    try:
        f = float(val)
    except Exception:
        return None
    f = max(0.0, min(50.0, f))
    return (f / 50.0) * 100.0


def _get_kv(data: Any) -> Dict[str, Any]:
    if isinstance(data, dict):
        kv = data.get("kv", {})
        if isinstance(kv, dict):
            return kv
    return {}


class IquaBaseSensor(SensorEntity, CoordinatorEntity, ABC):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._device_uuid = device_uuid
        self._attr_unique_id = f"{device_uuid}_{entity_description.key}".lower()

        # IMPORTANT: lets HA display "Device name" + "Entity name"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        kv = _get_kv(self.coordinator.data)

        model = str(kv.get("model") or "Softener")
        sw_version = kv.get("base_software_version")
        pwa = kv.get("pwa")

        sw_version_str = str(sw_version) if sw_version else None
        pwa_str = str(pwa) if pwa else None

        name = f"iQua {model} ({sw_version_str})" if sw_version_str else f"iQua {model}"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_uuid)},
            name=name,
            manufacturer="iQua / EcoWater",
            model=model,
            sw_version=sw_version_str,
            serial_number=pwa_str,  # PWA sichtbar in Geräteinfos
            configuration_url=f"https://app.myiquaapp.com/devices/{self._device_uuid}",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        try:
            self.update_from_data(self.coordinator.data)
        except Exception:
            _LOGGER.exception("Failed to update sensor %s", self.entity_id)
            self._attr_native_value = None
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
        *,
        round_digits: Optional[int] = None,
        value_transform=None,
    ) -> None:
        super().__init__(coordinator, device_uuid, entity_description)
        self._kv_key = kv_key
        self._round_digits = round_digits
        self._value_transform = value_transform

    def update_from_data(self, data: Dict[str, Any]) -> None:
        kv = _get_kv(data)
        raw = kv.get(self._kv_key)

        if raw is None:
            self._attr_native_value = None
            return

        f = _kv_float(kv, self._kv_key)
        val: Any = f if f is not None else raw

        if self._value_transform is not None:
            val = self._value_transform(val)

        if isinstance(val, (int, float)) and self._round_digits is not None:
            val = _round(float(val), self._round_digits)

        self._attr_native_value = val


class IquaUsagePatternSensor(IquaBaseSensor):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
        table_key: str,
        row_label: str,
        *,
        round_digits: int = 1,
    ) -> None:
        super().__init__(coordinator, device_uuid, entity_description)
        self._table_key = table_key
        self._row_label = row_label
        self._round_digits = round_digits

    def update_from_data(self, data: Dict[str, Any]) -> None:
        tables = data.get("tables", {}) if isinstance(data, dict) else {}
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
                v = round(v, self._round_digits)
                attrs[str(day)] = v
                nums.append(v)
            except Exception:
                continue

        self._attr_extra_state_attributes = attrs
        self._attr_native_value = round(sum(nums) / len(nums), self._round_digits) if nums else None


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    config = hass.data[DOMAIN][config_entry.entry_id]
    device_uuid = config[CONF_DEVICE_UUID]
    coordinator: IquaSoftenerCoordinator = config["coordinator"]

    sensors: list[IquaBaseSensor] = [
        # Capacity
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_capacity_remaining_percent",
                name="Capacity remaining",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="capacity_remaining_percent",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_capacity_remaining_at_regen_percent",
                name="Avg capacity remaining at regen",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_capacity_remaining_at_regen",
            round_digits=1,
        ),

        # Water usage
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_treated_water_liters",
                name="Treated water total",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="treated_water",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_untreated_water_liters",
                name="Untreated water total",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="untreated_water",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_water_today_liters",
                name="Water today",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="water_today",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_daily_use_liters",
                name="Average daily use",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_daily_use",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_water_totalizer_liters",
                name="Water totalizer",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="water_totalizer",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_treated_water_available_liters",
                name="Treated water available",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="treated_water_left",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_current_flow_lpm",
                name="Current flow",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water-pump",
            ),
            kv_key="current_flow_rate",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_peak_flow_lpm",
                name="Peak flow",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-line",
            ),
            kv_key="peak_flow",
            round_digits=1,
        ),

        # Water usage history (table)
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_avg_pattern",
                name="Daily water usage pattern (avg)",
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Average Usage (Liters)",
            round_digits=1,
        ),
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_reserved_pattern",
                name="Daily water usage pattern (reserved)",
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
            round_digits=1,
        ),

        # Salt usage
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_total_kg",
                name="Salt total",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="salt_total",
            round_digits=2,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_salt_efficiency_ppm_per_kg",
                name="Total salt efficiency",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-bell-curve",
            ),
            kv_key="total_salt_efficiency",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_monitor_level_percent",
                name="Salt monitor level",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
            ),
            kv_key="salt_monitor_level",
            value_transform=_salt_monitor_to_percent,
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_out_of_salt_days",
                name="Out of salt (days)",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
                suggested_display_precision=0,
            ),
            kv_key="out_of_salt_days",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_salt_dose_per_recharge_kg",
                name="Avg salt dose per recharge",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_salt_dose_per_recharge",
            round_digits=3,
        ),

        # Rock removed
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_rock_removed_kg",
                name="Total rock removed",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="total_rock_removed",
            round_digits=3,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_average_rock_removed_kg",
                name="Daily avg rock removed",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="daily_average_rock_removed",
            round_digits=3,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_since_regen_rock_removed_kg",
                name="Rock removed since regen",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="since_regen_rock_removed",
            round_digits=3,
        ),
    ]

    async_add_entities(sensors)