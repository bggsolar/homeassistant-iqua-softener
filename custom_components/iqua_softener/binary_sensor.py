
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity import DeviceInfo
from .const import DOMAIN

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IquaRegenerationActiveBinarySensor(coordinator, entry)])

class IquaRegenerationActiveBinarySensor(BinarySensorEntity):
    _attr_name = "Regeneration läuft"
    _attr_icon = "mdi:sync"
    _attr_unique_id = "iqua_regeneration_laeuft"

    def __init__(self, coordinator, entry):
        self.coordinator = coordinator
        self._entry = entry

    @property
    def is_on(self):
        data = self.coordinator.data or {}
        rest = data.get("restzeit_regeneration")
        try:
            return rest is not None and int(rest) > 0
        except Exception:
            return False

    @property
    def device_info(self):
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="iQua Softener",
            manufacturer="iQua",
            model="Water Softener",
        )
