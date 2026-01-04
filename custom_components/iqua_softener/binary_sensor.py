from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    SODIUM_LIMIT_MG_L,

    DOMAIN,
    CONF_DEVICE_UUID,
    CONF_RAW_HARDNESS_DH,
    CONF_SOFTENED_HARDNESS_DH,
    CONF_RAW_SODIUM_MG_L,
    DEFAULT_RAW_SODIUM_MG_L,
    SODIUM_MG_PER_DH,
    SODIUM_LIMIT_MG_L,
)
from .coordinator import IquaSoftenerCoordinator


def _pwa_key_from_kv(kv: dict, device_uuid: str) -> str:
    model = str(kv.get('manufacturing_information.model') or '')
    pwa = str(kv.get('manufacturing_information.pwa') or '')
    if model and pwa:
        return f"{slugify(model)}_{slugify(pwa)}"
    if pwa:
        return slugify(pwa)
    return slugify(device_uuid)



def _bool(v: Any) -> Optional[bool]:
    """Normalize various truthy/falsey payload values to bool/None."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        s = str(v).strip().lower()
    except Exception:
        return None
    if s in ("1", "true", "on", "yes", "y", "enabled"):
        return True
    if s in ("0", "false", "off", "no", "n", "disabled"):
        return False
    # Numbers: treat >0 as True
    try:
        f = float(s.replace(",", "."))
        return f > 0
    except Exception:
        return None

def _float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        s = str(v).strip()
    except Exception:
        return None
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


@dataclass(frozen=True, kw_only=True)
class IquaBinarySensorEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[IquaSoftenerCoordinator], Optional[bool]]


BINARY_SENSORS: tuple[IquaBinarySensorEntityDescription, ...] = (
    IquaBinarySensorEntityDescription(
        key="regeneration_running",
        translation_key="regeneration_running",
        # Pure info-sensor: do NOT drive logic from HA state.
        value_fn=lambda c: _bool((c.data or {}).get("kv", {}).get("calculated.regeneration_running")),
    ),
    IquaBinarySensorEntityDescription(
        key="treated_capacity_ist_ready",
        translation_key="treated_capacity_ist_ready",
        value_fn=lambda c: _bool((c.data or {}).get("kv", {}).get("calculated.capacity_ist_ready")),
    ),
)



@dataclass(frozen=True, kw_only=True)
class IquaBinaryDescription(BinarySensorEntityDescription):
    pass


SODIUM_LIMIT_DESC = IquaBinaryDescription(
    key="sodium_limit_exceeded",
    translation_key="sodium_limit_exceeded",
    icon="mdi:alert-circle",
)


class IquaSodiumLimitBinarySensor(CoordinatorEntity[IquaSoftenerCoordinator], BinarySensorEntity):
    """Binary sensor: sodium limit exceeded (based on smoothed effective hardness)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        entry: ConfigEntry,
        description: BinarySensorEntityDescription,
        raw_hardness_dh: Any,
        softened_hardness_dh: Any,
        raw_sodium_mg_l: Any,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_uuid = device_uuid
        self._entry = entry
        self._raw_hardness_opt = raw_hardness_dh
        self._soft_hardness_opt = softened_hardness_dh
        self._raw_sodium_opt = raw_sodium_mg_l

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            name="iQua Softener",
            manufacturer="LEYCO",
            model="LEYCOsoft PRO",
        )
        self._attr_unique_id = f"{pwa_key}_{description.key}"
        self.entity_id = f"binary_sensor.iqua_{pwa_key}_{description.key}".lower()

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _get_ewma_value(self) -> Optional[float]:
        entry_runtime = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        ewma = entry_runtime.get("ewma", {}).get("effective_hardness", {})
        return _float(ewma.get("value"))

    def _read_hardness(self) -> tuple[Optional[float], Optional[float]]:
        raw = _float(self._raw_hardness_opt)
        soft = _float(self._soft_hardness_opt)
        if soft is None:
            soft = 0.0
        return raw, soft

    @property
    def is_on(self) -> Optional[bool]:
        raw_h, soft_h = self._read_hardness()
        if raw_h is None:
            return None
        h_eff = self._get_ewma_value()
        if h_eff is None:
            return None

        na_raw = _float(self._raw_sodium_opt)
        if na_raw is None:
            na_raw = float(DEFAULT_RAW_SODIUM_MG_L)

        removed_dh = max(float(raw_h) - float(h_eff), 0.0)
        na_eff = float(na_raw) + removed_dh * float(SODIUM_MG_PER_DH)
        return na_eff > float(SODIUM_LIMIT_MG_L)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw_h, soft_h = self._read_hardness()
        h_eff = self._get_ewma_value()
        na_raw = _float(self._raw_sodium_opt)
        if na_raw is None:
            na_raw = float(DEFAULT_RAW_SODIUM_MG_L)

        attrs: dict[str, Any] = {
            "raw_hardness_dh": raw_h,
            "effective_hardness_smoothed_dh": h_eff,
            "raw_sodium_mg_l": na_raw,
            "sodium_mg_per_dh": float(SODIUM_MG_PER_DH),
            "sodium_limit_mg_l": float(SODIUM_LIMIT_MG_L),
        }
        if raw_h is not None and h_eff is not None:
            removed_dh = max(float(raw_h) - float(h_eff), 0.0)
            attrs["removed_hardness_dh"] = round(removed_dh, 2)
            attrs["effective_sodium_mg_l"] = round(float(na_raw) + removed_dh * float(SODIUM_MG_PER_DH), 1)
        return attrs

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IquaSoftenerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_uuid: str = entry.data.get("device_uuid") or entry.data.get("device") or ""

    # Always create entities (state may be unknown until first poll).
    entities: list[BinarySensorEntity] = [
        IquaCoordinatorBinarySensor(coordinator, device_uuid, desc) for desc in BINARY_SENSORS
    ]
    async_add_entities(entities)


class IquaCoordinatorBinarySensor(CoordinatorEntity[IquaSoftenerCoordinator], BinarySensorEntity):
    """Binary sensor backed by the shared iQua coordinator."""

    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: IquaBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._device_uuid = device_uuid

        # Stable unique id per device
        self._attr_unique_id = f"{pwa_key}_{description.key}".lower()
        self.entity_id = f"binary_sensor.iqua_{pwa_key}_{description.key}".lower()
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Attach this entity to the same device card as sensors."""
        kv = (self.coordinator.data or {}).get("kv", {}) if isinstance((self.coordinator.data or {}).get("kv", {}), dict) else {}

        model = kv.get("device.model") or kv.get("device.model_name") or kv.get("device.type") or "Softener"
        sw = kv.get("device.sw_version") or kv.get("device.firmware") or kv.get("device.version")
        pwa = kv.get("device.pwa") or kv.get("device.serial") or kv.get("device.serial_number")

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

    @property
    def is_on(self) -> Optional[bool]:
        return self.entity_description.value_fn(self.coordinator)