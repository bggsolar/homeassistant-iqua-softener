from __future__ import annotations

import logging
import re

from homeassistant import config_entries, core
from homeassistant.helpers import device_registry as dr
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import (
    DOMAIN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_DEVICE_UUID,
)
from .coordinator import IquaSoftenerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor", "binary_sensor", "number"]


def _slugify_pwa(value: str) -> str:
    """Return a stable, HA-friendly slug for PWA strings."""
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _get_merged_entry_data(entry: config_entries.ConfigEntry) -> dict:
    """Merge entry.data + entry.options (options override data)."""
    merged = dict(entry.data)
    if entry.options:
        merged.update(entry.options)
    return merged


async def async_setup_entry(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    merged = _get_merged_entry_data(entry)

    # Backward compatible aliases (older versions may have used username/device_id etc.)
    # Feel free to extend if you had other historic keys.
    email = merged.get(CONF_EMAIL) or merged.get("username") or merged.get("user") or merged.get("mail")
    password = merged.get(CONF_PASSWORD) or merged.get("pass") or merged.get("pwd")
    device_uuid = merged.get(CONF_DEVICE_UUID) or merged.get("device_id") or merged.get("device_serial_number")

    # If options contain the keys but data doesn't -> migrate into entry.data for stability
    # (Optional, but helps avoid future issues)
    migrate_payload = {}
    if CONF_EMAIL not in entry.data and merged.get(CONF_EMAIL):
        migrate_payload[CONF_EMAIL] = merged[CONF_EMAIL]
    if CONF_PASSWORD not in entry.data and merged.get(CONF_PASSWORD):
        migrate_payload[CONF_PASSWORD] = merged[CONF_PASSWORD]
    if CONF_DEVICE_UUID not in entry.data and merged.get(CONF_DEVICE_UUID):
        migrate_payload[CONF_DEVICE_UUID] = merged[CONF_DEVICE_UUID]

    if migrate_payload:
        new_data = dict(entry.data)
        new_data.update(migrate_payload)
        hass.config_entries.async_update_entry(entry, data=new_data)
        # refresh merged after migration
        merged = _get_merged_entry_data(entry)
        email = merged.get(CONF_EMAIL) or email
        password = merged.get(CONF_PASSWORD) or password
        device_uuid = merged.get(CONF_DEVICE_UUID) or device_uuid

    # Hard validation: these are required
    missing = []
    if not email:
        missing.append(CONF_EMAIL)
    if not password:
        missing.append(CONF_PASSWORD)
    if not device_uuid:
        missing.append(CONF_DEVICE_UUID)

    if missing:
        # Not a temporary condition -> ConfigEntryError (no endless retries)
        raise ConfigEntryError(
            f"Missing required configuration keys in config entry: {', '.join(missing)}. "
            "Please remove the iQua Softener integration and add it again."
        )

    coordinator = IquaSoftenerCoordinator(
        hass,
        email=str(email),
        password=str(password),
        device_uuid=str(device_uuid),
    )

    try:
        await coordinator.async_load_baseline()
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        # Temporary API/network issues -> retry
        _LOGGER.warning("iQua Softener not ready yet: %s", err)
        raise ConfigEntryNotReady from err


    # After the first successful refresh we know manufacturing_information.pwa.
    # Update entry title and device name from UUID to PWA to keep UI naming consistent.
    try:
        kv = (coordinator.data or {}).get("kv") or {}
        pwa_raw = kv.get("manufacturing_information.pwa")
        pwa = _slugify_pwa(pwa_raw) if pwa_raw else None
        if pwa:
            desired_name = f"iQua {pwa}"
            # Update config entry title (Devices & Services list)
            if entry.title and str(device_uuid) in entry.title:
                hass.config_entries.async_update_entry(entry, title=desired_name)
            # Update device registry name (prefix shown in entity UI)
            dev_reg = dr.async_get(hass)
            for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
                if dev.name and str(device_uuid) in dev.name:
                    dev_reg.async_update_device(dev.id, name=desired_name)
    except Exception as err:
        _LOGGER.debug("Could not update entry/device name to PWA: %s", err)

    # Store runtime objects in hass.data (but don't rely on hass.data for config values)
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        CONF_DEVICE_UUID: str(device_uuid),
        "unsub_options_update_listener": entry.add_update_listener(options_update_listener),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def options_update_listener(hass: core.HomeAssistant, entry: config_entries.ConfigEntry) -> None:
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