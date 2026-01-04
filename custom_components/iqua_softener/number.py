from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant import config_entries, core
from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.helpers.entity import EntityCategory

from .const import (
    DOMAIN,
    CONF_DEVICE_UUID,
    CONF_RAW_HARDNESS_DH,
    CONF_SOFTENED_HARDNESS_DH,
    CONF_RAW_SODIUM_MG_L,
    DEFAULT_RAW_SODIUM_MG_L,
    DEFAULT_RAW_HARDNESS_DH,
)

_LOGGER = logging.getLogger(__name__)


def _get_opt_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


def _kv_first_value(kv: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first non-None value for the given keys."""
    for k in keys:
        if k in kv and kv.get(k) is not None:
            return kv.get(k)
    return None


def _cloud_hardness_dh_from_kv(kv: dict[str, Any]) -> float | None:
    """Extract hardness from KV (same source as treated capacity calc) and convert to °dH.

    The cloud may provide hardness either as grains/gal (gpg) or ppm (mg/L CaCO3).
    We follow the same heuristic as in sensor capacity calculation:
      - values > ~60 are treated as ppm
      - otherwise treated as gpg and converted to ppm via *17.1
    Then convert ppm -> °dH (1 °dH ≈ 17.848 mg/L CaCO3).
    """
    raw = _kv_first_value(
        kv,
        (
            "program.hardness_grains",
            "program.hardness",
            "program.hardness_ppm",
            "hardness_grains",
            "hardness",
            "hardness_ppm",
        ),
    )
    try:
        hard = float(raw)
    except Exception:
        return None
    if hard <= 0:
        return None

    # Normalize to ppm (mg/L CaCO3)
    ppm = hard / 17.1 if hard > 60 else hard * 17.1
    dh = ppm / 17.848
    # Keep one decimal like typical water hardness values
    return round(dh, 1)

@dataclass(frozen=True, kw_only=True)
class IquaNumberDescription(NumberEntityDescription):
    option_key: str
    default_value: float | None = None


RAW_HARDNESS_DESC = IquaNumberDescription(
    key="raw_hardness_dh",
    translation_key="raw_hardness_dh",
    icon="mdi:water-percent",
    option_key=CONF_RAW_HARDNESS_DH,
    default_value=DEFAULT_RAW_HARDNESS_DH,
    native_unit_of_measurement="°dH",
    native_min_value=0.0,
    native_max_value=40.0,
    native_step=0.1,
    entity_category=EntityCategory.CONFIG,
)

SOFTENED_HARDNESS_DESC = IquaNumberDescription(
    key="softened_hardness_dh",
    translation_key="softened_hardness_dh",
    icon="mdi:water-check",
    option_key=CONF_SOFTENED_HARDNESS_DH,
    default_value=0.0,  # assumed soft water hardness (°dH); set if your softener outlet is not ~0
    native_unit_of_measurement="°dH",
    native_min_value=0.0,
    native_max_value=20.0,
    native_step=0.1,
    entity_category=EntityCategory.CONFIG,
)


SODIUM_RAW_DESC = IquaNumberDescription(
    key="raw_sodium_mg_l",
    translation_key="raw_sodium_mg_l",
    icon="mdi:water-sodium",
    option_key=CONF_RAW_SODIUM_MG_L,
    default_value=DEFAULT_RAW_SODIUM_MG_L,
    native_unit_of_measurement="mg/L",
    native_min_value=0.0,
    native_max_value=500.0,
    native_step=0.1,
    entity_category=EntityCategory.CONFIG,
)

class IquaOptionsNumber(NumberEntity):
    """A NumberEntity backed by config_entry.options.

    Values are persisted in the config entry options. Updating a value will
    trigger the integration's options update listener (reload).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: core.HomeAssistant,
        entry: config_entries.ConfigEntry,
        device_uuid: str,
        description: IquaNumberDescription,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self.entity_description = description
        self._device_uuid = device_uuid

        self._attr_unique_id = f"{DOMAIN}_{device_uuid}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_uuid)},
            "name": f"iQua {device_uuid}",
            "manufacturer": "iQua / EcoWater",
        }

    @property
    def native_value(self) -> float | None:
        opt = (self._entry.options or {}).get(self.entity_description.option_key)
        v = _get_opt_float(opt)
        if v is None:
            return self.entity_description.default_value
        return v

    async def async_set_native_value(self, value: float) -> None:
        # Round to step precision (0.1)
        v = round(float(value), 1)
        new_opts = dict(self._entry.options or {})
        new_opts[self.entity_description.option_key] = v
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    async_add_entities,
) -> None:
    cfg = hass.data[DOMAIN][config_entry.entry_id]
    device_uuid: str = cfg[CONF_DEVICE_UUID]

    # Ensure sensible defaults if not set yet.
    opts = dict(config_entry.options or {})
    changed = False

    if _get_opt_float(opts.get(CONF_RAW_HARDNESS_DH)) is None:
        # Prefer cloud hardness (same KV source used for treated capacity calculation) as initial default.
        cfg = hass.data[DOMAIN][config_entry.entry_id]
        coordinator = cfg.get("coordinator")
        cloud_dh = None
        try:
            if coordinator and getattr(coordinator, "data", None):
                kv = coordinator.data.get("kv") or {}
                if isinstance(kv, dict):
                    cloud_dh = _cloud_hardness_dh_from_kv(kv)
        except Exception as err:
            _LOGGER.debug("Failed to derive cloud hardness default: %s", err)

        opts[CONF_RAW_HARDNESS_DH] = cloud_dh if cloud_dh is not None else DEFAULT_RAW_HARDNESS_DH
        changed = True
        changed = True

    if _get_opt_float(opts.get(CONF_SOFTENED_HARDNESS_DH)) is None:
        # assumed soft water hardness (°dH)
        opts[CONF_SOFTENED_HARDNESS_DH] = 0.0
        changed = True

    if _get_opt_float(opts.get(CONF_RAW_SODIUM_MG_L)) is None:
        opts[CONF_RAW_SODIUM_MG_L] = DEFAULT_RAW_SODIUM_MG_L
        changed = True

    if changed:
        hass.config_entries.async_update_entry(config_entry, options=opts)

    async_add_entities(
        [
            IquaOptionsNumber(hass, config_entry, device_uuid, RAW_HARDNESS_DESC),
            IquaOptionsNumber(hass, config_entry, device_uuid, SOFTENED_HARDNESS_DESC),
            IquaOptionsNumber(hass, config_entry, device_uuid, SODIUM_RAW_DESC),
        ]
    )
