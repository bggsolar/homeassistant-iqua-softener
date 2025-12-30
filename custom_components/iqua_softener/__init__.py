from __future__ import annotations

import logging

from homeassistant import config_entries, core
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, CONF_EMAIL, CONF_PASSWORD, CONF_DEVICE_UUID
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    """Set up iQua Softener from a config entry.

    Important: We force ONE immediate refresh on startup (async_config_entry_first_refresh),
    so sensors do not sit at 'unknown' until the first scheduled update_interval tick.
    """

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    device_uuid = entry.data[CONF_DEVICE_UUID]

    coordinator = IquaSoftenerCoordinator(
        hass,
        email=email,
        password=password,
        device_uuid=device_uuid,
    )

    # Store early so platforms can access it (and so unload works even if refresh fails)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        CONF_DEVICE_UUID: device_uuid,
    }

    # One immediate refresh on HA start / entry setup
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        # If the first refresh fails, HA should retry later
        raise ConfigEntryNotReady(str(err)) from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload integration when options change
    hass.data[DOMAIN][entry.entry_id]["unsub_options_update_listener"] = entry.add_update_listener(
        _async_options_updated
    )

    return True


async def _async_options_updated(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    unsub = entry_data.get("unsub_options_update_listener")
    if callable(unsub):
        unsub()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
