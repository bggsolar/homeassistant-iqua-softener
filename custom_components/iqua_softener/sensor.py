from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import Any, Dict, Optional, Callable

from homeassistant import config_entries, core
from homeassistant.core import callback
from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntityDescription,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfVolume,
    UnitOfMass,
    UnitOfTime,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_UUID,
    VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
)
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)


# -----------------------------
# Helpers
# -----------------------------
def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip()
    # remove some known decorations
    s = s.replace("%", "").replace("Days", "").replace("Day", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    if f is None:
        return None
    try:
        return int(round(f))
    except Exception:
        return None


def _round(v: Any, ndigits: int) -> Any:
    f = _to_float(v)
    if f is None:
        return None
    try:
        return round(f, ndigits)
    except Exception:
        return f


def _salt_monitor_to_percent(v: Any) -> Optional[int]:
    """
    Salt monitor level seems to be 0..50 where 50 == 100%.
    Convert to 0..100%.
    """
    f = _to_float(v)
    if f is None:
        return None
    if f < 0:
        f = 0
    if f > 50:
        f = 50
    return int(round((f / 50.0) * 100.0))


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


# -----------------------------
# Base entity classes
# -----------------------------
class IquaBaseSensor(SensorEntity, CoordinatorEntity, ABC):
    """
    Base sensor with:
      - stable unique_id: <device_uuid>_<entity_key>
      - shared DeviceInfo (PWA shown as serial_number)
    """

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._device_uuid = device_uuid

        # entity_description.key is already iqua_...
        self._attr_unique_id = f"{device_uuid}_{entity_description.key}".lower()

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data or {}
        kv: Dict[str, Any] = {}
        if isinstance(data, dict):
            kv_candidate = data.get("kv", {})
            if isinstance(kv_candidate, dict):
                kv = kv_candidate

        model = _as_str(kv.get("manufacturing_information.model")) or "Softener"
        sw = _as_str(kv.get("manufacturing_information.base_software_version"))
        pwa = _as_str(kv.get("manufacturing_information.pwa"))

        # Nice device name: iQua <Model> (rX.Y)
        name = f"iQua {model}"
        if sw:
            name = f"{name} ({sw})"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_uuid)},  # UUID internal identifier
            name=name,
            manufacturer="iQua / EcoWater",
            model=model,
            sw_version=sw,
            serial_number=pwa,  # show PWA in UI
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
    """Reads a single canonical kv key from coordinator.data['kv']."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entity_description: SensorEntityDescription,
        kv_key: str,
        *,
        transform: Optional[Callable[[Any], Any]] = None,
        round_digits: Optional[int] = None,
    ) -> None:
        super().__init__(coordinator, device_uuid, entity_description)
        self._kv_key = kv_key
        self._transform = transform
        self._round_digits = round_digits

    def update_from_data(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            self._attr_native_value = None
            return

        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        raw = kv.get(self._kv_key)
        if raw is None:
            self._attr_native_value = None
            return

        val: Any = raw
        if self._transform is not None:
            try:
                val = self._transform(val)
            except Exception:
                val = raw

        if self._round_digits is not None:
            # only round numeric-ish values
            if _to_float(val) is not None:
                val = _round(val, self._round_digits)

        self._attr_native_value = val


class IquaUsagePatternSensor(IquaBaseSensor):
    """
    Weekly table row sensor:
      - State: average of Sun..Sat values
      - Attributes: Sun..Sat
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
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

        if not isinstance(data, dict):
            return

        tables = data.get("tables", {})
        if not isinstance(tables, dict):
            return

        table = tables.get(self._table_key)
        if not isinstance(table, dict):
            return

        col_titles = table.get("column_titles", [])
        rows = table.get("rows", [])
        if not isinstance(col_titles, list) or not isinstance(rows, list):
            return

        row = next(
            (r for r in rows if isinstance(r, dict) and r.get("label") == self._row_label),
            None,
        )
        if not row:
            return

        values = row.get("values", [])
        if not isinstance(values, list):
            return

        attrs: Dict[str, Any] = {}
        nums: list[float] = []

        for i, day in enumerate(col_titles):
            if i >= len(values):
                break
            f = _to_float(values[i])
            if f is None:
                continue
            f = round(f, self._round_digits)
            attrs[str(day)] = f
            nums.append(f)

        self._attr_extra_state_attributes = attrs
        if nums:
            self._attr_native_value = round(sum(nums) / len(nums), self._round_digits)


# -----------------------------
# async_setup_entry
# -----------------------------
async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
):
    config = hass.data[DOMAIN][config_entry.entry_id]
    device_uuid = config[CONF_DEVICE_UUID]
    coordinator: IquaSoftenerCoordinator = config["coordinator"]

    sensors: list[IquaBaseSensor] = [
        # -----------------------------
        # Capacity
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_capacity_remaining_percent",
                translation_key="capacity_remaining_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="capacity.capacity_remaining_percent",
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
            kv_key="capacity.average_capacity_remaining_at_regen_percent",
            round_digits=1,
        ),

        # -----------------------------
        # Water usage
        # -----------------------------
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
            kv_key="water_usage.treated_water",
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
            kv_key="water_usage.untreated_water",
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
            kv_key="water_usage.water_today",
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
            kv_key="water_usage.average_daily_use",
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
            kv_key="water_usage.water_totalizer",
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
            kv_key="water_usage.treated_water_left",
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
            kv_key="water_usage.current_flow_rate",
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
            kv_key="water_usage.peak_flow",
            round_digits=1,
        ),

        # -----------------------------
        # Water usage history (table: daily_water_usage_patterns)
        # -----------------------------
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_daily_water_usage_avg_pattern",
                translation_key="daily_water_usage_avg_pattern",
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
                translation_key="daily_water_usage_reserved_pattern",
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
            round_digits=1,
        ),

        # -----------------------------
        # Salt usage
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_total_kg",
                translation_key="salt_total_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="salt_usage.salt_total",
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
            kv_key="salt_usage.total_salt_efficiency",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_salt_monitor_level_percent",
                translation_key="salt_monitor_level_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            kv_key="salt_usage.salt_monitor_level",
            transform=_salt_monitor_to_percent,
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_out_of_salt_days",
                translation_key="out_of_salt_days",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
            ),
            kv_key="salt_usage.out_of_salt_days",
            transform=_to_int,
            round_digits=None,
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
            kv_key="salt_usage.average_salt_dose_per_recharge",
            round_digits=3,
        ),

        # -----------------------------
        # Rock removed (FIXED: unique, no collision)
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_rock_removed_kg",
                translation_key="total_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            kv_key="rock_removed.total_rock_removed",
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
            kv_key="rock_removed.daily_average_rock_removed",
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
            kv_key="rock_removed.since_regen_rock_removed",
            round_digits=3,
        ),

        # -----------------------------
        # Regenerations
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_time_in_operation_days",
                translation_key="time_in_operation_days",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar",
            ),
            kv_key="regenerations.time_in_operation_days",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_regens",
                translation_key="total_regens",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:counter",
            ),
            kv_key="regenerations.total_regens",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_manual_regens",
                translation_key="manual_regens",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:hand",
            ),
            kv_key="regenerations.manual_regens",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_second_backwash_cycles",
                translation_key="second_backwash_cycles",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:refresh",
            ),
            kv_key="regenerations.second_backwash_cycles",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_time_since_last_recharge_days",
                translation_key="time_since_last_recharge_days",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
            ),
            kv_key="regenerations.time_since_last_recharge_days",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_average_days_between_recharge",
                translation_key="average_days_between_recharge",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
            ),
            kv_key="regenerations.average_days_between_recharge_days",
            # often string "3.6 Days"
            round_digits=1,
        ),

        # -----------------------------
        # Power outages
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_power_outages",
                translation_key="total_power_outages",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:power-plug-off",
            ),
            kv_key="power_outages.total_power_outages",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_total_times_power_lost",
                translation_key="total_times_power_lost",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:flash-off",
            ),
            kv_key="power_outages.total_times_power_lost",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_days_since_last_time_loss",
                translation_key="days_since_last_time_loss",
                native_unit_of_measurement=UnitOfTime.DAYS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar",
            ),
            kv_key="power_outages.days_since_last_time_loss",
            transform=_to_int,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_longest_recorded_outage",
                translation_key="longest_recorded_outage",
                icon="mdi:timer",
            ),
            kv_key="power_outages.longest_recorded_outage",
            # keep as string like "0:00:00"
        ),

        # -----------------------------
        # Functional check
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_water_meter_sensor_status",
                translation_key="water_meter_sensor_status",
                icon="mdi:water-check",
            ),
            kv_key="functional_check.water_meter_sensor",
            transform=_as_str,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_computer_board_status",
                translation_key="computer_board_status",
                icon="mdi:chip",
            ),
            kv_key="functional_check.computer_board",
            transform=_as_str,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_cord_power_supply_status",
                translation_key="cord_power_supply_status",
                icon="mdi:power-plug",
            ),
            kv_key="functional_check.cord_power_supply",
            transform=_as_str,
        ),

        # -----------------------------
        # Miscellaneous
        # -----------------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_second_output",
                translation_key="second_output",
                icon="mdi:toggle-switch",
            ),
            kv_key="miscellaneous.second_output",
            transform=_as_str,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_regeneration_enabled",
                translation_key="regeneration_enabled",
                icon="mdi:check-circle",
            ),
            kv_key="miscellaneous.regeneration_enabled",
            transform=_as_str,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="iqua_lockout_status",
                translation_key="lockout_status",
                icon="mdi:lock-open-variant",
            ),
            kv_key="miscellaneous.lockout_status",
            transform=_as_str,
        ),
    ]

    async_add_entities(sensors)