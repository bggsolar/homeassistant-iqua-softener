from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IquaSoftenerCoordinator


def _bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    try:
        # numbers/strings
        if str(v).strip().lower() in ("1", "true", "on", "yes"):
            return True
        if str(v).strip().lower() in ("0", "false", "off", "no"):
            return False
        f = float(v)
        return f != 0.0
    except Exception:
        return None


@dataclass(frozen=True, kw_only=True)
class IquaBinarySensorEntityDescription(BinarySensorEntityDescription):
    value_fn: Callable[[IquaSoftenerCoordinator], Optional[bool]]


BINARY_SENSORS: tuple[IquaBinarySensorEntityDescription, ...] = (
    # Pure info-sensor: do NOT drive logic from this entity state.
    IquaBinarySensorEntityDescription(
        key="regeneration_running",
        translation_key="regeneration_running",
        icon="mdi:refresh",
        value_fn=lambda c: _bool((c.data or {}).get("kv", {}).get("calculated.regeneration_running")),
    ),
    # Internal helper state exposed as info-sensor
    IquaBinarySensorEntityDescription(
        key="treated_capacity_ist_ready",
        translation_key="treated_capacity_ist_ready",
        icon="mdi:check-decagram",
        value_fn=lambda c: _bool((c.data or {}).get("kv", {}).get("calculated.capacity_ist_ready")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IquaSoftenerCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_uuid: str = entry.data["device_uuid"]

    entities: list[BinarySensorEntity] = [
        IquaCoordinatorBinarySensor(coordinator, device_uuid, desc) for desc in BINARY_SENSORS
    ]
    async_add_entities(entities)


class IquaCoordinatorBinarySensor(CoordinatorEntity[IquaSoftenerCoordinator], BinarySensorEntity):
    def __init__(
        self,
        coordinator: IquaSoftenerCoordinator,
        device_uuid: str,
        description: IquaBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{device_uuid}_{description.key}"
        self._attr_has_entity_name = True
        self._device_uuid = device_uuid

    @property
    def is_on(self) -> Optional[bool]:
        return self.entity_description.value_fn(self.coordinator)
