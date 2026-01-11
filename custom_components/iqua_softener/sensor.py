from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
import math
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
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_DEVICE_UUID,
    VOLUME_FLOW_RATE_LITERS_PER_MINUTE,
    CONF_HOUSE_WATERMETER_ENTITY,
    CONF_HOUSE_WATERMETER_UNIT_MODE,
    CONF_HOUSE_WATERMETER_FACTOR,
    CONF_RAW_HARDNESS_DH,
    CONF_SOFTENED_HARDNESS_DH,
    DEFAULT_RAW_HARDNESS_DH,
    DEFAULT_SOFTENED_HARDNESS_DH,
    HOUSE_UNIT_MODE_AUTO,
    HOUSE_UNIT_MODE_M3,
    HOUSE_UNIT_MODE_L,
    HOUSE_UNIT_MODE_FACTOR,
    CONF_RAW_SODIUM_MG_L,
    DEFAULT_RAW_SODIUM_MG_L,
    SODIUM_MG_PER_DH,
    SODIUM_LIMIT_MG_L,
    EWMA_TAU_SECONDS,
)
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)


# ---------- Helpers ----------

def _get_merged_entry_data(entry: config_entries.ConfigEntry) -> dict[str, Any]:
    """Merge entry.data + entry.options (options override data)."""
    merged = dict(entry.data)
    if entry.options:
        merged.update(entry.options)
    return merged


def _parse_optional_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


def _detect_unit_factor(unit: Optional[str]) -> Optional[float]:
    """Detect factor to convert given unit to liters.

    Returns:
      1000.0 for m³-like units, 1.0 for liters, None if unknown.
    """
    if not unit:
        return None
    u = str(unit).strip().lower()
    # m³ variants
    if u in {"m³", "m3", "m^3", "cbm", "cubic meters", "cubic meter"}:
        return 1000.0
    # liter variants
    if u in {"l", "ℓ", "liter", "liters", "litre", "litres"}:
        return 1.0
    return None


def _house_total_liters(
    hass: core.HomeAssistant,
    *,
    entity_id: str,
    unit_mode: str,
    factor_opt: Any,
) -> tuple[Optional[float], Optional[float], str]:
    """Read house watermeter from HA and convert to liters.

    Returns:
      (value_liters, factor_used, reason)
    where reason is "ok" or a short error code.
    """
    if not entity_id:
        return None, None, "missing_entity"

    st = hass.states.get(entity_id)
    if st is None:
        return None, None, "entity_not_found"

    if st.state in ("unknown", "unavailable", "none", ""):
        return None, None, "entity_unavailable"

    try:
        raw = float(str(st.state).replace(",", "."))
    except Exception:
        return None, None, "not_numeric"

    factor_used: Optional[float] = None
    mode = (unit_mode or HOUSE_UNIT_MODE_AUTO).strip().lower()
    if mode == HOUSE_UNIT_MODE_M3:
        factor_used = 1000.0
    elif mode == HOUSE_UNIT_MODE_L:
        factor_used = 1.0
    elif mode == HOUSE_UNIT_MODE_FACTOR:
        f = _parse_optional_float(factor_opt)
        if f is None or f <= 0:
            return None, None, "invalid_factor"
        factor_used = f
    else:  # auto
        unit = st.attributes.get("unit_of_measurement")
        factor_used = _detect_unit_factor(unit)
        if factor_used is None:
            return None, None, "unknown_unit"

    return raw * factor_used, factor_used, "ok"

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

# ---------- EWMA (Exponential Moving Average) helpers ----------

def _ewma_update(state: dict[str, Any], x: float, now_ts: float, tau_seconds: float) -> float:
    """Continuous-time EWMA update.

    state: {'value': float|None, 'ts': float|None}
    x: new sample
    now_ts: current timestamp (seconds)
    tau_seconds: time constant
    """
    prev = state.get('value')
    prev_ts = state.get('ts')
    if prev is None or prev_ts is None:
        state['value'] = float(x)
        state['ts'] = float(now_ts)
        return float(x)
    dt = max(float(now_ts) - float(prev_ts), 0.0)
    if dt <= 0.0 or tau_seconds <= 0:
        # no time advanced; keep previous value
        return float(prev)
    # alpha = 1 - exp(-dt/tau)
    alpha = 1.0 - math.exp(-dt / float(tau_seconds))
    new_val = float(prev) + alpha * (float(x) - float(prev))
    state['value'] = new_val
    state['ts'] = float(now_ts)
    return new_val


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


# ---------- Derived calculations (optional) ----------

class IquaDerivedBaseSensor(IquaBaseSensor):
    """Base for sensors that derive values from HA state + iQua data.

    These sensors are optional and will return None (-> unavailable) if
    required inputs are not configured or not valid.
    """

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: SensorEntityDescription,
        *,
        house_entity_id: str,
        house_unit_mode: str,
        house_factor: Any,
        raw_hardness_dh: Any,
        softened_hardness_dh: Any,
    ) -> None:
        super().__init__(coordinator, device_uuid, description)
        self._house_entity_id = house_entity_id
        self._house_unit_mode = house_unit_mode
        self._house_factor = house_factor
        self._raw_hardness_opt = raw_hardness_dh
        self._soft_hardness_opt = softened_hardness_dh

        self._calc_status: str = "disabled"
        self._calc_reason: str = "missing_inputs"
        self._factor_used: Optional[float] = None

    def _read_house_total_l(self) -> Optional[float]:
        v, factor_used, reason = _house_total_liters(
            self.hass,
            entity_id=self._house_entity_id,
            unit_mode=self._house_unit_mode,
            factor_opt=self._house_factor,
        )
        self._factor_used = factor_used
        if reason != "ok":
            self._calc_status = "disabled"
            self._calc_reason = reason
            return None
        return v

    def _read_soft_total_l(self) -> Optional[float]:
        # iQua already reports treated water total in liters
        data = self.coordinator.data or {}
        kv = data.get("kv", {}) if isinstance(data, dict) else {}
        if not isinstance(kv, dict):
            return None
        v = kv.get("water_usage.treated_water")
        return _to_float(v)

    def _read_hardness_inputs(self) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """Read hardness inputs.

        - raw hardness is required (°dH)
        - soft water hardness defaults to 0.0 °dH if not configured
        """
        raw = _parse_optional_float(self._raw_hardness_opt)
        soft = _parse_optional_float(self._soft_hardness_opt)
        if raw is None:
            return None, None, "missing_raw_hardness"
        if soft is None:
            soft = float(DEFAULT_SOFTENED_HARDNESS_DH)
        if raw < 0 or soft < 0:
            return None, None, "invalid_hardness"
        return raw, soft, None

    def _base_attrs(self) -> dict[str, Any]:
        return {
            "calc_status": self._calc_status,
            "calc_reason": self._calc_reason,
            "house_entity": self._house_entity_id,
            "house_unit_mode": self._house_unit_mode,
            "house_factor_used": self._factor_used,
        }


class IquaHouseTotalLitersSensor(IquaDerivedBaseSensor):
    """Normalized house watermeter value in liters (total_increasing)."""

    def update_from_data(self, data: Dict[str, Any]) -> None:
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        house_total_l = self._read_house_total_l()
        if house_total_l is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = self._base_attrs()
            return

        self._attr_native_value = _round(house_total_l, 1)
        self._attr_extra_state_attributes = self._base_attrs()


class IquaDailyCounterSensor(IquaDerivedBaseSensor, RestoreEntity):
    """Daily consumption based on a total_increasing source.

    This does not rely on recorder statistics; it stores the start-of-day total.
    """

    _attr_extra_restore_state_attributes = True

    def __init__(self, *args, source: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._source = source  # 'house' | 'soft' | 'delta'
        self._start_total: Optional[float] = None
        self._start_date: Optional[str] = None  # ISO date

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.attributes:
            self._start_total = _parse_optional_float(last.attributes.get("start_total"))
            self._start_date = last.attributes.get("start_date")

    def _today_iso(self) -> str:
        return dt_util.as_local(dt_util.utcnow()).date().isoformat()

    def _current_total(self) -> Optional[float]:
        if self._source == "house":
            return self._read_house_total_l()
        if self._source == "soft":
            return self._read_soft_total_l()
        if self._source == "delta":
            house_total_l = self._read_house_total_l()
            soft_total_l = self._read_soft_total_l()
            if house_total_l is None or soft_total_l is None:
                return None
            return max(house_total_l - soft_total_l, 0.0)
        return None

    def update_from_data(self, data: Dict[str, Any]) -> None:
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        total = self._current_total()
        if total is None:
            # reason set by _read_house_total_l() if relevant
            if self._source in ("soft", "delta") and self._calc_reason == "ok":
                self._calc_status = "disabled"
                self._calc_reason = "missing_source_total"
            self._attr_native_value = None
            self._attr_extra_state_attributes = {**self._base_attrs(), "start_total": self._start_total, "start_date": self._start_date}
            return

        today = self._today_iso()
        if self._start_date != today or self._start_total is None:
            # new day or first run
            self._start_date = today
            self._start_total = total

        daily = max(total - (self._start_total or 0.0), 0.0)
        self._attr_native_value = _round(daily, 1)
        self._attr_extra_state_attributes = {**self._base_attrs(), "start_total": self._start_total, "start_date": self._start_date}


class IquaTreatedHardnessDailySensor(IquaDerivedBaseSensor):
    """Compute effective outlet hardness for today's water usage (based on measured daily mixing)."""

    def __init__(self, *args, house_daily: IquaDailyCounterSensor, delta_daily: IquaDailyCounterSensor, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._house_daily = house_daily
        self._delta_daily = delta_daily

    def update_from_data(self, data: Dict[str, Any]) -> None:
        # Default attrs
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        raw_h, soft_h, err = self._read_hardness_inputs()
        if err:
            self._calc_status = "disabled"
            self._calc_reason = err
            self._attr_native_value = None
            self._attr_extra_state_attributes = self._base_attrs()
            return

        # Ensure daily sensors are updated from the same coordinator tick
        self._house_daily.update_from_data(data)
        self._delta_daily.update_from_data(data)

        house_today = _parse_optional_float(self._house_daily.native_value)
        delta_today = _parse_optional_float(self._delta_daily.native_value)
        if house_today is None or house_today <= 0 or delta_today is None:
            self._calc_status = "disabled"
            self._calc_reason = "missing_daily_volumes"
            self._attr_native_value = None
            self._attr_extra_state_attributes = {**self._base_attrs(), "raw_hardness_dh": raw_h, "softened_hardness_dh": soft_h}
            return

        roh_frac = max(min(delta_today / house_today, 1.0), 0.0)
        h_mix = (raw_h * roh_frac) + (soft_h * (1.0 - roh_frac))

        self._attr_native_value = _round(h_mix, 2)
        self._attr_extra_state_attributes = {
            **self._base_attrs(),
            "raw_hardness_dh": raw_h,
            "softened_hardness_dh": soft_h,
            "raw_fraction": _round(roh_frac, 4),
        }



class IquaEffectiveHardnessSmoothedSensor(RestoreEntity, IquaDerivedBaseSensor):
    """EWMA-smoothed effective outlet hardness (°dH).

    Uses an *interval* mixing ratio based on delta volumes between coordinator polls
    (avoids daily midnight resets) and applies an exponential moving average.
    """

    def __init__(
        self,
        *args,
        house_daily: IquaDailyCounterSensor,
        delta_daily: IquaDailyCounterSensor,
        ewma_state: dict[str, Any],
        tau_seconds: float,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._house_daily = house_daily
        self._delta_daily = delta_daily
        self._ewma_state = ewma_state
        self._tau_seconds = float(tau_seconds)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state not in (None, "unknown", "unavailable"):
            try:
                v = float(str(last.state).replace(",", "."))
                # initialize EWMA state with restored value
                self._ewma_state["value"] = v
                self._ewma_state["ts"] = datetime.utcnow().timestamp()
            except Exception:
                pass


    def update_from_data(self, data: Dict[str, Any]) -> None:
        """Update the EWMA-smoothed effective hardness.

        Uses *interval* mixing ratio based on delta volumes between coordinator polls:
        - house_total_l (from HA sensor) and
        - softened_total_l (from iQua controller)

        This avoids daily midnight resets and enables a true rolling behavior.
        If inputs are missing or no new water usage occurred, the last valid EWMA value
        is kept (hold-last) instead of resetting to unknown.
        """
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        # Read hardness inputs (raw + softened setpoints)
        raw_h, soft_h, err = self._read_hardness_inputs()
        if err:
            self._calc_status = "disabled"
            self._calc_reason = err
            # hold-last if available
            self._attr_native_value = self._ewma_state.get("value")
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": raw_h,
                "softened_hardness_dh": soft_h,
            }
            return

        # Interval totals
        house_total = self._read_house_total_l()
        soft_total = self._read_soft_total_l()

        if house_total is None or soft_total is None:
            self._calc_status = "disabled"
            self._calc_reason = "missing_totals"
            self._attr_native_value = self._ewma_state.get("value")
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": raw_h,
                "softened_hardness_dh": soft_h,
                "house_total_l": house_total,
                "soft_total_l": soft_total,
            }
            return

        prev_house = self._ewma_state.get("last_house_total")
        prev_soft = self._ewma_state.get("last_soft_total")

        # Initialize interval tracking on first run (or after restart)
        if prev_house is None or prev_soft is None:
            self._ewma_state["last_house_total"] = float(house_total)
            self._ewma_state["last_soft_total"] = float(soft_total)
            self._calc_reason = "init_interval"
            self._attr_native_value = self._ewma_state.get("value")
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": raw_h,
                "softened_hardness_dh": soft_h,
                "house_total_l": float(house_total),
                "soft_total_l": float(soft_total),
            }
            return

        # Detect counter resets (e.g., daily reset or controller reset) and re-baseline.
        if float(house_total) < float(prev_house) or float(soft_total) < float(prev_soft):
            self._ewma_state["last_house_total"] = float(house_total)
            self._ewma_state["last_soft_total"] = float(soft_total)
            self._calc_reason = "counter_reset_rebaseline"
            self._attr_native_value = self._ewma_state.get("value")
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": raw_h,
                "softened_hardness_dh": soft_h,
                "house_total_l": float(house_total),
                "soft_total_l": float(soft_total),
            }
            return

        delta_house = max(float(house_total) - float(prev_house), 0.0)
        delta_soft = max(float(soft_total) - float(prev_soft), 0.0)

        # If we see water usage but the treated-water counter did not advance,
        # we cannot derive a reliable mixing ratio. Hold the last value to avoid spikes.
        if delta_house > 0.0 and delta_soft <= 0.0:
            self._calc_reason = "treated_counter_stale"
            self._attr_native_value = self._ewma_state.get("value")
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": raw_h,
                "softened_hardness_dh": soft_h,
                "delta_house_l": float(delta_house),
                "delta_soft_l": float(delta_soft),
                "house_total_l": float(house_total),
                "soft_total_l": float(soft_total),
            }
            # Still persist totals to allow recovery on next tick
            self._ewma_state["last_house_total"] = float(house_total)
            self._ewma_state["last_soft_total"] = float(soft_total)
            return

        # Persist current totals for next interval
        self._ewma_state["last_house_total"] = float(house_total)
        self._ewma_state["last_soft_total"] = float(soft_total)

        # ---------------------------------------------------------------------
        # no_usage (hard HOLD)
        # ---------------------------------------------------------------------
        # If there is no new house usage since the last coordinator tick, we have
        # *no new information* about mixing. In that case, the physically correct
        # behavior is to HOLD the last known "effective hardness (smoothed)" value.
        #
        # Important: we must NOT time-advance the EWMA here, otherwise the EWMA
        # may drift up/down without any water flow (pure time artifact).
        if delta_house <= 0.0:
            self._calc_reason = "no_usage"

            prev = self._ewma_state.get("value")
            if prev is None:
                # First run / no restored value yet: use today's effective hardness
                # as a neutral initialization baseline (does not advance EWMA time).
                house_today = float(self._house_daily.native_value or 0.0)
                delta_today = float(self._delta_daily.native_value or 0.0)
                roh_frac_today = max(min((delta_today / house_today), 1.0), 0.0) if house_today > 0.0 else 0.0
                prev = (float(raw_h) * roh_frac_today) + (float(soft_h) * (1.0 - roh_frac_today))
                self._ewma_state["value"] = float(prev)
                # Do not set/advance ts here.

            self._attr_native_value = _round(float(prev), 2) if prev is not None else None
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": float(raw_h),
                "softened_hardness_dh": float(soft_h),
                "delta_house_l": float(delta_house),
                "delta_soft_l": float(delta_soft),
                "held_value_dh": _round(float(prev), 2) if prev is not None else None,
            }
            return
        # Treated (softened) total can be stale (controller updates delayed).
        # If house usage increased but softened total did not move at all in this interval,
        # we cannot compute a valid interval mixing ratio.
        #
        # Behavior:
        # - Re-baseline EWMA to *today's* effective hardness (daily mixing) to avoid a poisoned EWMA drifting to raw hardness
        # - Then hard hold-last (no EWMA update from this interval)
        if delta_house > 0.0 and delta_soft <= 0.0:
            # Compute today's effective hardness using daily counters (robust across midnight)
            house_today = float(self._house_daily.native_value or 0.0)
            delta_today = float(self._delta_daily.native_value or 0.0)

            if house_today > 0.0:
                roh_frac_today = max(min(delta_today / house_today, 1.0), 0.0)
            else:
                roh_frac_today = 0.0

            h_today = (float(raw_h) * roh_frac_today) + (float(soft_h) * (1.0 - roh_frac_today))

            now_ts = datetime.utcnow().timestamp()
            # Re-baseline EWMA to today's effective hardness (prevents drift towards raw hardness)
            self._ewma_state["value"] = float(h_today)
            self._ewma_state["ts"] = float(now_ts)

            self._calc_reason = "treated_counter_stale"
            self._attr_native_value = _round(float(h_today), 2)
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": float(raw_h),
                "softened_hardness_dh": float(soft_h),
                "effective_hardness_today_dh": _round(float(h_today), 2),
                "raw_fraction_today": _round(float(roh_frac_today), 4),
                "house_today_l": _round(float(house_today), 1),
                "delta_today_l": _round(float(delta_today), 1),
                "delta_house_l": float(delta_house),
                "delta_soft_l": float(delta_soft),
                "house_total_l": float(house_total),
                "soft_total_l": float(soft_total),
                "tau_seconds": float(self._tau_seconds),
                "ewma_ts": float(self._ewma_state.get("ts") or now_ts),
            }
            return

        delta_raw = max(delta_house - delta_soft, 0.0)
        roh_frac = max(min(delta_raw / delta_house, 1.0), 0.0)

        h_eff = (float(raw_h) * roh_frac) + (float(soft_h) * (1.0 - roh_frac))

        # ---------------------------------------------------------------------
        # Poison / plausibility guard
        # ---------------------------------------------------------------------
        # Even in "ok" mode, protect against impossible mixing results caused by
        # counter glitches or unit mismatches. Effective hardness must be within
        # [softened_hardness, raw_hardness] (with a tiny epsilon).
        lo = min(float(raw_h), float(soft_h)) - 0.01
        hi = max(float(raw_h), float(soft_h)) + 0.01
        if float(h_eff) < lo or float(h_eff) > hi:
            self._calc_reason = "invalid_mixing"
            prev = self._ewma_state.get("value")
            self._attr_native_value = _round(float(prev), 2) if prev is not None else None
            self._attr_extra_state_attributes = {
                **self._base_attrs(),
                "raw_hardness_dh": float(raw_h),
                "softened_hardness_dh": float(soft_h),
                "delta_house_l": float(delta_house),
                "delta_soft_l": float(delta_soft),
                "raw_fraction": float(roh_frac),
                "effective_hardness_calc_dh": float(h_eff),
                "held_value_dh": _round(float(prev), 2) if prev is not None else None,
            }
            return

        now_ts = datetime.utcnow().timestamp()
        h_smooth = _ewma_update(self._ewma_state, float(h_eff), now_ts, self._tau_seconds)

        self._attr_native_value = float(h_smooth)
        self._attr_extra_state_attributes = {
            **self._base_attrs(),
            "raw_hardness_dh": float(raw_h),
            "softened_hardness_dh": float(soft_h),
            "effective_hardness_dh": float(h_eff),
            "raw_fraction_interval": float(roh_frac),
            "delta_house_l": float(delta_house),
            "delta_soft_l": float(delta_soft),
            "delta_raw_l": float(delta_raw),
            "tau_seconds": float(self._tau_seconds),
            "ewma_ts": float(self._ewma_state.get("ts") or now_ts),
        }

class IquaEffectiveSodiumSensor(IquaDerivedBaseSensor):
    """Compute effective sodium concentration (mg/L) based on effective hardness reduction.

    Uses the EWMA-smoothed effective hardness when available.
    """

    def __init__(
        self,
        *args,
        house_daily: IquaDailyCounterSensor,
        delta_daily: IquaDailyCounterSensor,
        ewma_state: dict[str, Any],
        raw_sodium_mg_l: float,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._house_daily = house_daily
        self._delta_daily = delta_daily
        self._ewma_state = ewma_state
        self._raw_sodium_mg_l = float(raw_sodium_mg_l)

    def update_from_data(self, data: Dict[str, Any]) -> None:
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        raw_h, soft_h, err = self._read_hardness_inputs()
        if err:
            self._calc_status = "disabled"
            self._calc_reason = err
            self._attr_native_value = None
            self._attr_extra_state_attributes = self._base_attrs()
            return

        # Use smoothed hardness if initialized; otherwise compute a sample from today's mixing.
        h_eff_smooth = _parse_optional_float(self._ewma_state.get("value"))
        if h_eff_smooth is None:
            self._house_daily.update_from_data(data)
            self._delta_daily.update_from_data(data)
            house_today = _parse_optional_float(self._house_daily.native_value)
            delta_today = _parse_optional_float(self._delta_daily.native_value)
            if house_today is None or house_today <= 0 or delta_today is None:
                self._calc_status = "disabled"
                self._calc_reason = "missing_daily_volumes"
                self._attr_native_value = None
                self._attr_extra_state_attributes = self._base_attrs()
                return
            roh_frac = max(min(delta_today / house_today, 1.0), 0.0)
            h_eff_smooth = (raw_h * roh_frac) + (soft_h * (1.0 - roh_frac))

        removed_dh = max(float(raw_h) - float(h_eff_smooth), 0.0)
        na_eff = float(self._raw_sodium_mg_l) + (removed_dh * float(SODIUM_MG_PER_DH))

        self._attr_native_value = _round(na_eff, 1)
        self._attr_extra_state_attributes = {
            **self._base_attrs(),
            "raw_sodium_mg_l": self._raw_sodium_mg_l,
            "raw_hardness_dh": raw_h,
            "effective_hardness_dh_used": _round(float(h_eff_smooth), 2),
            "removed_hardness_dh": _round(removed_dh, 2),
            "sodium_mg_per_dh": float(SODIUM_MG_PER_DH),
            "sodium_limit_mg_l": float(SODIUM_LIMIT_MG_L),
        }

class IquaRawFractionDailySensor(IquaDerivedBaseSensor):
    """Compute raw-water fraction (mixing ratio) for today's water usage.

    This sensor reports the *share of non-softened (raw) water* in percent
    for the current day, based on:
      raw_liters_today = max(house_today - softened_today, 0)

    Unlike treated hardness, this does **not** require hardness inputs and
    remains available even if rest hardness is not set.
    """

    def __init__(self, *args, house_daily: IquaDailyCounterSensor, delta_daily: IquaDailyCounterSensor, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._house_daily = house_daily
        self._delta_daily = delta_daily

    def update_from_data(self, data: Dict[str, Any]) -> None:
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        # Ensure daily sensors are updated from the same coordinator tick
        self._house_daily.update_from_data(data)
        self._delta_daily.update_from_data(data)

        house_today = _parse_optional_float(self._house_daily.native_value)
        delta_today = _parse_optional_float(self._delta_daily.native_value)

        if house_today is None or house_today <= 0 or delta_today is None:
            # If house meter is missing, _read_house_total_l() has already set the reason.
            if self._calc_reason == "ok":
                self._calc_status = "disabled"
                self._calc_reason = "missing_daily_volumes"
            self._attr_native_value = None
            self._attr_extra_state_attributes = self._base_attrs()
            return

        roh_frac = max(min(delta_today / house_today, 1.0), 0.0)
        self._attr_native_value = _round(roh_frac * 100.0, 1)
        self._attr_extra_state_attributes = {
            **self._base_attrs(),
            "raw_fraction": _round(roh_frac, 4),
            "house_today_l": _round(house_today, 1),
            "raw_today_l": _round(delta_today, 1),
        }


# ---------- Setup ----------



class IquaSoftenedFractionDailySensor(IquaDerivedBaseSensor):
    """Compute softened-water fraction (share of softened water) for today's usage in percent.

    This is simply:
        softened_fraction = 100 - raw_fraction
    and is available whenever daily volumes are available.
    """

    def __init__(self, *args, house_daily: IquaDailyCounterSensor, delta_daily: IquaDailyCounterSensor, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._house_daily = house_daily
        self._delta_daily = delta_daily

    def update_from_data(self, data: Dict[str, Any]) -> None:
        self._calc_status = "enabled"
        self._calc_reason = "ok"

        # Ensure daily sensors are updated from the same coordinator tick
        self._house_daily.update_from_data(data)
        self._delta_daily.update_from_data(data)

        house_today = _parse_optional_float(self._house_daily.native_value)
        delta_today = _parse_optional_float(self._delta_daily.native_value)

        if house_today is None or house_today <= 0 or delta_today is None:
            if self._calc_reason == "ok":
                self._calc_status = "disabled"
                self._calc_reason = "missing_daily_volumes"
            self._attr_native_value = None
            self._attr_extra_state_attributes = {**self._base_attrs()}
            return

        roh_frac = max(min(delta_today / house_today, 1.0), 0.0)
        soft_frac = 1.0 - roh_frac

        self._attr_native_value = _round(soft_frac * 100.0, 1)
        self._attr_extra_state_attributes = {**self._base_attrs(), "raw_fraction_percent": _round(roh_frac * 100.0, 1)}


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
) -> None:
    cfg = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if cfg is None:
        # Fallback: some HA reload paths may call platform setup before hass.data is populated
        cfg = next(iter(hass.data.get(DOMAIN, {}).values()), None)
    if cfg is None:
        raise RuntimeError('iQua Softener coordinator not initialized')
    coordinator: IquaSoftenerCoordinator = cfg["coordinator"]
    device_uuid: str = cfg[CONF_DEVICE_UUID]

    merged = _get_merged_entry_data(config_entry)
    house_entity_id = str(merged.get(CONF_HOUSE_WATERMETER_ENTITY) or "").strip()
    house_unit_mode = str(merged.get(CONF_HOUSE_WATERMETER_UNIT_MODE) or HOUSE_UNIT_MODE_AUTO)
    house_factor = merged.get(CONF_HOUSE_WATERMETER_FACTOR)
    raw_hardness_dh = merged.get(CONF_RAW_HARDNESS_DH)
    softened_hardness_dh = merged.get(CONF_SOFTENED_HARDNESS_DH)
    raw_sodium_mg_l = merged.get(CONF_RAW_SODIUM_MG_L, DEFAULT_RAW_SODIUM_MG_L)

    # Shared EWMA runtime state (in-memory). Smoothed sensor restores its last value on startup.
    entry_runtime = hass.data.setdefault(DOMAIN, {}).setdefault(config_entry.entry_id, {})
    ewma_state = entry_runtime.setdefault("ewma", {}).setdefault("effective_hardness", {"value": None, "ts": None})

    # Provide a sensible default for raw hardness (user requested: 22.2 °dH).
    # Rest hardness remains optional; if missing/invalid, treated hardness calculation is disabled.
    if raw_hardness_dh in (None, ""):
        raw_hardness_dh = DEFAULT_RAW_HARDNESS_DH

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
                entity_registry_enabled_default=False,
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
                entity_registry_enabled_default=False,
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

    # ----- Optional derived sensors (delta + daily + treated hardness) -----
    # These sensors are always added, but will be unavailable unless inputs are configured.
    house_total_l_sensor = IquaHouseTotalLitersSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="house_water_total_l",
            translation_key="house_water_total_l",
            device_class=SensorDeviceClass.WATER,
            native_unit_of_measurement=UnitOfVolume.LITERS,
            state_class=SensorStateClass.TOTAL_INCREASING,
            icon="mdi:water",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
    )

    house_daily_l = IquaDailyCounterSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="house_water_daily_l",
            translation_key="house_water_daily_l",
            native_unit_of_measurement=UnitOfVolume.LITERS,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        source="house",
    )

    softened_daily_l = IquaDailyCounterSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="softened_water_daily_l",
            translation_key="softened_water_daily_l",
            native_unit_of_measurement=UnitOfVolume.LITERS,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water-check",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        source="soft",
    )

    delta_daily_l = IquaDailyCounterSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="delta_water_daily_l",
            translation_key="delta_water_daily_l",
            native_unit_of_measurement=UnitOfVolume.LITERS,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water-minus",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        source="delta",
    )

    treated_hardness_daily = IquaTreatedHardnessDailySensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="treated_hardness_daily_dh",
            translation_key="treated_hardness_daily_dh",
            native_unit_of_measurement="°dH",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water-opacity",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        house_daily=house_daily_l,
        delta_daily=delta_daily_l,
    )

    raw_fraction_daily = IquaRawFractionDailySensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="raw_fraction_daily_percent",
            translation_key="raw_fraction_daily_percent",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water-percent",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        house_daily=house_daily_l,
        delta_daily=delta_daily_l,
    )

    softened_fraction_daily = IquaSoftenedFractionDailySensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="softened_fraction_daily_percent",
            translation_key="softened_fraction_daily_percent",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:water-percent",
            entity_registry_enabled_default=False,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        house_daily=house_daily_l,
        delta_daily=delta_daily_l,
    )


    effective_hardness_smoothed = IquaEffectiveHardnessSmoothedSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="effective_hardness_smoothed_dh",
            translation_key="effective_hardness_smoothed_dh",
            native_unit_of_measurement="°dH",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:chart-bell-curve",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        house_daily=house_daily_l,
        delta_daily=delta_daily_l,
        ewma_state=ewma_state,
        tau_seconds=EWMA_TAU_SECONDS,
    )

    effective_sodium = IquaEffectiveSodiumSensor(
        coordinator,
        device_uuid,
        SensorEntityDescription(
            key="effective_sodium_mg_l",
            translation_key="effective_sodium_mg_l",
            native_unit_of_measurement="mg/L",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:shaker-outline",
            entity_registry_enabled_default=True,
        ),
        house_entity_id=house_entity_id,
        house_unit_mode=house_unit_mode,
        house_factor=house_factor,
        raw_hardness_dh=raw_hardness_dh,
        softened_hardness_dh=softened_hardness_dh,
        house_daily=house_daily_l,
        delta_daily=delta_daily_l,
        ewma_state=ewma_state,
        raw_sodium_mg_l=float(raw_sodium_mg_l),
    )


    sensors.extend(
        [
            house_total_l_sensor,
            house_daily_l,
            softened_daily_l,
            delta_daily_l,
            treated_hardness_daily,
            raw_fraction_daily,
            softened_fraction_daily,
            effective_hardness_smoothed,
            effective_sodium,
        ]
    )

    async_add_entities(sensors)
