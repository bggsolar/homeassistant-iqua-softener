import logging

from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant import config_entries, core
from homeassistant.exceptions import ConfigEntryNotReady

from iqua_softener import IquaSoftener

from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_DEVICE_SERIAL_NUMBER,
)
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    hass_data = dict(entry.data)

    # merge options (if any)
    if entry.options:
        hass_data.update(entry.options)

    device_serial_number = hass_data[CONF_DEVICE_SERIAL_NUMBER]

    coordinator = IquaSoftenerCoordinator(
        hass,
        IquaSoftener(
            hass_data[CONF_USERNAME],
            hass_data[CONF_PASSWORD],
            device_serial_number,
        ),
    )

    # IMPORTANT: do first refresh here (before forwarding 
    try:
        await coordinator.async_config_entry_first_refresh()
    except (ConfigEntryNotReady, UpdateFailed) as err:
        # UpdateFailed during first refresh usually means:
        # backend down, auth/token issue, temporary cloud problem
        raise ConfigEntryNotReady from err
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