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
    """Parse numeric values coming as strings like '76.5%' or '3.6 Days'."""
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

    @property
    def device_info(self) -> DeviceInfo:
        kv: Dict[str, Any] = {}
        data = getattr(self.coordinator, "data", None)
        if isinstance(data, dict):
            kv_candidate = data.get("kv", {})
            if isinstance(kv_candidate, dict):
                kv = kv_candidate

        model = str(kv.get("model") or "Softener")
        sw_version = kv.get("base_software_version")
        sw_version_str = str(sw_version) if sw_version else None

        name = f"iQua {model} ({sw_version_str})" if sw_version_str else f"iQua {model}"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_uuid)},
            name=name,
            manufacturer="iQua / EcoWater",
            model=model,
            sw_version=sw_version_str,
            serial_number=self._device_uuid,  # UUID in device infos
            configuration_url=f"https://app.myiquaapp.com/devices/{self._device_uuid}",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_from_data(self.coordinator.data)
        self.async_write_ha_state()

    @abstractmethod
    def update_from_data(self, data: Dict[str, Any]) -> None:
        ...


class IquaKVSensor(IquaBaseSensor):
    """Simple kv sensor: reads a key from data['kv'] and sets native_value (float if possible)."""

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
        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        raw = kv.get(self._kv_key)
        if raw is None:
            self._attr_native_value = None
            return

        f = _kv_float(kv, self._kv_key)

        # Prefer numeric value if possible
        val: Any = f if f is not None else raw

        # Apply transform (e.g. scale conversion)
        if self._value_transform is not None:
            try:
                val = self._value_transform(val)
            except Exception:
                pass

        # Rounding for numeric values
        if isinstance(val, (int, float)) and self._round_digits is not None:
            val = _round(float(val), self._round_digits)

        self._attr_native_value = val


class IquaUsagePatternSensor(IquaBaseSensor):
    """
    Represents a weekly table row (Sun..Sat) as:
      - state: weekly average (float)
      - attributes: Sun..Sat values (floats)
    """

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

        row = next(
            (r for r in rows if isinstance(r, dict) and r.get("label") == self._row_label),
            None,
        )
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


def _salt_monitor_to_percent(val: Any) -> Optional[float]:
    """
    Salt monitor seems to be 0..50 where 50 means 100%.
    Convert to percent (0..100).
    """
    try:
        f = float(val)
    except Exception:
        return None
    # clamp
    if f < 0:
        f = 0
    if f > 50:
        f = 50
    return (f / 50.0) * 100.0


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
                translation_key="capacity_remaining_percent",
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
                translation_key="average_capacity_remaining_at_regen_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_capacity_remaining_at_regen",
            round_digits=1,
        ),

        # ---------- Water usage ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_treated_water_liters",
                translation_key="treated_water_total",
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
                translation_key="untreated_water_total",
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
                translation_key="water_today",
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
                translation_key="average_daily_use",
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
                translation_key="water_totalizer",
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
                translation_key="treated_water_available",
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
                translation_key="current_flow",
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
                translation_key="peak_flow",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-line",
            ),
            kv_key="peak_flow",
            round_digits=1,
        ),

        # ---------- Water usage history (table) ----------
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_avg_pattern",
                translation_key="daily_water_usage_avg_pattern",
                native_unit_of_measurement=UnitOfVolume.LITERS,  # ✅ liters
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Average Usage (Liters)",
            round_digits=1,  # ✅ 1 decimal is enough
        ),
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_reserved_pattern",
                translation_key="daily_water_usage_reserved_pattern",
                native_unit_of_measurement=UnitOfVolume.LITERS,  # ✅ liters
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
            round_digits=1,
        ),

        # ---------- Salt usage ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_total_kg",
                translation_key="salt_total_kg",
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
                translation_key="total_salt_efficiency_ppm_per_kg",
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
                translation_key="salt_monitor_level_percent",
                native_unit_of_measurement=PERCENTAGE,  # ✅ now real percent
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="salt_monitor_level",
            value_transform=_salt_monitor_to_percent,  # ✅ 0..50 -> 0..100%
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_out_of_salt_days",
                translation_key="out_of_salt_days",
                native_unit_of_measurement="d",  # ✅ days
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
                translation_key="average_salt_dose_per_recharge_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="average_salt_dose_per_recharge",
            round_digits=3,
        ),

        # ---------- Rock removed ----------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_rock_removed_kg",
                translation_key="total_rock_removed_kg",
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
                translation_key="daily_average_rock_removed_kg",
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
                translation_key="since_regen_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="since_regen_rock_removed",
            round_digits=3,
        ),
    ]

    async_add_entities(sensors)