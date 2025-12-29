from __future__ import annotations

import logging

from homeassistant import config_entries, core
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEVICE_UUID
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    hass_data = dict(entry.data)
    if entry.options:
        hass_data.update(entry.options)

    coordinator = IquaSoftenerCoordinator(
        hass,
        email=hass_data[CONF_EMAIL],
        password=hass_data[CONF_PASSWORD],
        device_uuid=hass_data[CONF_DEVICE_UUID],
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        _LOGGER.warning("iQua Softener not ready yet: %s", err)
        raise ConfigEntryNotReady from err

    hass_data["coordinator"] = coordinator
    hass_data["unsub_options_update_listener"] = entry.add_update_listener(options_update_listener)
    hass.data[DOMAIN][entry.entry_id] = hass_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def options_update_listener(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    hass.data[DOMAIN][entry.entry_id]["unsub_options_update_listener"]()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok