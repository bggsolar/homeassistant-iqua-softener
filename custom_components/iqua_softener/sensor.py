from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from homeassistant import config_entries, core
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, CONF_DEVICE_UUID, VOLUME_FLOW_RATE_LITERS_PER_MINUTE
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)


# ---------- Helpers ----------

def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_float(v: Any) -> Optional[float]:
    """Convert many API strings to float. Returns None if not numeric."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if ":" in s:
        # HH:MM etc.
        return None

    # common junk from API (EN + DE)
    for junk in ("%", "Days", "Day", "Tage", "Tag"):
        s = s.replace(junk, "")
    s = s.strip()

    # German decimal comma -> dot
    s = s.replace(",", ".").strip()

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
        return v


def _salt_monitor_to_percent(raw: Any) -> Optional[float]:
    """salt_monitor_level seems 0..50 where 50 == 100%."""
    f = _to_float(raw)
    if f is None:
        return None
    f = max(0.0, min(50.0, f))
    return (f / 50.0) * 100.0


def _to_datetime(raw: Any) -> Optional[datetime]:
    """Parse time_message_received like '30/12/2025 21:38' -> timezone-aware datetime."""
    s = _as_str(raw)
    if not s:
        return None

    # Most observed format: dd/mm/YYYY HH:MM
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return dt
        except Exception:
            continue

    try:
        dt = dt_util.parse_datetime(s)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt
    except Exception:
        return None


# ---------- Base classes ----------

class IquaBaseSensor(SensorEntity, CoordinatorEntity[IquaSoftenerCoordinator], ABC):
    """Base sensor that initializes from coordinator data."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_uuid = device_uuid
        self._attr_unique_id = f"{device_uuid}_{description.key}".lower()

        # If coordinator already has data (e.g. after first_refresh), initialize state now
        try:
            if isinstance(coordinator.data, dict):
                self.update_from_data(coordinator.data)
        except Exception:
            pass

    @property
    def available(self) -> bool:
        # If the coordinator updates successfully, keep entities available.
        # Missing keys will show as 'unknown' instead of 'unavailable'.
        return bool(self.coordinator.last_update_success)

    @property
    def state_class(self):
        # Guard against non-numeric values being reported as numeric sensors.
        sc = getattr(self.entity_description, "state_class", None)
        if sc and not isinstance(self.native_value, (int, float)):
            return None
        return sc

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # If the coordinator already has data (e.g. after startup first_refresh),
        # initialize the state immediately so we don't stay 'unknown' until the next interval.
        try:
            if getattr(self.coordinator, "data", None):
                self._handle_coordinator_update()
        except Exception:
            # Don't break platform setup due to a single bad value.
            pass

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Ensure state is set at startup (avoids unknown until next poll)
        try:
            if isinstance(self.coordinator.data, dict):
                self.update_from_data(self.coordinator.data)
                self.async_write_ha_state()
        except Exception:
            pass

    @property
    def device_info(self) -> DeviceInfo:
        data = self.coordinator.data or {}
        kv = data.get("kv", {}) if isinstance(data, dict) else {}

        model = _as_str(kv.get("manufacturing_information.model")) or "Softener"
        sw = _as_str(kv.get("manufacturing_information.base_software_version"))
        pwa = _as_str(kv.get("manufacturing_information.pwa"))

        # Device name uses PWA (requested): iQua <model> (<pwa>)
        if pwa:
            name = f"iQua {model} ({pwa})"
        else:
            name = f"iQua {model}"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device_uuid)},
            name=name,
            manufacturer="iQua / EcoWater",
            model=model,
            sw_version=sw,
            serial_number=pwa,
            configuration_url=f"https://app.myiquaapp.com/devices/{self._device_uuid}",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.update_from_data(self.coordinator.data or {})
        self.async_write_ha_state()

    @abstractmethod
    def update_from_data(self, data: Dict[str, Any]) -> None:
        raise NotImplementedError
class IquaKVSensor(IquaBaseSensor):
    """Reads a canonical kv key from coordinator.data['kv'][canonical_kv_key]."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        canonical_kv_key: str,
        *,
        round_digits: Optional[int] = None,
        transform=None,
    ) -> None:
        super().__init__(coordinator, device_uuid, description)
        self._k = canonical_kv_key
        self._round_digits = round_digits
        self._transform = transform

    def update_from_data(self, data: Dict[str, Any]) -> None:
        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        raw = kv.get(self._k)
        if raw is None:
            self._attr_native_value = None
            return

        # numeric if possible else keep string
        val: Any
        f = _to_float(raw)
        val = f if f is not None else raw

        if self._transform is not None:
            try:
                val = self._transform(val)
            except Exception:
                pass

        if isinstance(val, (int, float)) and self._round_digits is not None:
            val = _round(float(val), self._round_digits)

        self._attr_native_value = val


class IquaTimestampSensor(IquaKVSensor):
    """Timestamp sensor (device_class TIMESTAMP)."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        canonical_kv_key: str,
        *,
        parser=_to_datetime,
    ) -> None:
        super().__init__(
            coordinator,
            device_uuid,
            description,
            canonical_kv_key,
            transform=parser,
        )


class IquaUsagePatternSensor(IquaBaseSensor):
    """Weekly table row:
      - state: weekly average (Liters)
      - attrs: Mon..Sun floats (we keep whatever column_titles provide)
    """

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        table_key: str,
        row_label: str,
        *,
        round_digits: int = 1,
    ) -> None:
        super().__init__(coordinator, device_uuid, description)
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
            f = _to_float(values[i])
            if f is None:
                continue
            f = _round(f, self._round_digits)
            attrs[str(day)] = f
            nums.append(f)

        self._attr_extra_state_attributes = attrs
        self._attr_native_value = _round(sum(nums) / len(nums), self._round_digits) if nums else None


# ---------- Setup ----------

async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
) -> None:
    cfg = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: IquaSoftenerCoordinator = cfg["coordinator"]
    device_uuid: str = cfg[CONF_DEVICE_UUID]

    sensors: list[IquaBaseSensor] = [
        # ------------------ Customer / metadata ------------------
        IquaTimestampSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="time_message_received",
                translation_key="time_message_received",
                device_class=SensorDeviceClass.TIMESTAMP,
                icon="mdi:message-text-clock",
            ),
            "customer.time_message_received",
        ),

        # ------------------ Capacity ------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="capacity_remaining_percent",
                translation_key="capacity_remaining_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "capacity.capacity_remaining_percent",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="average_capacity_remaining_at_regen_percent",
                translation_key="average_capacity_remaining_at_regen_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "capacity.average_capacity_remaining_at_regen_percent",
            round_digits=1,
        ),

        # ------------------ Water usage ------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="treated_water_total_l",
                translation_key="treated_water_total_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            "water_usage.treated_water",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="water_today_l",
                translation_key="water_today_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            "water_usage.water_today",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="average_daily_use_l",
                translation_key="average_daily_use_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "water_usage.average_daily_use",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="treated_water_available_l",
                translation_key="treated_water_available_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "water_usage.treated_water_left",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="current_flow_lpm",
                translation_key="current_flow_lpm",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water-pump",
            ),
            "water_usage.current_flow_rate",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="peak_flow_lpm",
                translation_key="peak_flow_lpm",
                native_unit_of_measurement=VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-line",
            ),
            "water_usage.peak_flow",
            round_digits=1,
        ),

        # ------------------ Water usage history (table) ------------------
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="daily_water_usage_avg_pattern_l",
                translation_key="daily_water_usage_avg_pattern_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
                suggested_display_precision=1,
            ),
            table_key="daily_water_usage_patterns",
            row_label="Average Usage (Liters)",
            round_digits=1,
        ),
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="daily_water_usage_reserved_pattern_l",
                translation_key="daily_water_usage_reserved_pattern_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
                suggested_display_precision=1,
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
            round_digits=1,
        ),

        # ------------------ Salt usage ------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="salt_total_kg",
                translation_key="salt_total_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            "salt_usage.salt_total",
            round_digits=2,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="salt_monitor_percent",
                translation_key="salt_monitor_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=0,
            ),
            "salt_usage.salt_monitor_level",
            transform=_salt_monitor_to_percent,
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="out_of_salt_days",
                translation_key="out_of_salt_days",
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
                suggested_display_precision=0,
            ),
            "salt_usage.out_of_salt_days",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="average_salt_dose_per_recharge_kg",
                translation_key="average_salt_dose_per_recharge_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=3,
            ),
            "salt_usage.average_salt_dose_per_recharge",
            round_digits=3,
        ),

        # ------------------ Regenerations ------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="time_in_operation_days",
                translation_key="time_in_operation_days",
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar",
                suggested_display_precision=0,
            ),
            "regenerations.time_in_operation_days",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="total_regens",
                translation_key="total_regens",
                state_class=SensorStateClass.TOTAL_INCREASING,
                icon="mdi:counter",
                suggested_display_precision=0,
            ),
            "regenerations.total_regens",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="manual_regens",
                translation_key="manual_regens",
                state_class=SensorStateClass.TOTAL_INCREASING,
                icon="mdi:waves-arrow-right",
                suggested_display_precision=0,
            ),
            "regenerations.manual_regens",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="time_since_last_recharge_days",
                translation_key="time_since_last_recharge_days",
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
                suggested_display_precision=0,
            ),
            "regenerations.time_since_last_recharge_days",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="average_days_between_recharge_days",
                translation_key="average_days_between_recharge_days",
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-range",
                suggested_display_precision=1,
            ),
            "regenerations.average_days_between_recharge_days",
            round_digits=1,
        ),

        # ------------------ Program settings (strings) ------------------
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="controller_time",
                translation_key="controller_time",
                icon="mdi:clock-outline",
            ),
            "program.controller_time",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="regen_time_remaining",
                translation_key="regen_time_remaining",
                icon="mdi:timer-outline",
            ),
            "program.regen_time_remaining",
        ),
    ]

    async_add_entities(sensors)
