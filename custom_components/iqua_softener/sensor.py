from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, Optional

from homeassistant import config_entries, core
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfVolume, UnitOfTime
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


def _to_datetime(v: Any) -> Any:
    """Parse iQua timestamps to timezone-aware datetime.

    Known format from Ease UI: '30/12/2025 21:38' (DD/MM/YYYY HH:MM).
    Returns timezone-aware datetime in UTC for device_class TIMESTAMP.
    """
    if v is None:
        return None

    # Already a datetime?
    try:
        from datetime import datetime as _dt
        if isinstance(v, _dt):
            return dt_util.as_utc(dt_util.as_local(v))
    except Exception:
        pass

    s = str(v).strip()
    if not s:
        return None

    # Try ISO first (sometimes APIs change)
    try:
        dt = dt_util.parse_datetime(s)
        if dt is not None:
            return dt_util.as_utc(dt_util.as_local(dt))
    except Exception:
        pass

    # DD/MM/YYYY HH:MM (observed)
    for fmt in ("%d/%m/%Y %H:%M", "%d.%m.%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)  # naive local time
            # Attach local timezone and convert to UTC
            dt = dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return dt_util.as_utc(dt)
        except Exception:
            continue

    return None


def _to_float(v: Any) -> Optional[float]:
    """Parse numbers that might come as '3.6 Days', '3,6 Tage', '76.5%' etc."""
    if v is None:
        return None

    s = str(v).strip()

    # normalize decimal comma
    s = s.replace(",", ".")

    # strip common suffixes/units/words from API/UI
    for token in (
        "%",
        "Days",
        "Day",
        "Tage",
        "Tag",
        "days",
        "day",
    ):
        s = s.replace(token, "")

    s = s.strip()

    try:
        return float(s)
    except Exception:
        return None


def _first_numeric_by_key_fragment(
    kv: dict[str, Any],
    *fragments: str,
) -> Optional[float]:
    """Find the first numeric value in kv whose key contains any fragment."""
    if not kv or not fragments:
        return None
    frags = tuple(f.lower() for f in fragments)
    for k, v in kv.items():
        kl = str(k).lower()
        if any(f in kl for f in frags):
            n = _to_float(v)
            if n is not None:
                return n
    return None



def _percent_from_api(raw: Any) -> Optional[float]:
    """Some API values are scaled by 10 (e.g. 765 == 76.5%)."""
    f = _to_float(raw)
    if f is None:
        return None
    # Common pattern: 0..1000 where 1000 == 100.0
    if f > 100:
        f = f / 10.0
    # guard
    if f < 0:
        f = 0
    if f > 100:
        # still too high? give up
        return None
    return f


def _treated_capacity_total_l(operating_capacity_grains: Any, hardness_grains: Any) -> Optional[float]:
    """Compute total treatable water in liters from capacity (grains) and hardness (grains/gal)."""
    cap = _to_float(operating_capacity_grains)
    hard = _to_float(hardness_grains)
    if cap is None or hard is None or hard <= 0:
        return None

    # Many iQua endpoints expose hardness as ppm (mg/L CaCO3). Convert heuristically.
    # 1 gpg ≈ 17.1 ppm.
    if hard > 60:  # values above ~60 are very likely ppm, not grains/gal
        hard = hard / 17.1

    gallons = cap / hard
    liters = gallons * 3.785412
    return liters
def _round(v: Optional[float], ndigits: int) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(float(v), ndigits)
    except Exception:
        return v


def _kv_first_value(
    kv: Dict[str, Any],
    *,
    exact_keys: tuple[str, ...] = (),
    suffixes: tuple[str, ...] = (),
    contains: tuple[str, ...] = (),
) -> Any:
    """Return first non-None value found in kv.

    We try, in order:
      1) exact key matches
      2) key endswith any suffix (case-insensitive)
      3) key contains any substring (case-insensitive)
    """
    if not isinstance(kv, dict):
        return None

    # 1) Exact keys
    for k in exact_keys:
        if k in kv and kv.get(k) is not None:
            return kv.get(k)

    if not (suffixes or contains):
        return None

    # Prepare lowercase view once
    kv_items = list(kv.items())
    for raw_k, raw_v in kv_items:
        if raw_v is None:
            continue
        lk = str(raw_k).lower()
        # 2) Suffix match
        for suf in suffixes:
            if lk.endswith(str(suf).lower()):
                return raw_v
        # 3) Contains match
        for sub in contains:
            if str(sub).lower() in lk:
                return raw_v
    return None


def _salt_monitor_to_percent(raw: Any) -> Optional[float]:
    """Salt monitor level seems 0..50 where 50 == 100%."""
    f = _to_float(raw)
    if f is None:
        return None
    f = max(0.0, min(50.0, f))
    return (f / 50.0) * 100.0


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Parse ISO string into aware datetime."""
    s = _as_str(value)
    if not s:
        return None
    try:
        # iQua uses Z
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_util.UTC)
        return dt
    except Exception:
        return None


# ---------- Base classes ----------

class IquaBaseSensor(SensorEntity, CoordinatorEntity[IquaSoftenerCoordinator], ABC):
    """Base sensor using translations (has_entity_name=True)."""

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

        # stable unique id per device
        self._attr_unique_id = f"{device_uuid}_{description.key}".lower()

    @property
    def device_info(self) -> DeviceInfo:
        """Device card in HA: show model, sw_version, and PWA as serial_number."""
        data = self.coordinator.data or {}
        kv = data.get("kv", {}) if isinstance(data, dict) else {}

        model = _as_str(kv.get("manufacturing_information.model")) or "Softener"
        sw = _as_str(kv.get("manufacturing_information.base_software_version"))
        pwa = _as_str(kv.get("manufacturing_information.pwa"))

        # Device name (avoid UUID + avoid firmware in entity_id slug by using PWA)
        # Example: "iQua Leycosoft Pro 9 (7383865)"
        name = f"iQua {model} ({pwa})" if pwa else f"iQua {model}"

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
        ...


class IquaKVSensor(IquaBaseSensor):
    """Reads a canonical kv key from coordinator.data['kv'][canonical_key]."""

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


class IquaTimestampSensor(IquaBaseSensor):
    """Timestamp sensor: value must be datetime."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        canonical_kv_key: str,
        *,
        transform=None,
    ) -> None:
        super().__init__(coordinator, device_uuid, description)
        self._k = canonical_kv_key
        # Default parser handles both ISO 8601 and iQua formats like '30/12/2025 21:38'
        self._transform = transform or _to_datetime

    def update_from_data(self, data: Dict[str, Any]) -> None:
        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        raw = kv.get(self._k)
        try:
            dt = self._transform(raw)
        except Exception:
            dt = None

        self._attr_native_value = dt



class IquaCalculatedCapacitySensor(IquaBaseSensor):
    """Calculated capacities in liters based on operating capacity and hardness."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        *,
        mode: str,
        round_digits: int = 0,
    ) -> None:
        super().__init__(coordinator, device_uuid, description)
        self._mode = mode  # "total" or "remaining"
        self._round_digits = round_digits
        # Initialize value from the first coordinator payload
        self.update_from_data(coordinator.data or {})


    def update_from_data(self, data: Dict[str, Any]) -> None:
        kv = data.get("kv", {})
        if not isinstance(kv, dict):
            self._attr_native_value = None
            return

        op_cap_raw = _kv_first_value(
            kv,
            exact_keys=(
                # historical / earlier mappings
                "configuration.operating_capacity_grains",
                "configuration_information.operating_capacity_grains",
                "program.operating_capacity_grains",
                "capacity.operating_capacity_grains",
                "operating_capacity_grains",
            ),
            # safety-net: the debug endpoint varies a lot between devices/accounts
            suffixes=("operating_capacity_grains", "operating_capacity"),
            contains=("operating_capacity_grains", "operating_capacity"),
        )

        hardness_raw = _kv_first_value(
            kv,
            exact_keys=(
                "program.hardness_grains",
                "program.hardness",
                "program.hardness_ppm",
                "hardness_grains",
                "hardness",
                "hardness_ppm",
            ),
            suffixes=("hardness_grains", "hardness", "hardness_ppm"),
            contains=("hardness_grains", "hardness", "hardness_ppm"),
        )

        total_l = _treated_capacity_total_l(op_cap_raw, hardness_raw)
        if total_l is None:
            _LOGGER.debug(
                "Calculated capacity (%s) missing operating_capacity_grains/hardness: op_cap=%s hardness=%s",
                self._mode,
                op_cap_raw,
                hardness_raw,
            )
            self._attr_native_value = None
            return

        if self._mode == "total":
            # Prefer coordinator-computed value if available (may include additional normalization)
            val = kv.get("calculated.treated_capacity_total_l", total_l)

        elif self._mode == "remaining_percent":
            # Prefer continuously updated remaining percent based on treated water counter + persisted baseline.
            pct_val = kv.get("calculated.treated_capacity_remaining_percent")
            if pct_val is not None:
                try:
                    val = float(pct_val)
                except Exception:
                    val = None
            else:
                # Derive percent from remaining liters if available
                rem = kv.get("calculated.treated_capacity_remaining_l")
                if rem is not None and total_l > 0:
                    try:
                        val = (float(rem) / float(total_l)) * 100.0
                    except Exception:
                        val = None
                else:
                    # Fallback to cloud-reported remaining percent (may update infrequently)
                    pct_raw = (
                        kv.get("capacity.capacity_remaining_percent")
                        or kv.get("status.capacity_remaining_percent")
                        or kv.get("detail.capacity_remaining_percent")
                        or kv.get("capacity_remaining_percent")
                    )
                    if pct_raw is None:
                        for k, v in kv.items():
                            if isinstance(k, str) and k.endswith(".capacity_remaining_percent"):
                                pct_raw = v
                                break
                    # Some API variants expose the remaining capacity percent as "restkapazitat".
                    if pct_raw is None:
                        pct_raw = _first_numeric_by_key_fragment(kv, "restkapaz", "remaining_capacity_percent")
                    pct = _percent_from_api(pct_raw)
                    if pct is None:
                        _LOGGER.debug(
                            "Calculated capacity (%s) missing remaining percent: raw=%s",
                            self._mode,
                            pct_raw,
                        )
                        self._attr_native_value = None
                        return
                    val = float(pct)

        else:
            # Remaining liters
            # Prefer continuously updated remaining value based on treated water counter + persisted baseline.
            rem = kv.get("calculated.treated_capacity_remaining_l")
            if rem is not None:
                try:
                    val = float(rem)
                except Exception:
                    val = None
            else:
                # Fallback to cloud-reported remaining percent (may update infrequently)
                pct_raw = (
                    kv.get("capacity.capacity_remaining_percent")
                    or kv.get("status.capacity_remaining_percent")
                    or kv.get("detail.capacity_remaining_percent")
                    or kv.get("capacity_remaining_percent")
                )
                if pct_raw is None:
                    for k, v in kv.items():
                        if isinstance(k, str) and k.endswith(".capacity_remaining_percent"):
                            pct_raw = v
                            break
                # Some API variants expose the remaining capacity percent as "restkapazitat".
                if pct_raw is None:
                    pct_raw = _first_numeric_by_key_fragment(kv, "restkapaz", "remaining_capacity_percent")
                pct = _percent_from_api(pct_raw)
                if pct is None:
                    _LOGGER.debug(
                        "Calculated capacity (%s) missing remaining percent and no baseline-derived remaining value: raw=%s",
                        self._mode,
                        pct_raw,
                    )
                    self._attr_native_value = None
                    return
                val = total_l * (pct / 100.0)

        self._attr_native_value = _round(val, self._round_digits)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute values whenever the coordinator updates."""
        self.update_from_data(self.coordinator.data or {})
        self.async_write_ha_state()

class IquaUsagePatternSensor(IquaBaseSensor):
    """
    Weekly table row:
      - state: weekly average (Liters)
      - attrs: Sun..Sat floats
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
        # ================== Customer / Metadata ==================
        IquaTimestampSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="last_message_received",
                translation_key="last_message_received",
                device_class=SensorDeviceClass.TIMESTAMP,
                icon="mdi:message-processing-outline",
            ),
            "customer.time_message_received",
            transform=_to_datetime,
        ),

        # ================== Capacity ==================
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="capacity_remaining_percent",
                translation_key="capacity_remaining_percent",
                entity_registry_enabled_default=False,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "capacity.capacity_remaining_percent",
            transform=_percent_from_api,
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="average_capacity_remaining_at_regen_percent",
                translation_key="average_capacity_remaining_at_regen_percent",
                entity_registry_enabled_default=True,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "capacity.average_capacity_remaining_at_regen_percent",
            round_digits=1,
        ),

        # ================== Water usage ==================
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
                key="untreated_water_total_l",
                translation_key="untreated_water_total_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            "water_usage.untreated_water",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="water_today_l",
                translation_key="water_today_l",
                device_class=None,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
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
                entity_registry_enabled_default=True,
                device_class=None,
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
                key="water_totalizer_l",
                translation_key="water_totalizer_l",
                device_class=SensorDeviceClass.WATER,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            "water_usage.water_totalizer",
            round_digits=1,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="treated_water_available_l",
                translation_key="treated_water_available_l",
                entity_registry_enabled_default=False,
                device_class=None,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            "water_usage.treated_water_left",
            round_digits=1,
        ),

        # --- Calculated remaining capacity (liters) ---
        IquaCalculatedCapacitySensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="calculated_treated_capacity_remaining_l",
                translation_key="calculated_treated_capacity_remaining_l",
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water-check",
            ),
            mode="remaining",
        ),
        # --- Calculated remaining capacity (percent) ---
        IquaCalculatedCapacitySensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="calculated_treated_capacity_remaining_percent",
                translation_key="calculated_treated_capacity_remaining_percent",
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water-percent",
            ),
            mode="remaining_percent",
            round_digits=1,
        ),
        IquaCalculatedCapacitySensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="calculated_treated_capacity_total_l",
                translation_key="calculated_treated_capacity_total_l",
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:water",
            ),
            mode="total",
        ),


# ================== Derived (local, persisted) ==================
IquaKVSensor(
    coordinator,
    device_uuid,
    SensorEntityDescription(
        key="calculated_days_since_last_regen_days",
        translation_key="calculated_days_since_last_regen_days",
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-clock",
    ),
    "calculated.days_since_last_regen_days",
    round_digits=2,
),
IquaKVSensor(
    coordinator,
    device_uuid,
    SensorEntityDescription(
        key="calculated_average_daily_use_l",
        translation_key="calculated_average_daily_use_l",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-sync",
    ),
    "calculated.average_daily_use_l",
    round_digits=1,
),
IquaKVSensor(
    coordinator,
    device_uuid,
    SensorEntityDescription(
        key="calculated_average_days_between_regen_days",
        translation_key="calculated_average_days_between_regen_days",
        native_unit_of_measurement=UnitOfTime.DAYS,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-refresh",
    ),
    "calculated.average_days_between_regen_days",
    round_digits=2,
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

        # ================== Water usage patterns (table) ==================
        IquaUsagePatternSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="daily_water_usage_avg_pattern_l",
                translation_key="daily_water_usage_avg_pattern_l",
                device_class=None,
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
                entity_registry_enabled_default=True,
                device_class=None,
                native_unit_of_measurement=UnitOfVolume.LITERS,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-week",
                suggested_display_precision=1,
            ),
            table_key="daily_water_usage_patterns",
            row_label="Reserved (Liters)",
            round_digits=1,
        ),

        # ================== Salt usage ==================
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
                key="total_salt_efficiency_ppm_per_kg",
                translation_key="total_salt_efficiency_ppm_per_kg",
                entity_registry_enabled_default=True,
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:chart-bell-curve",
                suggested_display_precision=0,
            ),
            "salt_usage.total_salt_efficiency",
            round_digits=0,
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
                entity_registry_enabled_default=True,
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
                entity_registry_enabled_default=True,
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=3,
            ),
            "salt_usage.average_salt_dose_per_recharge",
            round_digits=3,
        ),

        # ================== Rock removed ==================
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="total_rock_removed_kg",
                translation_key="total_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.TOTAL_INCREASING,
                suggested_display_precision=3,
            ),
            "rock_removed.total_rock_removed",
            round_digits=3,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="daily_average_rock_removed_kg",
                translation_key="daily_average_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=3,
            ),
            "rock_removed.daily_average_rock_removed",
            round_digits=3,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="since_regen_rock_removed_kg",
                translation_key="since_regen_rock_removed_kg",
                native_unit_of_measurement=UnitOfMass.KILOGRAMS,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=3,
            ),
            "rock_removed.since_regen_rock_removed",
            round_digits=3,
        ),

        # ================== Regenerations ==================
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
                key="second_backwash_cycles",
                translation_key="second_backwash_cycles",
                state_class=SensorStateClass.TOTAL_INCREASING,
                icon="mdi:repeat",
                suggested_display_precision=0,
            ),
            "regenerations.second_backwash_cycles",
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
                entity_registry_enabled_default=True,
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-range",
                suggested_display_precision=1,
            ),
            "regenerations.average_days_between_recharge_days",
            round_digits=1,
        ),

        # ================== Power outages ==================
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="total_power_outages",
                translation_key="total_power_outages",
                state_class=SensorStateClass.TOTAL_INCREASING,
                icon="mdi:flash-alert",
                suggested_display_precision=0,
            ),
            "power_outages.total_power_outages",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="total_times_power_lost",
                translation_key="total_times_power_lost",
                state_class=SensorStateClass.TOTAL_INCREASING,
                icon="mdi:flash",
                suggested_display_precision=0,
            ),
            "power_outages.total_times_power_lost",
            round_digits=0,
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="days_since_last_time_loss",
                translation_key="days_since_last_time_loss",
                native_unit_of_measurement="d",
                state_class=SensorStateClass.MEASUREMENT,
                icon="mdi:calendar-clock",
                suggested_display_precision=0,
            ),
            "power_outages.days_since_last_time_loss",
            round_digits=0,
        ),
        # longest_recorded_outage is a duration string -> keep as string
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="longest_recorded_outage",
                translation_key="longest_recorded_outage",
                icon="mdi:timer-outline",
            ),
            "power_outages.longest_recorded_outage",
        ),

        # ================== Functional check ==================
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="functional_water_meter_sensor",
                translation_key="functional_water_meter_sensor",
                icon="mdi:water-check",
            ),
            "functional_check.water_meter_sensor",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="functional_computer_board",
                translation_key="functional_computer_board",
                icon="mdi:cpu-64-bit",
            ),
            "functional_check.computer_board",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="functional_cord_power_supply",
                translation_key="functional_cord_power_supply",
                icon="mdi:power-plug",
            ),
            "functional_check.cord_power_supply",
        ),

        # ================== Misc ==================
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="misc_second_output",
                translation_key="misc_second_output",
                icon="mdi:information-outline",
            ),
            "miscellaneous.second_output",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="misc_regeneration_enabled",
                translation_key="misc_regeneration_enabled",
                icon="mdi:check-circle-outline",
            ),
            "miscellaneous.regeneration_enabled",
        ),
        IquaKVSensor(
            coordinator,
            device_uuid,
            SensorEntityDescription(
                key="misc_lockout_status",
                translation_key="misc_lockout_status",
                icon="mdi:lock-open-variant-outline",
            ),
            "miscellaneous.lockout_status",
        ),

        # ================== Program settings ==================
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