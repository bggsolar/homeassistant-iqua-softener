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

from .const import DOMAIN
from .coordinator import IquaSoftenerCoordinator


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
        self._attr_unique_id = f"{device_uuid}_{description.key}".lower()
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
